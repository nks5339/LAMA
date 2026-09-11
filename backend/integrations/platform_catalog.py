"""LAMA — platform integrations (iter-13.120).

Adds the following non-govt integrations requested by users:
  • keycloak_auth         — OIDC / SAML SSO via Keycloak
  • azure_blob_storage    — Azure Blob upload / download
  • s3_object_storage     — AWS S3 (or S3-compatible) object storage
  • redis_cache           — Redis cache client + FastAPI dependency

Each entry ships a mock-by-default Python template that follows the same
`<GATE>_MODE=mock|live` contract as the govt-services catalog. Flip the
mode env-var and provide the real credentials to go live — no code
changes required.

The templates are intentionally small — they wire the SDK, expose one
sensible router / dependency, and stay usable as a starting point. Users
are expected to extend them per their app's needs.

Java + Node stubs use `_stub_template` (same as the govt entries) — a
placeholder file that tells the user where to port the Python reference
implementation. Contributions welcome.
"""
from __future__ import annotations

from typing import Any, Dict, List

# We reuse the stub helper from catalog.py to keep the "port from Python"
# hint consistent across every family.
from integrations.catalog import _stub_template  # noqa: E402


# ---------------------------------------------------------------------------
# Keycloak (OIDC verifier + login redirect helper)
# ---------------------------------------------------------------------------
KEYCLOAK_PYTHON = '''"""LAMA-injected Keycloak OIDC integration (mock-by-default).

- Set `KEYCLOAK_MODE=live` and populate the env vars below to go live.
- Exposes:
    GET  /integrations/keycloak/login       → 302 to Keycloak auth endpoint
    GET  /integrations/keycloak/callback    → exchanges ?code for tokens
    GET  /integrations/keycloak/userinfo    → verifies Bearer + returns claims
"""
import os
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger("integrations.keycloak")
router = APIRouter(prefix="/integrations/keycloak", tags=["keycloak"])

_MODE          = os.getenv("KEYCLOAK_MODE", "mock").lower()
_BASE_URL      = os.getenv("KEYCLOAK_BASE_URL", "").rstrip("/")           # e.g. https://kc.example.com
_REALM         = os.getenv("KEYCLOAK_REALM", "")
_CLIENT_ID     = os.getenv("KEYCLOAK_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
_REDIRECT_URI  = os.getenv("KEYCLOAK_REDIRECT_URI", "http://localhost:8000/integrations/keycloak/callback")

def _realm_url() -> str:
    if not (_BASE_URL and _REALM):
        raise HTTPException(500, "KEYCLOAK_BASE_URL / KEYCLOAK_REALM not configured")
    return f"{_BASE_URL}/realms/{_REALM}"

@router.get("/login")
async def login():
    if _MODE != "live":
        return {"mode": "mock", "hint": "Set KEYCLOAK_MODE=live to enable real SSO."}
    url = (
        f"{_realm_url()}/protocol/openid-connect/auth"
        f"?client_id={_CLIENT_ID}&response_type=code&scope=openid%20profile%20email"
        f"&redirect_uri={_REDIRECT_URI}"
    )
    return RedirectResponse(url, status_code=302)

@router.get("/callback")
async def callback(code: str):
    if _MODE != "live":
        return {"mode": "mock", "code": code, "access_token": "mock-token", "id_token": "mock-id-token"}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            f"{_realm_url()}/protocol/openid-connect/token",
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "client_id":     _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "redirect_uri":  _REDIRECT_URI,
            },
        )
    if r.status_code != 200:
        raise HTTPException(401, f"Keycloak token exchange failed: {r.text[:300]}")
    return r.json()

async def keycloak_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """FastAPI dependency: verifies a Bearer token via Keycloak /userinfo."""
    if _MODE != "live":
        return {"sub": "mock-user", "preferred_username": "mock", "email": "mock@example.com"}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(
            f"{_realm_url()}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code != 200:
        raise HTTPException(401, "Invalid Keycloak token")
    return r.json()

@router.get("/userinfo")
async def userinfo(user: Dict[str, Any] = Depends(keycloak_user)):
    return user
'''

