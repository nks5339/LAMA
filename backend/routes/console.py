"""Console — Model Fabric, Agent Fabric, Prompt Engineering, Token Usage."""
import time
import httpx
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, Query

from db import (
    model_providers as mp_col,
    agent_configs as ac_col,
    token_usage_log as log_col,
    prompts as prompts_col,
    project_prompts,
    projects,
    kb_toon,
)
from fabric.model_fabric import (
    setup_default_provider,
    fabric_chat,
    estimate_cost,
    resolve_model,
    _rewrite_local_url_for_docker,
)
from factory_orchestrator import (
    get_project_factory_orchestrator_config,
    upsert_project_factory_orchestrator_config,
    test_project_factory_orchestrator_connection,
    delete_project_factory_orchestrator_config,
    ensure_project_workspace,
    wake_project_droid,
)
from onboarding import run_factory_first_time_onboarding
from log_tail import tail as _log_tail  # noqa: E402  (iter-14.17)

router = APIRouter(prefix="/console", tags=["console"])


# ── iter-14.17 — Live backend log tail ───────────────────────────────
# Feeds the Discovery · Live Telemetry popup so operators can see LLM
# routing decisions, provider errors, HF model loading, and confidence-
# loop progress without shelling into the container. Polls-based (no
# SSE) so we stay clear of the K8s 60s ingress timeout.
@router.get("/logs/tail")
async def get_logs_tail(
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=300, ge=1, le=1000),
    min_level: str = Query(default=""),
    contains: str = Query(default=""),
):
    """Return every log record captured since `since_seq`.

    Response:
      { records: [{seq, ts, level, name, msg}, ...],
        next_seq, dropped, capacity, size }
    """
    return _log_tail(
        since_seq=since_seq,
        limit=limit,
        min_level=(min_level or None),
        contains=(contains or None),
    )



def _mask_key(k: str) -> str:
    k = k or ""
    if len(k) < 8:
        return "***"
    return f"{k[:6]}...{k[-4:]}"


def _serialize_provider(p: Dict[str, Any]) -> Dict[str, Any]:
    p = {**p}
    p.pop("_id", None)
    p["api_key"] = _mask_key(p.get("api_key", ""))
    return p


# ── Providers ────────────────────────────────────────────────────────
@router.post("/providers/setup")
async def providers_setup(payload: dict):
    api_key = (payload or {}).get("api_key", "").strip()
    provider_type = (payload or {}).get("provider_type", "").strip().lower()
    # Ollama is the only provider where an empty API key is legitimate
    # (local runtime, no auth). Everything else still requires a key.
    if not api_key and provider_type != "ollama":
        raise HTTPException(400, "api_key required")
    name = (payload or {}).get("name", "")
    base_url = (payload or {}).get("base_url", "")
    # Azure needs a deployment name and an API version on top of the key:
    # the deployment sits in the URL path and the version is a query
    # parameter, so neither can be derived from the key or the base URL.
    azure_deployment = (payload or {}).get("azure_deployment", "").strip()
    azure_api_version = (payload or {}).get("azure_api_version", "").strip()
    if provider_type == "azure":
        missing = [
            label for label, value in (
                ("base_url (Azure endpoint)", base_url or os.environ.get("AZURE_ENDPOINT", "")),
                ("azure_deployment", azure_deployment or os.environ.get("AZURE_DEPLOYMENT", "")),
            ) if not value
        ]
        if missing:
            # Fail here with the field names rather than persisting a row
            # that cannot build a URL and only reveals the problem as a 404
            # on the operator's first real generation call.
            raise HTTPException(
                400,
                "Azure requires " + " and ".join(missing)
                + ". The endpoint is your account root (the deployment path is "
                  "appended automatically) and the deployment is the name you "
                  "chose in Azure, not a published model name.",
            )
    doc = await setup_default_provider(
        api_key, name=name, base_url=base_url, provider_type=provider_type,
        azure_deployment=azure_deployment, azure_api_version=azure_api_version,
    )
    return {"ok": True, "provider": _serialize_provider(doc)}


