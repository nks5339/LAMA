"""iter-14.14 — LangGraph + HF confidence engine tests.

Verifies:
  1. `is_available()` returns True in this environment (langgraph installed).
  2. `_extract_kb_tokens` finds structural KB tokens (BR-*, UC-*, ROLE.*)
     and falls back to identifier tokens when the digest is prose-heavy.
  3. `_split_sentences` filters headings and one-word bullets, caps length.
  4. `_decide_route` — routing logic (hf_accept / hf_reject / llm) given
     synthetic HF signals, without loading any models.
  5. Graph compiles (no torch load).
  6. `score_section_langgraph` returns the drop-in shape when the LLM
     node is monkey-patched (so we don't burn tokens in CI).
  7. `_score_section_now` transparently falls back to the legacy path when
     `LAMA_CONFIDENCE_ENGINE=langgraph` but the engine raises.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Add backend/ to path so `from confidence_langgraph …` works standalone.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# routes/srs.py imports db.py which requires MONGO_URL at import time.
# Match the convention used by test_iter13100_hydration.py.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

import confidence_langgraph as clg  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Availability probe — cheap, no model load.
# ---------------------------------------------------------------------------
def test_is_available_true_in_dev_env():
    """langgraph + langchain-core are installed via requirements.txt."""
    assert clg.is_available() is True


# ---------------------------------------------------------------------------
# 2. KB-token extraction.
# ---------------------------------------------------------------------------
def test_extract_kb_tokens_finds_structural_ids():
    kb_summary = "The system enforces BR-01 and BR-02 via ROLE.admin."
    ground_truth = "UC-05 covers WF-02 for TABLE.orders and COLUMN.status."
    toks = clg._extract_kb_tokens(kb_summary, ground_truth, cap=20)
    # Structural tokens dominate; note ordering: ground_truth is read first.
    assert "UC-05" in toks
    assert "WF-02" in toks
    assert "TABLE.orders" in toks
    assert "COLUMN.status" in toks
    assert "BR-01" in toks
    assert "BR-02" in toks
    assert "ROLE.admin" in toks


def test_extract_kb_tokens_falls_back_to_identifiers():
    # No structural tokens — must fall back to CamelCase / snake_case idents.
    kb_summary = "OrderService approves refund_request via payment_gateway."
    ground_truth = "InvoiceModel exposes totalAmount and customer_id fields."
    toks = clg._extract_kb_tokens(kb_summary, ground_truth, cap=20)
    lower = [t.lower() for t in toks]
    # At least one identifier from each source should surface.
    assert any("orderservice" in t or "invoicemodel" in t for t in lower)
    assert any("refund_request" in t or "payment_gateway" in t or "customer_id" in t
               for t in lower)


def test_extract_kb_tokens_dedupes_case_insensitively():
    toks = clg._extract_kb_tokens("BR-01 br-01 BR-01", "BR-01", cap=10)
    lowered = [t.lower() for t in toks]
    assert lowered.count("br-01") == 1


def test_extract_kb_tokens_respects_cap():
    kb = " ".join(f"BR-{i:02d}" for i in range(50))
    toks = clg._extract_kb_tokens(kb, "", cap=8)
    assert len(toks) == 8


# ---------------------------------------------------------------------------
# 3. Sentence splitting.
# ---------------------------------------------------------------------------
def test_split_sentences_filters_short_and_caps_length():
    text = (
        "This is a full sentence with enough length. "
        "Short. "
        "Another proper sentence follows the short bullet. "
        "Tiny."
    )
    sents = clg._split_sentences(text, cap=10, min_len=20)
    assert all(len(s) >= 20 for s in sents)
    assert len(sents) == 2  # only the two long-enough ones survive


def test_split_sentences_caps_count():
    text = ". ".join([f"This is proper sentence number {i} of many." for i in range(50)])
    sents = clg._split_sentences(text + ".", cap=5)
    assert len(sents) <= 5


# ---------------------------------------------------------------------------
# 4. Routing logic (pure — no models loaded).
# ---------------------------------------------------------------------------
def test_decide_route_hf_accept_on_high_coverage_no_contradictions():
    state = {
        "hf_coverage": {"coverage": 0.95, "uncovered": [],
                        "token_count": 20, "covered_count": 19},
        "hf_contradictions": {"contradictions": 0, "examples": [],
                              "pairs_checked": 6},
    }
    assert clg._decide_route(state) == "hf_accept"


def test_decide_route_hf_reject_on_low_coverage():
    state = {
        "hf_coverage": {"coverage": 0.20, "uncovered": ["BR-01", "UC-03"],
                        "token_count": 10, "covered_count": 2},
        "hf_contradictions": {"contradictions": 0, "examples": [],
                              "pairs_checked": 3},
    }
    assert clg._decide_route(state) == "hf_reject"


def test_decide_route_hf_reject_on_hard_contradictions():
    state = {
        "hf_coverage": {"coverage": 0.75, "uncovered": [],
                        "token_count": 10, "covered_count": 8},
        "hf_contradictions": {"contradictions": 3, "examples": ["a", "b"],
                              "pairs_checked": 6},
    }
    assert clg._decide_route(state) == "hf_reject"


def test_decide_route_llm_when_signals_ambiguous(monkeypatch):
    """iter-14.19 default (HF-only): ambiguous middle band → `hf_reject`.
    With `LAMA_HF_ALLOW_LLM_FALLBACK=1` (legacy) → `llm`.
    """
    state = {
        "hf_coverage": {"coverage": 0.70, "uncovered": ["BR-05"],
                        "token_count": 10, "covered_count": 7},
        "hf_contradictions": {"contradictions": 1, "examples": ["a"],
                              "pairs_checked": 6},
    }
    monkeypatch.delenv("LAMA_HF_ALLOW_LLM_FALLBACK", raising=False)
    assert clg._decide_route(state) == "hf_reject"
    monkeypatch.setenv("LAMA_HF_ALLOW_LLM_FALLBACK", "1")
    assert clg._decide_route(state) == "llm"


def test_decide_route_llm_when_hf_unavailable(monkeypatch):
    """iter-14.19: HF-only default routes unavailable-signals to `hf_reject`;
    legacy fallback flag restores `llm`."""
    state = {"hf_coverage": None, "hf_contradictions": None}
    monkeypatch.delenv("LAMA_HF_ALLOW_LLM_FALLBACK", raising=False)
    assert clg._decide_route(state) == "hf_reject"
    monkeypatch.setenv("LAMA_HF_ALLOW_LLM_FALLBACK", "1")
    assert clg._decide_route(state) == "llm"


# ---------------------------------------------------------------------------
# 5. Graph compilation.
# ---------------------------------------------------------------------------
def test_graph_compiles():
    # Reset the module-level cache so we exercise the real build path.
    clg._graph = None
    clg._graph_build_failed = False
    g = clg._build_graph()
    assert g is not None


# ---------------------------------------------------------------------------
# 6. score_section_langgraph — end-to-end shape check with the LLM node
#    monkey-patched (so CI never calls fabric_call).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_score_section_langgraph_routes_to_llm_when_hf_unavailable(monkeypatch):
    """iter-14.19: with LAMA_HF_ALLOW_LLM_FALLBACK=1 the graph still routes
    to the LLM node when HF is offline (legacy iter-14.14 behaviour).
    """
    monkeypatch.setenv("LAMA_HF_ALLOW_LLM_FALLBACK", "1")
    # Force the HF loaders to report unavailable — quickest way is to set
    # `_hf_load_failed = True` and clear the singletons.
    monkeypatch.setattr(clg, "_hf_embedder", None, raising=False)
    monkeypatch.setattr(clg, "_hf_nli", None, raising=False)
    monkeypatch.setattr(clg, "_hf_load_failed", True, raising=False)
    # Reset graph cache so the routing uses the current node table.
    monkeypatch.setattr(clg, "_graph", None, raising=False)
    monkeypatch.setattr(clg, "_graph_build_failed", False, raising=False)

    async def _fake_llm_eval(state):
        return {
            "route_taken": "llm",
            "llm_verdict": {"models_used": ["stub-model"]},
            "result": {
                "score": 91.0, "band": "good", "rationale": "stubbed",
                "gaps": [], "model": "stub-model", "context_missing": False,
                "engine": "langgraph", "route_taken": "llm",
                "hf_coverage": None, "hf_contradictions": None,
            },
        }
    monkeypatch.setattr(clg, "_llm_eval_node", _fake_llm_eval)

    r = await clg.score_section_langgraph(
        project_id="p1",
        cfg={"key": "workflow", "label": "Workflow", "instructions": "test"},
        content="This SRS section has enough content to be scored properly by the engine.",
        kb_summary="BR-01: validate input. BR-02: log audit.",
        ground_truth="UC-05 covers WF-02.",
    )
    assert r["engine"] == "langgraph"
    assert r["route_taken"] == "llm"
    assert r["score"] == 91.0
    for k in ("score", "band", "rationale", "gaps", "model", "context_missing"):
        assert k in r


@pytest.mark.asyncio
async def test_score_section_langgraph_empty_content_short_circuits():
    r = await clg.score_section_langgraph(
        project_id="p1",
        cfg={"key": "workflow", "label": "Workflow"},
        content="",
        kb_summary="BR-01: validate.",
        ground_truth="UC-05",
    )
    assert r["score"] == 0.0
    assert r["route_taken"] == "empty"
    assert r["engine"] == "langgraph"


# ---------------------------------------------------------------------------
# 7. Feature-flag fallback in _score_section_now.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_score_section_now_falls_back_when_langgraph_raises(monkeypatch):
    """LAMA_CONFIDENCE_ENGINE=langgraph but engine raises.

    iter-14.21 — strict-HF mode is now auto-ON whenever the langgraph
    engine is selected, so the legacy fabric_call fallback is
    deliberately BLOCKED (a scoring path must never route through
    Factory-CLI). Explicitly disable strict-HF via
    ``LAMA_CONFIDENCE_STRICT_HF=0`` to verify the legacy path is still
    reachable for operators who opt out.
    """
    monkeypatch.setenv("LAMA_CONFIDENCE_ENGINE", "langgraph")
    monkeypatch.setenv("LAMA_CONFIDENCE_STRICT_HF", "0")

    # Make the engine raise on first call.
    async def _boom(*a, **kw):
        raise RuntimeError("simulated engine failure")
    monkeypatch.setattr(clg, "score_section_langgraph", _boom)

    # Stub the legacy fabric path.
    from routes import srs as srs_mod

    async def _fake_pick_models():
        return ["cheap-model"]

    async def _fake_multi(*args, **kwargs):
        return {
            "sections": [{
                "key": kwargs["sections"][0]["key"],
                "score": 88.0, "band": "good",
                "rationale": "legacy path", "gaps": [], "evidence": [],
            }],
            "models_used": ["cheap-model"],
        }

    monkeypatch.setattr(srs_mod, "_pick_evaluator_models", _fake_pick_models)
    monkeypatch.setattr(srs_mod, "_score_artifact_multi_model", _fake_multi)

    r = await srs_mod._score_section_now(
        project_id="p1",
        cfg={"key": "workflow", "label": "Workflow", "instructions": "test"},
        content="This section has enough words to trigger scoring.",
        kb_summary="BR-01: validate.",
        ground_truth="UC-05: happy path.",
    )
    # Legacy path ran → engine tag says "fabric".
    assert r["engine"] == "fabric"
    assert r["route_taken"] == "llm"
    assert r["score"] == 88.0


@pytest.mark.asyncio
async def test_score_section_now_strict_hf_blocks_fabric_fallback(monkeypatch):
    """iter-14.21 — When strict-HF mode is on (auto-ON w/ langgraph engine
    or forced via LAMA_CONFIDENCE_STRICT_HF=1), a LangGraph failure MUST
    NOT trigger the fabric_call → Factory-CLI fallback. Instead the
    scorer returns an engine_unavailable stub the caller can surface.
    """
    monkeypatch.setenv("LAMA_CONFIDENCE_ENGINE", "langgraph")
    monkeypatch.delenv("LAMA_CONFIDENCE_STRICT_HF", raising=False)

    async def _boom(*a, **kw):
        raise RuntimeError("simulated engine failure")
    monkeypatch.setattr(clg, "score_section_langgraph", _boom)

    from routes import srs as srs_mod

    called = {"multi": 0}

    async def _fake_pick_models():
        return ["cheap-model"]

    async def _fake_multi(*args, **kwargs):
        called["multi"] += 1
        return {"sections": [{"score": 77.0, "band": "good"}], "models_used": []}

    monkeypatch.setattr(srs_mod, "_pick_evaluator_models", _fake_pick_models)
    monkeypatch.setattr(srs_mod, "_score_artifact_multi_model", _fake_multi)

    r = await srs_mod._score_section_now(
        project_id="p1",
        cfg={"key": "workflow", "label": "Workflow", "instructions": "test"},
        content="This section has enough words to trigger scoring.",
        kb_summary="BR-01: validate.",
        ground_truth="UC-05: happy path.",
    )
    # Strict-HF mode → fabric fallback NEVER runs → stub returned.
    assert called["multi"] == 0
    assert r["engine"] == "langgraph"
    assert r["route_taken"] == "engine_unavailable"
    assert r["score"] == 0.0


@pytest.mark.asyncio
async def test_score_section_now_uses_legacy_when_flag_unset(monkeypatch):
    monkeypatch.delenv("LAMA_CONFIDENCE_ENGINE", raising=False)

    from routes import srs as srs_mod

    async def _fake_pick_models():
        return ["cheap-model"]

    called = {"lg": 0}

    async def _fake_lg(*a, **kw):
        called["lg"] += 1
        return {"score": 99.0, "engine": "langgraph"}
    monkeypatch.setattr(clg, "score_section_langgraph", _fake_lg)

    async def _fake_multi(*args, **kwargs):
        return {
            "sections": [{
                "key": kwargs["sections"][0]["key"],
                "score": 87.0, "band": "good",
                "rationale": "legacy", "gaps": [], "evidence": [],
            }],
            "models_used": ["cheap-model"],
        }

    monkeypatch.setattr(srs_mod, "_pick_evaluator_models", _fake_pick_models)
    monkeypatch.setattr(srs_mod, "_score_artifact_multi_model", _fake_multi)

    r = await srs_mod._score_section_now(
        project_id="p1",
        cfg={"key": "workflow", "label": "Workflow"},
        content="This section has enough content.",
        kb_summary="BR-01: validate.",
        ground_truth="UC-05: happy path.",
    )
    assert called["lg"] == 0
    assert r["engine"] == "fabric"


# ---------------------------------------------------------------------------
# iter-14.15 — multi-section verdict adapter + feature-flag helper coverage.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_model_langgraph_returns_verdict_shape(monkeypatch):
    """`score_artifact_multi_model_langgraph` reshapes engine output to the
    verdict contract `pipeline.py` + `living.py` already parse."""

    async def _fake_section(project_id, cfg, content, kb_summary, ground_truth):
        return {
            "score": 96.0, "band": "high",
            "rationale": "hf_accept · coverage=0.94 · contradictions=0",
            "gaps": [], "model": "",
            "context_missing": False,
            "engine": "langgraph", "route_taken": "hf_accept",
            "hf_coverage": {"coverage": 0.94, "matched": 6, "total": 7},
            "hf_contradictions": {"contradictions": 0, "pairs": 5},
        }

    monkeypatch.setattr(clg, "score_section_langgraph", _fake_section)

    verdict = await clg.score_artifact_multi_model_langgraph(
        project_id="p1", stage="Discovery",
        artifact_text="section body",
        sections=[
            {"key": "workflow", "label": "Workflow", "what_to_check": "…"},
            {"key": "roles",    "label": "Roles",    "what_to_check": "…"},
        ],
        kb_summary="BR-01", ground_truth="UC-05",
        models=["m1", "m2"],
    )
    # Shape assertions the downstream callers rely on:
    assert verdict["stage"] == "Discovery"
    assert verdict["engine"] == "langgraph"
    assert verdict["overall_score"] == 96.0
    assert verdict["overall_band"] in ("high", "excellent")
    assert len(verdict["sections"]) == 2
    row0 = verdict["sections"][0]
    for k in ("key", "label", "score", "band", "rationale",
             "gaps", "evidence", "votes", "missing",
             "model_agreement_spread", "engine", "route_taken"):
        assert k in row0, f"row missing key={k}"
    assert row0["missing"] is False
    assert row0["evidence"] == []
    assert verdict["routes_taken"] == ["hf_accept", "hf_accept"]


@pytest.mark.asyncio
async def test_maybe_score_uses_legacy_when_flag_unset(monkeypatch):
    """When `LAMA_CONFIDENCE_ENGINE` is unset, the helper defers to legacy
    `confidence.score_artifact_multi_model` and NEVER touches the graph."""
    monkeypatch.delenv("LAMA_CONFIDENCE_ENGINE", raising=False)

    called = {"legacy": 0, "lg": 0}

    async def _legacy(**kwargs):
        called["legacy"] += 1
        return {"stage": kwargs["stage"], "overall_score": 88.0,
                "overall_band": "high", "models_used": ["legacy"],
                "sections": [{"key": kwargs["sections"][0]["key"],
                              "label": kwargs["sections"][0]["label"],
                              "score": 88.0, "band": "high",
                              "rationale": "legacy", "gaps": [],
                              "evidence": [], "votes": [], "missing": False,
                              "model_agreement_spread": 0.0}]}

    async def _lg(**kwargs):
        called["lg"] += 1
        return {"stage": kwargs["stage"], "overall_score": 0.0,
                "overall_band": "poor", "sections": [], "models_used": []}

    import confidence as confidence_mod
    monkeypatch.setattr(confidence_mod, "score_artifact_multi_model", _legacy)
    monkeypatch.setattr(clg, "score_artifact_multi_model_langgraph", _lg)

    v = await clg.maybe_score_artifact_multi_model(
        project_id="p1", stage="Discovery",
        artifact_text="body",
        sections=[{"key": "workflow", "label": "Workflow",
                   "what_to_check": "…"}],
        kb_summary="", ground_truth="",
    )
    assert called["legacy"] == 1
    assert called["lg"] == 0
    assert v["overall_score"] == 88.0


@pytest.mark.asyncio
async def test_maybe_score_routes_through_langgraph_when_flag_on(monkeypatch):
    """When flag is on AND engine is available, helper routes through the
    LangGraph adapter and skips the legacy path entirely."""
    monkeypatch.setenv("LAMA_CONFIDENCE_ENGINE", "langgraph")
    monkeypatch.setattr(clg, "is_available", lambda: True)

    called = {"legacy": 0, "lg": 0}

    async def _legacy(**kwargs):
        called["legacy"] += 1
        return {"stage": kwargs["stage"], "sections": [{"key": "x", "score": 0.0}]}

    async def _lg(**kwargs):
        called["lg"] += 1
        return {"stage": kwargs["stage"], "engine": "langgraph",
                "overall_score": 96.0, "overall_band": "high",
                "models_used": [],
                "sections": [{"key": kwargs["sections"][0]["key"],
                              "label": kwargs["sections"][0]["label"],
                              "score": 96.0, "band": "high",
                              "rationale": "hf_accept", "gaps": [],
                              "evidence": [], "votes": [], "missing": False,
                              "model_agreement_spread": 0.0,
                              "engine": "langgraph", "route_taken": "hf_accept",
                              "hf_coverage": None, "hf_contradictions": None}],
                "routes_taken": ["hf_accept"]}

    import confidence as confidence_mod
    monkeypatch.setattr(confidence_mod, "score_artifact_multi_model", _legacy)
    monkeypatch.setattr(clg, "score_artifact_multi_model_langgraph", _lg)

    v = await clg.maybe_score_artifact_multi_model(
        project_id="p1", stage="Discovery",
        artifact_text="body",
        sections=[{"key": "workflow", "label": "Workflow",
                   "what_to_check": "…"}],
        kb_summary="BR-01", ground_truth="UC-05",
    )
    assert called["lg"] == 1
    assert called["legacy"] == 0
    assert v["engine"] == "langgraph"
    assert v["overall_score"] == 96.0


@pytest.mark.asyncio
async def test_maybe_score_falls_back_on_langgraph_exception(monkeypatch):
    """Any exception from the LangGraph adapter → legacy fabric path. This
    is the safety-net guarantee the operator playbook depends on."""
    monkeypatch.setenv("LAMA_CONFIDENCE_ENGINE", "langgraph")
    monkeypatch.setattr(clg, "is_available", lambda: True)

    async def _lg_boom(**kwargs):
        raise RuntimeError("hf models did not load")

    called = {"legacy": 0}

    async def _legacy(**kwargs):
        called["legacy"] += 1
        return {"stage": kwargs["stage"], "overall_score": 82.0,
                "overall_band": "high", "models_used": ["legacy"],
                "sections": [{"key": kwargs["sections"][0]["key"],
                              "label": kwargs["sections"][0]["label"],
                              "score": 82.0, "band": "high",
                              "rationale": "legacy fallback",
                              "gaps": [], "evidence": [], "votes": [],
                              "missing": False,
                              "model_agreement_spread": 0.0}]}

    import confidence as confidence_mod
    monkeypatch.setattr(confidence_mod, "score_artifact_multi_model", _legacy)
    monkeypatch.setattr(clg, "score_artifact_multi_model_langgraph", _lg_boom)

    v = await clg.maybe_score_artifact_multi_model(
        project_id="p1", stage="Discovery",
        artifact_text="body",
        sections=[{"key": "workflow", "label": "Workflow",
                   "what_to_check": "…"}],
        kb_summary="", ground_truth="",
    )
    assert called["legacy"] == 1
    assert v["overall_score"] == 82.0


@pytest.mark.asyncio
async def test_maybe_score_falls_back_when_langgraph_returns_empty(monkeypatch):
    """Even without an exception, if the graph returns an empty sections
    list for a non-empty input, we fall back — otherwise downstream
    `verdict["sections"][0]` would IndexError."""
    monkeypatch.setenv("LAMA_CONFIDENCE_ENGINE", "langgraph")
    monkeypatch.setattr(clg, "is_available", lambda: True)

    async def _lg_empty(**kwargs):
        return {"stage": kwargs["stage"], "engine": "langgraph",
                "overall_score": 0.0, "sections": [], "routes_taken": []}

    called = {"legacy": 0}

    async def _legacy(**kwargs):
        called["legacy"] += 1
        return {"stage": kwargs["stage"], "overall_score": 90.0,
                "overall_band": "high",
                "sections": [{"key": kwargs["sections"][0]["key"],
                              "score": 90.0, "band": "high"}]}

    import confidence as confidence_mod
    monkeypatch.setattr(confidence_mod, "score_artifact_multi_model", _legacy)
    monkeypatch.setattr(clg, "score_artifact_multi_model_langgraph", _lg_empty)

    v = await clg.maybe_score_artifact_multi_model(
        project_id="p1", stage="Discovery",
        artifact_text="body",
        sections=[{"key": "workflow", "label": "Workflow",
                   "what_to_check": "…"}],
        kb_summary="", ground_truth="",
    )
    assert called["legacy"] == 1
    assert v["overall_score"] == 90.0
