"""iter-13.91.16 — Context bundle inspection routes.

Exposes the unified host-anchored stage-context bundler so operators
can preview EXACTLY what their LLM prompts will carry for any given
stage. Useful for debugging "why didn't the SRS pick up rule X?" type
questions without diving into Mongo / dev tools.
"""
from fastapi import APIRouter, HTTPException, Query

from context_bundler import (
    STAGE_KEYS,
    build_stage_context,
    render_for_prompt,
)

router = APIRouter(prefix="/context", tags=["context"])


@router.get("/{project_id}/{stage}")
async def get_stage_bundle(
    project_id: str,
    stage: str,
    fresh: bool = Query(default=False, description="Bypass the local-FS cache"),
):
    """Return the bundled context dict for (project, stage)."""
    if stage not in STAGE_KEYS:
        raise HTTPException(400, f"stage must be one of {STAGE_KEYS}")
    bundle = await build_stage_context(project_id, stage, use_cache=not fresh)
    if not bundle:
        raise HTTPException(404, "Project not found")
    return bundle


@router.get("/{project_id}/{stage}/preview")
async def get_stage_prompt_preview(
    project_id: str,
    stage: str,
    fresh: bool = Query(default=False),
):
    """Return the prompt-ready rendered string (what the LLM will see)."""
    if stage not in STAGE_KEYS:
        raise HTTPException(400, f"stage must be one of {STAGE_KEYS}")
    bundle = await build_stage_context(project_id, stage, use_cache=not fresh)
    if not bundle:
        raise HTTPException(404, "Project not found")
    return {
        "stage": stage,
        "project_id": project_id,
        "prompt_block": render_for_prompt(bundle),
        "char_count": (bundle.get("_meta") or {}).get("char_count", 0),
        "cache_hit": (bundle.get("_meta") or {}).get("cache_hit", False),
    }

