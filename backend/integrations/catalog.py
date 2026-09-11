"""LAMA — Govt-Service Integrations Catalog (iter-13.60).

A pluggable registry of Indian govt-service integrations that LAMA can
inject into the generated codebase. Each entry describes:

  • metadata        — id, label, category, description, provider options
  • env_vars        — the keys the operator sets at runtime to configure creds
  • file_templates  — per-language code that gets dropped into
                       `codegen_files` when the integration is enabled

Design contract:
  • Templates are intentionally **mock-first**: when the required env vars
    are unset, the generated code returns a canned response so the modernised
    service boots and the endpoint is callable end-to-end without an account
    at the actual provider. Flipping the env vars to real creds switches the
    same code path to live calls — no code changes, just configuration.
  • Each template is a Python `.format`-style string with `{placeholder}`
    tokens filled in by `renderer.render_for_project`. No Jinja dep.
  • Adding a new integration = append one entry. Adding a new target language
    for an existing integration = add a `file_templates["<lang>"]` block.
  • All sensitive material (PAN, Aadhaar number, OTP) is intentionally
    hashed before any log/audit write in the templates — DPDP Act 2023.
"""
from typing import Any, Dict, List

from integrations.audit_logger_templates import AUDIT_LOG_PYTHON
from integrations.dpg_templates import (
    ESIGNET_PYTHON_BODY,
    INJI_PYTHON_BODY,
    MOSIP_AUTH_PYTHON_BODY,
    DIVOC_PYTHON_BODY,
    SUNBIRD_RC_PYTHON_BODY,
    OPENG2P_PYTHON_BODY,
)
from integrations.dpg_india_templates import (
    BHARATKOSH_PYTHON_BODY,
    PFMS_PYTHON_BODY,
    API_SETU_PYTHON_BODY,
    BHASHINI_PYTHON_BODY,
    ABDM_PYTHON_BODY,
    JEEVAN_PRAMAAN_PYTHON_BODY,
)

# ---------------------------------------------------------------------------
# Reusable code blocks
# ---------------------------------------------------------------------------

# Shared FastAPI helper block prepended to every Python integration client.
# Keeping it in one place means a fix to the SSL/audit behaviour rolls out
# to every injected integration on the next regeneration.
PY_COMMON_HEADER = '''"""LAMA-injected integration: {label} ({integration_id}).

Configure via these environment variables:
{env_doc}

Mock mode: when {gate_env} is unset (or set to "mock") the endpoint
returns canned responses so the service is end-to-end callable without
provider credentials. Set {gate_env}=live and supply the env vars above
to switch to real calls — NO CODE CHANGES required.

CAUTION: never log raw PAN / Aadhaar / OTP / KYC payloads. This module
hashes sensitive identifiers (SHA-256, 32 chars) for any audit trail.
"""
import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("integrations.{integration_id}")

router = APIRouter(prefix="{route_prefix}", tags=["integrations:{integration_id}"])


def _mask_id(value: str, keep: int = 4) -> str:
    """Mask all but the last `keep` chars of an identifier for safe display."""
    v = (value or "").strip()
    if len(v) <= keep:
        return "*" * len(v)
    return "*" * (len(v) - keep) + v[-keep:]


def _hash_pii(value: str) -> str:
    """Stable 32-char hash for audit trails (never store raw PII)."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:32]


def _mode() -> str:
    """\"mock\" (default) or \"live\". Toggled by the gate env var."""
    return (os.getenv("{gate_env}", "mock").strip().lower() or "mock")


def _ssl_verify():
    """Honour LAMA-style TLS env vars so the same code works behind a
    corporate MITM proxy (Zscaler/Netskope)."""
    if os.getenv("LAMA_DISABLE_SSL_VERIFY", "").strip() in ("1", "true", "yes"):
        return False
    bundle = os.getenv("LAMA_CA_BUNDLE", "").strip()
    return bundle or True
'''


# ---------------------------------------------------------------------------
# Per-integration code templates
# ---------------------------------------------------------------------------

PAN_PYTHON = PY_COMMON_HEADER + '''

class PanVerifyRequest(BaseModel):
    pan: str = Field(..., min_length=10, max_length=10, description="Permanent Account Number — 10 chars (ABCDE1234F)")
    name_on_pan: Optional[str] = Field(None, description="Name as printed on PAN (for fuzzy match check)")


class PanVerifyResponse(BaseModel):
    verified: bool
    status: str
    name_on_pan: str = ""
    name_match: Optional[bool] = None
    pan_masked: str = ""
    request_id: str = ""
    latency_ms: int = 0
    mode: str = "mock"
    error: str = ""


_PAN_RE = __import__("re").compile(r"^[A-Z]{{5}}[0-9]{{4}}[A-Z]$")

_MOCK_PERSONAS = {{
    "ABCDE": ("RAMESH KUMAR",  "VALID"),
    "AAAPL": ("LATA MANGESHKAR", "VALID"),
    "FAKEX": ("",              "INVALID"),
    "ZZZZZ": ("",              "NOT_FOUND"),
}}


def _mock_verify(pan: str, name: str, request_id: str) -> PanVerifyResponse:
    persona = _MOCK_PERSONAS.get(pan[:5], ("VERIFIED USER", "VALID"))
    name_on_pan, status = persona
    verified = (status == "VALID")
    name_match = None
    if verified and name:
        nm = name.strip().upper()
        name_match = nm in name_on_pan.upper() or name_on_pan.upper() in nm
    return PanVerifyResponse(
        verified=verified, status=status,
        name_on_pan=name_on_pan if verified else "",
        name_match=name_match,
        pan_masked=_mask_id(pan, 4),
        request_id=request_id, mode="mock",
    )


async def _live_verify(pan: str, name: str, request_id: str) -> PanVerifyResponse:
    """Live call. Default wiring is to a Karza-style endpoint; switch to NSDL
    Protean / IDfy / Signzy by changing PAN_VERIFY_ENDPOINT_URL and the
    request/response shape in the two helper methods below."""
    url = os.getenv("PAN_VERIFY_ENDPOINT_URL", "https://api.karza.in/v3/pan")
    api_key = os.getenv("PAN_VERIFY_API_KEY", "")
    auth_kind = os.getenv("PAN_VERIFY_AUTH_KIND", "x-karza-key")  # or "bearer"
    if not api_key:
        return PanVerifyResponse(
            verified=False, status="ERROR",
            error="PAN_VERIFY_API_KEY is unset — refusing to call provider in live mode.",
            request_id=request_id, mode="live", pan_masked=_mask_id(pan, 4),
        )

    headers = {{"Content-Type": "application/json"}}
    if auth_kind == "bearer":
        headers["Authorization"] = f"Bearer {{api_key}}"
    else:
        headers["x-karza-key"] = api_key

    body = {{"pan": pan, "consent": "Y"}}
    if name:
        body["name"] = name

    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(url, json=body, headers=headers)

    if r.status_code != 200:
        return PanVerifyResponse(
            verified=False, status="ERROR",
            error=f"Provider HTTP {{r.status_code}}",
            request_id=request_id, mode="live", pan_masked=_mask_id(pan, 4),
        )
    data = r.json() if r.content else {{}}
    # Karza shape: {{"statusCode": 101, "result": {{"name": "...", "pan_status": "EXISTING AND VALID"}}}}
    result = data.get("result") or data
    raw_status = (result.get("pan_status") or result.get("status") or "").upper()
    name_on_pan = result.get("name") or ""
    verified = "VALID" in raw_status or raw_status == "ACTIVE"
    return PanVerifyResponse(
        verified=verified,
        status="VALID" if verified else (raw_status or "INVALID"),
        name_on_pan=name_on_pan,
        name_match=(name.strip().upper() in name_on_pan.upper()) if (verified and name and name_on_pan) else None,
        pan_masked=_mask_id(pan, 4),
        request_id=request_id, mode="live",
    )


@router.post("/verify", response_model=PanVerifyResponse)
async def verify_pan(payload: PanVerifyRequest):
    """Verify an Indian PAN. Mock by default; live when {gate_env}=live."""
    pan = payload.pan.upper().strip().replace(" ", "")
    name = (payload.name_on_pan or "").strip()
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()

    if not _PAN_RE.match(pan):
        return PanVerifyResponse(
            verified=False, status="INVALID",
            error="PAN format invalid (expected ABCDE1234F)",
            pan_masked=_mask_id(pan, 4), request_id=request_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            mode=_mode(),
        )

    mode = _mode()
    try:
        result = _mock_verify(pan, name, request_id) if mode == "mock" \\
                 else await _live_verify(pan, name, request_id)
    except httpx.TimeoutException:
        result = PanVerifyResponse(
            verified=False, status="ERROR", error="Provider timed out",
            pan_masked=_mask_id(pan, 4), request_id=request_id, mode=mode,
        )
    except Exception as e:  # noqa: BLE001 — explicit catch-all for provider errors
        logger.exception("pan.verify failed")
        result = PanVerifyResponse(
            verified=False, status="ERROR", error=str(e),
            pan_masked=_mask_id(pan, 4), request_id=request_id, mode=mode,
        )
    result.latency_ms = int((time.perf_counter() - started) * 1000)

    logger.info("pan.verify request_id=%s pan_hash=%s mode=%s verified=%s status=%s ms=%d",
                request_id, _hash_pii(pan), mode, result.verified, result.status, result.latency_ms)
    return result
'''