@router.get("/providers")
async def list_providers():
    docs = await mp_col.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"providers": [_serialize_provider(d) for d in docs]}


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, payload: dict):
    upd = {}
    # iter-14.34 — `key_enabled` is the new user-controlled on/off toggle
    # for the stored api_key. When False, resolve_model / _resolve_ollama_endpoint
    # treat the key as absent (no Bearer header, no cloud auto-flip).
    for f in ("name", "base_url", "is_active", "key_enabled"):
        if f in payload:
            upd[f] = payload[f]
    # iter-13.112 — Ollama localhost rewrite when running inside Docker.
    # If the operator edits an Ollama row and pastes localhost, transparently
    # substitute host.docker.internal so the container can reach the host.
    if "base_url" in upd:
        target = await mp_col.find_one({"id": provider_id}, {"_id": 0, "provider_type": 1})
        if target and (target.get("provider_type") or "").lower() == "ollama":
            upd["base_url"] = _rewrite_local_url_for_docker(upd["base_url"] or "")
    if "routing" in payload and isinstance(payload["routing"], dict):
        upd["routing"] = payload["routing"]
    # iter-13.112 — Stage-based model routing with generate/regenerate modes
    if "stage_routing_generate" in payload and isinstance(payload["stage_routing_generate"], dict):
        upd["stage_routing_generate"] = payload["stage_routing_generate"]
    if "stage_routing_regenerate" in payload and isinstance(payload["stage_routing_regenerate"], dict):
        upd["stage_routing_regenerate"] = payload["stage_routing_regenerate"]
    if payload.get("is_default") is True:
        await mp_col.update_many({}, {"$set": {"is_default": False}})
        upd["is_default"] = True
    upd["updated_at"] = datetime.now(timezone.utc).isoformat()
    r = await mp_col.update_one({"id": provider_id}, {"$set": upd})
    if r.matched_count == 0:
        raise HTTPException(404, "Provider not found")
    # If routing/default/active changed, blow away stale agent pins so
    # resolve_model picks the fresh default on the next call.
    if any(k in upd for k in ("is_default", "is_active", "routing", "stage_routing_generate", "stage_routing_regenerate")):
        try:
            from db import agent_configs as ac_col
            await ac_col.update_many({}, {"$set": {"provider_id": ""}})
        except Exception:
            pass
    doc = await mp_col.find_one({"id": provider_id}, {"_id": 0})
    return {"ok": True, "provider": _serialize_provider(doc)}


