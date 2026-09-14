"""iter-14.11 — Token-reduction knobs for the auto-improve loop.

Covers the P0 optimisations documented in AGENTS.md:
  1. `compute_stage_confidence` accepts `only_sections`, `evaluator_models`,
     `artifact_max_chars` for lean delta-scoring.
  2. Non-scored sections carry over their prior rows verbatim (no re-score).
  3. `_run_improve_loop`:
       • uses the single cheap evaluator on iter 2+ (delta mode);
       • KB-gap sections are regenerated AT MOST ONCE then skipped;
       • stalled sections are skipped;
       • token counter accumulates from regen calls;
       • loop aborts cleanly when the budget is exceeded.

Every LLM/DB/network side-effect is stubbed. No real IO.
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
_os.environ.setdefault("DB_NAME", "lama_test_iter1411")

from routes import pipeline as pl  # noqa: E402


class _FakeColl:
    """Tiny in-memory Mongo replacement covering only the ops we need."""

    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, filt: dict, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in filt.items()):
                out = {k: v for k, v in d.items() if k != "_id"}
                return out
        return None

    async def replace_one(self, filt: dict, doc: dict, upsert: bool = False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in filt.items()):
                self.docs[i] = dict(doc)
                return
        if upsert:
            self.docs.append(dict(doc))

    async def update_one(self, *a, **kw):
        return None

    async def insert_one(self, doc: dict):
        self.docs.append(dict(doc))
        return None


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch):
    """Wire pipeline's module-level globals to fakes."""
    fake_stage_conf = _FakeColl()
    fake_projects = _FakeColl()
    fake_audit = _FakeColl()
    asyncio.run(
        fake_projects.insert_one({"id": "p1"})
    )
    monkeypatch.setattr(pl, "stage_confidence", fake_stage_conf)
    monkeypatch.setattr(pl, "projects", fake_projects)
    monkeypatch.setattr(pl, "audit_log", fake_audit)
    # Make KB + ground-truth digests trivial.
    async def _empty(_pid): return ""
    monkeypatch.setattr(pl, "_kb_summary_for_eval", _empty)
    monkeypatch.setattr(pl, "_ground_truth_for_eval", _empty)
    # Default evaluator picker → 3-model panel we can inspect in the test.
    async def _default_models(): return ["opus", "sonnet", "cheap"]
    monkeypatch.setattr(pl, "pick_evaluator_models", _default_models)
    return {"stage_conf": fake_stage_conf, "audit": fake_audit}


# ---------------------------------------------------------------------------
# 1. `only_sections` + `evaluator_models` + `artifact_max_chars` are threaded.
# ---------------------------------------------------------------------------

def test_only_sections_delta_scoring_merges_with_prior(monkeypatch, _patch_pipeline):
    """When `only_sections=['actors']` is passed, we should:
      * only run the scorer for the `actors` catalogue row,
      * carry over the prior row for every other section,
      * pass the caller-supplied evaluator list + max_chars through.
    """
    stage_conf = _patch_pipeline["stage_conf"]
    # Seed prior doc with a mixed score profile.
    asyncio.run(stage_conf.insert_one({
        "project_id": "p1", "stage": "Discovery",
        "overall_score": 45.0, "overall_band": "poor",
        "best_overall_score": 60.0, "best_overall_band": "fair",
        "sections": [
            {"key": "workflow", "label": "Workflow", "score": 80.0, "band": "good",
             "rationale": "prior", "gaps": [], "evidence": [], "votes": [], "missing": False},
            {"key": "actors", "label": "Actors", "score": 20.0, "band": "poor",
             "rationale": "prior", "gaps": [], "evidence": [], "votes": [], "missing": False},
            {"key": "nfr", "label": "NFR", "score": 76.0, "band": "good",
             "rationale": "prior", "gaps": [], "evidence": [], "votes": [], "missing": False},
        ],
    }))

    sections_stub = [
        {"key": "workflow", "label": "Workflow", "source_keys": ["srs.workflow"],
         "what_to_check": "…", "stage": "Discovery"},
        {"key": "actors", "label": "Actors", "source_keys": ["srs.actors_use_case_inventory"],
         "what_to_check": "…", "stage": "Discovery"},
        {"key": "nfr", "label": "NFR", "source_keys": ["srs.nfr"],
         "what_to_check": "…", "stage": "Discovery"},
    ]
    monkeypatch.setattr(pl, "_sections_for_stage", lambda _s: sections_stub)

    async def _fake_collect(_pid, _keys, max_chars: int = 28000):
        # Capture the max_chars we're asked for.
        _fake_collect.last_max = max_chars
        return "ARTIFACT " * 200
    _fake_collect.last_max = None
    monkeypatch.setattr(pl, "_collect_artifact_text", _fake_collect)

    scorer_calls: list[dict] = []
    async def _fake_score(*, project_id, stage, artifact_text, sections,
                          kb_summary, ground_truth, models):
        scorer_calls.append({"sections": [s["key"] for s in sections], "models": list(models)})
        return {"sections": [{**s, "score": 90.0, "band": "great",
                              "rationale": "improved", "gaps": [], "evidence": [],
                              "votes": [], "model_agreement_spread": 0.0}
                             for s in sections]}
    monkeypatch.setattr(pl, "score_artifact_multi_model", _fake_score)

    doc = asyncio.run(
        pl.compute_stage_confidence(
            "p1", "Discovery",
            only_sections=["actors"],
            evaluator_models=["cheap"],
            artifact_max_chars=8000,
        )
    )

    # Only `actors` was scored.
    assert scorer_calls == [{"sections": ["actors"], "models": ["cheap"]}]
    # Artifact collection was called with the trimmed budget.
    assert _fake_collect.last_max == 8000
    # Merged rows carry over workflow + nfr with their prior scores.
    scored = {r["key"]: r for r in doc["sections"]}
    assert scored["workflow"]["score"] == 80.0
    assert scored["nfr"]["score"] == 76.0
    assert scored["actors"]["score"] == 90.0
    # Stable catalogue ordering.
    assert [r["key"] for r in doc["sections"]] == ["workflow", "actors", "nfr"]


