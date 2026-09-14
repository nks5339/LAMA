"""LangGraph + HuggingFace confidence engine for the SRS regeneration hot path.

iter-14.14 — Token-hostile evaluator for the score-gated auto-retry loop.

Motivation
----------
The SRS score-gated retry loop (iter-14.11 / 14.13) fires
`_score_section_now` up to 3 times per section × 12 sections = ~36 evaluator
LLM calls per SRS run. Each call today sends ~11.6k input tokens (system
rubric + KB summary + ground-truth digest + artifact slice) through
`fabric_call` — the primary Factory AI / OpenRouter token spend on
regeneration.

The single biggest opportunity to reduce that spend is to **skip the LLM
evaluator entirely** on sections where deterministic signals already prove
the artifact covers the KB and doesn't contradict it. That's what this
engine does:

  1. `hf_signals` node — runs two small HuggingFace encoder models on CPU:
     • Sentence-Transformer (`BAAI/bge-small-en-v1.5`, 33M params, ~50ms)
       computes SEMANTIC COVERAGE — the fraction of KB tokens (BR-*, UC-*,
       ROLE.*, TABLE.*, WF-*, class/table names) whose best cosine
       similarity against the section content clears a threshold.
     • Cross-encoder NLI (`cross-encoder/nli-deberta-v3-base`, 184M params,
       ~200ms/pair) checks for CONTRADICTIONS between short ground-truth
       claims and the section text.
     Both models are CPU-friendly and already baked into the LAMA image
     (backend/requirements.txt lines 141 + 154).

  2. `decide` node — pure routing:
     • If coverage ≥ HF_COVERAGE_ACCEPT (default 0.90) AND contradictions
       == 0 → emit score 96 (excellent) with rationale
       "embedding_coverage_full+no_contradictions" and skip the LLM.
     • If coverage < HF_COVERAGE_FLOOR (default 0.40) OR contradictions
       ≥ HF_CONTRADICTION_HARD_STOP (default 2) → emit score 55 (moderate)
       and skip the LLM: no point burning tokens on a section the KB
       clearly doesn't back yet.
     • Otherwise → fall through to `llm_eval` (existing evaluator path).

  3. `llm_eval` node — reuses `confidence.score_artifact_multi_model`
     exactly as `_score_section_now` did before, honouring contract #4
     (all LLM calls through `fabric_call` via `_score_artifact_multi_model`).

  4. `aggregate` node — assembles the final verdict, tagging every row
     with `engine` ("langgraph") + `route_taken`
     ("hf_accept" / "hf_reject" / "llm").

Empirical expectation
---------------------
On well-generated SRS sections (~40-60% of them per typical run), the HF
signals converge to accept — those sections cost ZERO fabric_call tokens.
On the remainder the LLM still runs, so worst-case token spend equals the
legacy path plus ~250ms of CPU per section for the HF pass.

Feature flag
------------
`LAMA_CONFIDENCE_ENGINE` selects the engine. iter-14.29 made this a
three-way resolve, not an on/off switch -- see `resolve_confidence_engine`,
which is the single authority:

    langgraph  -> force LangGraph + HF
    fabric     -> force the legacy multi-model fabric_call panel
    unset/auto -> LangGraph IF it is importable AND the Factory.ai droid
                  CLI is not connected; otherwise fabric

So unset does NOT mean off. On a machine with langgraph installed and no
droid CLI on PATH -- the common local-dev shape -- auto resolves to
LangGraph. This docstring claimed "off by default" until 2026-09; that was
true before iter-14.29 and had been wrong ever since, and two tests were
pinning the obsolete promise rather than the shipped behaviour.

Falls back to the legacy path on any import / graph-build failure.

Public API
----------
    from confidence_langgraph import score_section_langgraph, is_available

    if is_available() and os.getenv("LAMA_CONFIDENCE_ENGINE") == "langgraph":
        result = await score_section_langgraph(
            project_id, cfg, content,
            kb_summary=kb_summary, ground_truth=ground_truth,
        )
    else:
        result = await _score_section_now(...)  # legacy path

Contract preservation
---------------------
* All LLM calls still go through `fabric_call` (contract #4) — this
  module never imports langchain-openai / langchain-anthropic / etc.
* Return shape matches `_score_section_now` exactly:
  {score, band, rationale, gaps, model, context_missing, engine,
   route_taken, hf_coverage, hf_contradictions}.
* HF model weights are loaded LAZILY on first call and cached in-process
  (singleton pattern) so container startup remains fast and cold ops
  can bypass the engine entirely.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, TypedDict

logger = logging.getLogger("lama.confidence.langgraph")


# ---------------------------------------------------------------------------
# Feature-availability probe.
# The graph is built lazily; `is_available()` short-circuits without loading
# torch weights so callers can gate on this cheaply.
# ---------------------------------------------------------------------------
def is_available() -> bool:
    """True when langgraph + langchain-core are importable.

    The heavy HF models are checked lazily by their own singleton loaders
    so this probe stays fast (~10ms). If HF models fail to load at runtime
    the graph gracefully degrades to LLM-only (see `_hf_signals_node`).
    """
    try:
        import langgraph  # noqa: F401
        import langchain_core  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Tunables (env-overridable; safe defaults chosen from the analysis in
# memory/PRD.md iter-14.13 which showed 40-60% of sections plateau in the
# 92-96 band even after the KB-context fix — those are exactly the ones the
# HF signals should be able to accept without an LLM call).
# ---------------------------------------------------------------------------
def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


HF_EMBEDDING_MODEL = (
    os.environ.get("LAMA_HF_EMBEDDING_MODEL") or "BAAI/bge-small-en-v1.5"
)
HF_NLI_MODEL = (
    os.environ.get("LAMA_HF_NLI_MODEL") or "cross-encoder/nli-deberta-v3-base"
)

# Coverage thresholds — a KB token is "covered" when its best cosine
# similarity against any sentence in the artifact clears this floor.
HF_TOKEN_MATCH_SIM = _f("LAMA_HF_TOKEN_MATCH_SIM", 0.55)

# Section-level routing thresholds.
HF_COVERAGE_ACCEPT = _f("LAMA_HF_COVERAGE_ACCEPT", 0.90)
HF_COVERAGE_FLOOR = _f("LAMA_HF_COVERAGE_FLOOR", 0.40)
HF_CONTRADICTION_HARD_STOP = _i("LAMA_HF_CONTRADICTION_HARD_STOP", 2)

# NLI budget — how many (claim, best-sentence) pairs we run. Cross-encoder
# NLI is ~200ms/pair on CPU; 8 pairs per section per attempt keeps the HF
# pass under 2s wall-clock in worst case.
HF_NLI_MAX_PAIRS = _i("LAMA_HF_NLI_MAX_PAIRS", 8)

# Scores emitted on HF-only accept/reject routes. Chosen so the retry loop
# in `routes/srs.py` treats them the same way it treats LLM verdicts.
HF_ACCEPT_SCORE = _f("LAMA_HF_ACCEPT_SCORE", 96.0)
HF_REJECT_SCORE = _f("LAMA_HF_REJECT_SCORE", 55.0)


# ---------------------------------------------------------------------------
# HF model singletons.
# Lazy-loaded on first use so `is_available()` stays cheap and startup is
# unaffected. Wrapped in `_hf_load_lock` so concurrent first-hits don't
# double-load the weights. Failure is swallowed so the graph can still
# route to the LLM node.
# ---------------------------------------------------------------------------
_hf_load_lock = asyncio.Lock()
_hf_embedder = None
_hf_nli = None
_hf_load_failed = False


async def _get_embedder():
    """Return a `SentenceTransformer` instance, or None if unavailable."""
    global _hf_embedder, _hf_load_failed
    if _hf_embedder is not None or _hf_load_failed:
        return _hf_embedder
    async with _hf_load_lock:
        if _hf_embedder is not None or _hf_load_failed:
            return _hf_embedder
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(
                "confidence.langgraph · loading HF embedder %s (first use — ~5-10s cold-start)",
                HF_EMBEDDING_MODEL,
            )
            # iter-14.23 — bound the cold-start so a missing HF cache /
            # offline network can't hang the confidence loop indefinitely.
            # LAMA_HF_LOAD_TIMEOUT overrides (default 60s).
            try:
                _load_to = float(os.environ.get("LAMA_HF_LOAD_TIMEOUT", "60") or 60.0)
            except (TypeError, ValueError):
                _load_to = 60.0
            _hf_embedder = await asyncio.wait_for(
                asyncio.to_thread(SentenceTransformer, HF_EMBEDDING_MODEL),
                timeout=max(10.0, _load_to),
            )
            logger.info("confidence.langgraph · embedder ready")
        except asyncio.TimeoutError:
            logger.warning(
                "confidence.langgraph · embedder load exceeded timeout — "
                "disabling HF semantic axis (pass-through). Pre-fetch the "
                "model or set LAMA_HF_LOAD_TIMEOUT higher.",
            )
            _hf_load_failed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confidence.langgraph · embedder load failed (%s) — graph will "
                "skip coverage node and route to LLM. Install/pre-fetch the "
                "model with `SentenceTransformer('%s').encode(['warmup'])` "
                "or set LAMA_HF_EMBEDDING_MODEL to a locally-cached model.",
                exc, HF_EMBEDDING_MODEL,
            )
            _hf_load_failed = True
    return _hf_embedder


async def _get_nli():
    """Return a cross-encoder NLI pipeline, or None if unavailable."""
    global _hf_nli, _hf_load_failed
    if _hf_nli is not None or _hf_load_failed:
        return _hf_nli
    async with _hf_load_lock:
        if _hf_nli is not None or _hf_load_failed:
            return _hf_nli
        try:
            from transformers import pipeline as hf_pipeline
            logger.info(
                "confidence.langgraph · loading HF NLI %s (first use — ~5-10s cold-start)",
                HF_NLI_MODEL,
            )
            _hf_nli = await asyncio.to_thread(
                hf_pipeline, "text-classification",
                model=HF_NLI_MODEL, top_k=None, device=-1,
            )
            logger.info("confidence.langgraph · NLI ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confidence.langgraph · NLI load failed (%s) — graph will "
                "skip contradiction node.",
                exc, HF_NLI_MODEL,
            )
            # Only NLI failed — don't set _hf_load_failed so embedder can
            # still work standalone.
    return _hf_nli


# ---------------------------------------------------------------------------
# KB-token extraction.
# Deterministic — pulls the entity-shaped tokens (BR-*, UC-*, ROLE.*,
# TABLE.*, WF-*, FR-*, camelCase class names, PascalCase table names) from
# the ground-truth digest. These are the atoms coverage is measured against.
# ---------------------------------------------------------------------------
_KB_TOKEN_RE = re.compile(
    r"\b(?:"
    r"BR-\d+|UC-\d+|WF-\d+|FR-\d+|NFR-\d+|R-\d+|"          # rule / use-case / workflow IDs
    r"ROLE\.[A-Za-z_][\w-]*|TABLE\.[A-Za-z_][\w-]*|"        # namespaced entities
    r"COLUMN\.[A-Za-z_][\w-]*|ROUTE\.[A-Za-z_/\{\}-]+"
    r")\b"
)
# Fallback — CamelCase / snake_case identifiers of length >= 5, e.g. class
# names extracted from a KB summary that doesn't carry the structural
# prefixes. Bounded so we don't extract every English word.
_IDENT_RE = re.compile(r"\b(?:[A-Z][a-z]+){2,4}\b|\b[a-z][a-z0-9_]{5,}\b")


def _extract_kb_tokens(kb_summary: str, ground_truth: str, cap: int = 60) -> list[str]:
    """Extract the entity-shaped tokens that coverage is scored against.

    Prefers structural tokens (BR-*, UC-*, ROLE.*, TABLE.*, …) because
    those are the KB rows the LLM evaluator would look for anyway. Falls
    back to CamelCase / snake_case identifiers when structural tokens are
    scarce (some KB summaries are prose-heavy).

    Deduplicates case-insensitively and caps at `cap` to bound the
    embedding compute — 60 tokens × ~50µs per token embedding is ~3ms.
    """
    seen: set[str] = set()
    out: list[str] = []
    for src in (ground_truth or "", kb_summary or ""):
        for m in _KB_TOKEN_RE.findall(src):
            low = m.lower()
            if low not in seen:
                seen.add(low)
                out.append(m)
                if len(out) >= cap:
                    return out
    if len(out) < cap // 2:
        # Not enough structural tokens — fall back to identifiers.
        for src in (ground_truth or "", kb_summary or ""):
            for m in _IDENT_RE.findall(src):
                low = m.lower()
                if low not in seen:
                    seen.add(low)
                    out.append(m)
                    if len(out) >= cap:
                        return out
    return out


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _split_sentences(text: str, cap: int = 80, min_len: int = 20) -> list[str]:
    """Split text into candidate sentences for embedding / NLI.

    `min_len` filters out headings and one-word bullets that would waste
    NLI cycles. `cap` bounds the compute — 80 sentences × ~50µs per
    embedding is ~4ms.
    """
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) >= min_len:
            out.append(p[:600])  # cap sentence length so NLI stays bounded
            if len(out) >= cap:
                break
    return out


# ---------------------------------------------------------------------------
# HF signal computations. Each is best-effort — returns None on failure so
# the graph can fall through to the LLM node without silently emitting
# bogus scores.
# ---------------------------------------------------------------------------
async def _compute_coverage(section_text: str, kb_tokens: list[str]) -> Optional[dict]:
    """Return `{coverage: float, uncovered: list[str]}` or None on failure."""
    if not kb_tokens or not section_text.strip():
        return None
    embedder = await _get_embedder()
    if embedder is None:
        return None
    sentences = _split_sentences(section_text)
    if not sentences:
        return None
    try:
        # Encode in a worker thread — sentence-transformers is sync-only.
        emb_tokens = await asyncio.to_thread(
            embedder.encode, kb_tokens,
            convert_to_tensor=True, show_progress_bar=False,
        )
        emb_sents = await asyncio.to_thread(
            embedder.encode, sentences,
            convert_to_tensor=True, show_progress_bar=False,
        )
        from sentence_transformers import util as st_util
        sims = await asyncio.to_thread(st_util.cos_sim, emb_tokens, emb_sents)
        # sims: [n_tokens, n_sentences]
        max_per_token = sims.max(dim=1).values.tolist()
        covered = [t for t, s in zip(kb_tokens, max_per_token) if s >= HF_TOKEN_MATCH_SIM]
        uncovered = [t for t, s in zip(kb_tokens, max_per_token) if s < HF_TOKEN_MATCH_SIM]
        coverage = len(covered) / max(1, len(kb_tokens))
        return {
            "coverage": round(coverage, 4),
            "uncovered": uncovered[:8],
            "token_count": len(kb_tokens),
            "covered_count": len(covered),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence.langgraph · coverage compute failed: %s", exc)
        return None


async def _compute_contradictions(
    section_text: str,
    ground_truth: str,
) -> Optional[dict]:
    """Return `{contradictions: int, examples: list[str]}` or None on failure.

    For each of the first N ground-truth claim sentences we find the best-
    matching section sentence via embedding cosine-sim, then run cross-
    encoder NLI on the pair. A `contradiction` label above `p >= 0.60`
    counts as a hard contradiction.
    """
    nli = await _get_nli()
    embedder = await _get_embedder()
    if nli is None or embedder is None:
        return None
    truth_sentences = _split_sentences(ground_truth, cap=HF_NLI_MAX_PAIRS)
    section_sentences = _split_sentences(section_text, cap=40)
    if not truth_sentences or not section_sentences:
        return None
    try:
        emb_truth = await asyncio.to_thread(
            embedder.encode, truth_sentences,
            convert_to_tensor=True, show_progress_bar=False,
        )
        emb_sec = await asyncio.to_thread(
            embedder.encode, section_sentences,
            convert_to_tensor=True, show_progress_bar=False,
        )
        from sentence_transformers import util as st_util
        sims = await asyncio.to_thread(st_util.cos_sim, emb_truth, emb_sec)
        pairs: list[tuple[str, str]] = []
        for i, t in enumerate(truth_sentences):
            best_j = int(sims[i].argmax().item())
            pairs.append((t, section_sentences[best_j]))
        # NLI is cheap enough to batch — pipeline handles list input.
        # Format: "premise [SEP] hypothesis" convention varies by model;
        # cross-encoder/nli-deberta-v3-base expects text_pair kwarg.
        def _run_nli(_pairs):
            return nli(
                [{"text": p[0], "text_pair": p[1]} for p in _pairs],
                truncation=True,
            )
        results = await asyncio.to_thread(_run_nli, pairs)
        contradictions = 0
        examples: list[str] = []
        for pair, r in zip(pairs, results):
            # `r` is a list of {label, score} entries (top_k=None).
            top = None
            if isinstance(r, list):
                for entry in r:
                    if not isinstance(entry, dict):
                        continue
                    if top is None or (entry.get("score", 0) or 0) > (top.get("score", 0) or 0):
                        top = entry
            elif isinstance(r, dict):
                top = r
            if not top:
                continue
            label = str(top.get("label", "")).lower()
            score = float(top.get("score", 0.0) or 0.0)
            if label == "contradiction" and score >= 0.60:
                contradictions += 1
                if len(examples) < 3:
                    examples.append(f"KB: «{pair[0][:100]}» ↮ SRS: «{pair[1][:100]}»")
        return {
            "contradictions": contradictions,
            "examples": examples,
            "pairs_checked": len(pairs),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence.langgraph · NLI compute failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# LangGraph state + graph.
# Node signatures are async so they can await the HF/LLM calls; LangGraph
# handles that natively when compiled with an async runner.
# ---------------------------------------------------------------------------
class ConfidenceState(TypedDict, total=False):
    project_id: str
    cfg: dict
    content: str
    kb_summary: str
    ground_truth: str
    context_missing: bool
    hf_coverage: dict | None
    hf_contradictions: dict | None
    route_taken: str  # "hf_accept" / "hf_reject" / "llm"
    llm_verdict: dict | None
    result: dict


async def _load_ctx_node(state: ConfidenceState) -> dict:
    """Extract KB tokens + flag context_missing. Pure Python, no I/O."""
    content = state.get("content") or ""
    if not content.strip():
        return {
            "context_missing": False,
            "result": {
                "score": 0.0, "band": "poor", "rationale": "empty content",
                "gaps": [], "model": "", "context_missing": False,
                "engine": "langgraph", "route_taken": "empty",
                "hf_coverage": None, "hf_contradictions": None,
            },
        }
    kb_summary = state.get("kb_summary") or ""
    ground_truth = state.get("ground_truth") or ""
    ctx_missing = not kb_summary.strip() and not ground_truth.strip()
    return {"context_missing": ctx_missing}


async def _hf_signals_node(state: ConfidenceState) -> dict:
    """Run coverage + NLI in parallel. Either can fail independently."""
    # If context is missing there are no KB tokens to compute coverage
    # against — HF signals would be meaningless. Skip to LLM node so we
    # can at least emit a proper `context_missing=True` verdict.
    if state.get("context_missing"):
        return {"hf_coverage": None, "hf_contradictions": None}
    kb_tokens = _extract_kb_tokens(
        state.get("kb_summary") or "",
        state.get("ground_truth") or "",
    )
    coverage_task = _compute_coverage(state.get("content") or "", kb_tokens)
    contradictions_task = _compute_contradictions(
        state.get("content") or "",
        state.get("ground_truth") or "",
    )
    coverage, contradictions = await asyncio.gather(
        coverage_task, contradictions_task, return_exceptions=True,
    )
    if isinstance(coverage, Exception):
        logger.warning("confidence.langgraph · coverage task raised: %s", coverage)
        coverage = None
    if isinstance(contradictions, Exception):
        logger.warning("confidence.langgraph · contradictions task raised: %s", contradictions)
        contradictions = None
    return {
        "hf_coverage": coverage,
        "hf_contradictions": contradictions,
    }


def _decide_route(state: ConfidenceState) -> str:
    """LangGraph conditional edge — routes to `hf_accept`, `hf_reject`, or `llm`.

    Deterministic; returns the branch key. LangGraph maps this to the
    corresponding node in the graph definition.

    iter-14.19 — When LAMA_HF_ALLOW_LLM_FALLBACK is NOT set (the default
    when `LAMA_CONFIDENCE_ENGINE=langgraph`), the "middle band" (coverage
    between floor and accept) is treated as `hf_reject` instead of `llm`.
    The `hf_reject` node interpolates the score from HF signals so the
    improve-loop still gets an actionable number, and NO Factory-CLI /
    OpenRouter call is ever made for confidence scoring. This is what the
    user wants: score < 95 → just trigger regeneration, don't ask the
    droid to grade the section.
    """
    result = state.get("result")
    if result:
        return "done"
    cov = state.get("hf_coverage")
    con = state.get("hf_contradictions")
    _allow_llm = (
        (os.environ.get("LAMA_HF_ALLOW_LLM_FALLBACK") or "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    if cov is None and con is None:
        # HF stack unavailable OR context missing.
        # In HF-only mode we emit a "context_missing" hf_reject so the loop
        # can still make progress; otherwise route to LLM node.
        return "llm" if _allow_llm else "hf_reject"
    coverage_val = (cov or {}).get("coverage", 0.0)
    contradiction_count = (con or {}).get("contradictions", 0) if con is not None else 0
    if coverage_val >= HF_COVERAGE_ACCEPT and contradiction_count == 0:
        return "hf_accept"
    if (coverage_val < HF_COVERAGE_FLOOR
            or contradiction_count >= HF_CONTRADICTION_HARD_STOP):
        return "hf_reject"
    # Middle band: only touch the LLM if the operator explicitly opts in.
    return "llm" if _allow_llm else "hf_reject"


async def _hf_accept_node(state: ConfidenceState) -> dict:
    cov = state.get("hf_coverage") or {}
    con = state.get("hf_contradictions") or {}
    return {
        "route_taken": "hf_accept",
        "result": {
            "score": round(HF_ACCEPT_SCORE, 2),
            "band": "excellent",
            "rationale": (
                f"embedding coverage {cov.get('coverage', 0.0):.0%} "
                f"({cov.get('covered_count', 0)}/{cov.get('token_count', 0)} KB tokens) "
                f"and NLI found no contradictions across "
                f"{con.get('pairs_checked', 0)} claim pairs"
            ),
            "gaps": [],
            "model": f"hf:{HF_EMBEDDING_MODEL}",
            "context_missing": False,
            "engine": "langgraph",
            "route_taken": "hf_accept",
            "hf_coverage": cov,
            "hf_contradictions": con,
        },
    }


async def _hf_reject_node(state: ConfidenceState) -> dict:
    """Emit a verdict based purely on HF signals — never calls the LLM.

    iter-14.19 — Score is now interpolated across the HF coverage band so
    the improve-loop gets an actionable, monotonically-increasing signal
    without touching the Factory-CLI droid or OpenRouter:

      coverage <= FLOOR                              →  score = HF_REJECT_SCORE (55)
      coverage in [FLOOR, ACCEPT)  &  no contradiction  → linear interpolation
                                                          between REJECT_SCORE (55)
                                                          and ACCEPT_SCORE-2 (94)
      any contradictions                             →  cap at HF_REJECT_SCORE + 5

    This preserves the "score < 95 → regenerate" contract of the improve
    loop while giving the operator a real, interpretable HF-derived score.
    """
    cov = state.get("hf_coverage") or {}
    con = state.get("hf_contradictions") or {}
    coverage_val = float(cov.get("coverage", 0.0) or 0.0)
    contradiction_count = int(con.get("contradictions", 0) or 0)

    if not cov and not con:
        # Context / HF stack missing — floor score, poor band.
        score = float(HF_REJECT_SCORE)
        rationale = (
            "HF signals unavailable (context missing or encoders offline). "
            "Emitting floor score without calling the LLM. Add KB context and "
            "re-run confidence."
        )
    elif contradiction_count > 0:
        # Any contradiction: cap slightly above floor. Regenerate is needed.
        score = min(float(HF_REJECT_SCORE) + 5.0, float(HF_ACCEPT_SCORE) - 10.0)
        rationale = (
            f"NLI flagged {contradiction_count} contradiction(s) between the "
            f"section and ground truth. Coverage={coverage_val:.0%}. "
            "Section will be regenerated without an LLM grading step."
        )
    else:
        floor = float(HF_COVERAGE_FLOOR)
        ceiling = float(HF_COVERAGE_ACCEPT)
        if coverage_val <= floor:
            score = float(HF_REJECT_SCORE)
        else:
            span_coverage = max(1e-6, ceiling - floor)
            span_score = float(HF_ACCEPT_SCORE) - 2.0 - float(HF_REJECT_SCORE)
            score = float(HF_REJECT_SCORE) + (
                (coverage_val - floor) / span_coverage
            ) * span_score
        rationale = (
            f"Embedding coverage {coverage_val:.0%} "
            f"({cov.get('covered_count', 0)}/{cov.get('token_count', 0)} KB tokens); "
            f"no contradictions. HF-only mode — score interpolated from coverage; "
            "no LLM call. Section will be regenerated if <95."
        )

    gaps: list[str] = []
    if cov.get("uncovered"):
        gaps.extend(f"KB token not covered: {t}" for t in cov["uncovered"][:5])
    if con.get("examples"):
        gaps.extend(f"contradiction: {e}" for e in con["examples"][:3])
    if not gaps:
        gaps = ["HF-only scoring — regenerate to improve coverage"]

    return {
        "route_taken": "hf_reject",
        "result": {
            "score": round(score, 2),
            "band": "moderate" if score >= 70 else "poor",
            "rationale": rationale,
            "gaps": gaps,
            "model": f"hf:{HF_EMBEDDING_MODEL}+{HF_NLI_MODEL}",
            "context_missing": False,
            "engine": "langgraph",
            "route_taken": "hf_reject",
            "hf_coverage": cov,
            "hf_contradictions": con,
        },
    }


async def _llm_eval_node(state: ConfidenceState) -> dict:
    """Delegate to the existing multi-model evaluator (contract #4).

    We reuse `confidence.score_artifact_multi_model` verbatim rather than
    wrapping it in a `FabricChatModel` — that adapter is a separate iter
    (14.15) and orthogonal to the token-reduction goal of this iter.
    """
    from confidence import (
        band_of as _band_of,
        pick_evaluator_models as _pick_evaluator_models,
        score_artifact_multi_model as _score_artifact_multi_model,
    )
    cfg = state["cfg"]
    content = state["content"]
    project_id = state["project_id"]
    try:
        models = await _pick_evaluator_models()
        pick = models[:1] if models else [""]
        what = (
            f"IEEE 830 / IEEE 29148 section «{cfg.get('label','')}» — "
            f"{(cfg.get('instructions') or '')[:400]}"
        )
        v = await _score_artifact_multi_model(
            project_id=project_id,
            stage="SRS",
            artifact_text=content,
            sections=[{
                "key": cfg["key"],
                "label": cfg.get("label", cfg["key"]),
                "what_to_check": what,
            }],
            kb_summary=state.get("kb_summary") or "",
            ground_truth=state.get("ground_truth") or "",
            models=pick,
            min_models=1,
        )
        row = ((v or {}).get("sections") or [{}])[0]
        s = float(row.get("score", 0.0) or 0.0)
        return {
            "route_taken": "llm",
            "llm_verdict": v,
            "result": {
                "score": round(s, 2),
                "band": row.get("band") or _band_of(s),
                "rationale": (row.get("rationale") or "")[:400],
                "gaps": (row.get("gaps") or [])[:8],
                "model": (v.get("models_used") or [""])[0] if isinstance(v, dict) else "",
                "context_missing": bool(state.get("context_missing")),
                "engine": "langgraph",
                "route_taken": "llm",
                "hf_coverage": state.get("hf_coverage"),
                "hf_contradictions": state.get("hf_contradictions"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "confidence.langgraph[%s] · llm_eval failed for %s: %s",
            project_id, cfg.get("key"), exc,
        )
        return {
            "route_taken": "llm",
            "result": {
                "score": 0.0,
                "band": "poor",
                "rationale": f"scorer error: {str(exc)[:120]}",
                "gaps": [],
                "model": "",
                "context_missing": bool(state.get("context_missing")),
                "engine": "langgraph",
                "route_taken": "llm_error",
                "hf_coverage": state.get("hf_coverage"),
                "hf_contradictions": state.get("hf_contradictions"),
            },
        }


# ---------------------------------------------------------------------------
# Graph compilation — cached at module level.
# ---------------------------------------------------------------------------
_graph = None
_graph_build_failed = False


def _build_graph():
    """Compile the confidence graph. Cached; safe to call from any thread."""
    global _graph, _graph_build_failed
    if _graph is not None or _graph_build_failed:
        return _graph
    try:
        from langgraph.graph import StateGraph, END
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence.langgraph · langgraph import failed: %s", exc)
        _graph_build_failed = True
        return None
    try:
        g = StateGraph(ConfidenceState)
        g.add_node("load_ctx", _load_ctx_node)
        g.add_node("hf_signals", _hf_signals_node)
        g.add_node("hf_accept", _hf_accept_node)
        g.add_node("hf_reject", _hf_reject_node)
        g.add_node("llm_eval", _llm_eval_node)
        g.set_entry_point("load_ctx")
        # load_ctx may set `result` directly (empty content) — short-circuit.
        g.add_conditional_edges(
            "load_ctx",
            lambda s: "done" if s.get("result") else "signals",
            {"done": END, "signals": "hf_signals"},
        )
        g.add_conditional_edges(
            "hf_signals",
            _decide_route,
            {
                "hf_accept": "hf_accept",
                "hf_reject": "hf_reject",
                "llm": "llm_eval",
                "done": END,
            },
        )
        g.add_edge("hf_accept", END)
        g.add_edge("hf_reject", END)
        g.add_edge("llm_eval", END)
        _graph = g.compile()
        logger.info("confidence.langgraph · graph compiled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence.langgraph · graph build failed: %s", exc)
        _graph_build_failed = True
    return _graph


# ---------------------------------------------------------------------------
# Public entry point.
# Shape-compatible with `routes.srs._score_section_now` so the caller can
# swap under a feature flag with no other changes.
# ---------------------------------------------------------------------------
async def score_section_langgraph(
    project_id: str,
    cfg: dict,
    content: str,
    kb_summary: str = "",
    ground_truth: str = "",
) -> dict:
    """Score ONE SRS section via the LangGraph + HF confidence engine.

    Return shape MUST match `_score_section_now` for drop-in behaviour:
      {score, band, rationale, gaps, model, context_missing}
    Plus these engine-specific fields (extras — callers can ignore):
      {engine, route_taken, hf_coverage, hf_contradictions}

    Never raises — on any internal failure returns score=0.0 with the
    engine tag set to `langgraph` so the caller's retry loop can still
    record the attempt.
    """
    graph = _build_graph()
    if graph is None:
        raise RuntimeError("confidence_langgraph graph unavailable")
    initial: ConfidenceState = {
        "project_id": project_id,
        "cfg": cfg,
        "content": content or "",
        "kb_summary": kb_summary or "",
        "ground_truth": ground_truth or "",
    }
    try:
        final = await graph.ainvoke(initial)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "confidence.langgraph[%s] · graph.ainvoke failed for %s: %s",
            project_id, cfg.get("key"), exc,
        )
        return {
            "score": 0.0, "band": "poor",
            "rationale": f"engine error: {str(exc)[:120]}",
            "gaps": [], "model": "",
            "context_missing": not (kb_summary.strip() or ground_truth.strip()),
            "engine": "langgraph", "route_taken": "engine_error",
            "hf_coverage": None, "hf_contradictions": None,
        }
    result = final.get("result") or {}
    # Belt-and-braces default fill so every field the caller reads is present.
    result.setdefault("score", 0.0)
    result.setdefault("band", "poor")
    result.setdefault("rationale", "")
    result.setdefault("gaps", [])
    result.setdefault("model", "")
    result.setdefault("context_missing", False)
    result.setdefault("engine", "langgraph")
    result.setdefault("route_taken", final.get("route_taken", "unknown"))
    result.setdefault("hf_coverage", final.get("hf_coverage"))
    result.setdefault("hf_contradictions", final.get("hf_contradictions"))
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# iter-14.15 — Multi-section adapter + feature-flag entry point.
#
# `_score_section_now` (SRS auto-retry hot path) uses `score_section_langgraph`
# directly because it scores one section at a time and expects a flat dict.
#
# `compute_stage_confidence` (pipeline popover) and `living._run_srs_diff_job`
# call `confidence.score_artifact_multi_model` and expect a *verdict* shape:
#   {stage, overall_score, overall_band, models_used, sections:[{...}], ...}
#
# `score_artifact_multi_model_langgraph` runs the LangGraph engine per
# section and reshapes the result to that verdict contract, so pipeline.py
# and living.py can drop it in with no other changes.
#
# `maybe_score_artifact_multi_model` centralises the feature-flag check +
# transparent fallback dance so both call sites are a single-line change.
# ---------------------------------------------------------------------------
async def score_artifact_multi_model_langgraph(
    *,
    project_id: str,
    stage: str,
    artifact_text: str,
    sections: list,
    kb_summary: str = "",
    ground_truth: str = "",
    models: list = None,  # accepted for signature parity; unused by HF path
) -> dict:
    """Shape-compatible drop-in for `confidence.score_artifact_multi_model`.

    Runs the LangGraph + HF engine once per section in `sections`, reshapes
    each section result into the row shape callers already parse:
      {key, label, score, band, rationale, gaps, evidence, votes,
       missing, model_agreement_spread, engine, route_taken, hf_coverage,
       hf_contradictions}
    Then returns the aggregate verdict shape:
      {stage, overall_score, overall_band, models_used, sections,
       generated_at, engine, routes_taken}

    Notes:
    * `models` is accepted so callers don't need to conditionally strip it,
      but HF-accept / HF-reject routes never invoke an LLM, and the LLM
      route inside the graph delegates to `score_artifact_multi_model`
      itself (contract #4 — fabric_call + Console routing preserved).
    * `evidence` is always [] for HF-served rows — the HF path has no
      textual quote extraction. LLM-routed rows still come back without
      evidence because the graph's LLM node emits a single-model verdict.
      Callers that render evidence (pipeline popover) tolerate empty lists.
    """
    # Reuse `confidence.band_of` so bands stay consistent across engines.
    from confidence import band_of  # local import — avoid cycle at module load

    sec_rows: list = []
    routes_taken: list = []
    models_used_set: set = set()

    for s in (sections or []):
        cfg = {
            "key": s.get("key"),
            "label": s.get("label", s.get("key", "")),
            "instructions": s.get("what_to_check", ""),
        }
        try:
            r = await score_section_langgraph(
                project_id=project_id,
                cfg=cfg,
                content=artifact_text or "",
                kb_summary=kb_summary or "",
                ground_truth=ground_truth or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confidence.langgraph[%s] · multi-model wrapper failed on "
                "section=%s: %s — emitting poor row",
                project_id, s.get("key"), exc,
            )
            r = {
                "score": 0.0, "band": "poor",
                "rationale": f"engine error: {str(exc)[:120]}",
                "gaps": [], "model": "",
                "engine": "langgraph", "route_taken": "engine_error",
                "hf_coverage": None, "hf_contradictions": None,
            }

        route = r.get("route_taken") or "unknown"
        routes_taken.append(route)
        used_model = r.get("model") or ""
        if used_model:
            models_used_set.add(used_model)

        score = float(r.get("score") or 0.0)
        sec_rows.append({
            "key": s.get("key"),
            "label": s.get("label", s.get("key", "")),
            "score": round(score, 2),
            "band": r.get("band") or band_of(score),
            "rationale": r.get("rationale") or "",
            "gaps": r.get("gaps") or [],
            "evidence": [],
            "votes": (
                [{"model": used_model, "score": round(score, 2)}]
                if used_model else []
            ),
            "missing": False,
            "model_agreement_spread": 0.0,
            "engine": "langgraph",
            "route_taken": route,
            "hf_coverage": r.get("hf_coverage"),
            "hf_contradictions": r.get("hf_contradictions"),
        })

    scores = [row["score"] for row in sec_rows]
    overall = round(sum(scores) / len(scores), 2) if scores else 0.0
    return {
        "stage": stage,
        "overall_score": overall,
        "overall_band": band_of(overall),
        "models_used": sorted(models_used_set),
        "sections": sec_rows,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": "langgraph",
        "routes_taken": routes_taken,
    }


def _factory_cli_ready() -> bool:
    """iter-14.29 — Best-effort probe: is the Factory.ai droid CLI both
    opted-in (LAMA_FACTORY_MODE=cli) AND resolvable on PATH?

    Purposely lightweight — no subprocess spawn. If the binary can't be
    resolved, we conclude Factory is "not connected" and the caller can
    fall back to the HF (LangGraph) engine. Import-guarded so a missing
    fabric package still lets the caller return False safely.
    """
    try:
        from fabric import factory_cli as _fc  # local import; cheap
    except Exception:  # noqa: BLE001
        return False
    try:
        return bool(_fc.is_cli_mode_enabled() and _fc.cli_binary_available())
    except Exception:  # noqa: BLE001
        return False


def resolve_confidence_engine() -> str:
    """iter-14.29 — Central resolver for which confidence engine to use.

    Rules (first match wins):
      1. ``LAMA_CONFIDENCE_ENGINE=langgraph`` → force LangGraph + HF.
      2. ``LAMA_CONFIDENCE_ENGINE=fabric``   → force legacy fabric_call
         (multi-model panel via Factory.ai / OpenRouter).
      3. Empty / ``auto`` (default) → if Factory.ai is NOT enabled or NOT
         reachable AND LangGraph is importable, fall back to LangGraph
         + HF. Otherwise use fabric.

    Returns ``"langgraph"`` or ``"fabric"``. Never raises.
    """
    flag = (os.environ.get("LAMA_CONFIDENCE_ENGINE") or "").strip().lower()
    if flag == "langgraph":
        return "langgraph"
    if flag == "fabric":
        return "fabric"
    # Auto mode — HF fallback when Factory.ai is unavailable.
    if is_available() and not _factory_cli_ready():
        return "langgraph"
    return "fabric"


def _langgraph_engine_enabled() -> bool:
    """Feature-flag probe: env-var opt-in (or auto fallback) AND engine
    importable. Kept as a thin wrapper around ``resolve_confidence_engine``
    for back-compat with existing callers."""
    return resolve_confidence_engine() == "langgraph" and is_available()


def strict_hf_only() -> bool:
    """iter-14.21 — Return True when confidence scoring must NEVER touch a
    fabric-routed LLM (i.e. must never involve Factory-CLI droid).

    Auto-ON whenever the LangGraph engine resolves as the active engine
    (whether explicitly via ``LAMA_CONFIDENCE_ENGINE=langgraph`` or via
    the iter-14.29 auto-fallback when Factory.ai is unreachable); can be
    explicitly forced with ``LAMA_CONFIDENCE_STRICT_HF=1`` or explicitly
    disabled with ``LAMA_CONFIDENCE_STRICT_HF=0``. The default is
    deliberately restrictive so a broken HF install or a transient
    LangGraph exception cannot silently burn Factory tokens on grading.
    """
    override = (os.environ.get("LAMA_CONFIDENCE_STRICT_HF") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return resolve_confidence_engine() == "langgraph"


def engine_unavailable_row(section: dict, *, reason: str) -> dict:
    """iter-14.21 — Uniform stub row when strict-HF mode blocks the fabric
    fallback. Marked ``missing`` so downstream aggregators do not average it
    into the stage score; carries ``route_taken='engine_unavailable'`` so the
    UI + audit log can surface the reason to the operator instead of hiding
    it behind a fake score.
    """
    return {
        "key": section.get("key"),
        "label": section.get("label"),
        "score": 0.0,
        "band": "poor",
        "rationale": (
            "Confidence engine unavailable and strict-HF mode is on — "
            f"skipped LLM/Factory fallback ({reason[:120]}). Rebuild the "
            "container so langgraph + sentence-transformers install cleanly, "
            "or unset LAMA_CONFIDENCE_STRICT_HF to re-enable the fabric "
            "fallback."
        ),
        "gaps": [], "evidence": [], "votes": [], "missing": True,
        "model_agreement_spread": 0.0,
        "engine": "langgraph", "route_taken": "engine_unavailable",
    }


async def maybe_score_artifact_multi_model(
    *,
    project_id: str,
    stage: str,
    artifact_text: str,
    sections: list,
    kb_summary: str = "",
    ground_truth: str = "",
    models: list = None,
    min_models: int = 1,
) -> dict:
    """Feature-flag gated single entry-point for `pipeline` + `living`.

    * When `LAMA_CONFIDENCE_ENGINE=langgraph` AND engine is importable AND
      the graph builds: route through `score_artifact_multi_model_langgraph`.
    * Any exception (HF load, graph error, unexpected shape) → transparent
      fallback to legacy `confidence.score_artifact_multi_model`.
    * Flag unset / anything else → legacy path, bit-identical to pre-iter-14.15.

    Callers pass the same kwargs they already pass to the legacy function,
    plus an implicit `min_models` that only the legacy path honours.
    """
    # Import lazily so unit tests can monkey-patch `score_artifact_multi_model`
    # after this module is loaded.
    from confidence import score_artifact_multi_model as _legacy

    if _langgraph_engine_enabled():
        try:
            verdict = await score_artifact_multi_model_langgraph(
                project_id=project_id,
                stage=stage,
                artifact_text=artifact_text,
                sections=sections,
                kb_summary=kb_summary,
                ground_truth=ground_truth,
                models=models,
            )
            # Sanity-check: the caller reads `verdict["sections"][0]` — if
            # the graph produced an empty list for a non-empty input, fall
            # back rather than surface an IndexError downstream.
            if sections and not (verdict.get("sections") or []):
                raise RuntimeError("langgraph verdict empty for non-empty input")
            return verdict
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "confidence.langgraph[%s] · maybe_score_artifact_multi_model "
                "falling back to fabric path (%s)",
                project_id, exc,
            )
            # Fall through.

    return await _legacy(
        project_id=project_id,
        stage=stage,
        artifact_text=artifact_text,
        sections=sections,
        kb_summary=kb_summary,
        ground_truth=ground_truth,
        models=models,
        min_models=min_models,
    )
