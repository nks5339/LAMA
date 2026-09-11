"""iter-13.100 — Rolling-memory agent-session HTTP surface.

REST endpoints backing the durable, droid-handoff-aware LLM session
introduced in Phase 1-3. The frontend (`ChatPanel`, `ProjectContext`)
uses these to:

  • create a new session when the user clicks "New session"
  • resume an existing session by id (survives browser refresh + droid swap)
  • list / archive sessions per project
  • attach additional `ContextRef`s mid-conversation

Auth: piggy-backs on the rest of the API (JWT via `routes/auth.py`).
The `tenant_id` is sourced from the authenticated user when available,
falling back to "tenant_default" so split-process dev works without auth.

Security: every read/write goes through `agent_memory.*` which enforces
the HMAC signing contract + `HYDRATABLE_COLLECTIONS` allowlist. This
route file MUST NOT bypass those.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from agent_memory import (
    archive_session,
    attach_ref,
    create_session,
    get_session,
    list_sessions,
)
from db import projects as projects_col
from models import AgentSessionCreate, ContextRef

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _tenant_id_for_request(req: Request, project_id: str) -> str:
    """Best-effort tenant resolution.

    Order:
      1. `req.state.user.tenant_id` if the auth middleware populated it
         (currently only some routes do — `routes/auth.py` sets it).
      2. The project's own `tenant_id` field (multi-tenant — iter-13.68).
      3. "tenant_default" — split-process dev / pre-13.68 projects.
    """
    user = getattr(req.state, "user", None)
    if user and getattr(user, "tenant_id", "") and user.tenant_id != "*":
        return user.tenant_id
    proj = await projects_col.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1})
    return ((proj or {}).get("tenant_id") or "tenant_default")


# ─────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────
@router.post("")
async def create_session_endpoint(payload: AgentSessionCreate, request: Request):
    """Create a new rolling-memory session.

    Body: AgentSessionCreate (project_id, stage, agent_key, refs?, seed_summary?)
    Returns the persisted AgentSession dict.
    """
    if not payload.project_id.strip():
        raise HTTPException(400, "project_id is required")
    proj = await projects_col.find_one({"id": payload.project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    tenant_id = await _tenant_id_for_request(request, payload.project_id)
    sess = await create_session(payload, tenant_id=tenant_id)
    return sess.model_dump()


@router.get("/{session_id}")
async def get_session_endpoint(session_id: str):
    """Fetch a session by id. Returns 404 if absent."""
    sess = await get_session(session_id)
    if sess is None:
        raise HTTPException(404, "Session not found")
    return sess.model_dump()


@router.get("")
async def list_sessions_endpoint(
    project_id: str,
    stage: Optional[str] = None,
    agent_key: Optional[str] = None,
    include_archived: bool = False,
):
    """List sessions for a project, optionally filtered by stage / agent_key."""
    if not project_id.strip():
        raise HTTPException(400, "project_id query param is required")
    sessions = await list_sessions(
        project_id=project_id, stage=stage, agent_key=agent_key,
        include_archived=include_archived,
    )
    return {"sessions": [s.model_dump() for s in sessions], "count": len(sessions)}


@router.post("/{session_id}/archive")
async def archive_session_endpoint(session_id: str, payload: Optional[dict] = None):
    """Mark a session archived. New turns must NOT be appended after this.

    Use case: the user froze the stage this session belonged to — start a
    fresh one for the next stage instead.
    """
    reason = ((payload or {}).get("reason") or "").strip()
    sess = await archive_session(session_id, reason=reason)
    if sess is None:
        raise HTTPException(404, "Session not found")
    return sess.model_dump()


@router.post("/{session_id}/refs")
async def attach_ref_endpoint(session_id: str, ref: ContextRef):
    """Attach an additional context ref to an existing session.

    Re-attaching the same (collection, doc_id) replaces the previous
    entry (so callers can refresh the hash without growing the refs list).
    """
    sess = await attach_ref(session_id, ref)
    if sess is None:
        raise HTTPException(404, "Session not found")
    return sess.model_dump()