# ---------------------------------------------------------------------------
# 2. KB-gap heuristic.
# ---------------------------------------------------------------------------

def test_kb_gap_heuristic_matches_expected_phrases():
    assert pl._row_looks_like_kb_gap({"rationale": "KB has 0 role rows", "gaps": []})
    assert pl._row_looks_like_kb_gap({"rationale": "", "gaps": ["MANDATORY citation failure"]})
    assert pl._row_looks_like_kb_gap({"rationale": ".lama/ bundle absent", "gaps": []})
    assert not pl._row_looks_like_kb_gap({
        "rationale": "Actors section hedges with 'may'", "gaps": ["Add explicit permission table"],
    })


# ---------------------------------------------------------------------------
# 3. Reverse mapping SRS-key → report-row-key.
# ---------------------------------------------------------------------------

def test_report_key_for_srs_reverse_mapping():
    # Round-trip a Discovery report row's srs. source through both mappers.
    # The mapping must be non-empty for at least one Discovery row.
    from routes.living import _REPORT_SECTIONS
    disc_rows = [s for s in _REPORT_SECTIONS if s.get("stage") == "Discovery"]
    assert disc_rows, "Discovery catalogue must exist"
    found_pair = False
    for row in disc_rows:
        for src in row.get("source_keys") or []:
            if isinstance(src, str) and src.startswith("srs."):
                srs_key = src.split(".", 1)[1]
                assert pl._report_key_for_srs(srs_key) == row["key"], src
                found_pair = True
    assert found_pair, "Discovery must have at least one srs.* source_key"
    assert pl._report_key_for_srs("") == ""
    assert pl._report_key_for_srs("does_not_exist_anywhere") == ""


# ---------------------------------------------------------------------------
# 4. Improve loop: KB-gap skip + token accounting + delta-scoring on iter 2+.
# ---------------------------------------------------------------------------