@router.put("/providers/{provider_id}/key")
async def update_provider_key(provider_id: str, payload: dict):
    api_key = (payload or {}).get("api_key", "").strip()
    if not api_key:
        raise HTTPException(400, "api_key required")
    now = datetime.now(timezone.utc).isoformat()
    r = await mp_col.update_one(
        {"id": provider_id},
        {"$set": {"api_key": api_key, "detected_from_key": api_key[:8] + "...", "updated_at": now}},
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Provider not found")
    # Clear every stale agent pin — the previous failover may have pinned
    # agents to a DIFFERENT provider; without this, the freshly-funded key
    # is silently bypassed for already-pinned agents (e.g. srs.generate).
    try:
        from db import agent_configs as ac_col
        await ac_col.update_many({}, {"$set": {"provider_id": ""}})
    except Exception:
        pass
    return {"ok": True}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str):
    active_count = await mp_col.count_documents({"is_active": True})
    target = await mp_col.find_one({"id": provider_id}, {"_id": 0})
    if not target:
        raise HTTPException(404, "Provider not found")
    if target.get("is_active") and active_count <= 1:
        raise HTTPException(400, "Cannot delete the last active provider.")
    await mp_col.delete_one({"id": provider_id})
    if target.get("is_default"):
        # Promote the next active provider as default
        nxt = await mp_col.find_one({"is_active": True}, {"_id": 0}, sort=[("created_at", -1)])
        if nxt:
            await mp_col.update_one({"id": nxt["id"]}, {"$set": {"is_default": True}})
    return {"ok": True}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    p = await mp_col.find_one({"id": provider_id}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Provider not found")
    ptype = p.get("provider_type", "openrouter")
    base_url = p.get("base_url", "")
    # iter-14.34 — honour the key_enabled toggle; when OFF, behave as if
    # no key were configured (no Bearer header, no Ollama cloud auto-flip).
    api_key = p.get("api_key", "") if p.get("key_enabled", True) else ""
    model_id = (p.get("routing") or {}).get("low") or (p.get("models") or [{}])[0].get("id", "")
    if not model_id:
        return {"ok": False, "error": "No model configured for this provider."}
    is_cloud = False
    test_params: Dict[str, Any] = {}
    if ptype == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    elif ptype == "azure":
        # Mirror resolve_model exactly. This endpoint exists to tell the
        # operator whether their real calls will work, so building the URL
        # a different way here would make the button worse than useless: it
        # could pass while generation fails, or vice versa.
        deployment = p.get("azure_deployment", "") or model_id
        api_version = (
            p.get("azure_api_version")
            or os.environ.get("AZURE_API_VERSION", "")
            or "2024-02-15-preview"
        )
        root = (base_url or "").rstrip("/")
        if deployment and "/deployments/" not in root:
            root = f"{root}/openai/deployments/{deployment}"
        base_url = root
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        test_params = {"api-version": api_version}
    elif ptype == "ollama":
        # iter-14.21 — auto-detect cloud endpoint + Bearer auth (same
        # rules as resolve_model). Test connection now reflects the
        # real routing the LLM call will use.
        from fabric.model_fabric import _resolve_ollama_endpoint
        base_url, is_cloud = _resolve_ollama_endpoint(p, model_id)
        if is_cloud and api_key:
            headers = {"Authorization": f"Bearer {api_key}",
                       "Content-Type": "application/json"}
        else:
            headers = {"Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://lama.local", "X-Title": "LAMA"}
    from fabric.model_fabric import apply_token_limit
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": "Say 'ok' in one word."}],
               "temperature": 0.1}
    # The reasoning families reject `max_tokens` outright, so the shared
    # helper picks the field name. A budget of 10 is also too small for them:
    # reasoning tokens are drawn from the same allowance, so a tiny cap is
    # spent thinking and returns an empty string with finish_reason="length",
    # which reads as a dead provider. 2000 is enough to reason and still
    # answer "ok".
    _is_reasoning = apply_token_limit({}, model_id, 1).get("max_completion_tokens")
    payload = apply_token_limit(payload, model_id, 2000 if _is_reasoning else 10)
    t0 = time.time()
    # iter-14.25.6 — Ollama pre-flight: if the operator points at a local
    # Ollama endpoint but the requested model isn't pulled, the OpenAI-
    # compat /v1/chat/completions call blocks forever (Ollama tries to
    # pull the model in the background) and we surface an empty-string
    # httpx.ReadTimeout to the FE. Ping /api/tags first, list what's
    # actually installed, and return a clean actionable error instead.
    if ptype == "ollama" and not is_cloud:
        tag_url = base_url.rstrip("/")
        if tag_url.endswith("/v1"):
            tag_url = tag_url[:-3]
        try:
            from llm import _http_verify
            async with httpx.AsyncClient(timeout=5.0, verify=_http_verify()) as _c:
                _tags = await _c.get(f"{tag_url}/api/tags")
                if _tags.status_code == 200:
                    _installed = [
                        m.get("name", "") for m in (_tags.json() or {}).get("models", [])
                    ]
                    if _installed and not any(
                        (n == model_id or n.split(":")[0] == model_id.split(":")[0])
                        for n in _installed
                    ):
                        return {
                            "ok": False,
                            "error": (
                                f"Model '{model_id}' is not pulled on the local "
                                f"Ollama at {tag_url}. Run: `ollama pull {model_id}` "
                                f"— or pick one of the {len(_installed)} installed: "
                                f"{', '.join(_installed[:6])}"
                                f"{'…' if len(_installed) > 6 else ''}."
                            ),
                            "latency_ms": int((time.time() - t0) * 1000),
                            "model_used": model_id,
                            "cloud": is_cloud,
                            "endpoint": base_url,
                        }
        except Exception:
            # /api/tags unreachable → fall through and let the real call
            # produce the transport-level error.
            pass
    try:
        # iter-13.34 — honor the shared SSL policy (LAMA_DISABLE_SSL_VERIFY /
        # LAMA_CA_BUNDLE) so corporate-MITM users see the same behaviour
        # here as on the LLM-call path. Without verify=_http_verify(), the
        # "Test connection" button always failed with CERTIFICATE_VERIFY_FAILED
        # behind Zscaler / Netskope even when env-var bypass was configured.
        from llm import _http_verify
        # iter-14.25.10 — local Ollama cold-starts (loading a 7B+ model into
        # VRAM/RAM the first time after keep-alive expiry) can take 30-120s.
        # A blanket 30s timeout gave a false "fail" on the Console Test
        # button even though Ollama itself was healthy — repeated /api/ps
        # confirmed the model got loaded ~35s in, just after we bailed.
        # Bump the read-timeout for local Ollama to 180s; keep 30s for
        # cloud providers so a hard-down remote still fails fast.
        if ptype == "ollama" and not is_cloud:
            _timeout = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)
        else:
            _timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=_timeout, verify=_http_verify()) as client:
            r = await client.post(f"{base_url}/chat/completions", headers=headers,
                                  json=payload, params=test_params or None)
            latency_ms = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                        "latency_ms": latency_ms, "model_used": model_id,
                        "cloud": is_cloud, "endpoint": base_url}
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "model_used": model_id, "latency_ms": latency_ms,
                    "response": content[:100],
                    "cloud": is_cloud, "endpoint": base_url}
    except Exception as e:
        # iter-14.25.6 — httpx.ReadTimeout / ConnectTimeout serialise to ""
        # via str(), leaving the FE red banner empty. Fall back to the
        # exception class name so the operator always sees something
        # actionable (e.g. "ReadTimeout after 30s at http://host…/v1").
        msg = str(e)[:200]
        if not msg.strip():
            msg = f"{type(e).__name__} after {int(time.time() - t0)}s at {base_url}"
        # iter-14.25.10 — when local Ollama times out despite /api/tags
        # showing the model IS pulled, the almost-certain cause is a cold
        # model-load exceeding our timeout. Give the operator a specific
        # hint instead of the raw ReadTimeout.
        if ptype == "ollama" and not is_cloud and "Timeout" in type(e).__name__:
            msg = (
                f"{msg} — Ollama is reachable but the request timed out. "
                f"This usually means the '{model_id}' model is cold-loading "
                f"into VRAM/RAM (a 7B model can take 30-90s the first time). "
                f"Run `ollama run {model_id}` once to warm it, then retry, "
                f"or raise the Ollama `OLLAMA_KEEP_ALIVE` env var so it "
                f"doesn't evict between tests."
            )
        return {"ok": False, "error": msg, "latency_ms": int((time.time() - t0) * 1000),
                "model_used": model_id, "cloud": is_cloud, "endpoint": base_url}


