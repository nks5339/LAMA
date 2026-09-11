"""Rolling-memory layer for LLM agent sessions (iter-13.100).

Goal: give every `fabric_call(..., session_id=...)` an "infinite
conversation" that survives:
  • LLM context-window overflow      (rollover: older turns → summary)
  • Browser refresh                  (session ID lives in localStorage)
  • Droid handoff (Droid 1 → Droid 2)(both droids hit the same Mongo)

Hard contracts (don't violate):
  1. `summary` is a HINT only. Authoritative facts live in `refs`
     (stage_context / KB doc IDs). Server re-hydrates refs on every
     read; a tampered summary cannot smuggle in a fake "frozen SRS".
  2. Every summary is HMAC-signed with `LAMA_SESSION_SIGNING_KEY`.
     Sig mismatch on read → wipe the summary and start fresh.
  3. Rollover NEVER touches `live_turns`'s last K entries verbatim;
     only the older prefix is fed to the summariser.
  4. Audit-log every state change to `audit_log` (CLAUDE.md §8).

Module is self-contained — only `db`, `models`, `llm.fabric_call`
imports, no cross-route cross-imports.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from db import agent_sessions as sess_col
from db import audit_log as audit_col
from db import projects as projects_col
from models import (
    AgentSession,
    AgentSessionCreate,
    AgentTurn,
    ContextRef,
)

log = logging.getLogger("lama.agent_memory")


# ──────────────────────────────────────────────────────────────────────
# iter-13.100 / Phase 3 — Hydration allowlist.
#
# Refs are pointers to docs in arbitrary Mongo collections. To prevent a
# client-tampered AgentSession from smuggling in `agent_configs` (API
# keys), `users` (password hashes), etc., we enforce a strict
# collection→fields allowlist. Anything outside this map is REFUSED at
# hydration time and surfaces as a "forbidden" entry in the prompt
# instead of being silently dropped (so the LLM can't be tricked into
# thinking a missing/forbidden ref is "approved context").
#
# Each entry maps `collection_name → tuple_of_safe_fields_to_inline`.
# Adding a new collection here is a security review change — do not
# extend casually.
# ──────────────────────────────────────────────────────────────────────
HYDRATABLE_COLLECTIONS: Dict[str, Tuple[str, ...]] = {
    "stage_context":   ("stage", "version", "frozen_at", "outputs", "toon_summary"),
    "srs_documents":   ("version", "sections", "frozen"),
    "kb_files":        ("filename", "filetype", "kind", "kind_notes"),
    "kb_entities":     ("name", "type", "metadata"),
    "kb_toon":         ("toon", "version"),
    "data_models":     ("type", "content", "version", "frozen"),
    "arch_documents":  ("type", "content", "version", "frozen"),
    "codegen_files":   ("file_path", "content", "language", "file_type", "version"),
    "legacy_analysis": ("analysis", "version"),
    "business_ontologies": ("entities", "version"),
}

# Cap per-ref content inlined into the prompt. Refs that exceed this are
# truncated with a marker so the LLM knows the content was clipped. Keep
# the per-ref cap modest because a single session may attach many refs.
HYDRATED_REF_MAX_CHARS = int(
    (os.environ.get("LAMA_HYDRATED_REF_MAX_CHARS") or "16000").strip() or 16000
)


# ──────────────────────────────────────────────────────────────────────
# Per-agent memory policy.
#
# Lives next to `AGENT_COMPLEXITY` in fabric.model_fabric �� but kept HERE
# so a single import (`from agent_memory import AGENT_MEMORY`) is enough
# for callers that want to tune per-agent defaults without pulling the
# whole fabric module. The keys mirror `AGENT_COMPLEXITY`.
#
# Schema per entry:
#   keep_last_k        — verbatim turns retained after rollover
#   window_budget_pct  — % of model context_window before we trigger rollover
#                        (0.80 = roll over when prompt would use 80% of window)
#   summary_strategy   — "compact" (default, single-paragraph) |
#                        "bullets" (decision-list style) |
#                        "verbatim_decisions" (keep BR IDs / decisions intact)
# ──────────────────────────────────────────────────────────────────────
AGENT_MEMORY: Dict[str, Dict[str, Any]] = {
    # Discovery / SRS chat — exploratory, longer tail useful
    "srs.chat":            {"keep_last_k": 10, "window_budget_pct": 0.80, "summary_strategy": "verbatim_decisions"},
    "srs.gap_question":    {"keep_last_k": 6,  "window_budget_pct": 0.80, "summary_strategy": "compact"},
    # DataModel — structural, compress aggressively, keep BR IDs
    "datamodel.chat":      {"keep_last_k": 6,  "window_budget_pct": 0.75, "summary_strategy": "verbatim_decisions"},
    # Architecture — design decisions matter a lot
    "arch.chat":           {"keep_last_k": 8,  "window_budget_pct": 0.80, "summary_strategy": "verbatim_decisions"},
    # CodeGen — short tail; the per-file context dominates
    "codegen.chat":        {"keep_last_k": 4,  "window_budget_pct": 0.70, "summary_strategy": "compact"},
    # Living — drift detection, medium tail
    "living.chat":         {"keep_last_k": 6,  "window_budget_pct": 0.80, "summary_strategy": "compact"},
}

# Fallback for any agent_key not explicitly listed.
DEFAULT_MEMORY_POLICY: Dict[str, Any] = {
    "keep_last_k": 8,
    "window_budget_pct": 0.80,
    "summary_strategy": "compact",
}


def memory_policy(agent_key: str) -> Dict[str, Any]:
    """Return the (possibly default) memory policy for `agent_key`."""
    return AGENT_MEMORY.get((agent_key or "").strip(), DEFAULT_MEMORY_POLICY)


# ──────────────────────────────────────────────────────────────────────
# HMAC signing — protects `summary` from client-side tampering.
#
# The key is read from `LAMA_SESSION_SIGNING_KEY`. If unset, we
# auto-generate one on first use and persist it into Mongo (under a
# dedicated `_lama_secrets` doc on the `projects` collection — same
# storage volume, no new collection) so subsequent process restarts
# pick up the same key. This keeps dev frictionless without disabling
# the security boundary.
# ──────────────────────────────────────────────────────────────────────
_SIGNING_KEY_CACHE: Optional[bytes] = None
_SECRETS_DOC_ID = "__lama_secrets__"


async def _signing_key() -> bytes:
    """Return the HMAC key bytes. Cached process-locally after first read."""
    global _SIGNING_KEY_CACHE
    if _SIGNING_KEY_CACHE is not None:
        return _SIGNING_KEY_CACHE
    env_key = (os.environ.get("LAMA_SESSION_SIGNING_KEY") or "").strip()
    if env_key:
        _SIGNING_KEY_CACHE = env_key.encode("utf-8")
        return _SIGNING_KEY_CACHE
    # Auto-generate + persist (idempotent).
    doc = await projects_col.find_one({"id": _SECRETS_DOC_ID}, {"_id": 0, "agent_session_signing_key": 1})
    if doc and doc.get("agent_session_signing_key"):
        _SIGNING_KEY_CACHE = doc["agent_session_signing_key"].encode("utf-8")
        return _SIGNING_KEY_CACHE
    new_key = secrets.token_urlsafe(48)
    await projects_col.update_one(
        {"id": _SECRETS_DOC_ID},
        {"$set": {
            "id": _SECRETS_DOC_ID,
            "name": "(internal: lama secrets)",
            "agent_session_signing_key": new_key,
            "updated_at": _now_iso(),
        }},
        upsert=True,
    )
    _SIGNING_KEY_CACHE = new_key.encode("utf-8")
    log.warning(
        "LAMA_SESSION_SIGNING_KEY was not set — auto-generated and persisted. "
        "For production, set the env-var explicitly so the key survives Mongo wipes."
    )
    return _SIGNING_KEY_CACHE


def _refs_digest(refs: List[ContextRef]) -> str:
    """Stable canonical fingerprint of a refs list for HMAC payload."""
    parts = sorted(f"{r.collection}:{r.doc_id}:{r.hash}" for r in (refs or []))
    return "|".join(parts)


async def sign_summary(
    *, summary: str, refs: List[ContextRef],
    project_id: str, stage: str, agent_key: str,
) -> str:
    """Compute HMAC-SHA256 hex digest binding the summary to its context."""
    key = await _signing_key()
    payload = "\x1f".join([
        (summary or ""),
        _refs_digest(refs),
        project_id or "",
        stage or "",
        agent_key or "",
    ]).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


async def verify_summary_sig(session: AgentSession) -> bool:
    """Return True iff `session.summary_sig` matches the rest of the session.

    Empty summary AND empty sig → trivially valid (no rollover happened yet).
    """
    if not session.summary and not session.summary_sig:
        return True
    if not session.summary_sig:
        return False
    expected = await sign_summary(
        summary=session.summary, refs=session.refs,
        project_id=session.project_id, stage=session.stage,
        agent_key=session.agent_key,
    )
    return hmac.compare_digest(expected, session.summary_sig)


# ──────────────────────────────────────────────────────────────────────
# Token estimation. Reuse the chars/4 approximation already used
# elsewhere in the codebase (`llm.estimate_tokens`,
# `fabric.model_fabric.estimate_tokens`) — no new tiktoken dep.
# ────────────────────────────────────────────────────────��─────────────
def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def estimate_session_tokens(session: AgentSession) -> int:
    """Rough total prompt-side token cost of a hydrated session."""
    total = estimate_tokens(session.summary)
    for t in session.live_turns:
        total += t.token_count or estimate_tokens(t.content)
    # Refs are passed BY REFERENCE — token cost is paid when the route
    # hydrates them into the prompt. Account for label-only here; the
    # actual hydration tally happens in the caller.
    for r in session.refs:
        total += estimate_tokens(r.label) + 16
    return total


# ──────────────────────────────────────────────────────────────────────
# Audit-log helper (mirrors the pattern in routes/audit.py — kept inline
# to avoid a circular import from this low-level module).
# ──────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit(action: str, session: AgentSession, extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        entry: Dict[str, Any] = {
            "ts": _now_iso(),
            "action": action,
            "project_id": session.project_id,
            "tenant_id": session.tenant_id,
            "stage": session.stage,
            "agent_key": session.agent_key,
            "session_id": session.id,
            "rollover_count": session.rollover_count,
            "token_count": session.token_count,
        }
        if extra:
            entry.update(extra)
        await audit_col.insert_one(entry)
    except Exception:  # noqa: BLE001 — audit must never break the session
        log.warning("agent_memory audit-log insert failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────��───

# iter-13.100 Phase 5 — Stage handoff order. The freeze contract
# (`pipeline.require_stage_context`) already enforces this — we mirror
# it here so a session created in stage N+1 can auto-attach the
# frozen stage_context refs of every upstream stage as authoritative
# context. That way the LLM ALWAYS sees the freeze-gate handoff
# without each route having to wire it manually.
_STAGE_ORDER: Tuple[str, ...] = ("Discovery", "DataModel", "Architecture", "CodeGen", "Living")


def _upstream_stages(stage: str) -> Tuple[str, ...]:
    """Return the stages whose frozen handoff context is authoritative
    for `stage`. E.g. Architecture's upstream = (Discovery, DataModel)."""
    stage = (stage or "").strip()
    if stage not in _STAGE_ORDER:
        return ()
    idx = _STAGE_ORDER.index(stage)
    return _STAGE_ORDER[:idx]


