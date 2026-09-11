"""First-time Factory AI activation onboarding.

When a project enables the Factory orchestrator for the first time in the
Console, we automatically:

  1. Load the technology-agnostic modernization prompt from
     `prompts/factory-modernization-agent.yml`.
  2. Resolve the legacy folder (Factory `cwd` → falls back to KB-derived
     project root) and the lightweight KB file index already built by
     Stage 1 Discovery.
  3. Post a single bootstrapping message to the Factory session that
     instructs the Droid to (a) build its KB view of the legacy code and
     (b) emit the top-3 target stack recommendation with DB pinned to
     the legacy engine.
  4. Stamp `settings.factory_orchestrator.onboarded_at` so this never
     re-fires for the same project.

The function is **safe to call repeatedly** — it short-circuits when
`onboarded_at` is already set unless `force=True` is passed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from db import projects, kb_files, audit_log

log = logging.getLogger("lama.onboarding")

# Repo-root / prompts / factory-modernization-agent.yml
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "factory-modernization-agent.yml"

# Cap the KB file-index slice we send to Factory so the bootstrap prompt
# stays inside the Factory `_build_prompt_text` cap (~48k chars).
_KB_INDEX_MAX_FILES = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_modernization_prompt() -> str:
    """Read the YAML prompt. Falls back to a minimal inline version if
    the file is missing (e.g. the repo was deployed without `prompts/`).
    """
    try:
        if _PROMPT_PATH.exists():
            return _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Could not read %s: %s", _PROMPT_PATH, e)
    # Minimal inline fallback — same five phases, drastically compressed.
    return (
        "modernization-agent (inline fallback):\n"
        "P0 build KB from legacy folder.\n"
        "P1 recommend top-3 target stacks (PIN db engine to legacy; the 3\n"
        "   combos MUST use 3 DIFFERENT backend language families — never\n"
        "   two python or two java entries).\n"
        "P3 extract distinct business rules (no dedup, evidence-only).\n"
        "P2 validate legacy↔new parity (legacy wins, impl overrides SRS).\n"
        "P4 compute coverage_pct = fully_documented/total_discovered*100.\n"
    )


async def _legacy_file_index(project_id: str, limit: int = _KB_INDEX_MAX_FILES) -> List[str]:
    """Return up to `limit` legacy file paths already indexed by Stage 1.

    We use `kb_files` (the canonical KB collection per `db.py`) and emit
    a relative-path list so the Droid can `cat`/`grep` directly.
    """
    try:
        cursor = kb_files.find(
            {"project_id": project_id},
            {"_id": 0, "path": 1, "rel_path": 1},
        ).limit(limit)
        rows = await cursor.to_list(limit)
    except Exception as e:
        log.warning("kb_files query failed for %s: %s", project_id, e)
        return []
    out: List[str] = []
    for r in rows:
        p = (r.get("rel_path") or r.get("path") or "").strip()
        if p:
            out.append(p)
    return out


def _bootstrap_user_message(
    *,
    project_id: str,
    legacy_root: str,
    file_index: List[str],
) -> str:
    """Compose the one-shot bootstrap USER message sent to Factory."""
    idx_block = "\n".join(f"  - {p}" for p in file_index) if file_index else "  (empty — run KB scan first)"
    return (
        f"FACTORY AI FIRST-RUN ONBOARDING for project `{project_id}`.\n\n"
        f"LEGACY CODEBASE LOCATION: {legacy_root or '(use Droid cwd)'}\n"
        f"LEGACY FILE INDEX (first {len(file_index)}):\n{idx_block}\n\n"
        "TASKS (execute in order, follow the SYSTEM prompt's governance):\n"
        "  P0  Build your KB view of the legacy code from the index above.\n"
        "      Open files with `cat`/`grep`, cite as `path.ext:LINE`.\n"
        "  P1  Emit the top-3 target tech-stack combinations to\n"
        "      `docs/target-stack-recommendation.md`.\n"
        "      HARD CONSTRAINTS (iter-13.36):\n"
        "        (a) combo.db.engine == legacy.db.engine — no DB swap.\n"
        "        (b) Each of the 3 combinations MUST use a DIFFERENT\n"
        "            BACKEND LANGUAGE FAMILY. Acceptable families:\n"
        "            python, java, node, dotnet, php, go, ruby, rust.\n"
        "            Examples of VALID top-3:\n"
        "              1) FastAPI / Python 3.12 + PostgreSQL + React 19\n"
        "              2) Spring Boot 3.3 / Java 21 + PostgreSQL + React 19\n"
        "              3) NestJS / Node 22 LTS + PostgreSQL + React 19\n"
        "            INVALID (rejected):\n"
        "              1) FastAPI / Python\n"
        "              2) Django / Python   ← SAME family as #1, drop it.\n"
        "              3) Spring Boot / Java\n"
        "        (c) Each combination must vary across at least TWO of\n"
        "            {backend, frontend, architecture pattern} so the\n"
        "            user gets genuinely distinct options.\n"
        "  Acknowledge by listing the detected stack fingerprint\n"
        "  (langs, frameworks, db_engines) BEFORE you start P1.\n"
    )


async def run_factory_first_time_onboarding(
    project_id: str,
    *,
    force: bool = False,
    timeout: float = 180.0,
) -> Dict[str, Any]:
    """Idempotent first-run onboarding. Returns a status dict.

    Status keys:
      - `triggered`: bool — did we actually call Factory
      - `skipped`:   str  — reason if not triggered
      - `error`:     str  — populated only on failure
      - `onboarded_at`: ISO timestamp when set
    """
    # Local import to avoid an import cycle (factory_orchestrator imports
    # db; onboarding is imported by factory_orchestrator).
    from factory_orchestrator import (
        get_project_factory_orchestrator_config,
        route_via_factory_orchestrator,
        ensure_project_workspace,
    )

    try:
        cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    except Exception as e:
        return {"triggered": False, "skipped": "config_missing", "error": str(e)[:300]}

    if not cfg.get("enabled"):
        return {"triggered": False, "skipped": "factory_not_enabled"}
    if not cfg.get("app_key") or not cfg.get("computer_id"):
        return {"triggered": False, "skipped": "factory_incomplete_config"}

    proj = await projects.find_one(
        {"id": project_id}, {"_id": 0, "settings": 1}
    ) or {}
    settings_fo = ((proj.get("settings") or {}).get("factory_orchestrator") or {})
    if settings_fo.get("onboarded_at") and not force:
        return {
            "triggered": False,
            "skipped": "already_onboarded",
            "onboarded_at": settings_fo["onboarded_at"],
        }

    # iter-13.91.3 — proactively create the per-(tenant, project)
    # workspace directory on the Droid filesystem BEFORE sending the
    # bootstrap LLM message. Without this, the workspace is created
    # lazily (only when the first stage-agent call hits "Invalid cwd"),
    # so users who only enabled Factory but never ran an agent saw an
    # empty Droid home and assumed the integration was broken.
    workspace_result: Dict[str, Any] = {}
    try:
        workspace_result = await ensure_project_workspace(project_id)
        log.info("Factory workspace prep for %s: %s", project_id, workspace_result)
    except Exception as e:
        log.warning("Factory workspace prep failed for %s: %s", project_id, e)
        workspace_result = {"ok": False, "error": str(e)[:300]}

    system_prompt = _load_modernization_prompt()
    legacy_root = (cfg.get("cwd") or "").strip()
    file_index = await _legacy_file_index(project_id)
    user_msg = _bootstrap_user_message(
        project_id=project_id, legacy_root=legacy_root, file_index=file_index,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    try:
        # agent_key="onboarding.factory" so the dedicated session is kept
        # separate from regular stage-agent sessions.
        result: Optional[Dict[str, Any]] = await route_via_factory_orchestrator(
            messages=messages,
            project_id=project_id,
            agent_key="onboarding.factory",
            timeout=timeout,
        )
    except Exception as e:
        log.exception("Factory onboarding failed for %s", project_id)
        try:
            await audit_log.insert_one({
                "action": "factory_orchestrator.onboard.failed",
                "project_id": project_id,
                "error": str(e)[:500],
                "ts": _now_iso(),
            })
        except Exception:
            pass
        return {"triggered": True, "error": str(e)[:500]}

    onboarded_at = _now_iso()
    try:
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "settings.factory_orchestrator.onboarded_at": onboarded_at,
                "settings.factory_orchestrator.updated_at": onboarded_at,
            }},
        )
    except Exception:
        pass
    try:
        await audit_log.insert_one({
            "action": "factory_orchestrator.onboard.ok",
            "project_id": project_id,
            "preview": ((result or {}).get("content", "") or "")[:300],
            "workspace": workspace_result,
            "ts": onboarded_at,
        })
    except Exception:
        pass
    return {
        "triggered": True,
        "onboarded_at": onboarded_at,
        "preview": ((result or {}).get("content", "") or "")[:300],
        "workspace": workspace_result,
    }


def schedule_factory_first_time_onboarding(project_id: str) -> None:
    """Fire-and-forget wrapper used by the Console PUT handler.

    Runs in the FastAPI event loop without blocking the HTTP response —
    the user sees the Console save complete instantly while the Droid
    boots its session in the background.
    """
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(run_factory_first_time_onboarding(project_id))
    except RuntimeError:
        # No running loop (e.g. invoked from a sync context) — best effort.
        try:
            asyncio.run(run_factory_first_time_onboarding(project_id))
        except Exception as e:
            log.warning("onboarding schedule failed: %s", e)