# iter-14.21 — /validate/{id} — alias of /test with a stable, normalised
# response shape the Console FE can render as a green/red badge without
# knowing the underlying transport. Documented in CLAUDE.md as the
# recommended "credential ping" endpoint (was on the P2 backlog).
@router.post("/providers/{provider_id}/validate")
async def validate_provider(provider_id: str):
    r = await test_provider(provider_id)
    return {
        "ok":         bool(r.get("ok")),
        "cloud":      bool(r.get("cloud")),
        "endpoint":   r.get("endpoint", ""),
        "model_used": r.get("model_used", ""),
        "latency_ms": r.get("latency_ms", 0),
        "error":      r.get("error", ""),
    }


@router.post("/providers/{provider_id}/fetch-models")
async def fetch_provider_models(provider_id: str):
    p = await mp_col.find_one({"id": provider_id}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Provider not found")
    ptype = p.get("provider_type", "openrouter")
    base_url = p.get("base_url", "")
    # iter-14.34 — respect key_enabled toggle here too.
    api_key = p.get("api_key", "") if p.get("key_enabled", True) else ""
    if ptype == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        url = f"{base_url}/models"
    elif ptype == "ollama":
        headers = {}
        url = f"{base_url.replace('/v1', '')}/api/tags"
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
        url = f"{base_url}/models"
    try:
        # iter-13.34 — same SSL-policy honour as test_provider above.
        from llm import _http_verify
        async with httpx.AsyncClient(timeout=20.0, verify=_http_verify()) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            data = r.json()
            raw = data.get("data") or data.get("models") or []
            models = []
            for m in raw:
                mid = m.get("id") or m.get("name") or ""
                if mid:
                    models.append({"id": mid, "label": mid})
            if models:
                await mp_col.update_one({"id": provider_id}, {"$set": {"models": models,
                                                                       "updated_at": datetime.now(timezone.utc).isoformat()}})
            return {"ok": True, "models": models, "count": len(models)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/models/available")
async def list_available_models():
    docs = await mp_col.find({"is_active": True}, {"_id": 0}).to_list(50)
    out = []
    for p in docs:
        for m in p.get("models", []):
            out.append({"provider_id": p["id"], "provider_name": p.get("name", ""),
                        "provider_type": p.get("provider_type", ""),
                        "model_id": m.get("id", ""), "label": m.get("label", m.get("id", ""))})
    return {"models": out}


# ── Factory Orchestrator (per-project) ───────────────────────────────
@router.get("/factory-orchestrator/config")
async def get_factory_orchestrator_config(project_id: str = Query(default="")):
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        cfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "config": cfg}


@router.put("/factory-orchestrator/config")
async def put_factory_orchestrator_config(payload: dict):
    project_id = (payload or {}).get("project_id", "")
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        cfg = await upsert_project_factory_orchestrator_config(project_id, payload or {})
    except RuntimeError as e:
        msg = str(e)
        # iter-13.91.14 — cwd-collision is a user-fixable conflict, surface as 409.
        if "already in use by project" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(404, msg)
    # iter-15.48 — Invalidate the CLI probe cache so the next /test call
    # sees the freshly-saved binary path / auto level.
    try:
        from fabric.factory_cli import _invalidate_probe_cache
        _invalidate_probe_cache()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "config": cfg}