async def auto_attach_upstream_refs(project_id: str, stage: str) -> List[ContextRef]:
    """Build a refs list pointing at every frozen `stage_context` doc
    upstream of `stage`. Includes a sha256 hash of the canonical content
    so future drift can be detected by the hydrator.

    Returns an empty list when no upstream stages have been frozen yet
    (i.e. caller is starting a session in Discovery, or upstream stages
    are still in-flight).

    NOTE: This does NOT short-circuit `pipeline.require_stage_context`.
    Freeze gates remain the canonical entry permission; this helper is
    purely about pre-loading the resulting handoff into the session's
    LLM-visible context.
    """
    upstream = _upstream_stages(stage)
    if not upstream:
        return []
    try:
        from db import stage_context as sc_col
    except Exception:  # noqa: BLE001
        return []
    refs: List[ContextRef] = []
    for st in upstream:
        doc = await sc_col.find_one(
            {"project_id": project_id, "stage": st}, {"_id": 0},
        )
        if not doc or not doc.get("id"):
            # Upstream stage not yet frozen — skip silently. The
            # freeze gate at the route layer will reject downstream
            # operations until it is. This helper deliberately does
            # not raise so a "preview" session created mid-flight
            # still works.
            continue
        # Hash the same field-projection the hydrator will use, so the
        # initial hash matches and `drifted=False` on first hydration.
        allowed = HYDRATABLE_COLLECTIONS.get("stage_context", ())
        content = _content_for_ref(doc, allowed)
        refs.append(ContextRef(
            collection="stage_context",
            doc_id=doc["id"],
            label=f"{st} frozen v{doc.get('version', '?')}",
            hash=_ref_hash(content),
        ))
    return refs