# ---------------------------------------------------------------------------
# Azure Blob Storage
# ---------------------------------------------------------------------------
AZURE_BLOB_PYTHON = '''"""LAMA-injected Azure Blob Storage integration (mock-by-default).

- Set `AZURE_BLOB_MODE=live` and provide `AZURE_STORAGE_CONNECTION_STRING`
  (or the account_name + account_key pair) to go live.
- Exposes:
    POST /integrations/azure-blob/upload    (multipart file → blob URL)
    GET  /integrations/azure-blob/download/{blob_name}
    GET  /integrations/azure-blob/list
"""
import io
import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

logger = logging.getLogger("integrations.azure_blob")
router = APIRouter(prefix="/integrations/azure-blob", tags=["azure-blob"])

_MODE         = os.getenv("AZURE_BLOB_MODE", "mock").lower()
_CONN_STR     = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
_CONTAINER    = os.getenv("AZURE_STORAGE_CONTAINER", "lama-uploads")
_ACCOUNT_URL  = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")   # e.g. https://<acct>.blob.core.windows.net

def _client():
    if _MODE != "live":
        return None
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore
    except ImportError:
        raise HTTPException(500, "azure-storage-blob not installed. pip install azure-storage-blob")
    if _CONN_STR:
        return BlobServiceClient.from_connection_string(_CONN_STR)
    if _ACCOUNT_URL:
        from azure.identity import DefaultAzureCredential  # type: ignore
        return BlobServiceClient(_ACCOUNT_URL, credential=DefaultAzureCredential())
    raise HTTPException(500, "AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL not set")

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if _MODE != "live":
        return {"mode": "mock", "blob_name": file.filename, "size": len(data)}
    svc = _client()
    container = svc.get_container_client(_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    container.upload_blob(name=file.filename, data=data, overwrite=True)
    return {"mode": "live", "blob_name": file.filename, "size": len(data), "container": _CONTAINER}

@router.get("/download/{blob_name:path}")
async def download(blob_name: str):
    if _MODE != "live":
        return StreamingResponse(io.BytesIO(b"mock content"), media_type="application/octet-stream")
    svc = _client()
    blob = svc.get_container_client(_CONTAINER).get_blob_client(blob_name)
    try:
        stream = blob.download_blob().readall()
    except Exception as e:
        raise HTTPException(404, f"Blob '{blob_name}' not found: {e}")
    return StreamingResponse(
        io.BytesIO(stream),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{blob_name}"'},
    )

@router.get("/list")
async def list_blobs(prefix: Optional[str] = None):
    if _MODE != "live":
        return {"mode": "mock", "container": _CONTAINER, "blobs": []}
    svc = _client()
    container = svc.get_container_client(_CONTAINER)
    return {
        "mode":      "live",
        "container": _CONTAINER,
        "blobs":     [b.name for b in container.list_blobs(name_starts_with=prefix or "")],
    }
'''

