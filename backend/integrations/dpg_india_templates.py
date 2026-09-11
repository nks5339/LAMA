"""LAMA — Indian DPG/DPI integration Python templates (iter-13.64).

India-specific Digital Public Infrastructure integrations that follow the
same mock-first / `<GATE>_MODE` / env-driven contract as the rest of the
integrations catalog. Composed with the shared `PY_COMMON_HEADER` block
declared in catalog.py.

What ships here:
  - Bharatkosh (NTRP)      → /challan/create, /challan/{id}
  - PFMS                   → /beneficiary/validate, /payment/instruction
  - API Setu               → /catalog, /invoke/{api_id}
  - Bhashini               → /translate, /transliterate, /tts
  - ABDM (Ayushman Bharat) → /abha/create, /abha/{id}, /hpr/verify
  - Jeevan Pramaan         → /dlc/generate, /dlc/{pramaan_id}

Each template's mock branch returns realistic canned responses; the live
branch reads the env vars listed in the corresponding CATALOG entry and
refuses to call out if a required one is unset (returns a structured
error instead of failing). This is the exact contract every integration
in this codebase follows.
"""

# ---------------------------------------------------------------------------
# Bharatkosh (NTRP) — Non-Tax Receipt Portal
# ---------------------------------------------------------------------------

BHARATKOSH_PYTHON_BODY = '''

class BharatkoshChallanRequest(BaseModel):
    purpose_code: str = Field(..., description="NTRP purpose code (e.g. 0070 — Other Admin Services)")
    amount_rupees: float = Field(..., gt=0, le=100000000)
    payer_name: str = Field(..., max_length=200)
    payer_pan: Optional[str] = Field(None, description="PAN of the payer (optional)")
    purpose_description: str = Field("", max_length=500)
    reference_no: Optional[str] = Field(None, description="Your idempotency / order reference")


class BharatkoshChallanResponse(BaseModel):
    created: bool
    challan_id: str = ""
    pay_url: str = ""               # NTRP-hosted payment page URL
    expires_in_seconds: int = 0
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class BharatkoshChallanStatusResponse(BaseModel):
    challan_id: str
    status: str   # CREATED | PAID | EXPIRED | CANCELLED | FAILED
    amount_rupees: float = 0.0
    paid_at: str = ""
    bank_ref: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/challan/create", response_model=BharatkoshChallanResponse)
async def bharatkosh_create_challan(payload: BharatkoshChallanRequest):
    """Create a Bharatkosh / NTRP challan for a non-tax govt receipt."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        cid = "NTRP-MOCK-" + request_id.upper()
        return BharatkoshChallanResponse(
            created=True, challan_id=cid,
            pay_url=f"https://mock.lama.local/bharatkosh/pay/{{cid}}",
            expires_in_seconds=900,
            request_id=request_id, mode="mock",
        )
    base = os.getenv("BHARATKOSH_BASE_URL", "")
    dept_code = os.getenv("BHARATKOSH_DEPT_CODE", "")
    api_key = os.getenv("BHARATKOSH_API_KEY", "")
    if not (base and dept_code and api_key):
        return BharatkoshChallanResponse(
            created=False,
            error="BHARATKOSH_BASE_URL / BHARATKOSH_DEPT_CODE / BHARATKOSH_API_KEY unset",
            request_id=request_id, mode="live",
        )
    body = {{
        "deptCode": dept_code,
        "purposeCode": payload.purpose_code,
        "amount": int(round(payload.amount_rupees * 100)),
        "payerName": payload.payer_name,
        "payerPAN": payload.payer_pan or "",
        "purposeDescription": payload.purpose_description,
        "referenceNo": payload.reference_no or "",
    }}
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/challan/create", json=body,
                              headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return BharatkoshChallanResponse(
        created=(r.status_code in (200, 201)),
        challan_id=data.get("challanId", ""),
        pay_url=data.get("paymentUrl", ""),
        expires_in_seconds=int(data.get("expiresInSeconds", 0) or 0),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/challan/{{challan_id}}", response_model=BharatkoshChallanStatusResponse)
async def bharatkosh_challan_status(challan_id: str):
    """Poll the payment status of a previously-created Bharatkosh challan."""
    if _mode() == "mock":
        return BharatkoshChallanStatusResponse(
            challan_id=challan_id, status="PAID", amount_rupees=1500.0,
            paid_at=datetime.now(timezone.utc).isoformat(),
            bank_ref="NTRPREF" + challan_id[-8:].upper(),
            mode="mock",
        )
    base = os.getenv("BHARATKOSH_BASE_URL", "")
    api_key = os.getenv("BHARATKOSH_API_KEY", "")
    if not (base and api_key):
        return BharatkoshChallanStatusResponse(
            challan_id=challan_id, status="PENDING",
            error="BHARATKOSH_BASE_URL / BHARATKOSH_API_KEY unset", mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/api/v1/challan/{{challan_id}}",
                             headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return BharatkoshChallanStatusResponse(
        challan_id=challan_id, status=(data.get("status") or "PENDING").upper(),
        amount_rupees=(data.get("amount", 0) or 0) / 100.0,
        paid_at=data.get("paidAt", ""), bank_ref=data.get("bankRef", ""),
        mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# PFMS — Public Financial Management System
# ---------------------------------------------------------------------------

PFMS_PYTHON_BODY = '''