async def create_session(
    req: AgentSessionCreate,
    *,
    tenant_id: str = "tenant_default",
    auto_attach_upstream: bool = True,
) -> AgentSession:
    """Create a new rolling-memory session.

    When `auto_attach_upstream=True` (default — Phase 5) AND the caller
    didn't supply any refs explicitly, the session is pre-loaded with
    every frozen upstream `stage_context` ref. This makes the freeze-gate
    handoff automatically visible to the LLM without each route having to
    attach refs manually. Set False if the caller wants total control
    over what the session sees (e.g. a "fresh blank" exploration session).

    Optionally seeds a starting summary (e.g. from a droid-handoff brief).
    The seed summary is signed BEFORE the session is persisted so any
    future read finds a valid signature out of the gate.
    """
    seeded_refs = list(req.refs or [])
    if auto_attach_upstream and not seeded_refs:
        try:
            seeded_refs = await auto_attach_upstream_refs(req.project_id, req.stage)
        except Exception:  # noqa: BLE001 — best-effort, never block creation
            log.warning(
                "auto_attach_upstream_refs failed for project=%s stage=%s — "
                "creating session without upstream refs",
                req.project_id, req.stage, exc_info=True,
            )
            seeded_refs = []
    sess = AgentSession(
        project_id=req.project_id,
        tenant_id=tenant_id or "tenant_default",
        stage=req.stage,
        agent_key=req.agent_key,
        refs=seeded_refs,
        summary=req.seed_summary or "",
    )
    if sess.summary:
        sess.summary_sig = await sign_summary(
            summary=sess.summary, refs=sess.refs,
            project_id=sess.project_id, stage=sess.stage,
            agent_key=sess.agent_key,
        )
        sess.summary_model = "seed/handoff-brief"
    await sess_col.insert_one(sess.model_dump())
    await _audit("session.created", sess, {
        "seeded": bool(sess.summary),
        "auto_attached_refs": len(seeded_refs) if auto_attach_upstream and not (req.refs or []) else 0,
    })
    return sess