AADHAAR_PYTHON = PY_COMMON_HEADER + '''

class AadhaarOtpRequest(BaseModel):
    aadhaar: str = Field(..., min_length=12, max_length=12, description="12-digit Aadhaar number")


class AadhaarVerifyRequest(BaseModel):
    aadhaar: str = Field(..., min_length=12, max_length=12)
    otp: str = Field(..., min_length=6, max_length=6, description="OTP received on registered mobile/email")
    transaction_id: str = Field(..., description="Returned by /otp/initiate")


class AadhaarOtpResponse(BaseModel):
    transaction_id: str
    sent: bool
    masked_mobile: str = ""
    expires_in_seconds: int = 300
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class AadhaarVerifyResponse(BaseModel):
    verified: bool
    status: str
    name: str = ""
    dob: str = ""
    gender: str = ""
    address_masked: str = ""
    aadhaar_masked: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


_AADHAAR_RE = __import__("re").compile(r"^[2-9][0-9]{{11}}$")


@router.post("/otp/initiate", response_model=AadhaarOtpResponse)
async def aadhaar_otp_initiate(payload: AadhaarOtpRequest):
    """Step 1 of Aadhaar e-KYC: trigger an OTP to the resident's registered mobile."""
    aadhaar = payload.aadhaar.strip()
    request_id = uuid.uuid4().hex[:12]
    if not _AADHAAR_RE.match(aadhaar):
        raise HTTPException(400, "Invalid Aadhaar number (12 digits, must not start with 0/1)")

    if _mode() == "mock":
        return AadhaarOtpResponse(
            transaction_id=f"mock-txn-{{request_id}}",
            sent=True, masked_mobile="XXXXXX1234",
            request_id=request_id, mode="mock",
        )

    url = os.getenv("AADHAAR_OTP_URL", "")
    api_key = os.getenv("AADHAAR_API_KEY", "")
    if not (url and api_key):
        return AadhaarOtpResponse(
            transaction_id="", sent=False,
            error="AADHAAR_OTP_URL or AADHAAR_API_KEY is unset", request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(url, json={{"aadhaar": aadhaar, "consent": "Y"}},
                              headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return AadhaarOtpResponse(
        transaction_id=data.get("transaction_id", ""),
        sent=(r.status_code == 200),
        masked_mobile=data.get("masked_mobile", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/verify", response_model=AadhaarVerifyResponse)
async def aadhaar_verify(payload: AadhaarVerifyRequest):
    """Step 2: confirm OTP and pull e-KYC demographics."""
    aadhaar = payload.aadhaar.strip()
    request_id = uuid.uuid4().hex[:12]
    if not _AADHAAR_RE.match(aadhaar):
        raise HTTPException(400, "Invalid Aadhaar number")

    if _mode() == "mock":
        verified = payload.otp == "123456"  # convention for the mock
        return AadhaarVerifyResponse(
            verified=verified, status="VALID" if verified else "OTP_MISMATCH",
            name="RAMESH KUMAR" if verified else "",
            dob="1985-04-21" if verified else "",
            gender="M" if verified else "",
            address_masked="XX, MG Road, Bangalore" if verified else "",
            aadhaar_masked=_mask_id(aadhaar, 4),
            request_id=request_id, mode="mock",
            error="" if verified else "OTP mismatch (mock expects 123456)",
        )

    url = os.getenv("AADHAAR_VERIFY_URL", "")
    api_key = os.getenv("AADHAAR_API_KEY", "")
    if not (url and api_key):
        return AadhaarVerifyResponse(
            verified=False, status="ERROR",
            error="AADHAAR_VERIFY_URL or AADHAAR_API_KEY is unset",
            aadhaar_masked=_mask_id(aadhaar, 4), request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(url, json={{
            "aadhaar": aadhaar, "otp": payload.otp, "transaction_id": payload.transaction_id,
        }}, headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return AadhaarVerifyResponse(
        verified=(r.status_code == 200 and data.get("status") == "VALID"),
        status=data.get("status", "ERROR"),
        name=data.get("name", ""), dob=data.get("dob", ""), gender=data.get("gender", ""),
        address_masked=data.get("address_masked", ""),
        aadhaar_masked=_mask_id(aadhaar, 4),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


GSTIN_PYTHON = PY_COMMON_HEADER + '''

class GstinVerifyRequest(BaseModel):
    gstin: str = Field(..., min_length=15, max_length=15, description="15-char GSTIN")


class GstinVerifyResponse(BaseModel):
    verified: bool
    status: str
    legal_name: str = ""
    trade_name: str = ""
    state: str = ""
    constitution: str = ""
    registration_date: str = ""
    last_filing_status: str = ""
    gstin: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


_GSTIN_RE = __import__("re").compile(r"^[0-9]{{2}}[A-Z]{{5}}[0-9]{{4}}[A-Z]{{1}}[1-9A-Z]{{1}}Z[0-9A-Z]{{1}}$")