@router.get("/factory-orchestrator/test")
async def test_factory_orchestrator_config(
    project_id: str = Query(default=""),
    with_workspace: bool = Query(default=False),
):
    """Health-check Factory orchestrator for a project.

    iter-13.91.7 — `with_workspace=false` by default. MiniConsole polls
    this endpoint every 30 s; previously each call ran a full Factory
    bootstrap session (POST /sessions + mkdir + ~30 s polling) which
    flooded Factory with duplicate sessions. The Console's explicit
    "Create / refresh workspace" button (POST .../workspace) is where
    bootstrap should be triggered. Pass `with_workspace=true` only when
    you genuinely want to verify Droid filesystem state.

    iter-13.125 — When the project (or env default) selects CLI mode,
    we short-circuit to a `droid --version` probe instead of opening
    a Factory HTTP session. This way the same Test button works for
    both transports without the UI having to special-case the call.
    """
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        cfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
        project_mode = (cfg.get("mode") or "").strip().lower()
        from fabric import factory_cli as _factory_cli
        use_cli = (project_mode == "cli") or (
            project_mode != "api" and _factory_cli.is_cli_mode_enabled()
        )
        if use_cli:
            return await _factory_cli.test_cli_connection(cli_bin=cfg.get("cli_bin") or None)
        result = await test_project_factory_orchestrator_connection(
            project_id, with_workspace=bool(with_workspace),
        )
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.get("/factory-orchestrator/test-cli")
async def test_factory_orchestrator_cli(
    cli_bin: str = Query(default=""),
):
    """Standalone CLI probe — used by the Console's "Test CLI" button so
    operators can verify a candidate binary path *before* saving the
    config. Returns the same shape as `/factory-orchestrator/test` so
    the UI doesn't branch on response schema."""
    try:
        from fabric import factory_cli as _factory_cli
        return await _factory_cli.test_cli_connection(cli_bin=cli_bin or None)
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.delete("/factory-orchestrator/config")
async def delete_factory_orchestrator_config(project_id: str = Query(default="")):
    """iter-13.35 — fully wipe Factory orchestrator config for a project.

    Removes app key, computer id, cwd, cached session ids and the enabled
    flag in one shot. Audit-logged.
    """
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        result = await delete_project_factory_orchestrator_config(project_id)
        try:
            from db import audit_log
            from datetime import datetime, timezone
            await audit_log.insert_one({
                "action": "factory_orchestrator.delete",
                "project_id": project_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        return result
    except Exception as e:
        raise HTTPException(400, str(e)[:500])


# iter-13.91.3 — explicit "create workspace on Droid now" endpoint.
# Useful when the user has enabled Factory but the Droid was asleep at
# onboarding time, or when they wipe the Droid filesystem and want
# LAMA to recreate the per-project scratch dir.
@router.post("/factory-orchestrator/workspace")
async def create_factory_orchestrator_workspace(payload: dict):
    project_id = (payload or {}).get("project_id", "")
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        result = await ensure_project_workspace(project_id)
    except Exception as e:
        raise HTTPException(500, str(e)[:500])
    try:
        from db import audit_log
        from datetime import datetime, timezone
        await audit_log.insert_one({
            "action": "factory_orchestrator.workspace.create",
            "project_id": project_id,
            "result": result,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return result


# iter-13.91.4 — explicit "wake my Droid Computer" endpoint.
# Issues GET /computers/{id} against Factory (the auto-resume trigger)
# with the per-process throttle bypassed. Useful when the user knows
# their Droid is asleep and wants to warm it up before kicking off a
# stage agent, instead of paying the cold-start tax inside the first
# session create.
@router.post("/factory-orchestrator/wake")
async def wake_factory_orchestrator_droid(payload: dict):
    project_id = (payload or {}).get("project_id", "")
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        result = await wake_project_droid(project_id)
    except Exception as e:
        raise HTTPException(500, str(e)[:500])
    try:
        from db import audit_log
        from datetime import datetime, timezone
        await audit_log.insert_one({
            "action": "factory_orchestrator.droid.wake",
            "project_id": project_id,
            "result": result,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return result


# iter-13.36 — explicit re-trigger of the first-run modernization
# onboarding. The auto-fire happens inside upsert_*; this endpoint lets
# the Console UI (or curl) re-run it manually, or run it for the first
# time on a project that was activated before the auto-hook existed.
@router.post("/factory-orchestrator/onboard")
async def onboard_factory_orchestrator(payload: dict):
    project_id = (payload or {}).get("project_id", "")
    force = bool((payload or {}).get("force", False))
    if not project_id:
        raise HTTPException(400, "project_id required")
    try:
        result = await run_factory_first_time_onboarding(project_id, force=force)
    except Exception as e:
        raise HTTPException(500, str(e)[:500])
    return {"ok": True, **result}


# ── Agents ───────────────────────────────────────────────────────────
STAGES_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"]


@router.get("/agents")
async def list_agents():
    docs = await ac_col.find({}, {"_id": 0}).to_list(200)
    grouped = {s: {"orchestrator": [], "tasks": []} for s in STAGES_ORDER}
    for a in docs:
        stage = a.get("stage", "Discovery")
        grouped.setdefault(stage, {"orchestrator": [], "tasks": []})
        bucket = "orchestrator" if a.get("agent_type") == "orchestrator" else "tasks"
        grouped[stage][bucket].append(a)
    # Compute resolved model for each agent for display
    default_provider = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0})
    for stage in grouped.values():
        for bucket in stage.values():
            for a in bucket:
                if a.get("model_override"):
                    a["resolved_model"] = a["model_override"]
                elif default_provider:
                    a["resolved_model"] = (default_provider.get("routing") or {}).get(a.get("complexity", "medium"), "")
                else:
                    a["resolved_model"] = ""
                a["resolved_provider"] = (default_provider or {}).get("name", "(none)")
    return grouped


@router.get("/agents/{key}")
async def get_agent(key: str):
    a = await ac_col.find_one({"key": key}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Agent not found")
    return a


@router.put("/agents/{key}")
async def update_agent(key: str, payload: dict):
    upd: Dict[str, Any] = {}
    for f in ("status", "model_override", "provider_id", "max_tokens", "temperature",
              "wrap_prefix", "wrap_suffix", "replaced_template", "chain_to",
              "chain_condition", "token_budget_total", "complexity"):
        if f in payload:
            upd[f] = payload[f]
    if not upd:
        raise HTTPException(400, "Nothing to update")
    upd["updated_at"] = datetime.now(timezone.utc).isoformat()
    r = await ac_col.update_one({"key": key}, {"$set": upd})
    if r.matched_count == 0:
        raise HTTPException(404, "Agent not found")
    return {"ok": True}


@router.post("/agents/{key}/reset-budget")
async def reset_budget(key: str):
    r = await ac_col.update_one({"key": key}, {"$set": {"tokens_used_all_time": 0,
                                                        "updated_at": datetime.now(timezone.utc).isoformat()}})
    if r.matched_count == 0:
        raise HTTPException(404, "Agent not found")
    return {"ok": True}


@router.post("/agents/{key}/test")
async def test_agent(key: str, payload: dict):
    a = await ac_col.find_one({"key": key}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Agent not found")
    project_id = (payload or {}).get("project_id", "")
    test_message = (payload or {}).get("test_message",
                                       "Respond with one short sentence about your purpose.")
    sys_prompt = f"You are the LAMA agent '{a.get('label', key)}'. {a.get('description', '')}"
    if a.get("status") == "wrapped":
        sys_prompt = f"{a.get('wrap_prefix', '')}\n{sys_prompt}\n{a.get('wrap_suffix', '')}"
    elif a.get("status") == "replaced" and a.get("replaced_template"):
        sys_prompt = a["replaced_template"]
    try:
        r = await fabric_chat(
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": test_message}],
            agent_key=key, project_id=project_id, timeout=60.0,
            max_tokens=min(a.get("max_tokens", 1024), 1024),
        )
        return {
            "ok": True,
            "content_preview": (r.get("content", "") or "")[:500],
            "usage": r.get("usage", {}),
            "cost_usd": r.get("cost_usd", 0.0),
            "model_used": r.get("model", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.get("/agents/{key}/usage")
async def agent_usage(key: str):
    a = await ac_col.find_one({"key": key}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Agent not found")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cursor = log_col.find({"agent_key": key, "created_at": {"$gte": cutoff}}, {"_id": 0}).sort("created_at", -1)
    rows = await cursor.to_list(500)
    by_day: Dict[str, Dict[str, float]] = {}
    for r in rows:
        d = r.get("created_at", "")[:10]
        by_day.setdefault(d, {"tokens": 0, "cost": 0.0})
        by_day[d]["tokens"] += r.get("total_tokens", 0)
        by_day[d]["cost"] += r.get("cost_usd", 0.0)
    return {
        "last_run": {
            "tokens": a.get("tokens_used_last_run", 0),
            "model": a.get("last_run_model", ""),
            "cost_usd": a.get("last_run_cost_usd", 0.0),
            "at": a.get("last_run_at", ""),
        },
        "all_time_total": a.get("tokens_used_all_time", 0),
        "all_time_cost": sum(r.get("cost_usd", 0.0) for r in rows),
        "runs_last_7_days": [{"date": k, **v} for k, v in sorted(by_day.items())],
        "recent_runs": rows[:5],
    }


# ── Token usage reporting ────────────────────────────────────────────
@router.get("/usage/summary")
async def usage_summary(project_id: str = Query(default=""), days: int = Query(default=7)):
    # iter-14.59 — Aggregate server-side via a MongoDB pipeline instead of
    # `find().to_list(5000)`. On live projects (ceots ≈ 16k rows / 7d) the
    # 5000-row cap silently dropped the most recent stages (Living lost
    # ~333k tokens from `by_stage` because its rows were beyond the cap).
    # A `$group` per bucket runs against the full window, is index-friendly
    # on `created_at`, and returns only the aggregates the UI actually
    # consumes.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    match: Dict[str, Any] = {"created_at": {"$gte": cutoff}}
    if project_id:
        match["project_id"] = project_id

    async def _agg(pipeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return await log_col.aggregate(pipeline, allowDiskUse=True).to_list(10000)

    # Grand totals + one-shot bucket aggregations (parallelised on the driver).
    totals_pipe = [
        {"$match": match},
        {"$group": {"_id": None,
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}},
                    "runs":   {"$sum": 1}}},
    ]
    stage_pipe = [
        {"$match": match},
        {"$group": {"_id": {"$ifNull": ["$stage", "unknown"]},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}}}},
    ]
    agent_pipe = [
        {"$match": match},
        {"$group": {"_id": {"$ifNull": ["$agent_key", ""]},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}},
                    "runs":   {"$sum": 1}}},
    ]
    model_pipe = [
        {"$match": match},
        {"$group": {"_id": {"$ifNull": ["$model", ""]},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}}}},
    ]
    day_pipe = [
        {"$match": match},
        {"$group": {"_id": {"$substrBytes": [{"$ifNull": ["$created_at", ""]}, 0, 10]},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}}}},
    ]
    project_pipe = [
        {"$match": match},
        {"$group": {"_id": {"$ifNull": ["$project_id", ""]},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}},
                    "cost":   {"$sum": {"$ifNull": ["$cost_usd", 0.0]}},
                    "runs":   {"$sum": 1}}},
    ]
    project_stage_pipe = [
        {"$match": match},
        {"$group": {"_id": {"project_id": {"$ifNull": ["$project_id", ""]},
                            "stage":      {"$ifNull": ["$stage", "unknown"]}},
                    "tokens": {"$sum": {"$ifNull": ["$total_tokens", 0]}}}},
    ]

    totals_raw   = await _agg(totals_pipe)
    stage_rows   = await _agg(stage_pipe)
    agent_rows   = await _agg(agent_pipe)
    model_rows   = await _agg(model_pipe)
    day_rows     = await _agg(day_pipe)
    project_rows = await _agg(project_pipe)
    ps_rows      = await _agg(project_stage_pipe)

    grand = totals_raw[0] if totals_raw else {"tokens": 0, "cost": 0.0, "runs": 0}
    total_tokens = int(grand.get("tokens", 0))
    total_cost   = float(grand.get("cost", 0.0))
    total_runs   = int(grand.get("runs", 0))

    by_stage: Dict[str, Dict[str, Any]] = {
        r["_id"]: {"stage": r["_id"], "tokens": int(r["tokens"]), "cost": float(r["cost"])}
        for r in stage_rows
    }
    by_agent: Dict[str, Dict[str, Any]] = {
        r["_id"]: {"agent_key": r["_id"], "label": r["_id"],
                   "tokens": int(r["tokens"]), "cost": float(r["cost"]),
                   "runs": int(r["runs"])}
        for r in agent_rows
    }
    by_model: Dict[str, Dict[str, Any]] = {
        r["_id"]: {"model": r["_id"], "tokens": int(r["tokens"]), "cost": float(r["cost"])}
        for r in model_rows
    }
    by_day: Dict[str, Dict[str, Any]] = {
        r["_id"]: {"date": r["_id"], "tokens": int(r["tokens"]), "cost": float(r["cost"])}
        for r in day_rows
    }
    by_project: Dict[str, Dict[str, Any]] = {
        r["_id"]: {"project_id": r["_id"], "tokens": int(r["tokens"]),
                   "cost": float(r["cost"]), "runs": int(r["runs"])}
        for r in project_rows
    }
    by_project_stage: Dict[str, Dict[str, Any]] = {}
    for r in ps_rows:
        pid = r["_id"].get("project_id", "")
        stg = r["_id"].get("stage", "unknown")
        cross = by_project_stage.setdefault(
            pid, {"project_id": pid, "stages": {}, "tokens": 0},
        )
        cross["stages"][stg] = cross["stages"].get(stg, 0) + int(r["tokens"])
        cross["tokens"] += int(r["tokens"])

    # Resolve project_id → project name for the UI. One bulk find keeps this
    # cheap even when many projects exist. Unknown / deleted projects fall
    # back to a truncated id so the chart still renders something readable.
    proj_names: Dict[str, str] = {}
    pids = [pid for pid in by_project.keys() if pid]
    if pids:
        try:
            from db import projects as _projects_col
            cursor = _projects_col.find({"id": {"$in": pids}}, {"_id": 0, "id": 1, "name": 1})
            async for p in cursor:
                proj_names[p["id"]] = p.get("name") or p["id"][:8]
        except Exception:
            # Best-effort enrichment; never fail the whole summary on a
            # projects-collection blip.
            proj_names = {}
    for pid, row in by_project.items():
        row["project_name"] = proj_names.get(pid, pid[:8] if pid else "(no project)")

    # Enrich + sort the cross-tab the same way so the UI can render it
    # directly without extra joining.
    by_project_stage_list = []
    for pid, cross in by_project_stage.items():
        by_project_stage_list.append({
            "project_id": pid,
            "project_name": proj_names.get(pid, pid[:8] if pid else "(no project)"),
            "tokens": cross["tokens"],
            "stages": cross["stages"],
        })
    by_project_stage_list.sort(key=lambda x: -x["tokens"])

    return {
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "total_runs": total_runs,
        "by_stage": list(by_stage.values()),
        "by_agent": list(by_agent.values()),
        "by_model": list(by_model.values()),
        "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
        "by_project": sorted(by_project.values(), key=lambda x: -x["tokens"]),
        "by_project_stage": by_project_stage_list,
        # iter-13.31 — "Currently using" block for the always-on status bar.
        # The most recent token_usage_log row identifies which model the
        # last LLM call ran on (so the UI can show "now using <model>"
        # even when no job is actively streaming).
        "current": await _current_usage(project_id),
    }


