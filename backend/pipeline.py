"""Pipeline context loader.

Every stage route imports this to get upstream stage outputs.
Single source of truth for inter-stage data handoff.
"""
from typing import Optional, Dict, Any

from db import stage_context as stage_context_col


async def get_stage_context(project_id: str, stage: str) -> Optional[Dict[str, Any]]:
    doc = await stage_context_col.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0}
    )
    return doc


async def require_stage_context(project_id: str, stage: str, calling_stage: str) -> Dict[str, Any]:
    from fastapi import HTTPException
    doc = await get_stage_context(project_id, stage)
    if not doc:
        raise HTTPException(
            400,
            f"{stage} stage not frozen. Complete and freeze {stage} before starting {calling_stage}.",
        )
    return doc


async def get_module_context(project_id: str) -> str:
    """Return a compact module-structure context string for LLM prompts.

    Empty string when no MODULE entities exist (i.e. user never imported a
    module inventory). Works for any source language/framework."""
    from db import kb_entities
    mod_ents = await kb_entities.find(
        {"project_id": project_id, "type": "MODULE"}, {"_id": 0}
    ).to_list(100)
    if not mod_ents:
        return ""

    lines = ["APPLICATION MODULE STRUCTURE (use to group and organise output):"]
    for m in sorted(mod_ents, key=lambda x: x.get("table_ref_count", 0), reverse=True):
        lines.append(
            f"  [{m['name']}] {m.get('component_count', 0)} components, "
            f"{m.get('table_ref_count', 0)} table refs"
        )
        if m.get("description"):
            lines.append(f"    Description: {str(m['description'])[:150]}")
        if m.get("tables"):
            lines.append(f"    Key tables: {', '.join((m.get('tables') or [])[:8])}")
    return "\n".join(lines)



async def get_toon_summary(project_id: str) -> str:
    ctx = await get_stage_context(project_id, "Discovery")
    return (ctx or {}).get("toon_summary", "")


async def get_srs_section(project_id: str, section: str) -> str:
    ctx = await get_stage_context(project_id, "Discovery")
    return (ctx or {}).get("outputs", {}).get("srs_sections", {}).get(section, "")


async def get_domain_map(project_id: str) -> Dict:
    ctx = await get_stage_context(project_id, "Discovery")
    return (ctx or {}).get("outputs", {}).get("domain_map", {})


async def get_er_model(project_id: str) -> Dict:
    ctx = await get_stage_context(project_id, "Discovery")
    return (ctx or {}).get("outputs", {}).get("er_model", {})


async def save_stage_context(
    project_id: str,
    stage: str,
    outputs: Dict,
    sources: Dict,
    toon_summary: str = "",
    frozen_by: str = "system",
) -> None:
    from datetime import datetime, timezone
    from models import StageContext

    now = datetime.now(timezone.utc).isoformat()
    existing = await stage_context_col.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0}
    )
    version = ((existing or {}).get("version", 0)) + 1
    ctx = StageContext(
        project_id=project_id,
        stage=stage,
        frozen_at=now,
        frozen_by=frozen_by,
        version=version,
        outputs=outputs,
        toon_summary=toon_summary,
        sources=sources,
    )
    await stage_context_col.update_one(
        {"project_id": project_id, "stage": stage},
        {"$set": ctx.model_dump()},
        upsert=True,
    )