@router.post("/verify", response_model=GstinVerifyResponse)
async def verify_gstin(payload: GstinVerifyRequest):
    gstin = payload.gstin.strip().upper()
    request_id = uuid.uuid4().hex[:12]
    if not _GSTIN_RE.match(gstin):
        return GstinVerifyResponse(
            verified=False, status="INVALID",
            error="GSTIN format invalid",
            gstin=gstin, request_id=request_id, mode=_mode(),
        )

    if _mode() == "mock":
        return GstinVerifyResponse(
            verified=True, status="ACTIVE",
            legal_name="ACME WIDGETS PRIVATE LIMITED",
            trade_name="ACME WIDGETS", state="Karnataka",
            constitution="Private Limited Company",
            registration_date="2019-07-12",
            last_filing_status="GSTR-1 filed for 2026-04",
            gstin=gstin, request_id=request_id, mode="mock",
        )

    url = os.getenv("GSTIN_VERIFY_URL", "https://commonapi.mastergst.com/public/search")
    api_key = os.getenv("GSTIN_API_KEY", "")
    if not api_key:
        return GstinVerifyResponse(
            verified=False, status="ERROR",
            error="GSTIN_API_KEY is unset",
            gstin=gstin, request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(url, params={{"gstin": gstin}},
                             headers={{"client_id": os.getenv("GSTIN_CLIENT_ID", ""),
                                      "client_secret": api_key}})
    data = r.json() if r.content else {{}}
    body = data.get("data") or data
    return GstinVerifyResponse(
        verified=(r.status_code == 200 and (body.get("sts") in ("Active", "ACTIVE"))),
        status=body.get("sts", "ERROR"),
        legal_name=body.get("lgnm", ""), trade_name=body.get("tradeNam", ""),
        state=body.get("pradr", {{}}).get("addr", {{}}).get("stcd", ""),
        constitution=body.get("ctb", ""),
        registration_date=body.get("rgdt", ""),
        last_filing_status=body.get("lstupdt", ""),
        gstin=gstin, request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


DIGILOCKER_PYTHON = PY_COMMON_HEADER + '''

class DigilockerInitiateResponse(BaseModel):
    authorize_url: str
    state: str
    request_id: str = ""
    mode: str = "mock"


class DigilockerDocFetchRequest(BaseModel):
    access_token: str
    doc_type: str = Field("AADHAAR", description="Doc type to fetch — e.g. AADHAAR / DRIVING_LICENSE / PAN / CLASS_X_MARKSHEET")


class DigilockerDocFetchResponse(BaseModel):
    fetched: bool
    doc_type: str
    issued_to: str = ""
    issue_date: str = ""
    issuer: str = ""
    doc_uri: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.get("/authorize", response_model=DigilockerInitiateResponse)
async def digilocker_authorize():
    """Step 1: return the DigiLocker OAuth authorize URL the user is redirected to."""
    request_id = uuid.uuid4().hex[:12]
    state = uuid.uuid4().hex
    if _mode() == "mock":
        return DigilockerInitiateResponse(
            authorize_url=f"https://mock.lama.local/digilocker/authorize?state={{state}}",
            state=state, request_id=request_id, mode="mock",
        )
    client_id = os.getenv("DIGILOCKER_CLIENT_ID", "")
    redirect_uri = os.getenv("DIGILOCKER_REDIRECT_URI", "")
    if not (client_id and redirect_uri):
        raise HTTPException(500, "DIGILOCKER_CLIENT_ID / DIGILOCKER_REDIRECT_URI unset")
    authorize = (
        "https://api.digitallocker.gov.in/public/oauth2/1/authorize"
        f"?response_type=code&client_id={{client_id}}&redirect_uri={{redirect_uri}}"
        f"&state={{state}}&scope=basic+documents"
    )
    return DigilockerInitiateResponse(authorize_url=authorize, state=state, request_id=request_id, mode="live")


@router.post("/document", response_model=DigilockerDocFetchResponse)
async def digilocker_fetch_doc(payload: DigilockerDocFetchRequest):
    """Step 2: fetch an issued document given the OAuth access_token."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return DigilockerDocFetchResponse(
            fetched=True, doc_type=payload.doc_type,
            issued_to="RAMESH KUMAR", issue_date="2024-01-15",
            issuer="UIDAI" if payload.doc_type == "AADHAAR" else "Income Tax Department",
            doc_uri=f"mock://digilocker/{{payload.doc_type}}/abcd1234",
            request_id=request_id, mode="mock",
        )
    url = os.getenv("DIGILOCKER_DOC_URL", "https://api.digitallocker.gov.in/public/oauth2/1/files/issued")
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(url, headers={{"Authorization": f"Bearer {{payload.access_token}}"}})
    if r.status_code != 200:
        return DigilockerDocFetchResponse(
            fetched=False, doc_type=payload.doc_type,
            error=f"Provider HTTP {{r.status_code}}",
            request_id=request_id, mode="live",
        )
    data = r.json() if r.content else {{}}
    return DigilockerDocFetchResponse(
        fetched=True, doc_type=payload.doc_type,
        issued_to=data.get("issued_to", ""), issue_date=data.get("issue_date", ""),
        issuer=data.get("issuer", ""), doc_uri=data.get("uri", ""),
        request_id=request_id, mode="live",
    )
'''


ESIGN_PYTHON = PY_COMMON_HEADER + '''

class ESignInitiateRequest(BaseModel):
    document_base64: str = Field(..., description="The PDF/XML to sign, base64-encoded")
    signer_aadhaar: Optional[str] = None  # only required for Aadhaar e-Sign flows
    signer_name: str
    purpose: str = Field("Document signing", max_length=200)
    callback_url: Optional[str] = None


class ESignInitiateResponse(BaseModel):
    transaction_id: str
    redirect_url: str = ""
    expires_in_seconds: int = 600
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class ESignStatusResponse(BaseModel):
    transaction_id: str
    status: str  # "PENDING" | "SIGNED" | "REJECTED" | "EXPIRED"
    signed_document_base64: str = ""
    signed_at: str = ""
    signer_name: str = ""
    mode: str = "mock"


@router.post("/initiate", response_model=ESignInitiateResponse)
async def esign_initiate(payload: ESignInitiateRequest):
    request_id = uuid.uuid4().hex[:12]
    txn = uuid.uuid4().hex[:16]
    if _mode() == "mock":
        return ESignInitiateResponse(
            transaction_id=txn,
            redirect_url=f"https://mock.lama.local/esign/sign?txn={{txn}}",
            request_id=request_id, mode="mock",
        )
    url = os.getenv("ESIGN_INITIATE_URL", "")
    api_key = os.getenv("ESIGN_API_KEY", "")
    asp_id = os.getenv("ESIGN_ASP_ID", "")
    if not (url and api_key and asp_id):
        return ESignInitiateResponse(
            transaction_id="", error="ESIGN_INITIATE_URL / ESIGN_API_KEY / ESIGN_ASP_ID unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=60, verify=_ssl_verify()) as client:
        r = await client.post(url, json={{
            "asp_id": asp_id,
            "document": payload.document_base64,
            "signer_aadhaar": payload.signer_aadhaar or "",
            "signer_name": payload.signer_name,
            "purpose": payload.purpose,
            "callback_url": payload.callback_url or "",
        }}, headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return ESignInitiateResponse(
        transaction_id=data.get("transaction_id", txn),
        redirect_url=data.get("redirect_url", ""),
        expires_in_seconds=data.get("expires_in_seconds", 600),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/status/{{transaction_id}}", response_model=ESignStatusResponse)
async def esign_status(transaction_id: str):
    if _mode() == "mock":
        # Pretend everything signed in the mock world.
        return ESignStatusResponse(
            transaction_id=transaction_id, status="SIGNED",
            signed_document_base64="<base64 PDF would be here>",
            signed_at=datetime.now(timezone.utc).isoformat(),
            signer_name="RAMESH KUMAR", mode="mock",
        )
    url = os.getenv("ESIGN_STATUS_URL", "").rstrip("/") + f"/{{transaction_id}}"
    api_key = os.getenv("ESIGN_API_KEY", "")
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(url, headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return ESignStatusResponse(
        transaction_id=transaction_id, status=data.get("status", "PENDING"),
        signed_document_base64=data.get("signed_document_base64", ""),
        signed_at=data.get("signed_at", ""),
        signer_name=data.get("signer_name", ""),
        mode="live",
    )
'''


UPI_PYTHON = PY_COMMON_HEADER + '''

class UpiInitiateRequest(BaseModel):
    amount_rupees: float = Field(..., gt=0, le=100000)
    payer_vpa: Optional[str] = Field(None, description="Payer UPI ID, e.g. ramesh@oksbi (optional for collect-by-link)")
    purpose: str = Field("Payment", max_length=200)
    order_id: str = Field(..., description="Your idempotency / order reference")
    callback_url: Optional[str] = None


class UpiInitiateResponse(BaseModel):
    transaction_id: str
    intent_url: str = ""           # for app-to-app / QR
    collect_link: str = ""         # for collect-by-link
    expires_in_seconds: int = 600
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class UpiStatusResponse(BaseModel):
    transaction_id: str
    order_id: str
    status: str   # "PENDING" | "SUCCESS" | "FAILURE" | "EXPIRED"
    amount_rupees: float
    upi_ref: str = ""
    settled_at: str = ""
    mode: str = "mock"


@router.post("/initiate", response_model=UpiInitiateResponse)
async def upi_initiate(payload: UpiInitiateRequest):
    request_id = uuid.uuid4().hex[:12]
    txn = uuid.uuid4().hex[:14]
    if _mode() == "mock":
        return UpiInitiateResponse(
            transaction_id=txn,
            intent_url=f"upi://pay?pa=merchant@upi&pn=Demo&am={{payload.amount_rupees:.2f}}&tn={{payload.purpose}}&tr={{txn}}",
            collect_link=f"https://mock.lama.local/pay/{{txn}}",
            request_id=request_id, mode="mock",
        )
    url = os.getenv("UPI_INITIATE_URL", "")
    key_id = os.getenv("UPI_KEY_ID", "")
    secret = os.getenv("UPI_KEY_SECRET", "")
    merchant_vpa = os.getenv("UPI_MERCHANT_VPA", "")
    if not (url and key_id and secret and merchant_vpa):
        return UpiInitiateResponse(
            transaction_id="",
            error="UPI_INITIATE_URL / UPI_KEY_ID / UPI_KEY_SECRET / UPI_MERCHANT_VPA unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify(),
                                 auth=(key_id, secret)) as client:
        r = await client.post(url, json={{
            "amount": int(round(payload.amount_rupees * 100)),  # paise
            "currency": "INR",
            "method": "upi",
            "order_id": payload.order_id,
            "payer_vpa": payload.payer_vpa or "",
            "merchant_vpa": merchant_vpa,
            "purpose": payload.purpose,
            "callback_url": payload.callback_url or "",
        }})
    data = r.json() if r.content else {{}}
    return UpiInitiateResponse(
        transaction_id=data.get("id", txn),
        intent_url=data.get("intent_url", ""),
        collect_link=data.get("short_url") or data.get("payment_link", ""),
        expires_in_seconds=data.get("expires_in_seconds", 600),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/status/{{transaction_id}}", response_model=UpiStatusResponse)
async def upi_status(transaction_id: str):
    if _mode() == "mock":
        return UpiStatusResponse(
            transaction_id=transaction_id, order_id=transaction_id,
            status="SUCCESS", amount_rupees=100.0,
            upi_ref="UPI" + transaction_id[:10].upper(),
            settled_at=datetime.now(timezone.utc).isoformat(), mode="mock",
        )
    url = os.getenv("UPI_STATUS_URL", "").rstrip("/") + f"/{{transaction_id}}"
    key_id = os.getenv("UPI_KEY_ID", "")
    secret = os.getenv("UPI_KEY_SECRET", "")
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify(),
                                 auth=(key_id, secret)) as client:
        r = await client.get(url)
    data = r.json() if r.content else {{}}
    return UpiStatusResponse(
        transaction_id=transaction_id, order_id=data.get("order_id", transaction_id),
        status=(data.get("status") or "PENDING").upper(),
        amount_rupees=(data.get("amount", 0) or 0) / 100.0,
        upi_ref=data.get("acquirer_data", {{}}).get("rrn", "") or data.get("upi_ref", ""),
        settled_at=data.get("captured_at", ""),
        mode="live",
    )
'''


# Stub for languages we haven't templated yet — we still create one file
# so the user knows the integration was enabled and where to put the code.
def _stub_template(language: str) -> str:
    return (
        "// LAMA-injected integration stub for {{label}} ({{integration_id}}).\n"
        f"// A working {language} template hasn't shipped yet — "
        "open an issue or contribute one in backend/integrations/catalog.py.\n"
        "// Until then, port the Python template (which is the reference\n"
        "// implementation) by hand:\n//\n"
        "//   backend/integrations/catalog.py → CATALOG[{{integration_id}}]\n"
    )


# ---------------------------------------------------------------------------
# DPG / DPI templates — composed with the shared Python header so they get
# _mask_id / _hash_pii / _mode / _ssl_verify / router / logger / httpx for free.
# ---------------------------------------------------------------------------

ESIGNET_PYTHON    = PY_COMMON_HEADER + ESIGNET_PYTHON_BODY
INJI_PYTHON       = PY_COMMON_HEADER + INJI_PYTHON_BODY
MOSIP_AUTH_PYTHON = PY_COMMON_HEADER + MOSIP_AUTH_PYTHON_BODY
DIVOC_PYTHON      = PY_COMMON_HEADER + DIVOC_PYTHON_BODY
SUNBIRD_RC_PYTHON = PY_COMMON_HEADER + SUNBIRD_RC_PYTHON_BODY
OPENG2P_PYTHON    = PY_COMMON_HEADER + OPENG2P_PYTHON_BODY

# Indian-stack DPG/DPI templates (Bharatkosh, PFMS, API Setu, Bhashini,
# ABDM, Jeevan Pramaan) — same shared-header contract.
BHARATKOSH_PYTHON     = PY_COMMON_HEADER + BHARATKOSH_PYTHON_BODY
PFMS_PYTHON           = PY_COMMON_HEADER + PFMS_PYTHON_BODY
API_SETU_PYTHON       = PY_COMMON_HEADER + API_SETU_PYTHON_BODY
BHASHINI_PYTHON       = PY_COMMON_HEADER + BHASHINI_PYTHON_BODY
ABDM_PYTHON           = PY_COMMON_HEADER + ABDM_PYTHON_BODY
JEEVAN_PRAMAAN_PYTHON = PY_COMMON_HEADER + JEEVAN_PRAMAAN_PYTHON_BODY


# ---------------------------------------------------------------------------
# The catalog itself
# ---------------------------------------------------------------------------

CATALOG: List[Dict[str, Any]] = [
    {
        "id": "pan_verification",
        "label": "PAN Verification",
        "category": "KYC",
        "family": "govt-india",
        "icon": "id-card",
        "description": (
            "Verify the validity of an Indian Permanent Account Number (PAN). "
            "Optionally cross-check the name on the card. Routed through "
            "Karza by default; switchable to NSDL Protean / IDfy / Signzy by "
            "editing the generated client."
        ),
        "providers": ["Karza", "NSDL Protean", "IDfy", "Signzy"],
        "gate_env": "PAN_VERIFY_MODE",
        "env_vars": [
            {"name": "PAN_VERIFY_MODE",       "default": "mock", "desc": "mock | live"},
            {"name": "PAN_VERIFY_ENDPOINT_URL", "default": "https://api.karza.in/v3/pan", "desc": "Provider endpoint"},
            {"name": "PAN_VERIFY_API_KEY",    "default": "",     "desc": "Provider API key (required in live mode)"},
            {"name": "PAN_VERIFY_AUTH_KIND",  "default": "x-karza-key", "desc": "x-karza-key | bearer"},
        ],
        "route_prefix": "/integrations/pan",
        "endpoints": [{"method": "POST", "path": "/integrations/pan/verify"}],
        "file_templates": {
            "python":  ("app/integrations/pan_verification/router.py", PAN_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/PanController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/pan_verification/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "aadhaar_ekyc",
        "label": "Aadhaar e-KYC",
        "category": "KYC",
        "family": "govt-india",
        "icon": "fingerprint",
        "description": (
            "UIDAI-backed Aadhaar e-KYC with OTP. Two-step flow: initiate OTP, "
            "then verify and pull demographics. Audit-only — never logs raw "
            "Aadhaar number (SHA-256 hash only)."
        ),
        "providers": ["UIDAI Auth API", "Karza", "Signzy", "IDfy"],
        "gate_env": "AADHAAR_MODE",
        "env_vars": [
            {"name": "AADHAAR_MODE",       "default": "mock", "desc": "mock | live"},
            {"name": "AADHAAR_OTP_URL",    "default": "",     "desc": "Provider OTP-initiate endpoint"},
            {"name": "AADHAAR_VERIFY_URL", "default": "",     "desc": "Provider OTP-verify endpoint"},
            {"name": "AADHAAR_API_KEY",    "default": "",     "desc": "Bearer token for the provider"},
        ],
        "route_prefix": "/integrations/aadhaar",
        "endpoints": [
            {"method": "POST", "path": "/integrations/aadhaar/otp/initiate"},
            {"method": "POST", "path": "/integrations/aadhaar/verify"},
        ],
        "file_templates": {
            "python":  ("app/integrations/aadhaar_ekyc/router.py", AADHAAR_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/AadhaarController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/aadhaar_ekyc/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "gstin_verification",
        "label": "GSTIN Verification",
        "category": "Tax",
        "family": "govt-india",
        "icon": "receipt-text",
        "description": (
            "Look up a 15-character GSTIN against the GSTN registry. Returns "
            "legal/trade name, state, constitution, registration date and last "
            "return-filing status. Routed through MasterGST by default."
        ),
        "providers": ["GSTN (direct)", "MasterGST", "ClearTax", "Tally"],
        "gate_env": "GSTIN_VERIFY_MODE",
        "env_vars": [
            {"name": "GSTIN_VERIFY_MODE", "default": "mock", "desc": "mock | live"},
            {"name": "GSTIN_VERIFY_URL",  "default": "https://commonapi.mastergst.com/public/search", "desc": "Provider endpoint"},
            {"name": "GSTIN_CLIENT_ID",   "default": "",     "desc": "Provider client_id"},
            {"name": "GSTIN_API_KEY",     "default": "",     "desc": "Provider client_secret / API key"},
        ],
        "route_prefix": "/integrations/gstin",
        "endpoints": [{"method": "POST", "path": "/integrations/gstin/verify"}],
        "file_templates": {
            "python":  ("app/integrations/gstin_verification/router.py", GSTIN_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/GstinController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/gstin_verification/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "digilocker",
        "label": "DigiLocker Document Fetch",
        "category": "Identity",
        "family": "govt-india",
        "icon": "folder-lock",
        "description": (
            "OAuth2 flow against the MeitY DigiLocker / Meri Pehchaan platform. "
            "Returns a redirect URL for the user to authorise, then fetches "
            "their issued documents (Aadhaar / PAN / Driving License / Marksheets) "
            "with the access token."
        ),
        "providers": ["DigiLocker (MeitY)", "Meri Pehchaan"],
        "gate_env": "DIGILOCKER_MODE",
        "env_vars": [
            {"name": "DIGILOCKER_MODE",         "default": "mock", "desc": "mock | live"},
            {"name": "DIGILOCKER_CLIENT_ID",    "default": "",     "desc": "OAuth client_id from DigiLocker partner portal"},
            {"name": "DIGILOCKER_CLIENT_SECRET", "default": "",    "desc": "OAuth client_secret"},
            {"name": "DIGILOCKER_REDIRECT_URI", "default": "",     "desc": "Registered redirect URI"},
            {"name": "DIGILOCKER_DOC_URL",      "default": "https://api.digitallocker.gov.in/public/oauth2/1/files/issued", "desc": "Document-list endpoint"},
        ],
        "route_prefix": "/integrations/digilocker",
        "endpoints": [
            {"method": "GET",  "path": "/integrations/digilocker/authorize"},
            {"method": "POST", "path": "/integrations/digilocker/document"},
        ],
        "file_templates": {
            "python":  ("app/integrations/digilocker/router.py", DIGILOCKER_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/DigiLockerController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/digilocker/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "esign",
        "label": "Aadhaar e-Sign",
        "category": "Identity",
        "family": "govt-india",
        "icon": "pen-square",
        "description": (
            "Legally-valid Aadhaar e-Sign (IT Act 2008). Initiate signing for a "
            "PDF/XML document and poll for completion. Routed through eMudhra / "
            "NSDL e-Sign by default."
        ),
        "providers": ["eMudhra", "NSDL e-Sign", "Protean", "Leegality"],
        "gate_env": "ESIGN_MODE",
        "env_vars": [
            {"name": "ESIGN_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "ESIGN_INITIATE_URL",  "default": "",     "desc": "Provider sign-initiate endpoint"},
            {"name": "ESIGN_STATUS_URL",    "default": "",     "desc": "Provider sign-status endpoint (base, txn id appended)"},
            {"name": "ESIGN_API_KEY",       "default": "",     "desc": "Bearer token"},
            {"name": "ESIGN_ASP_ID",        "default": "",     "desc": "ASP/partner id from the e-Sign provider"},
        ],
        "route_prefix": "/integrations/esign",
        "endpoints": [
            {"method": "POST", "path": "/integrations/esign/initiate"},
            {"method": "GET",  "path": "/integrations/esign/status/{transaction_id}"},
        ],
        "file_templates": {
            "python":  ("app/integrations/esign/router.py", ESIGN_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/ESignController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/esign/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "upi_payment",
        "label": "UPI Payment Collection",
        "category": "Payments",
        "family": "govt-india",
        "icon": "indian-rupee",
        "description": (
            "NPCI UPI payment collection — generate a payment intent URL / "
            "collect-link and poll for settlement. Routed through Razorpay's "
            "UPI API by default; switchable to PayU / Cashfree / NPCI direct."
        ),
        "providers": ["Razorpay", "Cashfree", "PayU", "NPCI direct"],
        "gate_env": "UPI_MODE",
        "env_vars": [
            {"name": "UPI_MODE",         "default": "mock", "desc": "mock | live"},
            {"name": "UPI_INITIATE_URL", "default": "",     "desc": "Provider create-payment endpoint"},
            {"name": "UPI_STATUS_URL",   "default": "",     "desc": "Provider payment-status endpoint (base)"},
            {"name": "UPI_KEY_ID",       "default": "",     "desc": "Provider key_id (HTTP basic-auth user)"},
            {"name": "UPI_KEY_SECRET",   "default": "",     "desc": "Provider key_secret (HTTP basic-auth pwd)"},
            {"name": "UPI_MERCHANT_VPA", "default": "",     "desc": "Your registered UPI ID (e.g. merchant@hdfcbank)"},
        ],
        "route_prefix": "/integrations/upi",
        "endpoints": [
            {"method": "POST", "path": "/integrations/upi/initiate"},
            {"method": "GET",  "path": "/integrations/upi/status/{transaction_id}"},
        ],
        "file_templates": {
            "python":  ("app/integrations/upi_payment/router.py", UPI_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/UpiController.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/upi_payment/router.ts", _stub_template("TypeScript")),
        },
    },
    # ─── DPG / DPI ──────────────────────────────────────────────────────
    {
        "id": "esignet_oidc",
        "label": "eSignet (MOSIP) — OIDC",
        "category": "Identity",
        "family": "dpg",
        "icon": "key-round",
        "description": (
            "MOSIP eSignet — an OIDC-compliant identity provider used by "
            "national digital-ID stacks (India Stack-derived, Philippines "
            "PhilSys, Morocco). Three-leg flow: /authorize → /token → /userinfo."
        ),
        "providers": ["MOSIP eSignet", "Mosip-IDP"],
        "gate_env": "ESIGNET_MODE",
        "env_vars": [
            {"name": "ESIGNET_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "ESIGNET_BASE_URL",      "default": "",     "desc": "https://<eSignet host>/v1/esignet"},
            {"name": "ESIGNET_CLIENT_ID",     "default": "",     "desc": "OIDC client_id from the partner portal"},
            {"name": "ESIGNET_CLIENT_SECRET", "default": "",     "desc": "OIDC client_secret"},
            {"name": "ESIGNET_REDIRECT_URI",  "default": "",     "desc": "Registered redirect_uri"},
            {"name": "ESIGNET_SCOPE",         "default": "openid profile email", "desc": "Space-separated OIDC scopes"},
        ],
        "route_prefix": "/integrations/esignet",
        "endpoints": [
            {"method": "GET",  "path": "/integrations/esignet/authorize"},
            {"method": "POST", "path": "/integrations/esignet/token"},
            {"method": "GET",  "path": "/integrations/esignet/userinfo"},
        ],
        "file_templates": {
            "python": ("app/integrations/esignet_oidc/router.py", ESIGNET_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/EsignetOidcController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/esignet_oidc/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "inji_wallet",
        "label": "Inji (MOSIP) — VC Wallet",
        "category": "Identity",
        "family": "dpg",
        "icon": "wallet",
        "description": (
            "MOSIP Inji — issue verifiable credentials to a holder's mobile "
            "wallet and verify presented VCs (W3C VC Data Model). Two endpoints: "
            "/credentials/issue (issuer side) and /credentials/verify (verifier side)."
        ),
        "providers": ["MOSIP Inji", "Inji Web", "Inji Mobile"],
        "gate_env": "INJI_MODE",
        "env_vars": [
            {"name": "INJI_MODE",                "default": "mock", "desc": "mock | live"},
            {"name": "INJI_ISSUER_BASE_URL",     "default": "",     "desc": "Issuer service base URL"},
            {"name": "INJI_ISSUER_API_KEY",      "default": "",     "desc": "Issuer API key / bearer"},
            {"name": "INJI_ISSUER_ID",           "default": "",     "desc": "Issuer DID / id registered in the trust list"},
            {"name": "INJI_VERIFIER_BASE_URL",   "default": "",     "desc": "Verifier service base URL"},
            {"name": "INJI_VERIFIER_API_KEY",    "default": "",     "desc": "Verifier API key / bearer"},
        ],
        "route_prefix": "/integrations/inji",
        "endpoints": [
            {"method": "POST", "path": "/integrations/inji/credentials/issue"},
            {"method": "POST", "path": "/integrations/inji/credentials/verify"},
        ],
        "file_templates": {
            "python": ("app/integrations/inji_wallet/router.py", INJI_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/InjiWalletController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/inji_wallet/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "mosip_auth",
        "label": "MOSIP Auth — Yes/No + e-KYC",
        "category": "KYC",
        "family": "dpg",
        "icon": "user-check",
        "description": (
            "MOSIP IDA — yes/no auth and e-KYC against a MOSIP-based national "
            "ID (UIN/VID). Auth types: OTP, demographic, biometric. e-KYC "
            "follows a successful OTP auth and pulls demographics."
        ),
        "providers": ["MOSIP IDA"],
        "gate_env": "MOSIP_AUTH_MODE",
        "env_vars": [
            {"name": "MOSIP_AUTH_MODE",        "default": "mock", "desc": "mock | live"},
            {"name": "MOSIP_AUTH_BASE_URL",    "default": "",     "desc": "https://<mosip>/idauthentication endpoint"},
            {"name": "MOSIP_PARTNER_ID",       "default": "",     "desc": "Partner id registered with MOSIP"},
            {"name": "MOSIP_PARTNER_API_KEY",  "default": "",     "desc": "Partner API key / bearer"},
        ],
        "route_prefix": "/integrations/mosip-auth",
        "endpoints": [
            {"method": "POST", "path": "/integrations/mosip-auth/auth"},
            {"method": "POST", "path": "/integrations/mosip-auth/ekyc"},
        ],
        "file_templates": {
            "python": ("app/integrations/mosip_auth/router.py", MOSIP_AUTH_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/MosipAuthController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/mosip_auth/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "divoc",
        "label": "DIVOC — Open Verifiable Credentials",
        "category": "Identity",
        "family": "dpg",
        "icon": "badge-check",
        "description": (
            "DIVOC (Digital Infrastructure for Verifiable Open Credentialing) "
            "— issue and verify W3C verifiable credentials at scale "
            "(vaccination, education, agri certificates). Two endpoints: "
            "/issue and /verify."
        ),
        "providers": ["DIVOC", "Sunbird-DIVOC"],
        "gate_env": "DIVOC_MODE",
        "env_vars": [
            {"name": "DIVOC_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "DIVOC_BASE_URL",      "default": "",     "desc": "DIVOC issuance base URL"},
            {"name": "DIVOC_ISSUER_TOKEN",  "default": "",     "desc": "Bearer for the issuer API"},
            {"name": "DIVOC_VERIFY_URL",    "default": "",     "desc": "Public verification endpoint (no auth)"},
        ],
        "route_prefix": "/integrations/divoc",
        "endpoints": [
            {"method": "POST", "path": "/integrations/divoc/issue"},
            {"method": "POST", "path": "/integrations/divoc/verify"},
        ],
        "file_templates": {
            "python": ("app/integrations/divoc/router.py", DIVOC_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/DivocController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/divoc/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "sunbird_rc",
        "label": "Sunbird RC — Registry & Credentials",
        "category": "Identity",
        "family": "dpg",
        "icon": "database",
        "description": (
            "Sunbird Registry & Credentialing — schema-driven entity registry + "
            "VC issuance. Used by NCERT, ULIP, etc. Register entities, fetch "
            "by Open Studio id, and issue VCs against them."
        ),
        "providers": ["Sunbird RC", "Sunbird-Ed"],
        "gate_env": "SUNBIRD_RC_MODE",
        "env_vars": [
            {"name": "SUNBIRD_RC_MODE",     "default": "mock", "desc": "mock | live"},
            {"name": "SUNBIRD_RC_BASE_URL", "default": "",     "desc": "https://<sunbird>/registry"},
            {"name": "SUNBIRD_VC_BASE_URL", "default": "",     "desc": "https://<sunbird>/vc-issuer"},
            {"name": "SUNBIRD_RC_TOKEN",    "default": "",     "desc": "Bearer for both registry + VC issuer"},
        ],
        "route_prefix": "/integrations/sunbird",
        "endpoints": [
            {"method": "POST", "path": "/integrations/sunbird/registry/{schema}"},
            {"method": "GET",  "path": "/integrations/sunbird/registry/{schema}/{osid}"},
            {"method": "POST", "path": "/integrations/sunbird/credentials"},
        ],
        "file_templates": {
            "python": ("app/integrations/sunbird_rc/router.py", SUNBIRD_RC_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/SunbirdRcController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/sunbird_rc/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "openg2p",
        "label": "OpenG2P — Govt-to-Person Payments",
        "category": "Payments",
        "family": "dpg",
        "icon": "banknote",
        "description": (
            "OpenG2P — beneficiary registry + disbursement orchestration for "
            "social-protection programmes (PM-KISAN, NSAP style). Fetch a "
            "beneficiary, initiate a disbursement, poll status."
        ),
        "providers": ["OpenG2P"],
        "gate_env": "OPENG2P_MODE",
        "env_vars": [
            {"name": "OPENG2P_MODE",            "default": "mock", "desc": "mock | live"},
            {"name": "OPENG2P_BASE_URL",        "default": "",     "desc": "https://<openg2p>/api"},
            {"name": "OPENG2P_PARTNER_TOKEN",   "default": "",     "desc": "Partner bearer / API key"},
        ],
        "route_prefix": "/integrations/openg2p",
        "endpoints": [
            {"method": "GET",  "path": "/integrations/openg2p/beneficiary/{beneficiary_id}"},
            {"method": "POST", "path": "/integrations/openg2p/disbursement/initiate"},
            {"method": "GET",  "path": "/integrations/openg2p/disbursement/{disbursement_id}/status"},
        ],
        "file_templates": {
            "python": ("app/integrations/openg2p/router.py", OPENG2P_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/OpenG2PController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/openg2p/router.ts", _stub_template("TypeScript")),
        },
    },
    # ─── DPG / DPI — Indian Stack ───────────────────────────────────────
    {
        "id": "bharatkosh_ntrp",
        "label": "Bharatkosh (NTRP) — Non-Tax Receipts",
        "category": "Payments",
        "family": "dpg-india",
        "icon": "landmark",
        "description": (
            "Bharatkosh / NTRP — collect non-tax govt receipts via challan. "
            "Two endpoints: create a challan (returns NTRP-hosted pay URL) "
            "and poll its payment status."
        ),
        "providers": ["Bharatkosh (CGA)", "NTRP"],
        "gate_env": "BHARATKOSH_MODE",
        "env_vars": [
            {"name": "BHARATKOSH_MODE",      "default": "mock", "desc": "mock | live"},
            {"name": "BHARATKOSH_BASE_URL",  "default": "",     "desc": "Bharatkosh API base URL"},
            {"name": "BHARATKOSH_DEPT_CODE", "default": "",     "desc": "Your department / DDO code"},
            {"name": "BHARATKOSH_API_KEY",   "default": "",     "desc": "Bearer / API key issued by Bharatkosh"},
        ],
        "route_prefix": "/integrations/bharatkosh",
        "endpoints": [
            {"method": "POST", "path": "/integrations/bharatkosh/challan/create"},
            {"method": "GET",  "path": "/integrations/bharatkosh/challan/{challan_id}"},
        ],
        "file_templates": {
            "python": ("app/integrations/bharatkosh_ntrp/router.py", BHARATKOSH_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/BharatkoshNtrpController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/bharatkosh_ntrp/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "pfms",
        "label": "PFMS — Beneficiary Validation + DBT",
        "category": "Payments",
        "family": "dpg-india",
        "icon": "coins",
        "description": (
            "Public Financial Management System — penny-drop validate a "
            "beneficiary's bank account against PFMS and push a DBT-style "
            "payment instruction. The backbone for every centrally-sponsored "
            "scheme disbursement."
        ),
        "providers": ["PFMS (CGA)"],
        "gate_env": "PFMS_MODE",
        "env_vars": [
            {"name": "PFMS_MODE",         "default": "mock", "desc": "mock | live"},
            {"name": "PFMS_BASE_URL",     "default": "",     "desc": "PFMS API base URL"},
            {"name": "PFMS_AGENCY_CODE",  "default": "",     "desc": "Your registered agency code"},
            {"name": "PFMS_API_KEY",      "default": "",     "desc": "Bearer / API key"},
        ],
        "route_prefix": "/integrations/pfms",
        "endpoints": [
            {"method": "POST", "path": "/integrations/pfms/beneficiary/validate"},
            {"method": "POST", "path": "/integrations/pfms/payment/instruction"},
        ],
        "file_templates": {
            "python": ("app/integrations/pfms/router.py", PFMS_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/PfmsController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/pfms/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "api_setu",
        "label": "API Setu — Govt API Gateway",
        "category": "Identity",
        "family": "dpg-india",
        "icon": "network",
        "description": (
            "API Setu is the central Indian govt API marketplace (Driving "
            "Licence, Vehicle RC, Voter ID, Ration Card verifications and "
            "more). This integration exposes the catalogue and a generic "
            "/invoke endpoint that proxies any catalogued API by id."
        ),
        "providers": ["API Setu (NeGD / MeitY)"],
        "gate_env": "API_SETU_MODE",
        "env_vars": [
            {"name": "API_SETU_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "API_SETU_BASE_URL",      "default": "",     "desc": "API Setu base URL"},
            {"name": "API_SETU_CLIENT_ID",     "default": "",     "desc": "X-APISETU-CLIENTID"},
            {"name": "API_SETU_CLIENT_SECRET", "default": "",     "desc": "X-APISETU-APIKEY"},
        ],
        "route_prefix": "/integrations/api-setu",
        "endpoints": [
            {"method": "GET",  "path": "/integrations/api-setu/catalog"},
            {"method": "POST", "path": "/integrations/api-setu/invoke/{api_id}"},
        ],
        "file_templates": {
            "python": ("app/integrations/api_setu/router.py", API_SETU_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/ApiSetuController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/api_setu/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "bhashini",
        "label": "Bhashini — Translate / Transliterate / TTS",
        "category": "Language",
        "family": "dpg-india",
        "icon": "languages",
        "description": (
            "MeitY's Bhashini — text translation and transliteration across "
            "22 Indian languages plus TTS. Wire any user-facing service "
            "for multilingual support via three endpoints."
        ),
        "providers": ["Bhashini (MeitY)"],
        "gate_env": "BHASHINI_MODE",
        "env_vars": [
            {"name": "BHASHINI_MODE",     "default": "mock", "desc": "mock | live"},
            {"name": "BHASHINI_BASE_URL", "default": "",     "desc": "Bhashini API base URL"},
            {"name": "BHASHINI_API_KEY",  "default": "",     "desc": "Bhashini Authorization token"},
            {"name": "BHASHINI_USER_ID",  "default": "",     "desc": "Bhashini userID header"},
        ],
        "route_prefix": "/integrations/bhashini",
        "endpoints": [
            {"method": "POST", "path": "/integrations/bhashini/translate"},
            {"method": "POST", "path": "/integrations/bhashini/transliterate"},
            {"method": "POST", "path": "/integrations/bhashini/tts"},
        ],
        "file_templates": {
            "python": ("app/integrations/bhashini/router.py", BHASHINI_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/BhashiniController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/bhashini/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "abdm",
        "label": "ABDM — Ayushman Bharat Health Stack",
        "category": "Health",
        "family": "dpg-india",
        "icon": "heart-pulse",
        "description": (
            "Ayushman Bharat Digital Mission — create an ABHA (health "
            "account) via Aadhaar OTP, fetch an ABHA profile by number, "
            "and verify a Health Professional Registry (HPR) id."
        ),
        "providers": ["ABDM (NHA)"],
        "gate_env": "ABDM_MODE",
        "env_vars": [
            {"name": "ABDM_MODE",          "default": "mock", "desc": "mock | live"},
            {"name": "ABDM_BASE_URL",      "default": "",     "desc": "ABDM gateway base URL"},
            {"name": "ABDM_CLIENT_ID",     "default": "",     "desc": "X-CM-ID partner id"},
            {"name": "ABDM_CLIENT_SECRET", "default": "",     "desc": "Bearer access-token (rotate via /session)"},
        ],
        "route_prefix": "/integrations/abdm",
        "endpoints": [
            {"method": "POST", "path": "/integrations/abdm/abha/create"},
            {"method": "GET",  "path": "/integrations/abdm/abha/{abha_number}"},
            {"method": "POST", "path": "/integrations/abdm/hpr/verify"},
        ],
        "file_templates": {
            "python": ("app/integrations/abdm/router.py", ABDM_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/AbdmController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/abdm/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "jeevan_pramaan",
        "label": "Jeevan Pramaan — Digital Life Certificate",
        "category": "Identity",
        "family": "dpg-india",
        "icon": "file-badge",
        "description": (
            "Jeevan Pramaan — issue and look up Digital Life Certificates "
            "(DLCs) for pensioners. Backbone of the pensioner annual-verification "
            "process. Aadhaar-OTP and biometric flows are both supported."
        ),
        "providers": ["Jeevan Pramaan (MeitY)"],
        "gate_env": "JEEVAN_PRAMAAN_MODE",
        "env_vars": [
            {"name": "JEEVAN_PRAMAAN_MODE",     "default": "mock", "desc": "mock | live"},
            {"name": "JEEVAN_PRAMAAN_BASE_URL", "default": "",     "desc": "Jeevan Pramaan API base URL"},
            {"name": "JEEVAN_PRAMAAN_API_KEY",  "default": "",     "desc": "Bearer / API key"},
        ],
        "route_prefix": "/integrations/jeevan-pramaan",
        "endpoints": [
            {"method": "POST", "path": "/integrations/jeevan-pramaan/dlc/generate"},
            {"method": "GET",  "path": "/integrations/jeevan-pramaan/dlc/{pramaan_id}"},
        ],
        "file_templates": {
            "python": ("app/integrations/jeevan_pramaan/router.py", JEEVAN_PRAMAAN_PYTHON),
            "java":   ("src/main/java/com/lama/integrations/JeevanPramaanController.java", _stub_template("Java")),
            "nodejs": ("src/integrations/jeevan_pramaan/router.ts", _stub_template("TypeScript")),
        },
    },
    {
        "id": "audit_trail_logger",
        "label": "Audit-Trail Logger (ELK-ready)",
        "category": "Observability",
        "family": "utility",
        "icon": "scroll-text",
        "kind": "middleware",
        "description": (
            "Aspect-style request/response audit-trail injected into every "
            "@RestController (Java) / FastAPI route / Express handler. Captures "
            "request, response, client IP, class, handler, request_time, "
            "response_time and duration in seconds; emits ELK-friendly "
            "single-line JSON (Filebeat / Fluent Bit → Logstash → Elasticsearch). "
            "Optional best-effort HTTP push to a Logstash input. PII fields and "
            "regex patterns (PAN, Aadhaar, card, email, OTP, token, password, ...) "
            "are masked with `****`. Zero-overhead `off` mode for prod tuning."
        ),
        "providers": ["stdout (Filebeat/Fluent Bit)", "Logstash HTTP input", "Elasticsearch"],
        "gate_env": "AUDIT_TRAIL_MODE",
        "env_vars": [
            {"name": "AUDIT_TRAIL_MODE",            "default": "mock", "desc": "off | mock | live (live also POSTs to AUDIT_TRAIL_LOGSTASH_URL)"},
            {"name": "AUDIT_TRAIL_LOG_FORMAT",      "default": "json", "desc": "json (ELK-friendly, default) | text"},
            {"name": "AUDIT_TRAIL_LOG_LEVEL",       "default": "INFO", "desc": "Logger level used by the aspect/middleware"},
            {"name": "AUDIT_TRAIL_LOG_BODY",        "default": "0",    "desc": "1 to capture request/response body (PII-masked); 0 disables (default)"},
            {"name": "AUDIT_TRAIL_MAX_BODY_BYTES",  "default": "2048", "desc": "Hard cap on captured body length per side"},
            {"name": "AUDIT_TRAIL_EXCLUDE_PATHS",   "default": "/health,/metrics,/docs,/openapi.json", "desc": "Comma-sep exact paths to skip"},
            {"name": "AUDIT_TRAIL_PII_FIELDS",      "default": "password,pwd,pan,aadhaar,otp,token,authorization,api_key,secret,ssn,email,mobile,phone,card,cvv,pin,session,cookie", "desc": "Comma-sep field names (case-insensitive) masked with ****"},
            {"name": "AUDIT_TRAIL_PII_PATTERNS",    "default": "[A-Z]{5}[0-9]{4}[A-Z],\\\\b[2-9][0-9]{11}\\\\b,\\\\b[0-9]{12,19}\\\\b,\\\\b[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+\\\\b", "desc": "Comma-sep regexes whose matches get replaced with ****"},
            {"name": "AUDIT_TRAIL_SERVICE_NAME",    "default": "",     "desc": "Logical service name written into every record (defaults to the generated service)"},
            {"name": "AUDIT_TRAIL_LOGSTASH_URL",    "default": "",     "desc": "Optional HTTP URL of a Logstash http input (only used when MODE=live)"},
        ],
        "route_prefix": "/_audit",          # unused — kept for template parity
        "endpoints": [],                    # middleware, not a router
        "middleware": {
            # Symbol names exported by each language template — must match the
            # `class AuditTrailMiddleware` / `export const auditTrailMiddleware`
            # in audit_logger_templates.py.
            "python_class": "AuditTrailMiddleware",
            "node_export":  "auditTrailMiddleware",
        },
        "file_templates": {
            "python":  ("app/integrations/audit_trail_logger/middleware.py", AUDIT_LOG_PYTHON),
            "java":    ("src/main/java/com/lama/integrations/AuditTrailAspect.java", _stub_template("Java")),
            "nodejs":  ("src/integrations/audit_trail_logger/router.ts", _stub_template("TypeScript")),
        },
    },
]


# iter-13.120 — Non-govt platform integrations (Keycloak, Azure Blob, S3,
# Redis). Kept in a separate module to keep this file scoped to the
# original DPG catalog. They extend the same CATALOG list so the
# renderer + selection endpoints treat them identically.
from integrations.platform_catalog import PLATFORM_CATALOG  # noqa: E402
CATALOG.extend(PLATFORM_CATALOG)


def get_entry(integration_id: str) -> Dict[str, Any]:
    """Return the catalog entry or raise KeyError."""
    for e in CATALOG:
        if e["id"] == integration_id:
            return e
    raise KeyError(integration_id)


def catalog_for_ui() -> List[Dict[str, Any]]:
    """Catalog projection safe to ship to the UI (drops the code templates,
    which are large and not useful in the browser)."""
    out = []
    for e in CATALOG:
        out.append({k: v for k, v in e.items() if k != "file_templates"})
    return out