async def _current_usage(project_id: str = "") -> Dict[str, Any]:
    """Return the most recent LLM call's model + agent + token tally.

    Used by `MiniConsole.jsx` to show "Currently using …" on every page.
    Falls back to an empty block when no calls have happened yet so the
    UI can show a graceful "idle" state.
    """
    q: Dict[str, Any] = {}
    if project_id:
        q["project_id"] = project_id
    try:
        latest = await log_col.find_one(q, {"_id": 0}, sort=[("created_at", -1)])
    except Exception:
        latest = None
    if not latest:
        # Surface the default-routed model even before the first call so
        # operators see "what would run next" rather than a blank field.
        try:
            default_prov = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0})
            routing = (default_prov or {}).get("routing") or {}
            # iter-13.34 — prefer the active Console provider's high-tier
            # model over the env-var fallback so the MiniConsole doesn't
            # keep showing the legacy LAMA_DEFAULT_MODEL ("deepseek/…")
            # right after a Refresh App / fresh deploy on an Anthropic
            # default. Env var is the last-resort fallback only.
            picked = (
                routing.get("high")
                or routing.get("medium")
                or routing.get("low")
                or os.environ.get("LAMA_DEFAULT_MODEL", "")
                or ""
            )
            return {
                "model": picked,
                "provider": (default_prov or {}).get("name", "") or (default_prov or {}).get("provider_type", ""),
                "agent_key": "",
                "stage": "",
                "tokens": 0,
                "cost_usd": 0.0,
                "duration_ms": 0,
                "at": "",
                "status": "idle",
            }
        except Exception:
            return {"model": "", "provider": "", "agent_key": "", "stage": "",
                    "tokens": 0, "cost_usd": 0.0, "duration_ms": 0, "at": "", "status": "idle"}
    return {
        "model": latest.get("model", ""),
        "provider": latest.get("provider_type", ""),
        "agent_key": latest.get("agent_key", ""),
        "stage": latest.get("stage", ""),
        "tokens": latest.get("total_tokens", 0),
        "cost_usd": round(latest.get("cost_usd", 0.0) or 0.0, 6),
        "duration_ms": latest.get("duration_ms", 0),
        "at": latest.get("created_at", ""),
        "status": latest.get("status", "success"),
    }