class PfmsBeneficiaryValidateRequest(BaseModel):
    beneficiary_id: str = Field(..., description="PFMS beneficiary id / Aadhaar-linked id")
    bank_account: str = Field(..., min_length=6, max_length=20)
    ifsc: str = Field(..., min_length=11, max_length=11)
    name: Optional[str] = None


class PfmsBeneficiaryValidateResponse(BaseModel):
    valid: bool
    beneficiary_id: str
    account_holder_match: Optional[bool] = None
    bank_name: str = ""
    branch: str = ""
    bank_account_masked: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class PfmsPaymentRequest(BaseModel):
    beneficiary_id: str
    amount_rupees: float = Field(..., gt=0, le=10000000)
    program_code: str = Field(..., description="PFMS programme/scheme code")
    payment_purpose: str = Field("", max_length=200)
    payment_reference: Optional[str] = None


class PfmsPaymentResponse(BaseModel):
    accepted: bool
    payment_id: str = ""
    status: str = ""
    bank_ref: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/beneficiary/validate", response_model=PfmsBeneficiaryValidateResponse)
async def pfms_beneficiary_validate(payload: PfmsBeneficiaryValidateRequest):
    """Validate a beneficiary's bank account against PFMS (penny-drop style)."""
    request_id = uuid.uuid4().hex[:12]
    masked = _mask_id(payload.bank_account, 4)
    if _mode() == "mock":
        return PfmsBeneficiaryValidateResponse(
            valid=True, beneficiary_id=payload.beneficiary_id,
            account_holder_match=(payload.name is None or len(payload.name) > 1),
            bank_name="STATE BANK OF INDIA", branch="MG ROAD",
            bank_account_masked=masked,
            request_id=request_id, mode="mock",
        )
    base = os.getenv("PFMS_BASE_URL", "")
    agency = os.getenv("PFMS_AGENCY_CODE", "")
    api_key = os.getenv("PFMS_API_KEY", "")
    if not (base and agency and api_key):
        return PfmsBeneficiaryValidateResponse(
            valid=False, beneficiary_id=payload.beneficiary_id,
            bank_account_masked=masked,
            error="PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset",
            request_id=request_id, mode="live",
        )
    body = {{
        "agencyCode": agency, "beneficiaryId": payload.beneficiary_id,
        "bankAccount": payload.bank_account, "ifsc": payload.ifsc,
        "name": payload.name or "",
    }}
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/beneficiary/validate", json=body,
                              headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return PfmsBeneficiaryValidateResponse(
        valid=(r.status_code == 200 and bool(data.get("valid"))),
        beneficiary_id=payload.beneficiary_id,
        account_holder_match=data.get("nameMatch"),
        bank_name=data.get("bankName", ""), branch=data.get("branch", ""),
        bank_account_masked=masked,
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/payment/instruction", response_model=PfmsPaymentResponse)
async def pfms_payment_instruction(payload: PfmsPaymentRequest):
    """Push a DBT-style payment instruction into PFMS."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return PfmsPaymentResponse(
            accepted=True, payment_id="PFMS-MOCK-" + request_id.upper(),
            status="ACCEPTED", bank_ref="UTR" + request_id[:10].upper(),
            request_id=request_id, mode="mock",
        )
    base = os.getenv("PFMS_BASE_URL", "")
    agency = os.getenv("PFMS_AGENCY_CODE", "")
    api_key = os.getenv("PFMS_API_KEY", "")
    if not (base and agency and api_key):
        return PfmsPaymentResponse(
            accepted=False, status="FAILED",
            error="PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/payment", json={{
            "agencyCode": agency,
            "beneficiaryId": payload.beneficiary_id,
            "amount": int(round(payload.amount_rupees * 100)),
            "programCode": payload.program_code,
            "purpose": payload.payment_purpose,
            "reference": payload.payment_reference or "",
        }}, headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return PfmsPaymentResponse(
        accepted=(r.status_code in (200, 201, 202)),
        payment_id=data.get("paymentId", ""),
        status=data.get("status", "INITIATED"),
        bank_ref=data.get("bankRef", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201, 202) else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# API Setu — government API aggregator
# ---------------------------------------------------------------------------

API_SETU_PYTHON_BODY = '''

class ApiSetuCatalogResponse(BaseModel):
    apis: list = Field(default_factory=list)
    total: int = 0
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class ApiSetuInvokeRequest(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict,
                                   description="Path / query / body params for the chosen API")
    method: str = Field("POST", description="GET | POST — defaults to POST for verify-style APIs")


class ApiSetuInvokeResponse(BaseModel):
    api_id: str
    status: int
    body: Any = None
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.get("/catalog", response_model=ApiSetuCatalogResponse)
async def api_setu_catalog():
    """List the API Setu APIs the configured client has access to."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        apis = [
            {{"id": "driving-license-verify", "department": "MORTH", "method": "POST"}},
            {{"id": "vehicle-rc-verify", "department": "MORTH", "method": "POST"}},
            {{"id": "voter-id-verify", "department": "ECI", "method": "POST"}},
            {{"id": "ration-card-verify", "department": "DFPD", "method": "POST"}},
        ]
        return ApiSetuCatalogResponse(apis=apis, total=len(apis),
                                      request_id=request_id, mode="mock")
    base = os.getenv("API_SETU_BASE_URL", "")
    client_id = os.getenv("API_SETU_CLIENT_ID", "")
    client_secret = os.getenv("API_SETU_CLIENT_SECRET", "")
    if not (base and client_id and client_secret):
        return ApiSetuCatalogResponse(
            error="API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + "/api/v1/apis",
                             headers={{"X-APISETU-CLIENTID": client_id,
                                      "X-APISETU-APIKEY": client_secret}})
    data = r.json() if r.content else {{}}
    apis = data.get("apis") or data.get("items") or []
    return ApiSetuCatalogResponse(apis=apis, total=len(apis),
                                  request_id=request_id, mode="live",
                                  error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}")


