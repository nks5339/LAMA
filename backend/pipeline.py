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