async def get_session(session_id: str) -> Optional[AgentSession]:
    """Load a session by id. Returns None if not found.

    On signature mismatch the summary is WIPED in-place (and persisted)
    so subsequent calls don't keep tripping the same invalid signature.
    The session itself remains usable — just with a fresh empty summary.
    """
    doc = await sess_col.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        return None
    sess = AgentSession(**doc)
    if not await verify_summary_sig(sess):
        log.warning(
            "agent_memory: signature mismatch on session %s (pid=%s, agent=%s) — wiping summary",
            sess.id, sess.project_id, sess.agent_key,
        )
        sess.summary = ""
        sess.summary_sig = ""
        sess.summary_model = ""
        await sess_col.update_one(
            {"id": sess.id},
            {"$set": {
                "summary": "", "summary_sig": "", "summary_model": "",
                "updated_at": _now_iso(),
            }},
        )
        await _audit("session.summary_sig_invalid", sess)
    return sess


async def append_turn(
    session_id: str, *, role: str, content: str,
) -> Optional[AgentSession]:
    """Append a verbatim turn to the live tail. Caller is responsible for
    triggering rollover separately (see `maybe_rollover`)."""
    sess = await get_session(session_id)
    if sess is None:
        return None
    turn = AgentTurn(
        role=role, content=content,
        token_count=estimate_tokens(content),
    )
    sess.live_turns.append(turn)
    sess.token_count = estimate_session_tokens(sess)
    sess.updated_at = _now_iso()
    await sess_col.update_one(
        {"id": sess.id},
        {"$set": {
            "live_turns": [t.model_dump() for t in sess.live_turns],
            "token_count": sess.token_count,
            "updated_at": sess.updated_at,
        }},
    )
    return sess