# ---------------------------------------------------------------------------
# AWS S3 (or S3-compatible: MinIO, Cloudflare R2, DigitalOcean Spaces)
# ---------------------------------------------------------------------------
S3_PYTHON = '''"""LAMA-injected S3 object storage integration (mock-by-default).

- Set `S3_MODE=live` and provide AWS credentials (`AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_REGION`) or a role via env / IAM.
- For S3-compatible providers (MinIO, R2, Spaces), set `S3_ENDPOINT_URL`.
- Exposes:
    POST /integrations/s3/upload     (multipart file → key)
    GET  /integrations/s3/download/{key}
    GET  /integrations/s3/list
    GET  /integrations/s3/presign/{key}?expires_in=3600  → time-limited GET URL
"""
import io
import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

logger = logging.getLogger("integrations.s3")
router = APIRouter(prefix="/integrations/s3", tags=["s3"])

_MODE       = os.getenv("S3_MODE", "mock").lower()
_BUCKET     = os.getenv("S3_BUCKET", "lama-uploads")
_REGION     = os.getenv("AWS_REGION", "us-east-1")
_ENDPOINT   = os.getenv("S3_ENDPOINT_URL", "")   # non-empty = S3-compatible

def _client():
    if _MODE != "live":
        return None
    try:
        import boto3  # type: ignore
    except ImportError:
        raise HTTPException(500, "boto3 not installed. pip install boto3")
    kwargs = {"region_name": _REGION}
    if _ENDPOINT:
        kwargs["endpoint_url"] = _ENDPOINT
    return boto3.client("s3", **kwargs)

@router.post("/upload")
async def upload(file: UploadFile = File(...), key: Optional[str] = None):
    data = await file.read()
    obj_key = key or file.filename
    if _MODE != "live":
        return {"mode": "mock", "bucket": _BUCKET, "key": obj_key, "size": len(data)}
    _client().put_object(Bucket=_BUCKET, Key=obj_key, Body=data, ContentType=file.content_type or "application/octet-stream")
    return {"mode": "live", "bucket": _BUCKET, "key": obj_key, "size": len(data)}

@router.get("/download/{key:path}")
async def download(key: str):
    if _MODE != "live":
        return StreamingResponse(io.BytesIO(b"mock content"), media_type="application/octet-stream")
    try:
        obj = _client().get_object(Bucket=_BUCKET, Key=key)
    except Exception as e:
        raise HTTPException(404, f"Object '{key}' not found: {e}")
    return StreamingResponse(
        obj["Body"],
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(key)}"'},
    )

@router.get("/list")
async def list_objects(prefix: Optional[str] = None):
    if _MODE != "live":
        return {"mode": "mock", "bucket": _BUCKET, "objects": []}
    resp = _client().list_objects_v2(Bucket=_BUCKET, Prefix=prefix or "")
    return {
        "mode":    "live",
        "bucket":  _BUCKET,
        "objects": [{"key": o["Key"], "size": o["Size"]} for o in resp.get("Contents", [])],
    }

@router.get("/presign/{key:path}")
async def presign(key: str, expires_in: int = 3600):
    if _MODE != "live":
        return {"mode": "mock", "url": f"https://mock.local/{_BUCKET}/{key}?expires_in={expires_in}"}
    url = _client().generate_presigned_url(
        "get_object", Params={"Bucket": _BUCKET, "Key": key}, ExpiresIn=expires_in,
    )
    return {"mode": "live", "url": url, "expires_in": expires_in}
'''

# ---------------------------------------------------------------------------
# Redis cache (async client + FastAPI dependency)
# ---------------------------------------------------------------------------
REDIS_PYTHON = '''"""LAMA-injected Redis cache integration (mock-by-default).

- Set `REDIS_MODE=live` and `REDIS_URL` (e.g. redis://localhost:6379/0) to go live.
- Exposes:
    GET  /integrations/redis/get/{key}
    POST /integrations/redis/set/{key}?value=...&ttl_seconds=60
    DELETE /integrations/redis/del/{key}
    GET  /integrations/redis/health
- Also provides a FastAPI dependency `get_redis()` other routes can Depends() on.
"""
import os
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger("integrations.redis")
router = APIRouter(prefix="/integrations/redis", tags=["redis"])

_MODE = os.getenv("REDIS_MODE", "mock").lower()
_URL  = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_client_singleton = None

def _client():
    global _client_singleton
    if _MODE != "live":
        return None
    if _client_singleton is None:
        try:
            import redis.asyncio as aioredis  # type: ignore
        except ImportError:
            raise HTTPException(500, "redis package not installed. pip install redis>=5")
        _client_singleton = aioredis.from_url(_URL, decode_responses=True)
    return _client_singleton

async def get_redis():
    """FastAPI dependency — yields an async Redis client (or None in mock mode)."""
    return _client()

# Mock in-memory store — enough to keep dev flows working without a real Redis.
_MOCK: dict = {}

@router.get("/health")
async def health(r = Depends(get_redis)):
    if r is None:
        return {"mode": "mock", "ok": True}
    try:
        pong = await r.ping()
        return {"mode": "live", "ok": bool(pong)}
    except Exception as e:
        raise HTTPException(503, f"Redis unreachable: {e}")

@router.get("/get/{key}")
async def get(key: str, r = Depends(get_redis)):
    if r is None:
        return {"key": key, "value": _MOCK.get(key), "mode": "mock"}
    val = await r.get(key)
    return {"key": key, "value": val, "mode": "live"}

@router.post("/set/{key}")
async def set_(key: str, value: str, ttl_seconds: Optional[int] = None, r = Depends(get_redis)):
    if r is None:
        _MOCK[key] = value
        return {"key": key, "ok": True, "mode": "mock"}
    if ttl_seconds and ttl_seconds > 0:
        await r.set(key, value, ex=ttl_seconds)
    else:
        await r.set(key, value)
    return {"key": key, "ok": True, "mode": "live", "ttl_seconds": ttl_seconds}

@router.delete("/del/{key}")
async def delete(key: str, r = Depends(get_redis)):
    if r is None:
        _MOCK.pop(key, None)
        return {"key": key, "deleted": True, "mode": "mock"}
    n = await r.delete(key)
    return {"key": key, "deleted": bool(n), "mode": "live"}
'''