@router.post("/invoke/{{api_id}}", response_model=ApiSetuInvokeResponse)
async def api_setu_invoke(api_id: str, payload: ApiSetuInvokeRequest):
    """Invoke an API Setu-listed govt API by id, passing through `params`."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return ApiSetuInvokeResponse(
            api_id=api_id, status=200,
            body={{"verified": True, "name": "Ramesh Kumar",
                   "issued_on": "2020-01-15", "echo": payload.params}},
            request_id=request_id, mode="mock",
        )
    base = os.getenv("API_SETU_BASE_URL", "")
    client_id = os.getenv("API_SETU_CLIENT_ID", "")
    client_secret = os.getenv("API_SETU_CLIENT_SECRET", "")
    if not (base and client_id and client_secret):
        return ApiSetuInvokeResponse(
            api_id=api_id, status=0,
            error="API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset",
            request_id=request_id, mode="live",
        )
    url = base.rstrip("/") + f"/api/v1/invoke/{{api_id}}"
    headers = {{
        "X-APISETU-CLIENTID": client_id,
        "X-APISETU-APIKEY": client_secret,
        "Content-Type": "application/json",
    }}
    method = (payload.method or "POST").upper()
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        if method == "GET":
            r = await client.get(url, params=payload.params, headers=headers)
        else:
            r = await client.post(url, json=payload.params, headers=headers)
    body = None
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return ApiSetuInvokeResponse(
        api_id=api_id, status=r.status_code, body=body,
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# Bhashini — language translation / transliteration / TTS
# ---------------------------------------------------------------------------

BHASHINI_PYTHON_BODY = '''

class BhashiniTranslateRequest(BaseModel):
    source_lang: str = Field(..., description="ISO-639-1 code (e.g. en, hi, bn, ta)")
    target_lang: str = Field(..., description="ISO-639-1 code")
    text: str = Field(..., min_length=1, max_length=5000)


class BhashiniTranslateResponse(BaseModel):
    source_lang: str
    target_lang: str
    translated_text: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class BhashiniTransliterateRequest(BaseModel):
    source_script: str = Field(..., description="ISO-15924 source script (e.g. Latn, Deva)")
    target_script: str = Field(..., description="ISO-15924 target script")
    text: str = Field(..., min_length=1, max_length=5000)
    language: Optional[str] = None


class BhashiniTransliterateResponse(BaseModel):
    source_script: str
    target_script: str
    transliterated_text: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class BhashiniTtsRequest(BaseModel):
    language: str = Field(..., description="ISO-639-1 language code")
    text: str = Field(..., min_length=1, max_length=2000)
    voice: Optional[str] = Field("default", description="Provider voice id")


class BhashiniTtsResponse(BaseModel):
    language: str
    audio_base64: str = ""
    audio_format: str = "wav"
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


def _bhashini_creds():
    return (
        os.getenv("BHASHINI_BASE_URL", ""),
        os.getenv("BHASHINI_API_KEY", ""),
        os.getenv("BHASHINI_USER_ID", ""),
    )


@router.post("/translate", response_model=BhashiniTranslateResponse)
async def bhashini_translate(payload: BhashiniTranslateRequest):
    """Translate text between two Indian languages via Bhashini."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return BhashiniTranslateResponse(
            source_lang=payload.source_lang, target_lang=payload.target_lang,
            translated_text=f"[mock {{payload.source_lang}}→{{payload.target_lang}}] " + payload.text,
            request_id=request_id, mode="mock",
        )
    base, api_key, user_id = _bhashini_creds()
    if not (base and api_key and user_id):
        return BhashiniTranslateResponse(
            source_lang=payload.source_lang, target_lang=payload.target_lang,
            error="BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/translate", json={{
            "sourceLanguage": payload.source_lang,
            "targetLanguage": payload.target_lang, "text": payload.text,
        }}, headers={{"Authorization": api_key, "userID": user_id}})
    data = r.json() if r.content else {{}}
    return BhashiniTranslateResponse(
        source_lang=payload.source_lang, target_lang=payload.target_lang,
        translated_text=data.get("translatedText", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/transliterate", response_model=BhashiniTransliterateResponse)
async def bhashini_transliterate(payload: BhashiniTransliterateRequest):
    """Transliterate text between two scripts (e.g. Latn→Deva)."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return BhashiniTransliterateResponse(
            source_script=payload.source_script, target_script=payload.target_script,
            transliterated_text=f"[mock {{payload.source_script}}→{{payload.target_script}}] " + payload.text,
            request_id=request_id, mode="mock",
        )
    base, api_key, user_id = _bhashini_creds()
    if not (base and api_key and user_id):
        return BhashiniTransliterateResponse(
            source_script=payload.source_script, target_script=payload.target_script,
            error="BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/transliterate", json={{
            "sourceScript": payload.source_script,
            "targetScript": payload.target_script, "text": payload.text,
            "language": payload.language or "",
        }}, headers={{"Authorization": api_key, "userID": user_id}})
    data = r.json() if r.content else {{}}
    return BhashiniTransliterateResponse(
        source_script=payload.source_script, target_script=payload.target_script,
        transliterated_text=data.get("transliteratedText", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/tts", response_model=BhashiniTtsResponse)
async def bhashini_tts(payload: BhashiniTtsRequest):
    """Text-to-speech via Bhashini."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return BhashiniTtsResponse(
            language=payload.language,
            audio_base64="UklGRiYAAABXQVZFZm10IBAA",  # tiny WAV stub
            audio_format="wav", request_id=request_id, mode="mock",
        )
    base, api_key, user_id = _bhashini_creds()
    if not (base and api_key and user_id):
        return BhashiniTtsResponse(
            language=payload.language,
            error="BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=60, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/tts", json={{
            "language": payload.language, "text": payload.text,
            "voice": payload.voice or "default",
        }}, headers={{"Authorization": api_key, "userID": user_id}})
    data = r.json() if r.content else {{}}
    return BhashiniTtsResponse(
        language=payload.language,
        audio_base64=data.get("audio", ""),
        audio_format=data.get("format", "wav"),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# ABDM — Ayushman Bharat Digital Mission (Health Stack)
# ---------------------------------------------------------------------------

ABDM_PYTHON_BODY = '''

class AbdmAbhaCreateRequest(BaseModel):
    aadhaar_or_mobile: str = Field(..., description="Aadhaar (12 digits) or registered mobile (10 digits)")
    otp: str = Field(..., min_length=6, max_length=6)
    transaction_id: str = Field(..., description="Returned by the prior /otp/initiate step")
    health_id_preference: Optional[str] = Field(None, description="Desired ABHA address (e.g. ramesh@abdm)")


class AbdmAbhaCreateResponse(BaseModel):
    created: bool
    abha_number: str = ""           # 14-digit ABHA number, formatted
    abha_address: str = ""          # e.g. ramesh@abdm
    name: str = ""
    gender: str = ""
    dob: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class AbdmAbhaProfileResponse(BaseModel):
    abha_number: str
    abha_address: str = ""
    name: str = ""
    gender: str = ""
    dob: str = ""
    state: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class AbdmHprVerifyRequest(BaseModel):
    hpr_id: str = Field(..., description="Health Professional Registry id (e.g. dr.ramesh@hpr.abdm)")


class AbdmHprVerifyResponse(BaseModel):
    verified: bool
    hpr_id: str
    name: str = ""
    qualifications: list = Field(default_factory=list)
    registration_council: str = ""
    practice_status: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/abha/create", response_model=AbdmAbhaCreateResponse)
async def abdm_abha_create(payload: AbdmAbhaCreateRequest):
    """Create an ABHA (Ayushman Bharat Health Account) via Aadhaar OTP."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        ok = (payload.otp == "123456")
        return AbdmAbhaCreateResponse(
            created=ok,
            abha_number="91-1234-5678-9012" if ok else "",
            abha_address=(payload.health_id_preference or "ramesh@abdm") if ok else "",
            name="RAMESH KUMAR" if ok else "",
            gender="M" if ok else "", dob="1985-04-21" if ok else "",
            request_id=request_id, mode="mock",
            error="" if ok else "Mock OTP mismatch (expected 123456)",
        )
    base = os.getenv("ABDM_BASE_URL", "")
    client_id = os.getenv("ABDM_CLIENT_ID", "")
    client_secret = os.getenv("ABDM_CLIENT_SECRET", "")
    if not (base and client_id and client_secret):
        return AbdmAbhaCreateResponse(
            created=False,
            error="ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/v1/registration/aadhaar/createHealthIdWithPreVerified",
                              json={{"otp": payload.otp, "txnId": payload.transaction_id,
                                     "healthId": payload.health_id_preference or ""}},
                              headers={{"X-CM-ID": client_id,
                                        "Authorization": f"Bearer {{client_secret}}"}})
    data = r.json() if r.content else {{}}
    return AbdmAbhaCreateResponse(
        created=(r.status_code in (200, 201)),
        abha_number=data.get("healthIdNumber", ""),
        abha_address=data.get("healthId", ""),
        name=data.get("name", ""), gender=data.get("gender", ""),
        dob=data.get("dayOfBirth", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/abha/{{abha_number}}", response_model=AbdmAbhaProfileResponse)
async def abdm_abha_profile(abha_number: str):
    """Fetch an ABHA profile by 14-digit number."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return AbdmAbhaProfileResponse(
            abha_number=abha_number, abha_address="ramesh@abdm",
            name="RAMESH KUMAR", gender="M", dob="1985-04-21",
            state="KARNATAKA", request_id=request_id, mode="mock",
        )
    base = os.getenv("ABDM_BASE_URL", "")
    client_id = os.getenv("ABDM_CLIENT_ID", "")
    client_secret = os.getenv("ABDM_CLIENT_SECRET", "")
    if not (base and client_id and client_secret):
        return AbdmAbhaProfileResponse(
            abha_number=abha_number,
            error="ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/v1/account/profile/{{abha_number}}",
                             headers={{"X-CM-ID": client_id,
                                      "Authorization": f"Bearer {{client_secret}}"}})
    data = r.json() if r.content else {{}}
    return AbdmAbhaProfileResponse(
        abha_number=abha_number, abha_address=data.get("healthId", ""),
        name=data.get("name", ""), gender=data.get("gender", ""),
        dob=data.get("dayOfBirth", ""), state=data.get("stateName", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )


@router.post("/hpr/verify", response_model=AbdmHprVerifyResponse)
async def abdm_hpr_verify(payload: AbdmHprVerifyRequest):
    """Verify a Health Professional Registry id (doctor/nurse credential)."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return AbdmHprVerifyResponse(
            verified=True, hpr_id=payload.hpr_id,
            name="DR. RAMESH KUMAR",
            qualifications=["MBBS", "MD (General Medicine)"],
            registration_council="MEDICAL COUNCIL OF INDIA",
            practice_status="ACTIVE",
            request_id=request_id, mode="mock",
        )
    base = os.getenv("ABDM_BASE_URL", "")
    client_id = os.getenv("ABDM_CLIENT_ID", "")
    client_secret = os.getenv("ABDM_CLIENT_SECRET", "")
    if not (base and client_id and client_secret):
        return AbdmHprVerifyResponse(
            verified=False, hpr_id=payload.hpr_id,
            error="ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/v1/hpr/verify/{{payload.hpr_id}}",
                             headers={{"X-CM-ID": client_id,
                                      "Authorization": f"Bearer {{client_secret}}"}})
    data = r.json() if r.content else {{}}
    return AbdmHprVerifyResponse(
        verified=(r.status_code == 200 and bool(data.get("verified"))),
        hpr_id=payload.hpr_id, name=data.get("name", ""),
        qualifications=data.get("qualifications", []),
        registration_council=data.get("council", ""),
        practice_status=data.get("status", ""),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''


# ---------------------------------------------------------------------------
# Jeevan Pramaan — Digital Life Certificate for pensioners
# ---------------------------------------------------------------------------

JEEVAN_PRAMAAN_PYTHON_BODY = '''

class JeevanPramaanGenerateRequest(BaseModel):
    aadhaar: str = Field(..., min_length=12, max_length=12)
    pension_pdo_code: str = Field(..., description="Pension Disbursing Office code")
    ppo_number: str = Field(..., description="Pension Payment Order number")
    biometric_b64: Optional[str] = Field(None, description="Base64-encoded biometric capture (ISO template)")


class JeevanPramaanGenerateResponse(BaseModel):
    generated: bool
    pramaan_id: str = ""
    issued_on: str = ""
    valid_until: str = ""
    aadhaar_masked: str = ""
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


class JeevanPramaanDetailsResponse(BaseModel):
    pramaan_id: str
    aadhaar_masked: str = ""
    pensioner_name: str = ""
    pension_pdo_code: str = ""
    ppo_number: str = ""
    issued_on: str = ""
    valid_until: str = ""
    status: str = ""    # ACTIVE | EXPIRED | REVOKED
    request_id: str = ""
    mode: str = "mock"
    error: str = ""


@router.post("/dlc/generate", response_model=JeevanPramaanGenerateResponse)
async def jeevan_pramaan_generate(payload: JeevanPramaanGenerateRequest):
    """Generate a Digital Life Certificate (Jeevan Pramaan) for a pensioner."""
    request_id = uuid.uuid4().hex[:12]
    masked = _mask_id(payload.aadhaar, 4)
    if _mode() == "mock":
        return JeevanPramaanGenerateResponse(
            generated=True, pramaan_id="JP-MOCK-" + request_id.upper(),
            issued_on=datetime.now(timezone.utc).date().isoformat(),
            valid_until="2027-06-12",
            aadhaar_masked=masked,
            request_id=request_id, mode="mock",
        )
    base = os.getenv("JEEVAN_PRAMAAN_BASE_URL", "")
    api_key = os.getenv("JEEVAN_PRAMAAN_API_KEY", "")
    if not (base and api_key):
        return JeevanPramaanGenerateResponse(
            generated=False, aadhaar_masked=masked,
            error="JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.post(base.rstrip("/") + "/api/v1/dlc/generate", json={{
            "aadhaar": payload.aadhaar, "pdoCode": payload.pension_pdo_code,
            "ppoNumber": payload.ppo_number,
            "biometric": payload.biometric_b64 or "",
        }}, headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return JeevanPramaanGenerateResponse(
        generated=(r.status_code in (200, 201)),
        pramaan_id=data.get("pramaanId", ""),
        issued_on=data.get("issuedOn", ""),
        valid_until=data.get("validUntil", ""),
        aadhaar_masked=masked,
        request_id=request_id, mode="live",
        error="" if r.status_code in (200, 201) else f"Provider HTTP {{r.status_code}}",
    )


@router.get("/dlc/{{pramaan_id}}", response_model=JeevanPramaanDetailsResponse)
async def jeevan_pramaan_details(pramaan_id: str):
    """Fetch a previously-generated Jeevan Pramaan certificate."""
    request_id = uuid.uuid4().hex[:12]
    if _mode() == "mock":
        return JeevanPramaanDetailsResponse(
            pramaan_id=pramaan_id, aadhaar_masked="********1234",
            pensioner_name="RAMESH KUMAR",
            pension_pdo_code="MOCK-PDO-001", ppo_number="PPO/2020/12345",
            issued_on="2026-01-15", valid_until="2027-01-15",
            status="ACTIVE", request_id=request_id, mode="mock",
        )
    base = os.getenv("JEEVAN_PRAMAAN_BASE_URL", "")
    api_key = os.getenv("JEEVAN_PRAMAAN_API_KEY", "")
    if not (base and api_key):
        return JeevanPramaanDetailsResponse(
            pramaan_id=pramaan_id,
            error="JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset",
            request_id=request_id, mode="live",
        )
    async with httpx.AsyncClient(timeout=30, verify=_ssl_verify()) as client:
        r = await client.get(base.rstrip("/") + f"/api/v1/dlc/{{pramaan_id}}",
                             headers={{"Authorization": f"Bearer {{api_key}}"}})
    data = r.json() if r.content else {{}}
    return JeevanPramaanDetailsResponse(
        pramaan_id=pramaan_id, aadhaar_masked=data.get("aadhaarMasked", ""),
        pensioner_name=data.get("name", ""),
        pension_pdo_code=data.get("pdoCode", ""), ppo_number=data.get("ppoNumber", ""),
        issued_on=data.get("issuedOn", ""), valid_until=data.get("validUntil", ""),
        status=(data.get("status") or "").upper(),
        request_id=request_id, mode="live",
        error="" if r.status_code == 200 else f"Provider HTTP {{r.status_code}}",
    )
'''

