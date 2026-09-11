"""iter-14.21 — HuggingFace-backed semantic confidence for CodeGen files.

Adds a per-file **semantic similarity** score (0–100) between the
generated source and one or more *legacy* files that the target service
was migrated from. Used by `parity_loop.score_run` as an additional
scoring axis alongside structural / parity / coverage / schema /
evidence / requirement.

Design goals:

* **Zero mandatory cost** — reuses the sentence-transformers embedder
  already loaded by `backend/confidence_langgraph.py` (BAAI/bge-small-
  en-v1.5, ~50MB, already in `hf_cache/`). No new model download.
* **Graceful fallback** — when `sentence_transformers` fails to import
  OR the model is missing OR the KB has no legacy content, we return
  `None` and the caller assigns a pass-through score of 100 so the
  semantic axis never *drags* an otherwise-good file below threshold.
* **Bounded compute** — first-hit legacy corpus is capped at 40 chunks
  of up to 2 KB each per project; per-file embedding is a single
  forward pass on ~4 KB of text. Cold-start ~5 s; steady-state ~15 ms
  per file on CPU.
* **LangGraph friendly** — exposes a pure `async score_file(...)` and
  a `preload(project_id)` warm-up hook the graph orchestrator can pin
  to the loop's `START` node.

Env vars:
  LAMA_CODEGEN_HF_ENABLED=1        (default 1; set 0 to disable)
  LAMA_CODEGEN_HF_CORPUS_MAX=40    max legacy chunks pooled per project
  LAMA_CODEGEN_HF_CHUNK_CHARS=2000 max chars per legacy chunk kept
  LAMA_CODEGEN_HF_MIN_SIM=0.20     cosine below this → score floor 40
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("lama.codegen.hf_confidence")


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except ValueError:
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, "") or default)
    except ValueError:
        return default


def is_enabled() -> bool:
    flag = (os.environ.get("LAMA_CODEGEN_HF_ENABLED") or "1").strip().lower()
    return flag in {"1", "true", "yes", "on"}


# Per-project legacy-corpus cache. Keyed by project_id, value is
# {"chunks": [str, ...], "embeddings": Tensor}. Never grows past the
# CORPUS_MAX cap, so memory footprint is bounded (~40*2KB*768*4 ≈ 240KB
# per project embedding table).
_CORPUS_CACHE: Dict[str, dict] = {}


async def _load_corpus(project_id: str) -> Optional[dict]:
    """Pool up to CORPUS_MAX chunks of legacy source for this project."""
    if project_id in _CORPUS_CACHE:
        return _CORPUS_CACHE[project_id]
    corpus_max = _i("LAMA_CODEGEN_HF_CORPUS_MAX", 40)
    chunk_chars = _i("LAMA_CODEGEN_HF_CHUNK_CHARS", 2000)
    try:
        from db import kb_chunks
    except Exception as e:  # noqa: BLE001
        logger.warning("hf_confidence: cannot import db.kb_chunks: %s", e)
        return None
    try:
        docs = await kb_chunks.find(
            {"project_id": project_id}, {"_id": 0, "content": 1, "file_path": 1},
        ).limit(corpus_max * 4).to_list(corpus_max * 4)
    except Exception as e:  # noqa: BLE001
        logger.warning("hf_confidence: kb_chunks read failed: %s", e)
        return None
    if not docs:
        _CORPUS_CACHE[project_id] = {"chunks": [], "embeddings": None}
        return _CORPUS_CACHE[project_id]
    chunks: List[str] = []
    for d in docs:
        c = (d.get("content") or "").strip()
        if len(c) < 40:
            continue
        chunks.append(c[:chunk_chars])
        if len(chunks) >= corpus_max:
            break
    if not chunks:
        _CORPUS_CACHE[project_id] = {"chunks": [], "embeddings": None}
        return _CORPUS_CACHE[project_id]
    # Reuse the LangGraph embedder singleton.
    try:
        from confidence_langgraph import _get_embedder  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("hf_confidence: cannot import _get_embedder: %s", e)
        return None
    embedder = await _get_embedder()
    if embedder is None:
        return None
    import asyncio  # local — avoid module-level import cost
    try:
        embs = await asyncio.to_thread(
            embedder.encode, chunks,
            convert_to_tensor=True, normalize_embeddings=True,
            show_progress_bar=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("hf_confidence: corpus encode failed: %s", e)
        return None
    _CORPUS_CACHE[project_id] = {"chunks": chunks, "embeddings": embs}
    logger.info("hf_confidence: cached %d legacy chunks for project=%s",
                len(chunks), project_id)
    return _CORPUS_CACHE[project_id]


async def preload(project_id: str) -> Tuple[bool, str]:
    """Warm up the embedder + corpus before the loop kicks off."""
    if not is_enabled():
        return False, "disabled (LAMA_CODEGEN_HF_ENABLED=0)"
    corpus = await _load_corpus(project_id)
    if not corpus or corpus.get("embeddings") is None:
        return False, "no legacy corpus available (HF fallback active)"
    return True, f"corpus loaded ({len(corpus.get('chunks') or [])} chunks)"


async def score_file(
    project_id: str, generated_content: str,
) -> Optional[float]:
    """Return semantic similarity 0–100 vs the pooled legacy corpus.

    Returns None (not 0) when scoring cannot proceed — the caller must
    treat None as "n/a" (pass-through), not as failure.
    """
    if not is_enabled():
        return None
    if not (generated_content or "").strip():
        return None
    corpus = await _load_corpus(project_id)
    if not corpus or corpus.get("embeddings") is None:
        return None
    try:
        from confidence_langgraph import _get_embedder  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    embedder = await _get_embedder()
    if embedder is None:
        return None
    import asyncio
    try:
        q_emb = await asyncio.to_thread(
            embedder.encode, [generated_content[:8000]],
            convert_to_tensor=True, normalize_embeddings=True,
            show_progress_bar=False,
        )
        from sentence_transformers import util as st_util  # type: ignore
        sims = st_util.cos_sim(q_emb, corpus["embeddings"])[0]
        # Top-3 mean gives us stable signal without letting a single
        # unrelated legacy file dominate.
        top = sorted([float(s) for s in sims.tolist()], reverse=True)[:3]
        if not top:
            return None
        best = sum(top) / len(top)
    except Exception as e:  # noqa: BLE001
        logger.warning("hf_confidence: score_file failed: %s", e)
        return None
    min_sim = _f("LAMA_CODEGEN_HF_MIN_SIM", 0.20)
    # Map cosine (usually 0.0..1.0 for normalised code embeddings, but
    # practically ~0.2..0.8) → confidence 0..100.
    # Floor to 40 below min_sim so a mismatch doesn't crater the whole
    # file's score; ceiling at 100 above 0.90.
    if best <= min_sim:
        return 40.0
    if best >= 0.90:
        return 100.0
    # Linear interpolation between (min_sim, 55) and (0.90, 100).
    frac = (best - min_sim) / (0.90 - min_sim)
    return round(55.0 + frac * 45.0, 2)


def reset_cache() -> None:
    """Test hook — clear the per-project corpus cache."""
    _CORPUS_CACHE.clear()
