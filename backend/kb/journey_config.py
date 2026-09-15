"""Journey-KB toggle (iter-14.24 — Phase 1, refactored to Route/View anchors).

Journeys are **vertical migration-unit chunks** materialised from
``kb_graph`` + ``kb_entities``. Three kinds:

- ``api``    — anchored on a Route; carries controller → service →
               repository → tables → columns + roles + BR ids.
- ``ui``     — anchored on a view/form (JSP_FORM); carries fields +
               model bindings + pivot_route into the matching api_journey.
- ``column`` — anchored on a Column (the original Phase-1 shape);
               **opt-in only** — DataModel-focused lineage view.

Resolution order (mirrors ``kb/graph_config.py``):

1. Per-project override stored in ``kb_journey_config``:
   ``{project_id, enabled: bool, stages_enabled: [...], kinds_enabled: [...]}``.
   Set via ``POST /api/kb/{pid}/journeys/toggle``.
2. Env vars:
   - ``LAMA_USE_JOURNEY_KB`` — truthy: ``1/true/yes/on`` (default = OFF).
   - ``LAMA_JOURNEY_KB_STAGES`` — comma list of stages the journey slice
     is *allowed* to be injected into (default = ``codegen``).
     iter-14.25.8: SRS is REMOVED from the default set again.
     Rationale: the CGHS pilot repeatedly regressed on §5 Detailed
     Use Cases when journey slices were appended to the prompt —
     the model latched onto the top-N journeys in the block and
     wrote one long story per journey, silently dropping the
     remaining WF-*/UC-* IDs from the completeness roster. Journey
     KB stays on for CodeGen where the vertical slice is the unit
     of work; for SRS the deterministic UC roster + workflow
     inventory blocks are sufficient grounding.
     Operators who want journeys back on SRS for a specific project
     can set ``stages_enabled: ["srs", "codegen"]`` in the per-
     project override.
   - ``LAMA_JOURNEY_KINDS`` — comma list of anchor kinds to materialise.
     Default = ``api,ui``. ``column`` is opt-in only because column
     anchoring produces 5–20× more chunks than route anchoring for the
     same coverage, and it's only useful to the DataModel stage.

**Safety**: when disabled, the materialiser is skipped during Build KB
and every ``load_journeys()`` call returns ``[]``. No prompt / SRS /
CodeGen path is modified in Phase 1 — enabling the flag only *populates*
``kb_journeys``. Rollback = flip the toggle off (or unset the env).
"""
from __future__ import annotations

import os
from typing import Optional

from db import db as _mongo_db

kb_journey_config = _mongo_db.kb_journey_config

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

_DEFAULT_STAGES = ("codegen",)
_DEFAULT_KINDS = ("api", "ui")
_ALL_KINDS = ("api", "ui", "column")


def _env_default_enabled() -> bool:
    raw = (os.environ.get("LAMA_USE_JOURNEY_KB") or "").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    # iter-18.1 — default ON. Journeys are now the unit of work for
    # multi-agent CodeGen task division (`_journey_codegen_envelopes`),
    # not just a prompt-context slice, so an opt-in default meant the
    # richer graph-derived envelopes were never reachable out of the box.
    # Materialisation is deterministic (no LLM) and callers degrade to
    # the arch_services path when `kb_journeys` is empty, so defaulting
    # on cannot break a project that has not built a graph.
    return True


def _env_default_stages() -> list[str]:
    raw = (os.environ.get("LAMA_JOURNEY_KB_STAGES") or "").strip().lower()
    if not raw:
        return list(_DEFAULT_STAGES)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or list(_DEFAULT_STAGES)


def _env_default_kinds() -> list[str]:
    raw = (os.environ.get("LAMA_JOURNEY_KINDS") or "").strip().lower()
    if not raw:
        return list(_DEFAULT_KINDS)
    parts = [p.strip() for p in raw.split(",") if p.strip() in _ALL_KINDS]
    return parts or list(_DEFAULT_KINDS)