@router.get("/usage/log")
async def usage_log(project_id: str = Query(default=""), agent_key: str = Query(default=""),
                    limit: int = Query(default=50)):
    q: Dict[str, Any] = {}
    if project_id:
        q["project_id"] = project_id
    if agent_key:
        q["agent_key"] = agent_key
    rows = await log_col.find(q, {"_id": 0}).sort("created_at", -1).to_list(min(limit, 500))
    return {"logs": rows, "count": len(rows)}


# ── Prompt preview / test ────────────────────────────────────────────
async def _resolve_prompt_template(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts_col.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


async def _resolve_variables(project_id: str, template: str) -> Dict[str, str]:
    import re
    var_names = sorted(set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template)))
    proj = await projects.find_one({"id": project_id}, {"_id": 0}) if project_id else None
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0}) if project_id else None
    toon_context = (toon_doc or {}).get("toon", "") if toon_doc else ""
    resolved: Dict[str, str] = {}
    for v in var_names:
        if v == "project_name" and proj:
            resolved[v] = proj.get("name", "")
        elif v == "source_tech" and proj:
            resolved[v] = proj.get("source_tech", "")
        elif v == "target_tech" and proj:
            resolved[v] = proj.get("target_tech", "")
        elif v == "toon_context":
            resolved[v] = toon_context[:6000]
        else:
            resolved[v] = f"<{v}>"  # placeholder
    return resolved


