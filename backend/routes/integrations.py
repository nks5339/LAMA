"""LAMA — /api/integrations router (iter-13.60).

Exposes the govt-services catalog, per-project enable/disable + config
overrides, and a one-click injection that drops the integration's
client + router stub into the project's codegen_files.

The injected code is **mock-by-default** — the operator flips
`<GATE>_MODE=live` and supplies the provider env vars (no code changes
needed).
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import projects
from integrations.catalog import catalog_for_ui, get_entry
from integrations.renderer import (
    inject_project,
    list_selections,
    resolve_backend_lang,
    set_selection,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ─── Schemas ───────────────────────────────────────────────────────────

class SelectionRequest(BaseModel):
    enabled: bool = Field(..., description="True to enable, False to disable.")
    config_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-env-var default overrides (e.g. point PAN at NSDL).",
    )


class InjectRequest(BaseModel):
    language: Optional[str] = Field(
        None,
        description="Override the target language (python | java | nodejs). "
                    "Defaults to project.target_tech.",
    )


# ─── Catalog (project-agnostic) ────────────────────────────────────────

@router.get("/catalog")
async def get_catalog() -> Dict[str, Any]:
    """Return the full govt-services catalog (without code templates)."""
    items = catalog_for_ui()
    return {"items": items, "count": len(items)}


@router.get("/catalog/{integration_id}")
async def get_catalog_entry(integration_id: str) -> Dict[str, Any]:
    try:
        entry = get_entry(integration_id)
    except KeyError:
        raise HTTPException(404, f"Unknown integration: {integration_id}")
    return {k: v for k, v in entry.items() if k != "file_templates"}


# ─── Per-project selection + injection ─────────────────────────────────

@router.get("/{project_id}/selections")
async def get_selections(project_id: str) -> Dict[str, Any]:
    """Return one row per catalog entry, joined with this project's
    enabled/disabled state, overrides and last-injected metadata."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    rows = await list_selections(project_id)
    return {
        "project_id": project_id,
        "target_tech": proj.get("target_tech", ""),
        "resolved_language": resolve_backend_lang(proj.get("target_tech", "")),
        "items": rows,
        "enabled_count": sum(1 for r in rows if r.get("enabled")),
    }


@router.put("/{project_id}/selections/{integration_id}")
async def upsert_selection(
    project_id: str, integration_id: str, payload: SelectionRequest,
) -> Dict[str, Any]:
    """Enable/disable an integration and (optionally) set per-env-var
    override defaults. Does NOT inject — call /inject for that."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    try:
        doc = await set_selection(
            project_id, integration_id,
            enabled=payload.enabled,
            config_overrides=payload.config_overrides or {},
        )
    except KeyError:
        raise HTTPException(404, f"Unknown integration: {integration_id}")
    return doc


@router.post("/{project_id}/inject")
async def inject(project_id: str, payload: Optional[InjectRequest] = None) -> Dict[str, Any]:
    """Render every enabled integration into codegen_files. Idempotent —
    safe to call repeatedly; bumps file versions on every call."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    try:
        return await inject_project(
            project_id,
            language_override=(payload.language if payload else "") or "",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