PLATFORM_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "keycloak_auth",
        "label": "Keycloak SSO",
        "category": "Auth",
        "family": "platform",
        "icon": "shield-check",
        "description": (
            "OIDC / SAML single sign-on via Keycloak. Ships a login → callback → "
            "userinfo router and a FastAPI dependency to verify Bearer tokens. "
            "Mock mode returns a stub user so dev flows keep working without a "
            "real Keycloak."
        ),
        "providers":  ["Keycloak", "Red Hat SSO"],
        "gate_env":   "KEYCLOAK_MODE",
        "env_vars": [
            {"name": "KEYCLOAK_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "KEYCLOAK_BASE_URL",      "default": "",     "desc": "https://kc.example.com"},
            {"name": "KEYCLOAK_REALM",         "default": "",     "desc": "Realm name"},
            {"name": "KEYCLOAK_CLIENT_ID",     "default": "",     "desc": "OIDC client id"},
            {"name": "KEYCLOAK_CLIENT_SECRET", "default": "",     "desc": "OIDC client secret"},
            {"name": "KEYCLOAK_REDIRECT_URI",  "default": "http://localhost:8000/integrations/keycloak/callback",
             "desc": "OAuth2 redirect URI registered in Keycloak"},
        ],
        "route_prefix": "/integrations/keycloak",
        "endpoints": [
            {"method": "GET", "path": "/integrations/keycloak/login"},
            {"method": "GET", "path": "/integrations/keycloak/callback"},
            {"method": "GET", "path": "/integrations/keycloak/userinfo"},
        ],
        "file_templates": {
            "python": ("app/integrations/keycloak_auth/router.py", KEYCLOAK_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/KeycloakController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/keycloak_auth/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "azure_blob_storage",
        "label": "Azure Blob Storage",
        "category": "Storage",
        "family": "platform",
        "icon": "cloud-upload",
        "description": (
            "Upload / download / list objects on Azure Blob Storage. Ships a "
            "multipart upload endpoint, a streaming download endpoint, and a "
            "list endpoint. Auth via connection string OR Managed Identity "
            "(DefaultAzureCredential) when only an account URL is set."
        ),
        "providers":  ["Microsoft Azure"],
        "gate_env":   "AZURE_BLOB_MODE",
        "env_vars": [
            {"name": "AZURE_BLOB_MODE",                   "default": "mock",         "desc": "mock | live"},
            {"name": "AZURE_STORAGE_CONNECTION_STRING",   "default": "",             "desc": "Full connection string (preferred)"},
            {"name": "AZURE_STORAGE_ACCOUNT_URL",         "default": "",             "desc": "https://<acct>.blob.core.windows.net (used with DefaultAzureCredential)"},
            {"name": "AZURE_STORAGE_CONTAINER",           "default": "lama-uploads", "desc": "Container / bucket name"},
        ],
        "route_prefix": "/integrations/azure-blob",
        "endpoints": [
            {"method": "POST", "path": "/integrations/azure-blob/upload"},
            {"method": "GET",  "path": "/integrations/azure-blob/download/{blob_name}"},
            {"method": "GET",  "path": "/integrations/azure-blob/list"},
        ],
        "file_templates": {
            "python": ("app/integrations/azure_blob_storage/router.py", AZURE_BLOB_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/AzureBlobController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/azure_blob_storage/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "s3_object_storage",
        "label": "AWS S3 (or S3-compatible)",
        "category": "Storage",
        "family": "platform",
        "icon": "database",
        "description": (
            "AWS S3 client with upload / download / list / presign endpoints. "
            "Also works with S3-compatible providers (MinIO, Cloudflare R2, "
            "DigitalOcean Spaces) — set `S3_ENDPOINT_URL`. Credentials via "
            "env vars or IAM role."
        ),
        "providers":  ["AWS S3", "MinIO", "Cloudflare R2", "DigitalOcean Spaces"],
        "gate_env":   "S3_MODE",
        "env_vars": [
            {"name": "S3_MODE",              "default": "mock",         "desc": "mock | live"},
            {"name": "S3_BUCKET",            "default": "lama-uploads", "desc": "Target bucket"},
            {"name": "AWS_REGION",           "default": "us-east-1",    "desc": "AWS region"},
            {"name": "AWS_ACCESS_KEY_ID",    "default": "",             "desc": "Access key (omit to use IAM role / instance profile)"},
            {"name": "AWS_SECRET_ACCESS_KEY","default": "",             "desc": "Secret key"},
            {"name": "S3_ENDPOINT_URL",      "default": "",             "desc": "Non-empty ⇒ S3-compatible endpoint (MinIO / R2 / Spaces)"},
        ],
        "route_prefix": "/integrations/s3",
        "endpoints": [
            {"method": "POST",   "path": "/integrations/s3/upload"},
            {"method": "GET",    "path": "/integrations/s3/download/{key}"},
            {"method": "GET",    "path": "/integrations/s3/list"},
            {"method": "GET",    "path": "/integrations/s3/presign/{key}"},
        ],
        "file_templates": {
            "python": ("app/integrations/s3_object_storage/router.py", S3_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/S3Controller.java", _stub_template("Java")),
            "nodejs": ("src/integrations/s3_object_storage/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "redis_cache",
        "label": "Redis Cache",
        "category": "Cache",
        "family": "platform",
        "icon": "zap",
        "description": (
            "Async Redis client wrapped as a FastAPI dependency plus simple "
            "get / set / delete / health endpoints. Mock mode uses an in-memory "
            "dict so dev flows keep working without a real Redis."
        ),
        "providers":  ["Redis OSS", "Redis Cloud", "AWS ElastiCache", "Azure Cache for Redis", "Upstash"],
        "gate_env":   "REDIS_MODE",
        "env_vars": [
            {"name": "REDIS_MODE", "default": "mock",                    "desc": "mock | live"},
            {"name": "REDIS_URL",  "default": "redis://localhost:6379/0", "desc": "Full Redis URL (rediss:// for TLS)"},
        ],
        "route_prefix": "/integrations/redis",
        "endpoints": [
            {"method": "GET",    "path": "/integrations/redis/health"},
            {"method": "GET",    "path": "/integrations/redis/get/{key}"},
            {"method": "POST",   "path": "/integrations/redis/set/{key}"},
            {"method": "DELETE", "path": "/integrations/redis/del/{key}"},
        ],
        "file_templates": {
            "python": ("app/integrations/redis_cache/router.py", REDIS_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/RedisController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/redis_cache/router.ts", _stub_template("TypeScript")),
        },
    },
]