def _clean_kinds(raw_list: list) -> list[str]:
    """Normalise + filter to the valid set. Preserves user order."""
    out: list[str] = []
    for k in raw_list or []:
        k2 = str(k).strip().lower()
        if k2 in _ALL_KINDS and k2 not in out:
            out.append(k2)
    return out


async def is_journey_kb_enabled(project_id: Optional[str]) -> bool:
    """Return True iff journey materialisation / injection is enabled."""
    if project_id:
        try:
            doc = await kb_journey_config.find_one(
                {"project_id": project_id}, {"_id": 0, "enabled": 1}
            )
            if doc and isinstance(doc.get("enabled"), bool):
                return bool(doc["enabled"])
        except Exception:  # noqa: BLE001
            pass
    return _env_default_enabled()


async def journey_kb_stages(project_id: Optional[str]) -> list[str]:
    """Which stages are eligible for journey-slice prompt injection.

    Phase 1 doesn't consume the return value at prompt sites — it's here
    so operators can pre-configure before Phase 2 lands.
    """
    if project_id:
        try:
            doc = await kb_journey_config.find_one(
                {"project_id": project_id},
                {"_id": 0, "stages_enabled": 1},
            )
            if doc and isinstance(doc.get("stages_enabled"), list):
                out = [str(s).strip().lower() for s in doc["stages_enabled"] if s]
                if out:
                    return out
        except Exception:  # noqa: BLE001
            pass
    return _env_default_stages()


async def journey_kb_kinds(project_id: Optional[str]) -> list[str]:
    """Which anchor kinds (``api`` / ``ui`` / ``column``) to materialise."""
    if project_id:
        try:
            doc = await kb_journey_config.find_one(
                {"project_id": project_id},
                {"_id": 0, "kinds_enabled": 1},
            )
            if doc and isinstance(doc.get("kinds_enabled"), list):
                out = _clean_kinds(doc["kinds_enabled"])
                if out:
                    return out
        except Exception:  # noqa: BLE001
            pass
    return _env_default_kinds()


async def set_journey_kb_config(
    project_id: str,
    enabled: Optional[bool] = None,
    stages_enabled: Optional[list[str]] = None,
    kinds_enabled: Optional[list[str]] = None,
) -> dict:
    """Upsert the per-project journey-KB config. Returns the resulting doc."""
    from datetime import datetime, timezone

    update: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if enabled is not None:
        update["enabled"] = bool(enabled)
    if stages_enabled is not None:
        cleaned = [str(s).strip().lower() for s in stages_enabled if s]
        update["stages_enabled"] = cleaned or list(_DEFAULT_STAGES)
    if kinds_enabled is not None:
        cleaned_k = _clean_kinds(kinds_enabled)
        update["kinds_enabled"] = cleaned_k or list(_DEFAULT_KINDS)
    await kb_journey_config.update_one(
        {"project_id": project_id},
        {"$set": update, "$setOnInsert": {"project_id": project_id}},
        upsert=True,
    )
    doc = await kb_journey_config.find_one(
        {"project_id": project_id}, {"_id": 0}
    ) or {}
    return doc


def env_default_journey_kb() -> bool:
    """Synchronous accessor for the env-derived default (no project lookup)."""
    return _env_default_enabled()


def all_journey_kinds() -> tuple[str, ...]:
    return _ALL_KINDS


async def is_stage_enabled(project_id: Optional[str], stage: str) -> bool:
    """Phase-2 helper — is journey-slice injection enabled for ``stage``?

    Combines the master toggle with the stages allowlist. Callers at
    prompt-injection sites should gate on this before pulling journeys.
    """
    if not stage:
        return False
    if not await is_journey_kb_enabled(project_id):
        return False
    stages = await journey_kb_stages(project_id)
    return stage.strip().lower() in stages