async def attach_ref(session_id: str, ref: ContextRef) -> Optional[AgentSession]:
    """Add a context ref to a session. Re-signs the summary so the new
    refs digest is reflected in the signature."""
    sess = await get_session(session_id)
    if sess is None:
        return None
    # De-dupe on (collection, doc_id) — re-attaching just refreshes the hash.
    sess.refs = [
        r for r in sess.refs
        if not (r.collection == ref.collection and r.doc_id == ref.doc_id)
    ]
    sess.refs.append(ref)
    if sess.summary:
        sess.summary_sig = await sign_summary(
            summary=sess.summary, refs=sess.refs,
            project_id=sess.project_id, stage=sess.stage,
            agent_key=sess.agent_key,
        )
    sess.updated_at = _now_iso()
    await sess_col.update_one(
        {"id": sess.id},
        {"$set": {
            "refs": [r.model_dump() for r in sess.refs],
            "summary_sig": sess.summary_sig,
            "updated_at": sess.updated_at,
        }},
    )
    return sess


async def archive_session(session_id: str, *, reason: str = "") -> Optional[AgentSession]:
    """Mark a session archived (e.g. when its stage gets frozen).
    Archived sessions are still readable but new turns should NOT be
    appended — start a fresh session for the next stage instead."""
    sess = await get_session(session_id)
    if sess is None:
        return None
    sess.status = "archived"
    sess.updated_at = _now_iso()
    await sess_col.update_one(
        {"id": sess.id},
        {"$set": {"status": "archived", "updated_at": sess.updated_at}},
    )
    await _audit("session.archived", sess, {"reason": reason})
    return sess


async def list_sessions(
    *, project_id: str, stage: Optional[str] = None,
    agent_key: Optional[str] = None, include_archived: bool = False,
) -> List[AgentSession]:
    q: Dict[str, Any] = {"project_id": project_id}
    if stage:
        q["stage"] = stage
    if agent_key:
        q["agent_key"] = agent_key
    if not include_archived:
        q["status"] = "active"
    cursor = sess_col.find(q, {"_id": 0}).sort([("updated_at", -1)])
    return [AgentSession(**d) async for d in cursor]


# ──────────────────────────────────────────────────────────────────────
# Rollover — the heart of the rolling-memory pattern.
#
# When called, we:
#   1. Compute the resolved context_window for this agent's model.
#   2. If estimated session tokens are below the threshold, no-op.
#   3. Otherwise, split live_turns into (older_prefix, last_K).
#   4. Summarise older_prefix via fabric_call (using the project's own
#      Console-configured LLM). Re-uses the iter-12 fallback chain so a
#      failed summary doesn't kill the session — we keep the old summary
#      and skip the rollover this round.
#   5. Persist new summary (HMAC-signed), keep last_K verbatim.
# ──────────────────────────────────────────────────────────────────────
ROLLOVER_SYSTEM_PROMPT = (
    "You are the MEMORY COMPRESSOR for an autonomous code-modernization "
    "agent (LAMA). You receive a chronological transcript of older "
    "conversation turns plus an existing summary (possibly empty). "
    "Your job: produce a NEW summary that lets a fresh LLM session "
    "continue the work WITHOUT losing critical context.\n\n"
    "HARD RULES:\n"
    "  • Preserve every Business Rule ID (BR-###), decision, accepted/"
    "rejected option, frozen artifact reference, and open question "
    "VERBATIM. These are non-negotiable.\n"
    "  • Drop chit-chat, restated user instructions, and intermediate "
    "reasoning that was superseded.\n"
    "  • Cap at 2000 words. Use compact prose or bullets — your choice "
    "based on the strategy hint below.\n"
    "  • NEVER invent facts. If something was discussed but the outcome "
    "was inconclusive, say 'undecided: <topic>'.\n"
    "  • Do not include the new summary inside code fences. Plain text "
    "only.\n"
)


