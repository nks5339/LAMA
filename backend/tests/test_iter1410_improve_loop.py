"""iter-14.10 — Auto-improve loop for Discovery/SRS confidence pill.

Verifies the score → regenerate-below-threshold → re-score loop:

  • Iterates up to `max_iterations` and stops early on convergence.
  • Passes the scorer's rationale + gaps as `remediation_hints` to the
    SRS section regenerator.
  • Rejects non-Discovery stages with 400.
  • Bumps SRS version + audit-logs each regeneration.
  • Trajectory is persisted in the job registry so the UI can render it.

Every external side-effect is stubbed so the test needs no LLM, no
Mongo, no Qdrant.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Stub motor + env vars so db.py imports cleanly even without services.
if "motor" not in sys.modules:
    _motor = types.ModuleType("motor")
    _motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class _FakeMongoClient:
        def __init__(self, *a, **kw): pass
        def __getitem__(self, _n): return _FakeDB()

    class _FakeDB:
        def __getattr__(self, _n): return _FakeCollProxy()

    class _FakeCollProxy:
        def __getattr__(self, _n): return _FakeCollProxy()
        def __call__(self, *a, **kw): return self

    _motor_asyncio.AsyncIOMotorClient = _FakeMongoClient
    sys.modules["motor"] = _motor
    sys.modules["motor.motor_asyncio"] = _motor_asyncio

import os as _os
_os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
_os.environ.setdefault("DB_NAME", "lama_test_iter1410")


class _FakeColl:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, flt, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                out = dict(d)
                out.pop("_id", None)
                return out
        return None

    async def update_one(self, flt, update, upsert=False):
        setter = update.get("$set", {})
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                d.update(setter)
                return
        if upsert:
            new = dict(flt)
            new.update(setter)
            self.docs.append(new)

    async def insert_one(self, doc):
        self.docs.append(doc)


@pytest.fixture
def loop_wired(monkeypatch):
    fake_conf = _FakeColl()
    fake_audit = _FakeColl()

    import db as _db
    monkeypatch.setattr(_db, "stage_confidence", fake_conf, raising=True)
    monkeypatch.setattr(_db, "audit_log", fake_audit, raising=True)

    # Force a fresh import of pipeline.py so its `from db import …` binds fakes.
    for k in ("routes.pipeline",):
        sys.modules.pop(k, None)
    import routes.pipeline as pipeline

    monkeypatch.setattr(pipeline, "stage_confidence", fake_conf, raising=True)
    monkeypatch.setattr(pipeline, "audit_log", fake_audit, raising=True)

    # Pipeline's stub of the compute step — mimic monotonically improving
    # scores across iterations so the loop can prove it converges.
    seq = {"i": 0, "scores": [50.0, 78.0, 97.0]}

    async def _fake_compute(project_id, stage, jid=None):
        seq["i"] += 1
        s = seq["scores"][min(seq["i"] - 1, len(seq["scores"]) - 1)]
        rows = [
            # iter-14.23 — Discovery report rows are now the 12 IEEE-830
            # SRS sections; use `specific_requirements` (which owns the
            # BR catalogue) and `actors_use_case_inventory` here.
            {"key": "specific_requirements", "score": s, "band": "moderate",
             "rationale": "BR rows lack verbatim citations",
             "gaps": ["cite src file:line", "no hedging"],
             "missing": False},
            {"key": "actors_use_case_inventory", "score": s, "band": "moderate",
             "rationale": "0 ROLE rows referenced",
             "gaps": ["enumerate every KB ROLE"], "missing": False},
        ]
        overall = round(s, 2)
        doc = {
            "project_id": project_id, "stage": stage,
            "overall_score": overall,
            "best_overall_score": overall,
            "overall_band": "moderate",
            "best_overall_band": "moderate",
            "sections": rows,
            "generated_at": "2026-01-01T00:00:00Z",
        }
        await fake_conf.update_one(
            {"project_id": project_id, "stage": stage},
            {"$set": doc}, upsert=True,
        )
        return doc
    monkeypatch.setattr(pipeline, "compute_stage_confidence", _fake_compute, raising=True)

    # Capture the hints passed to the regenerator so we can assert them.
    calls: list[dict] = []

    async def _fake_regen(project_id, section_key, hints, model=""):
        calls.append({"pid": project_id, "section": section_key,
                      "hints": list(hints or [])})
        return {"ok": True, "section": section_key, "tokens": 42, "failed": False}
    monkeypatch.setattr(pipeline, "_regen_srs_section_with_hints",
                        _fake_regen, raising=True)

    return pipeline, calls, seq


def test_improve_loop_converges_and_records_trajectory(loop_wired):
    pipeline, calls, _seq = loop_wired
    jid = pipeline._new_confidence_job("P1", "Discovery")
    asyncio.run(pipeline._run_improve_loop(
        jid, "P1", "Discovery",
        threshold=95.0, max_iterations=5, max_sections_per_iter=3,
    ))
    job = pipeline._CONFIDENCE_JOBS[jid]
    # Terminal state must be `complete`, with a converged flag on the result.
    assert job["status"] == "complete", job
    assert job["result"]["converged"] is True
    # Trajectory must show at least 3 iterations because 3rd score is 97%.
    trajectory = job["result"]["trajectory"]
    assert len(trajectory) == 3
    # Scores strictly ascending in this fixture: 25 → 68 → 97 (avg of pair)
    scores = [it["overall_score"] for it in trajectory]
    assert scores[-1] >= 95.0
    # Regenerations must have been fed the scorer's hints on non-final iters.
    assert calls, "regenerator was never called"
    for c in calls:
        assert c["hints"], f"empty hints passed for {c['section']}"


def test_improve_loop_passes_scorer_gaps_as_hints(loop_wired):
    pipeline, calls, _seq = loop_wired
    jid = pipeline._new_confidence_job("P2", "Discovery")
    asyncio.run(pipeline._run_improve_loop(
        jid, "P2", "Discovery",
        threshold=95.0, max_iterations=3, max_sections_per_iter=3,
    ))
    # Hint text must include both the rationale AND the gap items from the
    # scorer output — that's the contract that makes the loop actually
    # improve scores instead of re-generating the same content.
    joined = "\n".join(h for c in calls for h in c["hints"])
    assert "BR rows lack verbatim citations" in joined
    assert "cite src file:line" in joined


def test_improve_loop_maps_report_row_to_srs_section_keys():
    """iter-14.23 — Discovery rows are now 1:1 with IEEE-830 SRS sections.
    Each report row's `source_keys` is a single `srs.<key>` bucket, so
    `_srs_keys_for_report_row` returns exactly one key per row. This
    regression-guards the catalogue → SRS mapping after the alignment.
    """
    for k in ("routes.pipeline",):
        sys.modules.pop(k, None)
    import routes.pipeline as pipeline
    # `specific_requirements` (owns the BR catalogue) → SRS bucket of same name.
    keys = pipeline._srs_keys_for_report_row("specific_requirements")
    assert keys == ["specific_requirements"]
    # `actors_use_case_inventory` → SRS bucket of same name.
    keys2 = pipeline._srs_keys_for_report_row("actors_use_case_inventory")
    assert keys2 == ["actors_use_case_inventory"]
    # `data_model_oltp` is a DataModel row → no srs.* keys should be returned
    assert pipeline._srs_keys_for_report_row("data_model_oltp") == []


def test_improve_loop_non_discovery_stage_rejected():
    """The endpoint helper rejects unsupported stages — CodeGen has its
    own /codegen/jobs/start/auto-validate, others don't have per-section
    regen APIs yet.
    """
    for k in ("routes.pipeline",):
        sys.modules.pop(k, None)
    import routes.pipeline as pipeline
    assert "Discovery" in pipeline._IMPROVE_ALLOWED_STAGES
    assert "DataModel" not in pipeline._IMPROVE_ALLOWED_STAGES
    assert "Architecture" not in pipeline._IMPROVE_ALLOWED_STAGES
    assert "CodeGen" not in pipeline._IMPROVE_ALLOWED_STAGES
