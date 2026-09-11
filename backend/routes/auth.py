"""Auth routes — login / logout / me. iter-13.68."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import (
    get_current_user,
    hash_password,
    issue_token,
    verify_password,
)
from db import audit_log, tenants as tenants_col, users as users_col
from models import (
    LoginRequest,
    LoginResponse,
    Tenant,
    User,
    UserPublic,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_public(u: User) -> UserPublic:
    return UserPublic(**u.model_dump(exclude={"password_hash"}))


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    raw = await users_col.find_one({"username": payload.username.strip()}, {"_id": 0})
    if not raw:
        raise HTTPException(401, "Invalid credentials")
    user = User(**raw)
    if not user.is_active:
        raise HTTPException(403, "Account is disabled")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")

    tenant: Tenant | None = None
    if user.tenant_id and user.tenant_id != "*":
        t_raw = await tenants_col.find_one({"id": user.tenant_id}, {"_id": 0})
        if t_raw:
            tenant = Tenant(**t_raw)
            if not tenant.is_active:
                raise HTTPException(403, "Tenant is disabled — contact your admin")

    token, expires_at = issue_token(user)
    now = datetime.now(timezone.utc).isoformat()
    await users_col.update_one(
        {"id": user.id},
        {"$set": {"last_login_at": now, "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "auth.login",
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "at": now,
        "details": {"username": user.username, "role": user.role},
    })
    return LoginResponse(
        token=token,
        token_type="bearer",
        expires_at=expires_at,
        user=_to_public(user),
        tenant=tenant,
    )


@router.get("/me")
async def me(ctx: dict = Depends(get_current_user)):
    return {
        "user": _to_public(ctx["user"]).model_dump(),
        "tenant": ctx["tenant"].model_dump() if ctx["tenant"] else None,
    }


@router.post("/change-password")
async def change_password(
    payload: dict,
    ctx: dict = Depends(get_current_user),
):
    """Self-service password change. Body: {old_password, new_password}."""
    old = (payload.get("old_password") or "").strip()
    new = (payload.get("new_password") or "").strip()
    if not old or not new:
        raise HTTPException(400, "old_password and new_password are required")
    if len(new) < 8:
        raise HTTPException(400, "new_password must be at least 8 characters")
    user: User = ctx["user"]
    if not verify_password(old, user.password_hash):
        raise HTTPException(401, "Old password is incorrect")
    now = datetime.now(timezone.utc).isoformat()
    await users_col.update_one(
        {"id": user.id},
        {"$set": {"password_hash": hash_password(new), "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "auth.password_change",
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "at": now,
    })
    return {"ok": True}


@router.post("/logout")
async def logout(ctx: dict = Depends(get_current_user)):
    """Stateless JWT — the backend just records the event. The frontend
    is expected to drop the token from localStorage."""
    await audit_log.insert_one({
        "action": "auth.logout",
        "user_id": ctx["user"].id,
        "tenant_id": ctx["user"].tenant_id,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}

