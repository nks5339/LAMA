"""LAMA — DPG / DPI integration Python templates (iter-13.63).

Mock-first templates for Digital Public Goods / Digital Public Infrastructure
platforms — wired into the integrations catalog the same way as the Indian
govt-services entries (PAN/Aadhaar/...). Every template uses placeholders
filled in by `renderer.render_for_project`:
  {integration_id}, {label}, {gate_env}, {route_prefix}, {env_doc}.

Live-mode env vars per template are listed in the catalog entry. When the
required ones are unset in live mode, the endpoint returns a structured
error instead of failing — same contract as the rest of the catalog.

What lands here:
  - eSignet      (MOSIP OIDC)        → /authorize, /token, /userinfo
  - Inji         (MOSIP wallet/VC)   → /credentials/issue, /credentials/verify
  - MOSIP Auth   (national auth)     → /auth (yes/no), /ekyc
  - DIVOC        (open VC platform)  → /issue, /verify
  - Sunbird RC   (registry + creds)  → /registry/{schema}, /credentials,
                                       /registry/{schema}/{osid}
  - OpenG2P      (G2P payments)      → /beneficiary/{id},
                                       /disbursement/initiate,
                                       /disbursement/{id}/status
"""

# ---------------------------------------------------------------------------
# eSignet (MOSIP) — OIDC-compliant authentication
# ---------------------------------------------------------------------------

ESIGNET_PYTHON_BODY = '''

class EsignetAuthorizeResponse(BaseModel):
    authorize_url: str
    state: str
    nonce: str
    request_id: str = ""
    mode: str = "mock"


class EsignetTokenRequest(BaseModel):
    code: str = Field(..., description="OIDC authorization code returned to redirect_uri")
    code_verifier: Optional[str] = Field(None, description="PKCE code_verifier (optional)")


class EsignetTokenResponse(BaseModel):
    access_token: str = ""
    id_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class EsignetUserinfoResponse(BaseModel):
    sub: str = ""
    name: str = ""
    given_name: str = ""
    family_name: str = ""
    email: str = ""
    phone_number: str = ""
    picture_uri: str = ""
    raw: Dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.get("/authorize", response_model=EsignetAuthorizeResponse)
async def esignet_authorize():
    """Step 1 — return the eSignet OIDC authorize URL the user is redirected to."""
    request_id = uuid.uuid4().hex[:12]
    state = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    if _mode() == "mock":
        return EsignetAuthorizeResponse(
            authorize_url=f"https://mock.lama.local/esignet/authorize?state={{state}}&nonce={{nonce}}",
            state=state, nonce=nonce, request_id=request_id, mode="mock",
        )
    base = os.getenv("ESIGNET_BASE_URL", "")
    client_id = os.getenv("ESIGNET_CLIENT_ID", "")
    redirect_uri = os.getenv("ESIGNET_REDIRECT_URI", "")
    scope = os.getenv("ESIGNET_SCOPE", "openid profile email")
    if not (base and client_id and redirect_uri):
        raise HTTPException(500, "ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_REDIRECT_URI unset")
    authorize = (
        base.rstrip("/") + "/authorize"
        f"?response_type=code&client_id={{client_id}}&redirect_uri={{redirect_uri}}"
        f"&scope={{scope.replace(' ', '+')}}&state={{state}}&nonce={{nonce}}"
        "&acr_values=mosip:idp:acr:generated-code"
    )
    return EsignetAuthorizeResponse(authorize_url=authorize, state=state, nonce=nonce,
                                    request_id=request_id, mode="live")


@router.post("/token", response_model=EsignetTokenResponse)
async def esignet_token(payload: EsignetTokenRequest):
    """Step 2 — exchange the OIDC auth code for access_token + id_token."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return EsignetTokenResponse(
            access_token=f"mock.access.{{request_id}}", id_token=f"mock.id.{{request_id}}",
            expires_in=3600, scope="openid profile email",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("ESIGNET_BASE_URL", "")
    client_id = os.getenv("ESIGNET_CLIENT_ID", "")
    client_secret = os.getenv("ESIGNET_CLIENT_SECRET", "")
    redirect_uri = os.getenv("ESIGNET_REDIRECT_URI", "")
    if not (base and client_id and client_secret and redirect_uri):
        return EsignetTokenResponse(
            error="ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_CLIENT_SECRET / ESIGNET_REDIRECT_URI unset",
            request_id=request_id, mode="live",
        )
    form = {{
        "grant_type": "authorization_code", "code": payload.code,
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }}
    if payload.code_verifier:
        form["code_verifier"] = payload.code_verifier
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/token", data=form,
                              headers={{"Content-Type": "application/x-www-form-urlencoded"}})
    data = r.json() if r.content else {{}}
    return EsignetTokenResponse(
        access_token=data.get("access_token", ""), id_token=data.get("id_token", ""),
        token_type=data.get("token_type", "Bearer"),
        expires_in=int(data.get("expires_in", 0) or 0),
        scope=data.get("scope", ""), request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/userinfo", response_model=EsignetUserinfoResponse)
async def esignet_userinfo(access_token: str):
    """Step 3 — fetch the authenticated user's claims."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return EsignetUserinfoResponse(
            sub="mock-sub-" + request_id, name="Ramesh Kumar",
            given_name="Ramesh", family_name="Kumar",
            email="ramesh@example.test", phone_number="+91XXXXX12345",
            request_id=request_id, mode="mock",
            raw={{"acr": "mosip:idp:acr:generated-code"}},
        )
    base = os.getenv("ESIGNET_BASE_URL", "")
    if not base:
        return EsignetUserinfoResponse(
            error="ESIGNET_BASE_URL unset", request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + "/userinfo",
                             headers={{"Authorization": f"Bearer {{access_token}}"}})
    if r.status_code != 200:
        return EsignetUserinfoResponse(error=f"Provider HTTP {{r.status_code}}",
                                       request_id=request_id, mode="live")
    data = r.json() if r.content else {{}}
    return EsignetUserinfoResponse(
        sub=data.get("sub", ""), name=data.get("name", ""),
        given_name=data.get("given_name", ""), family_name=data.get("family_name", ""),
        email=data.get("email", ""), phone_number=data.get("phone_number", ""),
        picture_uri=data.get("picture", ""),
        raw=data, request_id=request_id, mode="live",
    )
'''