async def maybe_rollover(
    session_id: str, *,
    resolved_context_window: int,
    fabric_call,                       # injected to break import cycle
    summary_model: str = "",
) -> Tuple[AgentSession, bool]:
    """Run a rollover pass if the session is over budget.

    Returns (session, rolled_over_bool). `fabric_call` is injected by the
    caller (typically `llm.fabric_call`) so this module stays free of
    circular imports.
    """
    sess = await get_session(session_id)
    if sess is None:
        raise RuntimeError(f"agent_memory: session {session_id!r} not found")

    policy = memory_policy(sess.agent_key)
    budget = max(1024, int(resolved_context_window * policy["window_budget_pct"]))
    sess.window_budget = budget
    current = estimate_session_tokens(sess)
    if current < budget:
        # Persist updated window_budget snapshot but skip rollover.
        await sess_col.update_one(
            {"id": sess.id}, {"$set": {"window_budget": budget}},
        )
        return sess, False

    keep_k = max(1, int(policy["keep_last_k"]))
    if len(sess.live_turns) <= keep_k:
        # Nothing to roll over — last K already covers everything. Bail
        # so we don't keep re-summarising the same tiny tail.
        return sess, False

    older = sess.live_turns[:-keep_k]
    tail = sess.live_turns[-keep_k:]

    # Build the summariser prompt.
    transcript = "\n\n".join(
        f"[{t.role.upper()}] {t.content}" for t in older
    )
    strategy_hint = {
        "compact":             "Single coherent paragraph, ≤400 words.",
        "bullets":             "Tight bullet list, one decision per bullet.",
        "verbatim_decisions":  "Bullets — keep BR IDs and decisions exactly as written; compress everything else.",
    }.get(policy["summary_strategy"], "Single coherent paragraph, ≤400 words.")

    refs_hint = "; ".join(f"{r.collection}:{r.label or r.doc_id}" for r in sess.refs) or "(none)"

    user_msg = (
        f"Strategy hint: {strategy_hint}\n"
        f"Existing summary (may be empty):\n---\n{sess.summary or '(empty)'}\n---\n\n"
        f"Authoritative context refs (DO NOT summarise these — they are "
        f"pulled fresh from the DB on every call):\n  {refs_hint}\n\n"
        f"Older transcript to compress:\n---\n{transcript}\n---\n\n"
        f"Produce the new summary now."
    )

    try:
        result = await fabric_call(
            messages=[
                {"role": "system", "content": ROLLOVER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            agent_key="agent_memory.rollover",
            project_id=sess.project_id,
            model=summary_model,        # may be "" → fabric routes via Console
            temperature=0.1,
            max_tokens=4096,
            timeout=120.0,
        )
        new_summary = (result.get("content") or "").strip()
        used_model = result.get("model") or summary_model or ""
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "agent_memory rollover failed for session %s — keeping old summary. err=%s",
            sess.id, str(exc)[:200],
        )
        await _audit("session.rollover_failed", sess, {"error": str(exc)[:200]})
        return sess, False

    if not new_summary:
        # Empty summary returned — same fail-safe.
        await _audit("session.rollover_empty", sess)
        return sess, False

    sess.summary = new_summary
    sess.summary_model = used_model
    sess.summary_sig = await sign_summary(
        summary=sess.summary, refs=sess.refs,
        project_id=sess.project_id, stage=sess.stage,
        agent_key=sess.agent_key,
    )
    sess.live_turns = tail
    sess.rollover_count += 1
    sess.token_count = estimate_session_tokens(sess)
    sess.updated_at = _now_iso()
    await sess_col.update_one(
        {"id": sess.id},
        {"$set": {
            "summary": sess.summary,
            "summary_sig": sess.summary_sig,
            "summary_model": sess.summary_model,
            "live_turns": [t.model_dump() for t in sess.live_turns],
            "rollover_count": sess.rollover_count,
            "token_count": sess.token_count,
            "window_budget": sess.window_budget,
            "updated_at": sess.updated_at,
        }},
    )
    await _audit("session.rolled_over", sess, {
        "summarised_turn_count": len(older),
        "kept_turn_count": len(tail),
        "summary_model": sess.summary_model,
    })
    log.info(
        "agent_memory: rolled over session %s (pid=%s agent=%s) — summarised %d turns, kept %d",
        sess.id, sess.project_id, sess.agent_key, len(older), len(tail),
    )
    return sess, True


# ──────────────────────────────────────────────────────────────────────
# Envelope-building helper — used by `fabric_call` (Phase 2) to convert
# an AgentSession into a messages[] prefix for the next LLM call.
#
# Output shape: [system?, ...refs-hydrated?, summary?, ...live_turns]
# The caller appends its own user message after this prefix.
# ──────────────────────────────────────────────────────────────────────
async def _fetch_doc(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    """Look up a doc in `collection` by id. Returns None on miss / error.

    Uses the module-level `db` object so monkeypatched test fakes are
    picked up automatically (tests replace `db.<collection>` with an
    in-memory FakeCollection).
    """
    try:
        import db as _db
        # Prefer the named attribute (so tests that monkeypatch
        # `db.stage_context` work). Fall back to the dynamic accessor
        # for any collection not pre-exported in db.py.
        col = getattr(_db, collection, None)
        if col is None:
            col = _db.db[collection]
        return await col.find_one({"id": doc_id}, {"_id": 0})
    except Exception:  # noqa: BLE001
        return None


def _content_for_ref(doc: Dict[str, Any], allowed_fields: Tuple[str, ...]) -> str:
    """Render the allow-listed fields of a doc as a compact JSON string."""
    projected = {k: doc[k] for k in allowed_fields if k in doc}
    try:
        text = json.dumps(projected, default=str, sort_keys=True, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = repr(projected)
    if len(text) > HYDRATED_REF_MAX_CHARS:
        text = text[:HYDRATED_REF_MAX_CHARS] + f"\n…[truncated; {len(text) - HYDRATED_REF_MAX_CHARS} chars dropped]"
    return text


def _ref_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def hydrate_refs(refs: List[ContextRef]) -> List[Dict[str, Any]]:
    """Resolve each ref against its collection, returning a list of dicts:

        {
          "ref":      the original ContextRef,
          "status":   "ok" | "missing" | "forbidden" | "error",
          "content":  fresh content string (empty when not ok),
          "drifted":  bool — True iff `ref.hash` was set AND differs from
                      the freshly-computed hash (content changed since
                      the ref was attached),
          "current_hash": sha256 of the fresh content (empty when not ok),
        }

    Hydration is the SINGLE point where untrusted ref material becomes
    LLM-visible. The `HYDRATABLE_COLLECTIONS` allowlist is enforced here
    and nowhere else — DO NOT bypass.
    """
    out: List[Dict[str, Any]] = []
    for ref in (refs or []):
        col_name = (ref.collection or "").strip()
        if col_name not in HYDRATABLE_COLLECTIONS:
            out.append({
                "ref": ref, "status": "forbidden",
                "content": "", "drifted": False, "current_hash": "",
            })
            log.warning(
                "agent_memory.hydrate_refs: refused non-allowlisted collection=%r doc_id=%s",
                col_name, ref.doc_id,
            )
            continue
        doc = await _fetch_doc(col_name, ref.doc_id)
        if not doc:
            out.append({
                "ref": ref, "status": "missing",
                "content": "", "drifted": False, "current_hash": "",
            })
            continue
        try:
            content = _content_for_ref(doc, HYDRATABLE_COLLECTIONS[col_name])
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "agent_memory.hydrate_refs: render failed for %s/%s: %s",
                col_name, ref.doc_id, exc,
            )
            out.append({
                "ref": ref, "status": "error",
                "content": "", "drifted": False, "current_hash": "",
            })
            continue
        h = _ref_hash(content)
        drifted = bool((ref.hash or "") and (ref.hash or "") != h)
        out.append({
            "ref": ref, "status": "ok",
            "content": content, "drifted": drifted, "current_hash": h,
        })
    return out


async def build_prompt_prefix(
    session: AgentSession,
    *,
    hydrate: bool = True,
) -> List[Dict[str, str]]:
    """Build the OpenAI-style messages[] prefix that captures everything
    this session "remembers".

    When `hydrate=True` (default, Phase-3 secure mode):
      • each ref's CURRENT content is fetched fresh from its collection
        and inlined as its own system message — this is the
        AUTHORITATIVE fact source.
      • the rolling summary is downgraded to a "HINT only" message
        sitting between authoritative refs and live turns.
      • content drift since the ref was attached is announced inline so
        the LLM can react (`[DRIFTED since attached]`).
      • forbidden / missing refs surface as explicit notices so the LLM
        cannot be tricked into treating them as "approved silence".

    When `hydrate=False` (legacy / test mode):
      • refs are mentioned by collection+label only — caller is
        responsible for fetching content elsewhere. Used by tests that
        want to assert pure prefix shape without exercising the
        hydrator.

    Caller pattern:
        prefix = await build_prompt_prefix(session)
        messages = prefix + [{"role": "user", "content": user_query}]
        result = await fabric_call(messages=messages, ..., session_id=session.id)
        await append_turn(session.id, role="user", content=user_query)
        await append_turn(session.id, role="assistant", content=result["content"])
    """
    out: List[Dict[str, str]] = []

    if hydrate and session.refs:
        hydrated = await hydrate_refs(session.refs)
        for h in hydrated:
            ref: ContextRef = h["ref"]
            label = ref.label or f"{ref.collection}/{ref.doc_id}"
            status = h["status"]
            if status == "ok":
                drift_tag = " — [DRIFTED since attached]" if h["drifted"] else ""
                out.append({
                    "role": "system",
                    "content": (
                        f"=== AUTHORITATIVE REF · {ref.collection}/{ref.doc_id} "
                        f"({label}){drift_tag} ===\n"
                        f"{h['content']}\n"
                        f"=== END REF ==="
                    ),
                })
            elif status == "missing":
                out.append({
                    "role": "system",
                    "content": (
                        f"=== REF MISSING · {ref.collection}/{ref.doc_id} ({label}) ===\n"
                        f"This ref was attached earlier but the referenced doc "
                        f"no longer exists. Do NOT invent its content; if you "
                        f"need it, ask the user to re-attach.\n"
                        f"=== END REF ==="
                    ),
                })
            elif status == "forbidden":
                out.append({
                    "role": "system",
                    "content": (
                        f"=== REF FORBIDDEN · {ref.collection}/{ref.doc_id} ({label}) ===\n"
                        f"This ref points at a non-allowlisted collection and "
                        f"was refused. Ignore it.\n"
                        f"=== END REF ==="
                    ),
                })
            else:  # error
                out.append({
                    "role": "system",
                    "content": (
                        f"=== REF ERROR · {ref.collection}/{ref.doc_id} ({label}) ===\n"
                        f"Could not render this ref. Ignore it for this turn.\n"
                        f"=== END REF ==="
                    ),
                })

    if not hydrate and session.refs:
        # Legacy / test-friendly label-only output (pre-Phase-3 shape).
        ref_lines = ["AUTHORITATIVE CONTEXT REFS (re-hydrated on every call):"]
        for r in session.refs:
            tag = r.label or f"{r.collection}/{r.doc_id}"
            ref_lines.append(f"  • {r.collection}:{r.doc_id} — {tag}")
        out.append({"role": "system", "content": "\n".join(ref_lines)})

    if session.summary:
        # Phase 3: summary is now positioned AFTER hydrated refs and
        # explicitly labelled as a HINT, not a source of truth. That way
        # any contradiction between summary and a hydrated ref resolves
        # in favour of the ref (the LLM gets the right cue from order +
        # label).
        summary_label = (
            "ROLLING MEMORY SUMMARY (HINT only — authoritative facts are in "
            "the AUTHORITATIVE REF blocks above):"
            if hydrate and session.refs
            else "ROLLING MEMORY SUMMARY (compressed from older turns; "
                 "AUTHORITATIVE FACTS are in stage_context refs below, not here):"
        )
        out.append({"role": "system", "content": summary_label + "\n" + session.summary})

    for t in session.live_turns:
        if t.role in ("user", "assistant", "system"):
            out.append({"role": t.role, "content": t.content})
    return out