@router.post("/prompts/preview")
async def preview_prompt(payload: dict):
    key = (payload or {}).get("prompt_key", "")
    project_id = (payload or {}).get("project_id", "")
    if not key:
        raise HTTPException(400, "prompt_key required")
    template = await _resolve_prompt_template(project_id, key)
    if not template:
        raise HTTPException(404, "Prompt not found")
    resolved = await _resolve_variables(project_id, template)
    try:
        resolved_template = template.format(**resolved)
    except Exception as e:
        resolved_template = template + f"\n\n[render-error: {e}]"
    variables = [
        {"name": k, "resolved": (v or "")[:300], "token_estimate": max(1, len(v or "") // 4)}
        for k, v in resolved.items()
    ]
    total_tokens = sum(v["token_estimate"] for v in variables) + max(1, len(template) // 4)
    # Find model that will run for this key
    model_id, _, _, _meta = await resolve_model(key)
    # Cost the preview against the provider this agent actually resolves to.
    # Reading the default row here was wrong whenever the agent was pinned to
    # a different provider via `provider_id`, which quoted the preview at the
    # wrong vendor's rates.
    default_provider = await mp_col.find_one({"is_default": True}, {"_id": 0})
    ptype = (
        _meta.get("provider_type")
        or (default_provider or {}).get("provider_type", "openrouter")
    )
    cost = estimate_cost(model_id, total_tokens, 0, ptype)
    return {
        "resolved_template": resolved_template[:8000],
        "variables": variables,
        "total_token_estimate": total_tokens,
        "cost_estimate_usd": round(cost, 6),
        "model_that_will_run": model_id,
    }


@router.post("/prompts/test")
async def test_prompt(payload: dict):
    key = (payload or {}).get("prompt_key", "")
    project_id = (payload or {}).get("project_id", "")
    model_override = (payload or {}).get("model_override", "")
    if not key:
        raise HTTPException(400, "prompt_key required")
    template = await _resolve_prompt_template(project_id, key)
    if not template:
        raise HTTPException(404, "Prompt not found")
    resolved = await _resolve_variables(project_id, template)
    try:
        system_prompt = template.format(**resolved)
    except Exception:
        system_prompt = template
    t0 = time.time()
    try:
        r = await fabric_chat(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": "Run."}],
            agent_key=key, project_id=project_id, timeout=60.0,
            model_override=model_override, max_tokens=1024,
        )
        return {
            "ok": True,
            "content": (r.get("content", "") or "")[:4000],
            "usage": r.get("usage", {}),
            "cost_usd": r.get("cost_usd", 0.0),
            "model_used": r.get("model", ""),
            "duration_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "duration_ms": int((time.time() - t0) * 1000)}
