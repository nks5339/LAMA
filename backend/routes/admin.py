"""Super-admin routes — tenant CRUD, user CRUD, dashboard analytics.

iter-13.68 — Multi-tenant.
Every endpoint requires `role=super_admin` via `require_super_admin`.
The dashboard aggregates project counts + token usage + cost from
`token_usage_log` joined to `projects.tenant_id`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from auth import hash_password, require_super_admin
from db import (
    audit_log,
    projects as projects_col,
    tenants as tenants_col,
    token_usage_log,
    users as users_col,
)
from models import (
    Tenant,
    TenantCreate,
    TenantUpdate,
    User,
    UserCreate,
    UserPublic,
    UserUpdate,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_VALID_ROLES = {"super_admin", "tenant_admin", "tenant_user"}


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "tenant"


def _to_public(u: User) -> UserPublic:
    return UserPublic(**u.model_dump(exclude={"password_hash"}))


# --------------------------------------------------------------------
# Tenants
# --------------------------------------------------------------------
@router.get("/tenants")
async def list_tenants(_: dict = Depends(require_super_admin)):
    docs = await tenants_col.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    out: List[dict] = []
    for d in docs:
        proj_count = await projects_col.count_documents({"tenant_id": d["id"]})
        user_count = await users_col.count_documents({"tenant_id": d["id"]})
        out.append({**d, "project_count": proj_count, "user_count": user_count})
    return {"tenants": out, "count": len(out)}


@router.post("/tenants", response_model=Tenant)
async def create_tenant(payload: TenantCreate, _: dict = Depends(require_super_admin)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Tenant name is required")
    slug = _slugify(payload.slug or name)
    existing = await tenants_col.find_one({"slug": slug}, {"_id": 0})
    if existing:
        raise HTTPException(409, f"Tenant with slug '{slug}' already exists")
    tenant = Tenant(name=name, slug=slug, description=payload.description or "")
    await tenants_col.insert_one(tenant.model_dump())
    await audit_log.insert_one({
        "action": "admin.tenant.create",
        "tenant_id": tenant.id,
        "at": tenant.created_at,
        "details": {"name": name, "slug": slug},
    })
    return tenant


@router.patch("/tenants/{tenant_id}", response_model=Tenant)
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: dict = Depends(require_super_admin),
):
    raw = await tenants_col.find_one({"id": tenant_id}, {"_id": 0})
    if not raw:
        raise HTTPException(404, "Tenant not found")
    patch: dict = {}
    if payload.name is not None:
        patch["name"] = payload.name.strip()
    if payload.description is not None:
        patch["description"] = payload.description
    if payload.is_active is not None:
        patch["is_active"] = bool(payload.is_active)
    if not patch:
        return Tenant(**raw)
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    await tenants_col.update_one({"id": tenant_id}, {"$set": patch})
    await audit_log.insert_one({
        "action": "admin.tenant.update",
        "tenant_id": tenant_id,
        "at": patch["updated_at"],
        "details": patch,
    })
    fresh = await tenants_col.find_one({"id": tenant_id}, {"_id": 0})
    return Tenant(**fresh)


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str, _: dict = Depends(require_super_admin)):
    if tenant_id == "tenant_default":
        raise HTTPException(400, "Cannot delete the default tenant")
    proj = await projects_col.count_documents({"tenant_id": tenant_id})
    if proj > 0:
        raise HTTPException(
            409,
            f"Tenant has {proj} project(s). Re-assign or delete them first.",
        )
    user_n = await users_col.count_documents({"tenant_id": tenant_id})
    if user_n > 0:
        raise HTTPException(
            409,
            f"Tenant has {user_n} user(s). Delete them first.",
        )
    r = await tenants_col.delete_one({"id": tenant_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "Tenant not found")
    await audit_log.insert_one({
        "action": "admin.tenant.delete",
        "tenant_id": tenant_id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


# --------------------------------------------------------------------
# Users
# --------------------------------------------------------------------
@router.get("/users")
async def list_users(
    tenant_id: Optional[str] = None,
    _: dict = Depends(require_super_admin),
):
    q: dict = {}
    if tenant_id:
        q["tenant_id"] = tenant_id
    docs = await users_col.find(q, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return {
        "users": [_to_public(User(**d)).model_dump() for d in docs],
        "count": len(docs),
    }


@router.post("/users", response_model=UserPublic)
async def create_user(payload: UserCreate, _: dict = Depends(require_super_admin)):
    username = (payload.username or "").strip()
    if not username:
        raise HTTPException(400, "username is required")
    if len(payload.password or "") < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if payload.role not in _VALID_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_VALID_ROLES)}")
    existing = await users_col.find_one({"username": username}, {"_id": 0})
    if existing:
        raise HTTPException(409, "Username already exists")

    if payload.role == "super_admin":
        tenant_id = "*"
    else:
        tenant_id = (payload.tenant_id or "").strip()
        if not tenant_id:
            raise HTTPException(400, "tenant_id is required for non-super-admin roles")
        t = await tenants_col.find_one({"id": tenant_id}, {"_id": 0})
        if not t:
            raise HTTPException(404, f"Tenant '{tenant_id}' not found")

    user = User(
        username=username,
        full_name=payload.full_name or "",
        email=payload.email or "",
        role=payload.role,
        tenant_id=tenant_id,
        password_hash=hash_password(payload.password),
    )
    await users_col.insert_one(user.model_dump())
    await audit_log.insert_one({
        "action": "admin.user.create",
        "user_id": user.id,
        "tenant_id": tenant_id,
        "at": user.created_at,
        "details": {"username": username, "role": payload.role},
    })
    return _to_public(user)


@router.patch("/users/{user_id}", response_model=UserPublic)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    _: dict = Depends(require_super_admin),
):
    raw = await users_col.find_one({"id": user_id}, {"_id": 0})
    if not raw:
        raise HTTPException(404, "User not found")
    patch: dict = {}
    if payload.full_name is not None:
        patch["full_name"] = payload.full_name
    if payload.email is not None:
        patch["email"] = payload.email
    if payload.role is not None:
        if payload.role not in _VALID_ROLES:
            raise HTTPException(400, f"role must be one of {sorted(_VALID_ROLES)}")
        patch["role"] = payload.role
        if payload.role == "super_admin":
            patch["tenant_id"] = "*"
    if payload.is_active is not None:
        patch["is_active"] = bool(payload.is_active)
    if payload.password:
        if len(payload.password) < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        patch["password_hash"] = hash_password(payload.password)
    if not patch:
        return _to_public(User(**raw))
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    await users_col.update_one({"id": user_id}, {"$set": patch})
    await audit_log.insert_one({
        "action": "admin.user.update",
        "user_id": user_id,
        "at": patch["updated_at"],
        "details": {k: v for k, v in patch.items() if k != "password_hash"},
    })
    fresh = await users_col.find_one({"id": user_id}, {"_id": 0})
    return _to_public(User(**fresh))


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, ctx: dict = Depends(require_super_admin)):
    if ctx["user"].id == user_id:
        raise HTTPException(400, "Cannot delete the currently logged-in account")
    r = await users_col.delete_one({"id": user_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "User not found")
    await audit_log.insert_one({
        "action": "admin.user.delete",
        "user_id": user_id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


# --------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------
@router.get("/dashboard")
async def dashboard(_: dict = Depends(require_super_admin)):
    """Per-tenant aggregate: projects, users, total tokens, total cost ($).

    Token usage is read from `token_usage_log`; we look up the project's
    `tenant_id` for each row. For tenants with zero LLM calls the totals
    are 0 — they still appear so the chart shows them.
    """
    tenant_docs = await tenants_col.find({}, {"_id": 0}).sort("name", 1).to_list(500)

    # Build {project_id -> tenant_id} once.
    project_to_tenant: dict[str, str] = {}
    async for p in projects_col.find({}, {"_id": 0, "id": 1, "tenant_id": 1}):
        project_to_tenant[p["id"]] = p.get("tenant_id") or "tenant_default"

    # Aggregate token usage by project_id, then fold into tenant_id.
    pipeline = [
        {
            "$group": {
                "_id": "$project_id",
                "total_tokens": {"$sum": "$total_tokens"},
                "total_cost":   {"$sum": "$cost_usd"},
                "calls":        {"$sum": 1},
            }
        }
    ]
    per_tenant: dict[str, dict] = {}
    async for row in token_usage_log.aggregate(pipeline):
        pid = row.get("_id") or ""
        tid = project_to_tenant.get(pid, "tenant_default")
        bucket = per_tenant.setdefault(tid, {"tokens": 0, "cost": 0.0, "calls": 0})
        bucket["tokens"] += int(row.get("total_tokens") or 0)
        bucket["cost"]   += float(row.get("total_cost") or 0.0)
        bucket["calls"]  += int(row.get("calls") or 0)

    rows: List[dict] = []
    grand_projects = grand_users = grand_tokens = grand_calls = 0
    grand_cost = 0.0
    for t in tenant_docs:
        tid = t["id"]
        proj_n = await projects_col.count_documents({"tenant_id": tid})
        user_n = await users_col.count_documents({"tenant_id": tid})
        agg = per_tenant.get(tid, {"tokens": 0, "cost": 0.0, "calls": 0})
        rows.append({
            "tenant_id": tid,
            "name": t.get("name", ""),
            "slug": t.get("slug", ""),
            "is_active": bool(t.get("is_active", True)),
            "project_count": proj_n,
            "user_count": user_n,
            "total_tokens": int(agg["tokens"]),
            "total_cost_usd": round(float(agg["cost"]), 6),
            "llm_calls": int(agg["calls"]),
        })
        grand_projects += proj_n
        grand_users += user_n
        grand_tokens += int(agg["tokens"])
        grand_cost   += float(agg["cost"])
        grand_calls  += int(agg["calls"])

    return {
        "tenants": rows,
        "totals": {
            "tenant_count":   len(rows),
            "project_count":  grand_projects,
            "user_count":     grand_users,
            "total_tokens":   grand_tokens,
            "total_cost_usd": round(grand_cost, 6),
            "llm_calls":      grand_calls,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