# ---------------------------------------------------------------------------
# Inji (MOSIP) — Verifiable Credential wallet + verifier
# ---------------------------------------------------------------------------

INJI_PYTHON_BODY = '''

class InjiIssueRequest(BaseModel):
    holder_id: str = Field(..., description="Wallet holder DID / phone / email")
    credential_type: str = Field("ProofOfIdentityCredential")
    claims: Dict[str, Any] = Field(default_factory=dict)


class InjiIssueResponse(BaseModel):
    issued: bool
    credential_id: str = ""
    credential_jwt: str = ""
    push_status: str = ""              # PUSHED | PENDING | FAILED
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class InjiVerifyRequest(BaseModel):
    credential_jwt: str = Field(..., description="VC (or VP) JWT presented by the holder")


class InjiVerifyResponse(BaseModel):
    valid: bool
    issuer: str = ""
    credential_type: str = ""
    subject_claims: Dict[str, Any] = Field(default_factory=dict)
    expires_at: str = ""
    revoked: bool = False
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/credentials/issue", response_model=InjiIssueResponse)
async def inji_issue(payload: InjiIssueRequest):
    """Issuer side — sign a VC for `holder_id` and push it to their Inji wallet."""
    request_id = uuid.uuid4().hex[:12]
    holder_hash = _hash_pii(payload.holder_id)
    if _mode() == "mock":
        return InjiIssueResponse(
            issued=True, credential_id="vc:mock:" + request_id,
            credential_jwt="eyJtb2NrIjoidmMifQ." + request_id + ".sig",
            push_status="PUSHED", request_id=request_id, mode="mock",
        )
    base = os.getenv("INJI_ISSUER_BASE_URL", "")
    key = os.getenv("INJI_ISSUER_API_KEY", "")
    issuer_id = os.getenv("INJI_ISSUER_ID", "")
    if not (base and key and issuer_id):
        return InjiIssueResponse(
            issued=False, push_status="FAILED",
            error="INJI_ISSUER_BASE_URL / INJI_ISSUER_API_KEY / INJI_ISSUER_ID unset",
            request_id=request_id, mode="live",
        )
    body = {{"issuer": issuer_id, "holder": payload.holder_id,
            "credentialType": payload.credential_type, "claims": payload.claims}}
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/credentials/issue", json=body,
                              headers={{"Authorization": f"Bearer {{key}}"}})
    data = r.json() if r.content else {{}}
    logger.info("inji.issue holder_hash=%s status=%s", holder_hash, r.status_code)
    return InjiIssueResponse(
        issued=(r.status_code in (200, 201)),
        credential_id=data.get("credential_id", ""),
        credential_jwt=data.get("credential", ""),
        push_status=data.get("push_status", "PENDING"),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/credentials/verify", response_model=InjiVerifyResponse)
async def inji_verify(payload: InjiVerifyRequest):
    """Verifier side — validate a VC/VP JWT presented by a holder."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return InjiVerifyResponse(
            valid=True, issuer="did:web:mock-issuer.lama.local",
            credential_type="ProofOfIdentityCredential",
            subject_claims={{"name": "Ramesh Kumar", "dob": "1985-04-21"}},
            expires_at="2030-12-31T23:59:59Z",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("INJI_VERIFIER_BASE_URL", "")
    key = os.getenv("INJI_VERIFIER_API_KEY", "")
    if not (base and key):
        return InjiVerifyResponse(
            valid=False, error="INJI_VERIFIER_BASE_URL / INJI_VERIFIER_API_KEY unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/credentials/verify",
                              json={{"credential": payload.credential_jwt}},
                              headers={{"Authorization": f"Bearer {{key}}"}})
    data = r.json() if r.content else {{}}
    return InjiVerifyResponse(
        valid=(r.status_code == 200 and bool(data.get("valid"))),
        issuer=data.get("issuer", ""), credential_type=data.get("credentialType", ""),
        subject_claims=data.get("credentialSubject", {{}}),
        expires_at=data.get("expiresAt", ""), revoked=bool(data.get("revoked", False)),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# MOSIP Auth — yes/no + e-KYC
# ---------------------------------------------------------------------------

MOSIP_AUTH_PYTHON_BODY = '''

