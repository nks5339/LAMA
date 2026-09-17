"""Project CRUD endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone

from auth import get_current_user, is_super_admin, tenant_filter
from db import projects, audit_log, stage_context as stage_context_col
from models import Project, ProjectCreate
from kb.target_stack import validate_target_stack
from kb.graph_config import env_default_graph_kb

router = APIRouter(prefix="/projects", tags=["projects"])


def _resolve_create_tenant(payload: ProjectCreate, ctx: dict) -> str:
    """Pick the tenant_id for a new project.
    - tenant_user / tenant_admin: forced to caller's tenant_id (any value
      they sent is ignored).
    - super_admin: must supply payload.tenant_id (we 400 if missing).
    """
    user = ctx["user"]
    if user.role == "super_admin":
        tid = (payload.tenant_id or "").strip()
        if not tid:
            raise HTTPException(
                400,
                "Super-admin must specify tenant_id when creating a project.",
            )
        return tid
    return user.tenant_id


@router.post("", response_model=Project)
async def create_project(
    payload: ProjectCreate,
    ctx: dict = Depends(get_current_user),
):
    # iter-13.59 — target_tech is now optional at create time. The user
    # picks it later from the Discovery → TargetStackSuggester (top-3
    # recommendations + "Others" custom builder). When the caller still
    # passes a value (e.g. legacy seed / API client), we run it through
    # `validate_target_stack` so disallowed hybrids (e.g. "PHP FastAPI")
    # are auto-corrected rather than blocked. Empty string is fine and
    # bypasses validation.
    raw_target = (payload.target_tech or "").strip()
    if raw_target:
        verdict = validate_target_stack(raw_target, detected_language="")
        safe_target = raw_target if verdict["valid"] else verdict["suggested"]
    else:
        verdict = {"valid": True, "suggested": "", "issues": [], "rationale": "deferred to post-KB selection"}
        safe_target = ""

    proj_dict = payload.model_dump()
    proj_dict["target_tech"] = safe_target
    proj_dict["tenant_id"] = _resolve_create_tenant(payload, ctx)
    proj = Project(**proj_dict)

    # iter-14.93 — Override stage_status + starting stage based on project_type.
    # Tool project types have their own 3-stage pipelines (mirroring the tool's
    # tab layout). Legacy migration keeps the classical 5-stage default.
    ptype = (proj_dict.get("project_type") or "legacy_migration").strip()
    if ptype == "gap_analysis":
        proj.project_type = "gap_analysis"
        proj.stage = "Input"
        proj.stage_status = {
            "Input": "active",
            "KnowledgeBase": "locked",
            "Report": "locked",
        }
    elif ptype == "tech_transformer":
        proj.project_type = "tech_transformer"
        proj.stage = "Input"
        proj.stage_status = {
            "Input": "active",
            "KnowledgeBase": "locked",
            "Output": "locked",
        }
    elif ptype == "direct_transform":
        # iter-22 — Direct Transform. No KnowledgeBase stage: it is folder-path
        # driven and builds no KB, so offering one would be a stage that can
        # never become active.
        proj.project_type = "direct_transform"
        proj.stage = "Input"
        proj.stage_status = {
            "Input": "active",
            "Transform": "locked",
            "Output": "locked",
        }
    else:
        proj.project_type = "legacy_migration"

    doc = proj.model_dump()
    doc["target_stack_validation"] = {
        "submitted":  raw_target,
        "accepted":   safe_target,
        "valid":      verdict["valid"],
        "issues":     verdict["issues"],
        "rationale":  verdict["rationale"],
    }
    await projects.insert_one(doc)
    await audit_log.insert_one({
        "action": "project.create",
        "project_id": proj.id,
        "tenant_id": proj.tenant_id,
        "user_id": ctx["user"].id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {
            "name": proj.name,
            "target_stack_valid": verdict["valid"],
            "target_stack_substituted": (not verdict["valid"]),
            "target_stack_deferred": (not raw_target),
        },
    })
    return proj


@router.get("", response_model=List[Project])
async def list_projects(ctx: dict = Depends(get_current_user)):
    q = tenant_filter(ctx)
    docs = await projects.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [Project(**d) for d in docs]


async def _get_owned_project(project_id: str, ctx: dict) -> dict:
    doc = await projects.find_one({"id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Project not found")
    if not is_super_admin(ctx) and doc.get("tenant_id") != ctx["user"].tenant_id:
        # Treat as 404 to avoid leaking the existence of cross-tenant projects.
        raise HTTPException(404, "Project not found")
    return doc


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, ctx: dict = Depends(get_current_user)):
    doc = await _get_owned_project(project_id, ctx)
    return Project(**doc)


@router.get("/{project_id}/pipeline")
async def get_pipeline_status(project_id: str, ctx: dict = Depends(get_current_user)):
    """Per-stage pipeline status — used by the sidebar to render lock / available / frozen badges."""
    await _get_owned_project(project_id, ctx)
    stages = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"]
    result: dict = {}
    for stage in stages:
        doc = await stage_context_col.find_one(
            {"project_id": project_id, "stage": stage},
            {"_id": 0, "outputs.srs_sections": 0, "toon_summary": 0},
        )
        if doc:
            result[stage] = {
                "frozen": True,
                "frozen_at": doc.get("frozen_at"),
                "version": doc.get("version", 1),
                "sources": doc.get("sources", {}),
                "output_keys": list(doc.get("outputs", {}).keys()),
            }
        else:
            result[stage] = {"frozen": False}
    return result


# ---------- Settings (iter-13.31) ----------
class ProjectSettingsPatch(BaseModel):
    # None == clear override (fall back to env default).
    use_graph_kb: Optional[bool] = None


@router.get("/{project_id}/settings")
async def get_project_settings(project_id: str, ctx: dict = Depends(get_current_user)):
    """Return per-project tunable settings (graph-KB toggle, etc.)."""
    await _get_owned_project(project_id, ctx)
    doc = await projects.find_one(
        {"id": project_id}, {"_id": 0, "id": 1, "settings": 1}
    )
    if doc is None:
        raise HTTPException(404, "Project not found")
    settings = doc.get("settings") or {}
    override = settings.get("use_graph_kb")
    env_default = env_default_graph_kb()
    return {
        "use_graph_kb": override if isinstance(override, bool) else None,
        "use_graph_kb_effective": override if isinstance(override, bool) else env_default,
        "use_graph_kb_env_default": env_default,
    }


@router.patch("/{project_id}/settings")
async def patch_project_settings(
    project_id: str,
    payload: ProjectSettingsPatch,
    ctx: dict = Depends(get_current_user),
):
    """Update per-project settings. Pass ``use_graph_kb=null`` to clear the
    override and revert to the env-var default."""
    await _get_owned_project(project_id, ctx)
    update: dict = {}
    if payload.use_graph_kb is None:
        update["$unset"] = {"settings.use_graph_kb": ""}
    else:
        update["$set"] = {"settings.use_graph_kb": bool(payload.use_graph_kb)}
    update.setdefault("$set", {})["updated_at"] = datetime.now(timezone.utc).isoformat()
    await projects.update_one({"id": project_id}, update)
    await audit_log.insert_one({
        "action": "project.settings.update",
        "project_id": project_id,
        "user_id": ctx["user"].id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"use_graph_kb": payload.use_graph_kb},
    })
    return await get_project_settings(project_id, ctx)


# ---------- Delete project (iter-13.88) ----------
@router.delete("/{project_id}")
async def delete_project(project_id: str, ctx: dict = Depends(get_current_user)):
    """Permanently delete a project and EVERY reference to it.

    Wipes (in order):
      • KB collections — kb_files / kb_chunks / kb_entities / kb_toon /
        kb_graph / business_ontologies / ontology_snapshots /
        legacy_analysis / kb_git_sources / kb_module_selection /
        br_table_links + Qdrant vectors (via the existing KB purge).
      • Downstream artefacts — srs_documents / stage_context /
        conversations / messages / data_models / bus_matrix /
        olap_models / migration_artifacts / arch_documents /
        arch_services / codegen_files / codegen_runs /
        living_artifacts / living_runs / stage_confidence /
        freeze_gates / living_reports.
      • Project-scoped configs — github_configs, project_prompts,
        project_integrations, data_sources, agent_configs (project_id
        pin), token_usage_log.
      • Cloned legacy files on disk under ``/tmp/lama-git/{project_id}``.
      • The project document itself.
    Audit-logged before the project row is removed.

    Tenant-scoped: `tenant_user`/`tenant_admin` can only delete projects
    in their own tenant; `super_admin` may delete any project.
    """
    proj = await _get_owned_project(project_id, ctx)

    # Reuse the canonical KB+downstream wipe so we share one implementation
    # with the clone-git/scan-folder REPLACE paths and the existing
    # `DELETE /api/kb/{pid}/all` endpoint.
    from routes.kb import _purge_project_kb
    kb_stats = await _purge_project_kb(
        project_id, clear_legacy_path=True, clear_downstream=True,
    )

    # Project-scoped configs not covered by `_purge_project_kb` because
    # they're not KB / pipeline artefacts. Run in parallel (iter-13.120).
    from db import (
        github_configs as _gh_col,
        project_prompts as _pp_col,
        project_integrations as _pi_col,
        data_sources as _ds_col,
        token_usage_log as _tul_col,
    )
    import asyncio as _asyncio
    _extras_defs = [
        ("github_configs",       _gh_col,  {"project_id": project_id}),
        ("project_prompts",      _pp_col,  {"project_id": project_id}),
        ("project_integrations", _pi_col,  {"project_id": project_id}),
        ("data_sources",         _ds_col,  {"project_id": project_id}),
        ("token_usage_log",      _tul_col, {"project_id": project_id}),
    ]

    async def _del_extra(name, col, filt):
        try:
            r = await col.delete_many(filt)
            return name, int(getattr(r, "deleted_count", 0) or 0)
        except Exception as e:  # noqa: BLE001
            return name, f"err: {type(e).__name__}"

    _extras_results = await _asyncio.gather(*[
        _del_extra(n, c, f) for n, c, f in _extras_defs
    ])
    extras: dict = {name: val for name, val in _extras_results}

    # Audit BEFORE deleting the project doc — keeps the trail attributable.
    await audit_log.insert_one({
        "action": "project.delete",
        "project_id": project_id,
        "tenant_id": proj.get("tenant_id", ""),
        "user_id": ctx["user"].id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {
            "name": proj.get("name"),
            "kb_purge": kb_stats,
            "config_purge": extras,
        },
    })

    # Clean up the on-disk clone working directory (best-effort).
    try:
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile
        clone_dir = _os.path.join(_tempfile.gettempdir(), "lama-git", project_id)
        if _os.path.isdir(clone_dir):
            _shutil.rmtree(clone_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    # Finally remove the project document itself.
    await projects.delete_one({"id": project_id})

    return {
        "ok": True,
        "deleted_project_id": project_id,
        "kb_purge": kb_stats,
        "config_purge": extras,
    }