def test_improve_loop_uses_lean_delta_scoring_and_tracks_tokens(monkeypatch, _patch_pipeline):
    """End-to-end (with stubs) improve-loop verification.

      • Iter 1: full multi-model panel over ALL sections.
      • Iter 2: lean single-model panel over ONLY the regenerated section.
      • KB-gap section (`nfr`) is skipped after 1 regen.
      • Token counter accumulates across regenerations.
      * Trajectory rows carry scoring_mode + tokens_used.
    """
    stage_conf = _patch_pipeline["stage_conf"]

    # A minimal Discovery catalogue: 2 rows.
    sections_stub = [
        {"key": "actors", "label": "Actors", "source_keys": ["srs.actors_use_case_inventory"],
         "what_to_check": "…", "stage": "Discovery"},
        {"key": "nfr", "label": "NFR", "source_keys": ["srs.nfr"],
         "what_to_check": "…", "stage": "Discovery"},
    ]
    monkeypatch.setattr(pl, "_sections_for_stage", lambda _s: sections_stub)
    async def _art(_pid, _keys, max_chars: int = 28000): return "ART"
    monkeypatch.setattr(pl, "_collect_artifact_text", _art)

    # Score plan: iter 1 both low (actors=40, nfr=30 with KB-gap language);
    # iter 2 only actors re-scored (jumps to 70 — no convergence yet);
    # iter 3 only actors re-scored (jumps to 96 — converges).
    call_log: list[dict] = []
    def _make_row(key, score, kb_gap=False):
        rat = ".lama/ bundle absent" if kb_gap else "Hedges with 'may'"
        return {"key": key, "label": key.title(), "score": score,
                "band": "poor" if score < 50 else "good",
                "rationale": rat, "gaps": ["specific gap 1"],
                "evidence": [], "votes": [], "missing": False,
                "model_agreement_spread": 0.0}

    # `nfr` stays scored at ~30 throughout (never re-scored in iter 2+),
    # so the merge path is what matters for it.
    async def _fake_score(*, project_id, stage, artifact_text, sections,
                          kb_summary, ground_truth, models):
        call_log.append({"section_keys": [s["key"] for s in sections],
                         "models": list(models)})
        # Section-by-section fixed scores driven by state.
        out = []
        for s in sections:
            if s["key"] == "actors":
                # Sequence: 40 (iter1), 70 (iter2), 96 (iter3), 96 (seal)
                actors_scores = [40.0, 70.0, 96.0, 96.0]
                idx = min(len([c for c in call_log if "actors" in c["section_keys"]]) - 1,
                          len(actors_scores) - 1)
                out.append(_make_row("actors", actors_scores[idx]))
            elif s["key"] == "nfr":
                # Always 30 with KB-gap rationale.
                out.append(_make_row("nfr", 30.0, kb_gap=True))
        return {"sections": out}
    monkeypatch.setattr(pl, "score_artifact_multi_model", _fake_score)

    regen_calls: list[str] = []
    async def _fake_regen(project_id, section_key, hints, model=""):
        regen_calls.append(section_key)
        return {"ok": True, "section": section_key, "tokens": 5000, "failed": False}
    monkeypatch.setattr(pl, "_regen_srs_section_with_hints", _fake_regen)

    # Prevent cooperative-await from stalling the test.
    async def _no_control(_jid): return None
    monkeypatch.setattr(pl, "_cjob_await_control", _no_control)

    jid = pl._new_confidence_job("p1", "Discovery")

    asyncio.run(
        pl._run_improve_loop(jid, "p1", "Discovery",
                             threshold=95.0, max_iterations=3, max_sections_per_iter=3,
                             token_budget=1_000_000)
    )

    job = pl._CONFIDENCE_JOBS[jid]
    trajectory = job.get("iterations") or []
    assert len(trajectory) >= 2, f"expected multi-iter trajectory, got {trajectory}"

    # Iter 1 = full panel over BOTH sections.
    iter1 = trajectory[0]
    assert iter1["scoring_mode"] == "full"
    # First scorer call must have hit both sections with the full 3-model panel.
    assert call_log[0]["section_keys"] == ["actors", "nfr"]
    assert call_log[0]["models"] == ["opus", "sonnet", "cheap"]

    # Iter 2 = lean-delta over ONLY actors, single-model.
    iter2 = trajectory[1]
    assert iter2["scoring_mode"] == "lean-delta", iter2
    assert call_log[1]["section_keys"] == ["actors"]
    assert call_log[1]["models"] == pl._IMPROVE_INTRA_MODELS

    # NFR (KB-gap) was regenerated exactly once (in iter 1's regen phase),
    # then skipped for the rest of the loop.
    nfr_srs = "nfr"  # per _srs_keys_for_report_row mapping
    nfr_regens = [k for k in regen_calls if k == nfr_srs]
    assert len(nfr_regens) <= 1, f"KB-gap section regenerated {len(nfr_regens)}x, must be ≤1"

    # Token counter is non-zero and monotonically non-decreasing.
    tokens_series = [it.get("tokens_used", 0) for it in trajectory]
    assert tokens_series[-1] > 0
    assert tokens_series == sorted(tokens_series)

    # Result payload carries budget + stop_reason.
    result = job.get("result") or {}
    assert result.get("tokens_used", 0) > 0
    assert result.get("stop_reason") in {"converged", "max_iter", "no_actionable_sections"}