class MosipAuthRequest(BaseModel):
    uin_or_vid: str = Field(..., description="UIN or VID issued by the MOSIP-based national ID")
    auth_type: str = Field("OTP", description="OTP | DEMO | BIO")
    otp: Optional[str] = None
    demographics: Optional[Dict[str, Any]] = None
    biometrics_b64: Optional[str] = None


class MosipAuthResponse(BaseModel):
    authenticated: bool
    auth_type: str
    identity_masked: str = ""
    transaction_id: str = ""
    response_code: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class MosipEkycRequest(BaseModel):
    uin_or_vid: str
    otp: str
    transaction_id: str


class MosipEkycResponse(BaseModel):
    success: bool
    name: str = ""
    dob: str = ""
    gender: str = ""
    address_masked: str = ""
    identity_masked: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/auth", response_model=MosipAuthResponse)
async def mosip_auth(payload: MosipAuthRequest):
    """yes/no auth against a MOSIP-based national ID (UIN/VID)."""
    request_id = uuid.uuid4().hex[:12]
    masked = _mask_id(payload.uin_or_vid, 4)
    if _mode() == "mock":
        ok = True
        if payload.auth_type.upper() == "OTP":
            ok = (payload.otp == "123456")
        return MosipAuthResponse(
            authenticated=ok, auth_type=payload.auth_type.upper(),
            identity_masked=masked, transaction_id="mock-txn-" + request_id,
            response_code="OK" if ok else "AUTH_FAILED",
            request_id=request_id, mode="mock",
            error="" if ok else "Mock OTP mismatch (expected 123456)",
        )
    base = os.getenv("MOSIP_AUTH_BASE_URL", "")
    partner_id = os.getenv("MOSIP_PARTNER_ID", "")
    partner_key = os.getenv("MOSIP_PARTNER_API_KEY", "")
    if not (base and partner_id and partner_key):
        return MosipAuthResponse(
            authenticated=False, auth_type=payload.auth_type.upper(),
            error="MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset",
            identity_masked=masked, request_id=request_id, mode="live",
        )
    body = {{
        "id": "mosip.identity.auth", "version": "1.0",
        "requestTime": datetime.now(timezone.utc).isoformat(),
        "individualId": payload.uin_or_vid,
        "individualIdType": "VID" if len(payload.uin_or_vid) == 16 else "UIN",
        "authType": {{"otp": payload.auth_type.upper() == "OTP",
                      "demo": payload.auth_type.upper() == "DEMO",
                      "bio":  payload.auth_type.upper() == "BIO"}},
        "request": {{"otp": payload.otp or "",
                     "demographics": payload.demographics or {{}},
                     "biometrics": payload.biometrics_b64 or ""}},
    }}
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/idauthentication/v1/auth/" + partner_id,
                              json=body,
                              headers={{"Authorization": f"Bearer {{partner_key}}"}})
    data = r.json() if r.content else {{}}
    auth_ok = bool((data.get("response") or {{}}).get("authStatus"))
    return MosipAuthResponse(
        authenticated=auth_ok, auth_type=payload.auth_type.upper(),
        identity_masked=masked, transaction_id=data.get("transactionID", ""),
        response_code=(data.get("response") or {{}}).get("authToken", "") or "OK",
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/ekyc", response_model=MosipEkycResponse)
async def mosip_ekyc(payload: MosipEkycRequest):
    """Pull e-KYC demographics after a successful OTP-auth."""
    request_id = uuid.uuid4().hex[:12]
    masked = _mask_id(payload.uin_or_vid, 4)
    if _mode() == "mock":
        ok = (payload.otp == "123456")
        return MosipEkycResponse(
            success=ok, name="RAMESH KUMAR" if ok else "",
            dob="1985-04-21" if ok else "", gender="M" if ok else "",
            address_masked="XX, MG Road, Bengaluru" if ok else "",
            identity_masked=masked, request_id=request_id, mode="mock",
            error="" if ok else "Mock OTP mismatch (expected 123456)",
        )
    base = os.getenv("MOSIP_AUTH_BASE_URL", "")
    partner_id = os.getenv("MOSIP_PARTNER_ID", "")
    partner_key = os.getenv("MOSIP_PARTNER_API_KEY", "")
    if not (base and partner_id and partner_key):
        return MosipEkycResponse(
            success=False, identity_masked=masked,
            error="MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/idauthentication/v1/kyc/" + partner_id,
                              json={{"individualId": payload.uin_or_vid, "otp": payload.otp,
                                     "transactionID": payload.transaction_id}},
                              headers={{"Authorization": f"Bearer {{partner_key}}"}})
    data = r.json() if r.content else {{}}
    body = (data.get("response") or {{}}).get("identity") or {{}}
    return MosipEkycResponse(
        success=(r.status_code == 200), name=body.get("name", ""),
        dob=body.get("dateOfBirth", ""), gender=body.get("gender", ""),
        address_masked=body.get("addressLine1", ""),
        identity_masked=masked, request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# DIVOC — open verifiable credentials
# ---------------------------------------------------------------------------

DIVOC_PYTHON_BODY = '''

class DivocIssueRequest(BaseModel):
    entity: str = Field(..., description="Registry entity name (e.g. VaccinationCertificate)")
    payload: Dict[str, Any]


class DivocIssueResponse(BaseModel):
    issued: bool
    certificate_id: str = ""
    osid: str = ""
    qr_url: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class DivocVerifyRequest(BaseModel):
    signed_credential: str = Field(..., description="Signed JSON-LD VC or JWT form")


class DivocVerifyResponse(BaseModel):
    valid: bool
    entity: str = ""
    issuer: str = ""
    issued_to: str = ""
    revoked: bool = False
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/issue", response_model=DivocIssueResponse)
async def divoc_issue(payload: DivocIssueRequest):
    """Issue an open verifiable credential through DIVOC."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return DivocIssueResponse(
            issued=True, certificate_id="cert:mock:" + request_id,
            osid="1-" + request_id,
            qr_url=f"https://mock.lama.local/divoc/verify/{{request_id}}",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("DIVOC_BASE_URL", "")
    token = os.getenv("DIVOC_ISSUER_TOKEN", "")
    if not (base and token):
        return DivocIssueResponse(
            issued=False, error="DIVOC_BASE_URL / DIVOC_ISSUER_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/" + payload.entity,
                              json=payload.payload,
                              headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return DivocIssueResponse(
        issued=(r.status_code in (200, 201)),
        certificate_id=data.get("certificateId", ""),
        osid=data.get("osid", ""), qr_url=data.get("verifyUrl", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/verify", response_model=DivocVerifyResponse)
async def divoc_verify(payload: DivocVerifyRequest):
    """Verify a signed DIVOC credential (JSON-LD VC or JWT)."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return DivocVerifyResponse(
            valid=True, entity="VaccinationCertificate",
            issuer="did:web:mock-issuer.lama.local",
            issued_to="Ramesh Kumar", request_id=request_id, mode="mock",
        )
    base = os.getenv("DIVOC_VERIFY_URL", "")
    if not base:
        return DivocVerifyResponse(
            valid=False, error="DIVOC_VERIFY_URL unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base, json={{"signedCredential": payload.signed_credential}})
    data = r.json() if r.content else {{}}
    return DivocVerifyResponse(
        valid=(r.status_code == 200 and bool(data.get("verified"))),
        entity=data.get("entity", ""), issuer=data.get("issuer", ""),
        issued_to=data.get("issuedTo", ""), revoked=bool(data.get("revoked", False)),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# Sunbird RC — schema-driven registry + credentialing
# ---------------------------------------------------------------------------

SUNBIRD_RC_PYTHON_BODY = '''

class SunbirdRegisterRequest(BaseModel):
    entity: Dict[str, Any]


class SunbirdRegisterResponse(BaseModel):
    success: bool
    osid: str = ""
    schema_name: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class SunbirdEntityResponse(BaseModel):
    osid: str
    schema_name: str
    entity: Dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class SunbirdCredentialRequest(BaseModel):
    schema_name: str
    osid: str


class SunbirdCredentialResponse(BaseModel):
    issued: bool
    credential_id: str = ""
    credential_jwt: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/registry/{{schema}}", response_model=SunbirdRegisterResponse)
async def sunbird_register(schema: str, payload: SunbirdRegisterRequest):
    """Register an entity under a Sunbird RC schema."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return SunbirdRegisterResponse(
            success=True, osid="1-mock-" + request_id, schema_name=schema,
            request_id=request_id, mode="mock",
        )
    base = os.getenv("SUNBIRD_RC_BASE_URL", "")
    token = os.getenv("SUNBIRD_RC_TOKEN", "")
    if not (base and token):
        return SunbirdRegisterResponse(
            success=False, schema_name=schema,
            error="SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + f"/api/v1/{{schema}}",
                              json=payload.entity,
                              headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return SunbirdRegisterResponse(
        success=(r.status_code in (200, 201)),
        osid=(data.get("result") or {{}}).get((schema or "").lower(), {{}}).get("osid", ""),
        schema_name=schema, request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/registry/{{schema}}/{{osid}}", response_model=SunbirdEntityResponse)
async def sunbird_get_entity(schema: str, osid: str):
    """Fetch a single entity by Open Studio id."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return SunbirdEntityResponse(
            osid=osid, schema_name=schema,
            entity={{"osid": osid, "name": "Mock Entity"}},
            request_id=request_id, mode="mock",
        )
    base = os.getenv("SUNBIRD_RC_BASE_URL", "")
    token = os.getenv("SUNBIRD_RC_TOKEN", "")
    if not (base and token):
        return SunbirdEntityResponse(
            osid=osid, schema_name=schema,
            error="SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/api/v1/{{schema}}/{{osid}}",
                             headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return SunbirdEntityResponse(
        osid=osid, schema_name=schema, entity=data,
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/credentials", response_model=SunbirdCredentialResponse)
async def sunbird_issue_credential(payload: SunbirdCredentialRequest):
    """Issue a verifiable credential about an existing registry entity."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return SunbirdCredentialResponse(
            issued=True, credential_id="vc:mock:" + request_id,
            credential_jwt="eyJtb2NrIjoidmMifQ." + request_id + ".sig",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("SUNBIRD_VC_BASE_URL", "")
    token = os.getenv("SUNBIRD_RC_TOKEN", "")
    if not (base and token):
        return SunbirdCredentialResponse(
            issued=False, error="SUNBIRD_VC_BASE_URL / SUNBIRD_RC_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/credentials",
                              json={{"schema": payload.schema_name, "osid": payload.osid}},
                              headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return SunbirdCredentialResponse(
        issued=(r.status_code in (200, 201)),
        credential_id=data.get("id", ""), credential_jwt=data.get("credential", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# OpenG2P — Government-to-Person payments
# ---------------------------------------------------------------------------

OPENG2P_PYTHON_BODY = '''

class OpenG2PBeneficiaryResponse(BaseModel):
    found: bool
    beneficiary_id: str = ""
    name_masked: str = ""
    bank_account_masked: str = ""
    program_ids: list = Field(default_factory=list)
    kyc_status: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class OpenG2PDisburseRequest(BaseModel):
    beneficiary_id: str
    amount_rupees: float = Field(..., gt=0, le=10000000)
    program_id: str
    payment_reference: Optional[str] = None


class OpenG2PDisburseResponse(BaseModel):
    accepted: bool
    disbursement_id: str = ""
    status: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class OpenG2PStatusResponse(BaseModel):
    disbursement_id: str
    status: str
    amount_rupees: float = 0.0
    settled_at: str = ""
    bank_ref: str = ""
    mode: str = "mock"
    error: str = ""


@router.get("/beneficiary/{{beneficiary_id}}", response_model=OpenG2PBeneficiaryResponse)
async def openg2p_beneficiary(beneficiary_id: str):
    """Fetch a registered G2P beneficiary record."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return OpenG2PBeneficiaryResponse(
            found=True, beneficiary_id=beneficiary_id,
            name_masked="RAMESH K****", bank_account_masked="****1234",
            program_ids=["PM-KISAN", "NSAP"], kyc_status="VERIFIED",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("OPENG2P_BASE_URL", "")
    token = os.getenv("OPENG2P_PARTNER_TOKEN", "")
    if not (base and token):
        return OpenG2PBeneficiaryResponse(
            found=False, beneficiary_id=beneficiary_id,
            error="OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/api/v1/beneficiary/{{beneficiary_id}}",
                             headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return OpenG2PBeneficiaryResponse(
        found=(r.status_code == 200), beneficiary_id=beneficiary_id,
        name_masked=data.get("nameMasked", ""),
        bank_account_masked=data.get("bankAccountMasked", ""),
        program_ids=data.get("programIds", []),
        kyc_status=data.get("kycStatus", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/disbursement/initiate", response_model=OpenG2PDisburseResponse)
async def openg2p_disburse(payload: OpenG2PDisburseRequest):
    """Kick off a G2P disbursement to a registered beneficiary."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return OpenG2PDisburseResponse(
            accepted=True, disbursement_id="g2p-mock-" + request_id,
            status="ACCEPTED", request_id=request_id, mode="mock",
        )
    base = os.getenv("OPENG2P_BASE_URL", "")
    token = os.getenv("OPENG2P_PARTNER_TOKEN", "")
    if not (base and token):
        return OpenG2PDisburseResponse(
            accepted=False, status="FAILED",
            error="OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/disbursement", json={{
            "beneficiaryId": payload.beneficiary_id,
            "amount": int(round(payload.amount_rupees * 100)),
            "currency": "INR", "programId": payload.program_id,
            "paymentReference": payload.payment_reference or "",
        }}, headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return OpenG2PDisburseResponse(
        accepted=(r.status_code in (200, 201, 202)),
        disbursement_id=data.get("disbursementId", ""),
        status=data.get("status", "INITIATED"),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201, 202) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/disbursement/{{disbursement_id}}/status", response_model=OpenG2PStatusResponse)
async def openg2p_disburse_status(disbursement_id: str):
    """Poll a previously-initiated G2P disbursement."""
    if _mode() == "mock":
        return OpenG2PStatusResponse(
            disbursement_id=disbursement_id, status="SUCCESS",
            amount_rupees=2000.0, settled_at=datetime.now(timezone.utc).isoformat(),
            bank_ref="UTR" + disbursement_id[:10].upper(), mode="mock",
        )
    base = os.getenv("OPENG2P_BASE_URL", "")
    token = os.getenv("OPENG2P_PARTNER_TOKEN", "")
    if not (base and token):
        return OpenG2PStatusResponse(
            disbursement_id=disbursement_id, status="PENDING",
            error="OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset", mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/api/v1/disbursement/{{disbursement_id}}",
                             headers={{"Authorization": f"Bearer {{token}}"}})
    data = r.json() if r.content else {{}}
    return OpenG2PStatusResponse(
        disbursement_id=disbursement_id,
        status=(data.get("status") or "PENDING").upper(),
        amount_rupees=(data.get("amount", 0) or 0) / 100.0,
        settled_at=data.get("settledAt", ""), bank_ref=data.get("bankRef", ""),
        mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''

