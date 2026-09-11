"""iter-14.9 — Confidence pill high-water-mark regression tests.

Verifies that `compute_stage_confidence` NEVER regresses `best_overall_score`
across recomputes, even when the multi-model evaluator returns a lower
score on the second run (LLM jitter). Also verifies:

  • best_* snapshot fields track the winning run
  • a `partial` (stopped-early) run does NOT displace the high-water mark
  • the frontend contract fields are all present on the persisted doc

These tests stub out every external dependency (Mongo, LLM evaluator,
KB / ground-truth loaders) so they run with zero I/O and no OPENROUTER
key needed.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

# Make backend/ importable regardless of pytest cwd
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# ────────────────────────────────────────────────────────────────────
# Stub out `motor.motor_asyncio` BEFORE anything imports db.py — the
# CI / local test env may not have motor installed, and this test
# doesn't touch Mongo anyway.
# ────────────────────────────────────────────────────────────────────
if "motor" not in sys.modules:
    _motor = types.ModuleType("motor")
    _motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class _FakeMongoClient:  # noqa: D401
        def __init__(self, *a, **kw): pass
        def __getitem__(self, _name): return _FakeDB()

    class _FakeDB:
        def __getattr__(self, _name): return _FakeCollProxy()

    class _FakeCollProxy:
        def __getattr__(self, _name): return _FakeCollProxy()
        def __call__(self, *a, **kw): return self

    _motor_asyncio.AsyncIOMotorClient = _FakeMongoClient
    sys.modules["motor"] = _motor
    sys.modules["motor.motor_asyncio"] = _motor_asyncio


import os as _os  # noqa: E402
_os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
_os.environ.setdefault("DB_NAME", "lama_test_iter149")


# ────────────────────────────────────────────────────────────────────
# Fake stage_confidence collection — supports find_one + update_one({upsert})
# ────────────────────────────────────────────────────────────────────
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
def wired(monkeypatch):
    """Import `routes.pipeline` with every external side-effect stubbed."""
    fake_conf = _FakeColl()
    fake_audit = _FakeColl()

    # 1. Patch db so pipeline picks up the fakes.
    import db as _db
    monkeypatch.setattr(_db, "stage_confidence", fake_conf, raising=True)
    monkeypatch.setattr(_db, "audit_log", fake_audit, raising=True)

    # 2. Reload pipeline so its `from db import stage_confidence` binds to fake.
    if "routes.pipeline" in sys.modules:
        del sys.modules["routes.pipeline"]
    if "routes" in sys.modules and hasattr(sys.modules["routes"], "pipeline"):
        delattr(sys.modules["routes"], "pipeline")
    import routes.pipeline as pipeline  # noqa: E402

    # 3. Stub the LLM / KB collaborators — pipeline calls these directly.
    async def _fake_kb(_pid): return {"summary": "kb"}
    async def _fake_gt(_pid): return {"gt": "x"}
    async def _fake_pick(): return ["stub-model-A", "stub-model-B"]
    async def _fake_collect(_pid, _keys): return "SOME ARTIFACT TEXT"

    monkeypatch.setattr(pipeline, "_kb_summary_for_eval", _fake_kb, raising=True)
    monkeypatch.setattr(pipeline, "_ground_truth_for_eval", _fake_gt, raising=True)
    monkeypatch.setattr(pipeline, "pick_evaluator_models", _fake_pick, raising=True)
    monkeypatch.setattr(pipeline, "_collect_artifact_text", _fake_collect, raising=True)

    # Single fake catalogue section so we don't depend on the real one.
    monkeypatch.setattr(
        pipeline, "_sections_for_stage",
        lambda _stage: [{
            "key": "sec1", "label": "Section 1",
            "what_to_check": "check", "source_keys": ["k1"],
        }],
        raising=True,
    )

    return pipeline, fake_conf


def _install_scorer(pipeline, monkeypatch, score: float):
    """Replace the multi-model scorer with one that returns a fixed score."""
    async def _fake(**_kw):
        return {"sections": [{
            "key": "sec1", "label": "Section 1",
            "score": score, "band": "excellent" if score >= 95 else "moderate",
            "rationale": "r", "gaps": [], "evidence": [], "votes": [],
            "model_agreement_spread": 0.0,
        }]}
    monkeypatch.setattr(pipeline, "score_artifact_multi_model", _fake, raising=True)


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

def test_best_survives_a_regression_recompute(wired, monkeypatch):
    """Run 1 = 96%, Run 2 = 88% → best must stay 96 and pill fields honest."""
    pipeline, conf = wired

    _install_scorer(pipeline, monkeypatch, 96.0)
    doc1 = asyncio.run(pipeline.compute_stage_confidence("P1", "Discovery"))
    assert doc1["overall_score"] == 96.0
    assert doc1["best_overall_score"] == 96.0
    assert doc1["best_overall_band"] == "excellent"

    _install_scorer(pipeline, monkeypatch, 88.0)
    doc2 = asyncio.run(pipeline.compute_stage_confidence("P1", "Discovery"))
    assert doc2["overall_score"] == 88.0, "latest should reflect the new run"
    assert doc2["best_overall_score"] == 96.0, "best must NOT regress"
    assert doc2["best_overall_band"] == "excellent"
    # best snapshot must be the winning run's data, not the latest one
    assert doc2["best_sections"][0]["score"] == 96.0


def test_best_advances_on_improvement(wired, monkeypatch):
    """Run 1 = 80%, Run 2 = 92% → best should advance to 92."""
    pipeline, _conf = wired

    _install_scorer(pipeline, monkeypatch, 80.0)
    asyncio.run(pipeline.compute_stage_confidence("P2", "Discovery"))

    _install_scorer(pipeline, monkeypatch, 92.0)
    doc = asyncio.run(pipeline.compute_stage_confidence("P2", "Discovery"))
    assert doc["overall_score"] == 92.0
    assert doc["best_overall_score"] == 92.0
    assert doc["best_sections"][0]["score"] == 92.0


def test_partial_run_never_displaces_best(wired, monkeypatch):
    """A partial (stopped-early) run — signalled by len(rows) < len(sections)
    when jid is set — must not overwrite an existing high-water mark, even
    if by coincidence its (partial) score is numerically higher."""
    pipeline, _conf = wired

    # Establish a solid baseline.
    _install_scorer(pipeline, monkeypatch, 91.0)
    asyncio.run(pipeline.compute_stage_confidence("P3", "Discovery"))

    # Now make the run "partial" by expanding the catalogue but stopping
    # cooperatively after the very first section via the control flag.
    monkeypatch.setattr(
        pipeline, "_sections_for_stage",
        lambda _stage: [
            {"key": "sec1", "label": "Section 1", "what_to_check": "c", "source_keys": ["k"]},
            {"key": "sec2", "label": "Section 2", "what_to_check": "c", "source_keys": ["k"]},
        ],
        raising=True,
    )

    # Create a job whose control flag is already "stopping" so the loop
    # exits after the first section is scored.
    jid = pipeline._new_confidence_job("P3", "Discovery")
    pipeline._CONFIDENCE_JOBS[jid]["control"] = "stopping"

    # High-scoring partial — must still be rejected as best.
    _install_scorer(pipeline, monkeypatch, 99.0)
    doc = asyncio.run(pipeline.compute_stage_confidence("P3", "Discovery", jid=jid))
    assert doc["partial"] is True
    assert doc["best_overall_score"] == 91.0, "partial run must not displace best"


def test_frontend_contract_fields_present(wired, monkeypatch):
    """The ConfidenceBadge popover reads best_overall_score, best_overall_band,
    best_generated_at, best_sections, best_models_used. Regression-guard so
    a future refactor can't quietly drop any of them."""
    pipeline, _conf = wired

    _install_scorer(pipeline, monkeypatch, 87.5)
    doc = asyncio.run(pipeline.compute_stage_confidence("P4", "Discovery"))

    for field in (
        "overall_score", "overall_band",
        "best_overall_score", "best_overall_band", "best_generated_at",
        "best_sections", "best_models_used",
        "best_section_count", "best_sections_below_95", "best_missing_artifacts",
    ):
        assert field in doc, f"missing frontend-contract field: {field}"
    assert doc["best_overall_score"] == 87.5
    assert isinstance(doc["best_sections"], list) and doc["best_sections"]
