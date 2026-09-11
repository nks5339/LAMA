"""Auth utility — bcrypt password hashing, JWT issue/decode, FastAPI deps.

iter-13.68 — Multi-tenant.
- `hash_password` / `verify_password` — bcrypt via passlib.
- `issue_token` / `decode_token` — HS256 JWT, secret from `LAMA_JWT_SECRET`
  (defaults to a development secret + WARN). Token TTL = 24 hours.
- `get_current_user` — FastAPI dependency that returns the User pydantic
  model + active Tenant (None for super_admin). Returns 401 on missing /
  invalid / expired token, 403 on disabled user/tenant.
- `require_super_admin` — narrower dep that 403s anyone but super_admin.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from fastapi import Depends, Header, HTTPException
from passlib.context import CryptContext

from db import users as users_col, tenants as tenants_col
from models import Tenant, User

log = logging.getLogger("lama.auth")

# --- Password hashing ---------------------------------------------------
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


# --- JWT -----------------------------------------------------------------
_DEV_SECRET = "lama-dev-secret-change-me-in-production"
_ALGO = "HS256"
_TTL_HOURS = int(os.environ.get("LAMA_JWT_TTL_HOURS", "24"))
# iter-14.17.1 — Warn ONCE per process instead of on every _secret()
# call. This warning fires from `verify_token` on every authenticated
# request, which drowned the Live Telemetry log pane in duplicates.
_DEV_SECRET_WARNED = False


def _secret() -> str:
    global _DEV_SECRET_WARNED
    s = os.environ.get("LAMA_JWT_SECRET", "").strip()
    if not s:
        if not _DEV_SECRET_WARNED:
            log.warning(
                "LAMA_JWT_SECRET is not set — using dev fallback. Set a strong "
                "secret in production (compose `environment:` or -e flag). "
                "This warning will not repeat."
            )
            _DEV_SECRET_WARNED = True
        return _DEV_SECRET
    return s


def issue_token(user: User) -> Tuple[str, str]:
    """Return (jwt_token, expires_at_iso)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=_TTL_HOURS)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=_ALGO)
    return token, exp.isoformat()


def decode_token(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=[_ALGO])


# --- FastAPI dependencies ----------------------------------------------
def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Invalid Authorization header (expected Bearer <token>)")
    return parts[1].strip()


async def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """Resolve the bearer token to a {user, tenant} dict. 401 on any failure.

    Returns a plain dict (rather than a Pydantic model) so callers can
    grab `user.tenant_id`, `user.role`, etc. without forcing a serialise.
    """
    token = _extract_bearer(authorization)
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — log in again")
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "Token missing subject")

    raw = await users_col.find_one({"id": user_id}, {"_id": 0})
    if not raw:
        raise HTTPException(401, "User no longer exists")
    user = User(**raw)
    if not user.is_active:
        raise HTTPException(403, "User account is disabled")

    tenant: Optional[Tenant] = None
    if user.tenant_id and user.tenant_id != "*":
        t_raw = await tenants_col.find_one({"id": user.tenant_id}, {"_id": 0})
        if not t_raw:
            raise HTTPException(403, "User's tenant no longer exists")
        tenant = Tenant(**t_raw)
        if not tenant.is_active:
            raise HTTPException(403, "Tenant is disabled — contact your admin")

    return {"user": user, "tenant": tenant, "token_payload": payload}


async def require_super_admin(ctx: dict = Depends(get_current_user)) -> dict:
    if ctx["user"].role != "super_admin":
        raise HTTPException(403, "Super-admin only")
    return ctx


def is_super_admin(ctx: dict) -> bool:
    return bool(ctx and ctx.get("user") and ctx["user"].role == "super_admin")


def tenant_filter(ctx: dict) -> dict:
    """Mongo filter that scopes a query to the caller's tenant.
    Super-admin gets an empty filter (sees everything)."""
    if is_super_admin(ctx):
        return {}
    return {"tenant_id": ctx["user"].tenant_id}

