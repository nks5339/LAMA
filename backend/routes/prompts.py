"""Prompt library endpoints."""
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone

from db import prompts, project_prompts
from models import PromptUpdate

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("")
async def list_prompts():
    docs = await prompts.find({}, {"_id": 0}).sort("key", 1).to_list(500)
    return docs


@router.put("/{key}")
async def update_prompt(key: str, payload: PromptUpdate):
    now = datetime.now(timezone.utc).isoformat()
    existing = await prompts.find_one({"key": key}, {"_id": 0})
    if not existing:
        raise HTTPException(404, "Prompt not found")
    new_version = existing.get("version", 1) + 1
    update = {
        "template": payload.template,
        "version": new_version,
        "updated_at": now,
    }
    if payload.description is not None:
        update["description"] = payload.description
    await prompts.update_one({"key": key}, {"$set": update})
    doc = await prompts.find_one({"key": key}, {"_id": 0})
    return doc


@router.get("/project/{project_id}")
async def list_project_prompts(project_id: str):
    docs = await project_prompts.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    return docs


@router.put("/project/{project_id}/{key}")
async def update_project_prompt(project_id: str, key: str, payload: PromptUpdate):
    now = datetime.now(timezone.utc).isoformat()
    existing = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    version = (existing.get("version", 0) + 1) if existing else 1
    doc = {
        "project_id": project_id,
        "key": key,
        "template": payload.template,
        "description": payload.description or (existing.get("description", "") if existing else ""),
        "version": version,
        "updated_at": now,
    }
    await project_prompts.update_one(
        {"project_id": project_id, "key": key},
        {"$set": doc},
        upsert=True,
    )
    return doc


@router.delete("/project/{project_id}/{key}")
async def delete_project_prompt(project_id: str, key: str):
    await project_prompts.delete_one({"project_id": project_id, "key": key})
    return {"ok": True}
