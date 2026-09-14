"""OpenRouter LLM client (OpenAI-compatible HTTP API via httpx)."""
import os
import contextvars
import shutil
import httpx
from typing import List, Dict, Optional, Any

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


# iter-13.50 — Transport-error classifier.
#
# WHY: a DNS / network failure inside chat_completion used to propagate
# as the raw glibc message `[Errno -2] Name or service not known`. Every
# Architecture sub-job (HLD/LLD/Sequence/API contracts) catches the
# exception per-section and inlines `str(e)` into the artifact body, so
# the user ends up looking at a Mermaid diagram whose only content is
# `Note over System: error: [Errno -2] Name or service not known`.
# Rewrap such errors with an actionable message so:
#   • the toast in the UI tells the user EXACTLY which host failed
#   • the per-section catch sites can detect transport failures and
#     short-circuit the whole job instead of polluting the artifact
def _classify_transport_error(exc: BaseException, host_hint: str = "") -> str:
    """Return a short, human-actionable description of a network failure.
    Returns "" when `exc` doesn't look like a transport error."""
    msg = str(exc) or ""
    low = msg.lower()
    dns_markers = (
        "name or service not known", "nodename nor servname",
        "name resolution", "getaddrinfo failed", "temporary failure in name resolution",
        "[errno -2]", "[errno -3]", "[errno 8]", "[errno 11001]",
    )
    if any(m in low for m in dns_markers):
        return (
            f"DNS resolution failed for {host_hint or 'the LLM endpoint'} "
            f"(getaddrinfo: {msg.strip()}). Check the container's "
            f"/etc/resolv.conf, the provider base_url in Console → Models, "
            f"or your network's outbound DNS."
        )
    conn_markers = (
        "connection refused", "no route to host", "network is unreachable",
        "connection reset", "ssl: certificate_verify_failed", "ssl handshake",
    )
    if any(m in low for m in conn_markers):
        return (
            f"Network connection to {host_hint or 'the LLM endpoint'} failed: "
            f"{msg.strip()}. Check connectivity, proxy settings, or LAMA_CA_BUNDLE."
        )
    if "timed out" in low or "timeout" in low and "httpx" in low:
        return f"LLM endpoint {host_hint or ''} timed out: {msg.strip()}".strip()
    return ""


class TransportError(RuntimeError):
    """Raised by chat_completion / fabric_call when the failure is a
    transport-level issue (DNS, connect, SSL) rather than a model error.
    Lets stage jobs detect this and abort cleanly instead of inlining
    `str(e)` into per-section artifact bodies."""


# ─────────────────────────────────────────────────────────────────────
# iter-13.38 — Project context propagation for Factory.ai routing.
#
# Why: `fabric_call` infers `project_id` from the caller's stack frame
# when it isn't passed explicitly. That works for `await chat_completion(...)`
# in a route handler. It does NOT work when the call is wrapped in
# `asyncio.create_task(chat_completion(...))` — by the time the coroutine
# runs the f_back chain is the asyncio loop, not the route handler. Every
# DataModel job uses create_task; some Architecture / CodeGen paths do
# too. Result: project_id resolves empty → Factory orchestrator is
# skipped → request silently falls through to the Console-default LLM,
# which is exactly the "I picked Factory.ai, why is Claude Sonnet being
# fetched?" complaint.
#
# Fix: a contextvars-based current_project_id. Python's contextvars are
# automatically inherited by `asyncio.create_task` (PEP 567 §running
# context), so setting it once at the top of each route handler makes it
# available to every nested LLM call regardless of how they're scheduled.
# ─────────────────────────────────────────────────────────────────────
_current_project_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lama_current_project_id", default=""
)


def set_current_project_id(project_id: str) -> None:
    """Pin the current project_id into the request-scoped context.

    Call this at the top of every stage route handler right after
    extracting `project_id` from the payload. Inherited automatically by
    `asyncio.create_task` so background-LLM coroutines see it.
    """
    _current_project_id.set((project_id or "").strip())


def get_current_project_id() -> str:
    """Read the contextvar — empty string when no project context is set."""
    try:
        return _current_project_id.get() or ""
    except LookupError:
        return ""


# iter-15.52 — Global "if Droid is up, always use it" resolver.
# Caches the resolution across calls so we don't hit Mongo on every LLM
# request. Cache is short-lived (30s) so operators can enable/disable
# Factory in Console without a backend restart.
_DEFAULT_FACTORY_PID_CACHE: Dict[str, Any] = {"pid": "", "expires_at": 0.0}
_DEFAULT_FACTORY_PID_TTL_SEC = 30.0


async def _resolve_default_factory_project_id() -> str:
    """Return a project_id to use for Factory routing when the caller did
    not supply one. Rule: if the Droid CLI probe is reachable (cheap
    cached probe from iter-15.48) AND at least one project has
    `settings.factory_orchestrator.enabled=true`, return that project's
    id. Otherwise return "" (fall through to fabric/Ollama).

    This is the single choke-point that makes "if Droid is active,
    always use it; else Ollama" the default — no call site has to
    plumb project_id."""
    import time as _t
    now = _t.time()
    cached = _DEFAULT_FACTORY_PID_CACHE
    if cached.get("expires_at", 0) > now:
        return cached.get("pid") or ""
    pid = ""
    try:
        from fabric.factory_cli import test_cli_connection
        probe = await test_cli_connection()
        if probe and probe.get("ok"):
            from db import projects as _projects_coll
            doc = await _projects_coll.find_one(
                {"settings.factory_orchestrator.enabled": True},
                {"_id": 0, "id": 1},
                sort=[("updated_at", -1)],
            )
            if doc and isinstance(doc.get("id"), str):
                pid = doc["id"]
    except Exception:  # noqa: BLE001
        pid = ""
    _DEFAULT_FACTORY_PID_CACHE["pid"] = pid
    _DEFAULT_FACTORY_PID_CACHE["expires_at"] = now + _DEFAULT_FACTORY_PID_TTL_SEC
    return pid


# iter-13.81.2 — request-scoped agent_key override. Frame-walk inference
# in `fabric_call` is best-effort and collapses every datamodel job to
# `datamodel.chat` (→ `datamodel.generate` bucket) even on re-runs. When
# a route handler KNOWS the call is a regenerate / re-run / gap pass it
# should call `set_current_agent_key("datamodel.regenerate")` (and similar
# for the other stages) so the Factory orchestrator picks up the right
# Console bucket. The contextvar is inherited by `asyncio.create_task`.
_current_agent_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lama_current_agent_key", default=""
)


def set_current_agent_key(agent_key: str) -> None:
    """Pin the agent_key for every nested LLM call in this async context.

    Highest-priority override: beats both the explicit ``agent_key=`` kwarg
    being omitted AND the frame-walk inference inside ``fabric_call``.
    Set to empty string ('') to clear.
    """
    _current_agent_key.set((agent_key or "").strip())


def get_current_agent_key() -> str:
    try:
        return _current_agent_key.get() or ""
    except LookupError:
        return ""


def _http_verify():
    """Return the `verify=` argument for httpx clients used by every LLM call.

    Controlled by env-vars (checked in order):
      • LAMA_DISABLE_SSL_VERIFY=1|true|yes  → return False (DEV/CORPORATE
        PROXY ONLY — silences certificate-verify failures from things like
        Zscaler / Netskope / corporate MITM SSL inspection. Do NOT enable in
        production.)
      • LAMA_CA_BUNDLE=/path/to/cacert.pem  → return that path (the *proper*
        fix when your network has its own root CA).
      • Otherwise → True (use system trust store).
    """
    if os.environ.get("LAMA_DISABLE_SSL_VERIFY", "").lower() in {"1", "true", "yes", "on"}:
        # Silence the InsecureRequestWarning that urllib3 / httpx emit once.
        try:
            import warnings as _w
            _w.filterwarnings("ignore", message="Unverified HTTPS request")
        except Exception:
            pass
        return False
    bundle = os.environ.get("LAMA_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle and os.path.exists(bundle):
        return bundle
    return True


# ─────────────────────────────────────────────────────────────────────
# iter-13.100 — Context-aware model selection.
# Automatically upgrade to a cloud model with larger context when the
# prompt exceeds the local model's context window.
# ─────────────────────────────────────────────────────────────────────
_OLLAMA_MODEL_CONTEXTS = {
    # Local models with limited context
    "llama2:7b": 4096,
    "llama3:latest": 8192,
    "llama3.1:8b": 131072,
    "qwen2.5-coder:7b": 32768,  # Advertised 32K but Ollama may load with 4K default
    "qwen2.5-coder:32b": 32768,
    "qwen3:4b": 262144,
    "qwen3-coder:30b": 262144,
    # Cloud models with large context
    "kimi-k2.7-code:cloud": 262144,
    "deepseek-v3.1:671b-cloud": 163840,
    "gpt-oss:20b-cloud": 131072,
    "gpt-oss:120b-cloud": 131072,
    "qwen3-coder:480b-cloud": 262144,
}

# Fallback models ordered by preference (large context, local first, then cloud)
# NOTE: Cloud models (ending in -cloud) require Ollama subscription
_LARGE_CONTEXT_FALLBACKS = [
    # Local models with large context (no subscription needed)
    "qwen3-coder:30b",           # 262K context, local 18GB
    "qwen2.5-coder:32b",         # 32K context, local 19GB
    "llama3.1:8b",               # 128K context, local 4.9GB
    # Cloud fallbacks (require subscription - may fail with 403)
    "kimi-k2.7-code:cloud",      # 262K context
    "deepseek-v3.1:671b-cloud",  # 164K context
]


def _estimate_tokens(messages: List[Dict]) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    total_chars = 0
    for m in messages:
        content = m.get("content") or ""
        total_chars += len(content)
    return total_chars // 4


async def _get_ollama_model_context(model_id: str, base_url: str = "") -> int:
    """Query Ollama for the model's actual context length."""
    # First check our static mapping
    if model_id in _OLLAMA_MODEL_CONTEXTS:
        static_ctx = _OLLAMA_MODEL_CONTEXTS[model_id]
        # For local models, verify with Ollama's actual loaded context
        if not model_id.endswith("-cloud"):
            try:
                url = (base_url or "http://host.docker.internal:11434").rstrip("/")
                if url.endswith("/v1"):
                    url = url[:-3]
                async with httpx.AsyncClient(timeout=3.0, verify=False) as client:
                    r = await client.get(f"{url}/api/tags")
                    if r.status_code == 200:
                        data = r.json()
                        for m in data.get("models", []):
                            if m.get("name") == model_id or m.get("model") == model_id:
                                details = m.get("details", {})
                                ctx = details.get("context_length", 0)
                                if ctx > 0:
                                    return ctx
            except Exception:
                pass
        return static_ctx
    # Unknown model — assume small context to be safe
    return 4096


async def _check_and_upgrade_model_for_context(
    messages: List[Dict],
    model_id: str,
    base_url: str = "",
    agent_key: str = "",
) -> tuple[str, bool]:
    """Check if the prompt fits in the model's context. If not, return a larger-context model.
    
    Returns: (model_id, was_upgraded)
    """
    import logging
    logger = logging.getLogger("lama.llm")
    
    # Skip check for cloud providers (OpenRouter, Anthropic, etc.)
    if not model_id or "ollama" not in (base_url or "").lower():
        # Not an Ollama model, assume adequate context
        return model_id, False
    
    # Skip for cloud Ollama models
    if model_id.endswith("-cloud"):
        return model_id, False
    
    estimated_tokens = _estimate_tokens(messages)
    model_context = await _get_ollama_model_context(model_id, base_url)
    
    # Add safety margin (20%) for response tokens
    required_context = int(estimated_tokens * 1.2)
    
    if required_context <= model_context:
        return model_id, False
    
    # Context too small — try to find a cloud fallback
    logger.warning(
        "iter-13.100: model=%s context=%d tokens but prompt requires ~%d tokens. "
        "Auto-upgrading to a larger-context model for agent=%s",
        model_id, model_context, required_context, agent_key,
    )
    
    for fallback in _LARGE_CONTEXT_FALLBACKS:
        fallback_ctx = _OLLAMA_MODEL_CONTEXTS.get(fallback, 0)
        if fallback_ctx >= required_context:
            logger.info(
                "iter-13.100: upgraded %s → %s (context %d → %d) for agent=%s",
                model_id, fallback, model_context, fallback_ctx, agent_key,
            )
            return fallback, True
    
    # No suitable fallback found — proceed with original and let it fail
    logger.warning(
        "iter-13.100: no suitable large-context fallback found. "
        "Proceeding with %s (may fail or truncate). Consider using OpenRouter.",
        model_id,
    )
    return model_id, False


# NOTE (iter-13.30): no hard-coded vendor slugs anywhere in the call paths.
# `AVAILABLE_MODELS` below is ONLY a last-resort enumeration shown to the
# UI when the Console has no active providers configured (e.g. first boot).
# Once a provider is added in Console → Models, `get_available_models()`
# returns the provider's actual `model_catalogue` instead — every routing
# decision then flows from `Console.routing[tier]` + `AGENT_COMPLEXITY`.
AVAILABLE_MODELS = [
    # Bootstrap-only enumeration — shown when Console has no providers yet.
    # Add/remove models in Console → Models · Provider catalogue. The
    # complexity tiers (low/medium/high) defined in
    # `fabric.model_fabric.PROVIDER_PRESETS.default_models` drive the
    # actual selection at call time.
    {"id": "anthropic/claude-opus-4.7", "label": "Claude Opus 4.7", "default_for": ["high"]},
    {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6", "default_for": ["medium"]},
    {"id": "deepseek/deepseek-chat", "label": "DeepSeek Chat", "default_for": ["low"]},
    {"id": "qwen/qwen-2.5-72b-instruct", "label": "Qwen 2.5 72B", "default_for": ["fallback"]},
    {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Llama 3.3 70B", "default_for": ["fallback"]},
    # Ollama — local runtime, no key required. Listed so they show up in the
    # dropdown before a Console provider exists. Selection still requires an
    # actual Ollama provider configured in Console → Models (paste empty key
    # + http://localhost:11434/v1 base URL).
    {"id": "llama3:latest", "label": "Llama 3 — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "llama3.1:8b", "label": "Llama 3.1 8B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "llama2:7b", "label": "Llama 2 7B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "qwen2.5-coder:32b", "label": "Qwen 2.5 Coder 32B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "qwen3:4b", "label": "Qwen 3 4B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "qwen3-coder:30b", "label": "Qwen 3 Coder 30B — Ollama local", "default_for": ["local"], "provider": "ollama"},
    {"id": "deepseek-v3.1:671b-cloud", "label": "DeepSeek v3.1 671B — Ollama", "default_for": ["cloud"], "provider": "ollama"},
    {"id": "gpt-oss:20b-cloud", "label": "GPT OSS 20B — Ollama", "default_for": ["cloud"], "provider": "ollama"},
    {"id": "gpt-oss:120b-cloud", "label": "GPT OSS 120B — Ollama", "default_for": ["cloud"], "provider": "ollama"},
]


async def get_available_models() -> List[Dict]:
    """Console-sourced model list (iter-13.30).

    Returns the active providers' catalogue when at least one provider is
    configured in Console → Models; otherwise falls back to the bootstrap
    enumeration above. The frontend dropdown consumes this — there is NO
    hard-coded vendor preference anywhere downstream.
    """
    try:
        from db import model_providers as mp_col
        cursor = mp_col.find({"is_active": True}, {"_id": 0}).sort([("is_default", -1)])
        out: List[Dict] = []
        seen: set = set()
        async for p in cursor:
            routing = p.get("routing") or {}
            tier_for: Dict[str, str] = {v: k for k, v in routing.items() if v}
            for m in (p.get("models") or []):
                mid = m.get("id") or ""
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                tier = tier_for.get(mid, "")
                tags = [tier] if tier else []
                out.append({
                    "id": mid,
                    "label": m.get("label") or mid,
                    "provider": p.get("name") or p.get("provider_type", ""),
                    "default_for": tags,
                })
        if out:
            return out
    except Exception:
        pass
    return AVAILABLE_MODELS


def _env_default_model() -> str:
    """Last-resort model when fabric/Console are unavailable. Env-driven,
    no hard-coded vendor slug. Falls back to empty so the caller surfaces
    a clear 'configure a provider in Console' error rather than burning a
    request on a non-existent model."""
    return (os.environ.get("LAMA_DEFAULT_MODEL", "") or "").strip()


# ── iter-13.115 — OLLAMA-FIRST FALLBACK ───────────────────────────────
#
# Operator preference: when the primary route (Factory.ai or a cloud
# fabric provider) is unreachable / out of credits / 5xx, prefer a
# locally-configured Ollama provider over the env-var OpenRouter
# fallback. Rationale:
#   • Ollama is free, local, and respects the user's "I don't want
#     surprise cloud bills" intent.
#   • OPENROUTER_API_KEY is often unfunded in dev/test environments,
#     so falling back to it just turns a Factory outage into a 402.
#   • The user explicitly asked: "when factory is not reachable, do
#     not try to connect openrouter, better connect ollama."
#
# This helper finds the first active provider that looks like Ollama
# (provider_type=="ollama" OR base_url points at localhost / 127.0.0.1
# / host.docker.internal / 0.0.0.0) and calls it via the existing
# `fabric_chat` path with the agent temporarily pinned to that
# provider. Returns the routed dict on success, or None when no Ollama
# provider is configured or the call itself fails (the caller then
# proceeds to the env-var OpenRouter fallback as before).
#
# Opt out with LAMA_OLLAMA_FIRST_FALLBACK=0 to restore the prior
# behaviour (straight env-var OpenRouter fallback).
async def _try_ollama_fallback(
    *,
    messages: List[Dict],
    agent_key: str,
    project_id: str,
    kwargs: Dict,
) -> Optional[Dict]:
    """Attempt one LLM call via the first active Ollama-shaped provider.

    Returns the response dict on success, None when no Ollama provider
    is configured / the call fails. Best-effort — never raises.
    """
    if (os.environ.get("LAMA_OLLAMA_FIRST_FALLBACK", "1") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    try:
        from db import model_providers as mp_col, agent_configs as ac_col
    except Exception:  # noqa: BLE001
        return None
    try:
        ollama_prov = None
        async for p in mp_col.find({"is_active": True}, {"_id": 0}).sort([("priority", 1)]):
            ptype = (p.get("provider_type") or "").lower()
            burl = (p.get("base_url") or "").lower()
            if (
                ptype == "ollama"
                or "localhost" in burl
                or "127.0.0.1" in burl
                or "host.docker.internal" in burl
                or "0.0.0.0" in burl
            ):
                ollama_prov = p
                break
        if not ollama_prov:
            return None
    except Exception:  # noqa: BLE001
        return None

    prov_id = ollama_prov.get("id") or ""
    prov_name = ollama_prov.get("name") or prov_id or "ollama"
    # Resolve a model id: prefer this provider's routing for the agent's
    # complexity tier, else first entry in its catalogue.
    model_id = ""
    try:
        from fabric.model_fabric import AGENT_COMPLEXITY
        agent = await ac_col.find_one({"key": agent_key}, {"_id": 0}) if agent_key else None
        complexity = (agent or {}).get("complexity") \
            or AGENT_COMPLEXITY.get(agent_key, "medium")
        routing = ollama_prov.get("routing") or {}
        model_id = routing.get(complexity) or routing.get("medium") or routing.get("low") or ""
        if not model_id:
            cat = ollama_prov.get("models") or []
            if cat:
                model_id = (cat[0] or {}).get("id") or ""
    except Exception:  # noqa: BLE001
        model_id = ""
    if not model_id:
        return None

    # Pin the agent to this provider for the duration of the call (matches
    # the pattern used by fabric_chat_with_failover).
    prior_pin = ""
    try:
        agent_doc = await ac_col.find_one({"key": agent_key}, {"_id": 0, "provider_id": 1}) if agent_key else None
        prior_pin = (agent_doc or {}).get("provider_id", "") or ""
        if agent_key and prov_id:
            await ac_col.update_one(
                {"key": agent_key},
                {"$set": {"provider_id": prov_id}},
                upsert=True,
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        import logging as _lg
        _lg.getLogger("lama.llm").info(
            "iter-13.115: Ollama-first fallback — routing agent=%s via "
            "provider=%s model=%s (factory/fabric primary unreachable).",
            agent_key, prov_name, model_id,
        )
    except Exception:
        pass

    try:
        from fabric.model_fabric import fabric_chat
        # iter-13.100 — Check if prompt fits in model context, upgrade if needed
        base_url = ollama_prov.get("base_url") or ""
        upgraded_model, was_upgraded = await _check_and_upgrade_model_for_context(
            messages=messages,
            model_id=model_id,
            base_url=base_url,
            agent_key=agent_key,
        )
        if was_upgraded:
            model_id = upgraded_model
            try:
                import logging as _lg
                _lg.getLogger("lama.llm").info(
                    "iter-13.100: context-upgraded to %s for agent=%s",
                    model_id, agent_key,
                )
            except Exception:
                pass
        result = await fabric_chat(
            messages=messages,
            agent_key=agent_key or "unknown",
            project_id=project_id,
            model_override=model_id,
            max_tokens=kwargs.get("max_tokens", 0) or 0,
            temperature=kwargs.get("temperature", 0.3),
            timeout=kwargs.get("timeout", 600.0),
            response_format=kwargs.get("response_format"),
        )
        if isinstance(result, dict) and (result.get("content") or "").strip():
            result = await _sanitize_response_inplace(
                result, project_id, agent_key, source="ollama-fallback",
            )
            return result
        return None
    except Exception as exc:  # noqa: BLE001
        try:
            import logging as _lg
            _lg.getLogger("lama.llm").warning(
                "iter-13.115: Ollama fallback via %s failed: %s",
                prov_name, str(exc)[:240],
            )
        except Exception:
            pass
        return None
    finally:
        # Restore the prior pin so the failover state isn't permanent.
        try:
            if agent_key:
                await ac_col.update_one(
                    {"key": agent_key},
                    {"$set": {"provider_id": prior_pin}},
                )
        except Exception:  # noqa: BLE001
            pass


async def chat_completion(
    messages: List[Dict[str, str]],
    model: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 90.0,
) -> Dict:
    """Send a chat completion request to OpenRouter. Returns dict with `content` and `usage`.

    `model` empty → resolves from `LAMA_DEFAULT_MODEL` env-var. Raises a
    clear error if both are empty so the caller learns to configure a
    provider in Console → Models instead of silently routing to a guess.

    UNREACHABLE as of iter-14.31 — see docs/RECON.md. Every stage route
    binds the name `chat_completion` to `fabric_call` at import
    (``from llm import fabric_call as chat_completion``), so no caller
    anywhere resolves to THIS function; and iter-14.31 removed the
    env-var OpenRouter fallback inside `_fabric_call_impl` that used to
    be its last caller, replacing it with a hard failure. Deliberately
    NOT given the `response_format` plumbing the live path received —
    adding parameters to unreachable code is the waste this audit
    exists to remove. Flagged for a removal decision in Phase 2 rather
    than deleted here, because a fallback deserves its own evidence
    trail and its own commit.
    """
    if not OPENROUTER_API_KEY:
        # iter-14.8 — make this error actionable. Users see this on the
        # factory_cli path when droid times out and env-var OpenRouter is
        # the last fallback; the historical message ("OPENROUTER_API_KEY
        # not configured") looked like a bug when the user only intended
        # to use droid. Spell out the remediation options so the fix is
        # obvious from the log line alone.
        raise RuntimeError(
            "OPENROUTER_API_KEY not configured — the factory-cli path "
            "failed or timed out, and no fallback LLM is available. Fix "
            "one of: (a) verify `droid auth login` inside the container "
            "and that the model is responsive (`docker exec lama droid "
            "exec -m claude-sonnet-4-6 \"ping\"`), (b) set OPENROUTER_API_KEY "
            "in your .env / compose environment so the fallback works, "
            "or (c) reduce the SRS prompt size (Discovery → shrink KB "
            "scope) if droid is legitimately timing out on 80K-token "
            "prompts."
        )
    model = (model or _env_default_model()).strip()
    if not model:
        raise RuntimeError(
            "No model resolved — configure a provider in Console → Models "
            "(or set LAMA_DEFAULT_MODEL env-var for the legacy fallback)."
        )

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://lama.local",
        "X-Title": "LAMA",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=timeout, verify=_http_verify()) as client:
        try:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.WriteTimeout, httpx.PoolTimeout, OSError) as te:
            # iter-13.50 — surface DNS/connect failures with the host name so
            # the user knows WHERE the request died. Caller code (arch jobs)
            # checks `isinstance(e, TransportError)` to abort the whole job
            # instead of writing the error into per-section content.
            classified = _classify_transport_error(te, host_hint=OPENROUTER_BASE_URL) \
                or f"Transport error talking to {OPENROUTER_BASE_URL}: {te}"
            raise TransportError(classified) from te
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text}")
        data = resp.json()

    # Defensive: some providers (or routing layers) occasionally return a 200
    # with no `choices` (rate-limited downstream, model rotation gap, etc.).
    # Surface a clear error instead of raising KeyError mid-section.
    choices = data.get("choices") or []
    if not choices:
        err = data.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(
            f"OpenRouter returned no choices for model '{model}': "
            f"{msg or 'empty response body'} (raw={str(data)[:300]})"
        )
    choice = choices[0]
    content = ((choice or {}).get("message") or {}).get("content") or ""
    usage = data.get("usage", {})
    return {
        "content": content,
        "model": data.get("model", model),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def estimate_tokens(text: str) -> int:
    """Estimate token count for `text`.

    iter-14.12 — prefer `tiktoken` (cl100k_base) when available for a
    ~15-30% more accurate count vs the legacy `len(text)//4` heuristic
    on JSON / TOON / code prompts. Falls back to the heuristic when
    tiktoken isn't installed or fails to encode.

    Kept synchronous and side-effect-free so it can be called from hot
    paths (chat, SRS section budgeting, session-memory accounting).
    """
    if not text:
        return 1
    enc = _get_tiktoken_encoder()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text, disallowed_special=())))
        except Exception:  # noqa: BLE001 — tokeniser must never break a call
            pass
    return max(1, len(text) // 4)


# Lazy cached tokeniser — tiktoken's `get_encoding` warms up ~40ms.
_TIKTOKEN_ENCODER = None
_TIKTOKEN_ENCODER_LOADED = False


def _get_tiktoken_encoder():
    """Return a cached cl100k_base encoder, or None if unavailable."""
    global _TIKTOKEN_ENCODER, _TIKTOKEN_ENCODER_LOADED
    if _TIKTOKEN_ENCODER_LOADED:
        return _TIKTOKEN_ENCODER
    _TIKTOKEN_ENCODER_LOADED = True
    try:
        import tiktoken  # type: ignore[import-not-found]
        _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 — offline env / package absent
        _TIKTOKEN_ENCODER = None
    return _TIKTOKEN_ENCODER


# ── iter-13.101 — LLM trace recorder ─────���────────────────────────────
#
# Every public `fabric_call(...)` invocation is wrapped to capture:
#   • request_prompt       — full messages array (no truncation; we DO
#                            redact obvious secrets — see _redact_msg)
#   • response_prompt      — assistant `content` (and provider raw if any)
#   • request_time         — ISO-8601 UTC at call-start
#   • response_time        — ISO-8601 UTC at call-end (success or failure)
#   • elapsed_ms           — response_time − request_time, integer ms
#   • stage / agent_key    — already inferred by _fabric_call_impl
#   • status               — "SUCCESS" | "FAIL"
#   • error_reason         — exception class + str(exc) on FAIL, else ""
#
# The trace doc lands in `llm_traces` (one row per call). The
# corresponding `audit_log` row carries only the `trace_id` pointer so
# the human-readable log stays short and the Audit page only pays the
# bandwidth for full prompts when the user clicks "Detail Log Trace".
#
# Failure-safe by design: any persistence error is swallowed (we'd
# rather lose a trace than abort an LLM call).
def _trace_stage_for_agent(agent_key: str) -> str:
    """Best-effort agent_key → Stage name mapping."""
    if not agent_key:
        return ""
    k = agent_key.split(".", 1)[0].lower()
    return {
        "srs":       "Discovery",
        "kb":        "Discovery",
        "ontology":  "Discovery",
        "chat":      "Discovery",
        "datamodel": "DataModel",
        "arch":      "Architecture",
        "codegen":   "CodeGen",
        "living":    "Living",
    }.get(k, "")


_REDACT_KEY_PATTERNS = (
    "api_key", "apikey", "secret", "password", "token", "authorization",
)


def _redact_msg(m):
    """Copy of a single message with obvious secret keys redacted."""
    if not isinstance(m, dict):
        return m
    out = {}
    for k, v in m.items():
        kl = str(k).lower()
        if any(p in kl for p in _REDACT_KEY_PATTERNS):
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out


async def _record_llm_trace(
    *,
    trace_id: str,
    messages: List[Dict],
    response: Optional[Dict],
    started_iso: str,
    started_perf: float,
    agent_key: str,
    project_id: str,
    kwargs: Dict,
    status: str,
    error_reason: str,
) -> None:
    """Persist one trace doc + thin pointer in `audit_log`. Best-effort."""
    try:
        import time as _time
        from datetime import datetime as _dt
        finished_perf = _time.perf_counter()
        finished_iso = _dt.utcnow().isoformat()
        elapsed_ms = int(round((finished_perf - started_perf) * 1000))
        resp_content = ""
        resp_model = ""
        resp_usage = None
        if isinstance(response, dict):
            resp_content = response.get("content") or ""
            resp_model = response.get("model") or ""
            resp_usage = response.get("usage")
        stage_name = _trace_stage_for_agent(agent_key)
        safe_kwargs = {}
        for k, v in (kwargs or {}).items():
            if any(p in str(k).lower() for p in _REDACT_KEY_PATTERNS):
                continue
            if callable(v):
                continue
            safe_kwargs[k] = v
        trace_doc = {
            "trace_id":         trace_id,
            "project_id":       project_id or "",
            "stage":            stage_name,
            "agent_key":        agent_key or "",
            "status":           status,
            "error_reason":     error_reason or "",
            "request_time":     started_iso,
            "response_time":    finished_iso,
            "elapsed_ms":       elapsed_ms,
            "request_prompt":   [_redact_msg(m) for m in (messages or [])],
            "response_prompt":  resp_content,
            "response_model":   resp_model,
            "response_usage":   resp_usage,
            "kwargs":           safe_kwargs,
            "raw_response":     (
                {k: v for k, v in response.items() if k != "content"}
                if isinstance(response, dict) else None
            ),
        }
        try:
            from db import llm_traces, audit_log as _audit
            await llm_traces.insert_one(trace_doc)
            await _audit.insert_one({
                "action":     f"llm.call:{stage_name or 'unknown'}:{status.lower()}",
                "project_id": project_id or "",
                "at":         finished_iso,
                "details": {
                    "trace_id":   trace_id,
                    "agent_key":  agent_key or "",
                    "stage":      stage_name,
                    "status":     status,
                    "elapsed_ms": elapsed_ms,
                    "model":      resp_model,
                    "error":      error_reason or "",
                },
            })
        except Exception:
            pass
    except Exception:
        pass


async def probe_generation_providers(project_id: str = "") -> dict:
    """iter-14.32 — Health-probe every possible generation provider IN
    PRIORITY ORDER: Factory.ai → any Console provider (Anthropic / OpenAI
    / Groq / Ollama / …) → local Ollama.

    Returns a dict:
      {
        "chosen": "factory" | "anthropic" | "ollama" | "openai" | "groq" | None,
        "detail": <human-readable one-line reason>,
        "checks": [
          {"provider": "factory", "healthy": bool, "reason": "..."},
          {"provider": "anthropic", "healthy": bool, "reason": "..."},
          {"provider": "ollama", "healthy": bool, "reason": "..."},
          ...
        ],
      }

    Cheap: no LLM call is made. We only verify (a) is the provider
    configured/enabled, (b) is the binary/URL reachable, (c) for Ollama
    the /api/tags endpoint responds in <2s. Payment/quota errors can
    only be detected at real-call time; the fabric_call fall-forward
    logic handles those separately.
    """
    checks: list[dict] = []
    chosen: str | None = None
    detail: str = ""

    # ── 1) Factory.ai (droid CLI or API) ─────────────────────────────
    factory_ok = False
    factory_reason = ""
    try:
        _binary = shutil.which("droid")
        if not _binary:
            factory_reason = "droid binary not found in PATH"
        else:
            _fcfg = {}
            if project_id:
                try:
                    from factory_orchestrator import get_project_factory_orchestrator_config as _gpfc
                    _fcfg = await _gpfc(project_id, include_secret=False) or {}
                except Exception as _fe:  # noqa: BLE001
                    factory_reason = f"factory config load failed: {str(_fe)[:120]}"
            _enabled = bool(_fcfg.get("enabled"))
            _env_mode = (os.environ.get("LAMA_FACTORY_MODE", "") or "").strip().lower()
            if _enabled or _env_mode in {"cli", "api"}:
                factory_ok = True
                factory_reason = f"droid={_binary}, mode={_fcfg.get('mode') or _env_mode or 'cli'}"
            else:
                factory_reason = "factory_orchestrator.enabled=False and LAMA_FACTORY_MODE unset"
    except Exception as _e:  # noqa: BLE001
        factory_reason = f"probe error: {str(_e)[:120]}"
    checks.append({"provider": "factory", "healthy": factory_ok, "reason": factory_reason})
    if factory_ok and not chosen:
        chosen = "factory"
        detail = f"Factory.ai ready ({factory_reason})"

    # ── 2) Console-configured providers (Anthropic / OpenAI / Groq / Ollama) ──
    # Priority within Console: Anthropic → OpenAI → Groq → Ollama.
    # Any other type (openrouter, deepseek, …) is treated as generic
    # "cloud" and only picked if none of the preferred types match.
    # Azure leads because an enterprise deployment is a committed, funded
    # contract — when one is configured it is the intended route, not a
    # fallback. Gemini sits with the other cloud vendors. Ollama stays last
    # of the configured providers: it is free and local, so it is the right
    # thing to fall back TO but the wrong thing to prefer over a paid
    # endpoint the operator deliberately set up.
    _preferred_order = ["azure", "anthropic", "openai", "gemini", "groq", "ollama"]
    _providers_by_type: dict[str, dict] = {}
    try:
        from db import model_providers as _mp_col
        async for _p in _mp_col.find(
            {"is_active": True},
            {"_id": 0, "name": 1, "provider_type": 1, "base_url": 1, "api_key": 1, "is_default": 1},
        ):
            _pt = (_p.get("provider_type") or "").lower()
            if _pt and _pt not in _providers_by_type:
                _providers_by_type[_pt] = _p
    except Exception as _e:  # noqa: BLE001
        checks.append({"provider": "console", "healthy": False,
                       "reason": f"providers query failed: {str(_e)[:120]}"})

    for _ptype in _preferred_order + [t for t in _providers_by_type if t not in _preferred_order]:
        _p = _providers_by_type.get(_ptype)
        if not _p:
            if _ptype in _preferred_order:
                checks.append({"provider": _ptype, "healthy": False,
                               "reason": "no active Console provider of this type"})
            continue
        _ok = False
        _reason = ""
        if _ptype == "ollama":
            _url = (_p.get("base_url") or "http://host.docker.internal:11434").rstrip("/")
            # iter-14.25.7 — Console stores the OpenAI-compat base
            # (…/v1) but /api/tags lives at the Ollama root. Without
            # this strip the health probe always 404s and the whole
            # fabric_call() health-gate silently skips Ollama.
            if _url.endswith("/v1"):
                _url = _url[:-3]
            try:
                async with httpx.AsyncClient(timeout=2.0, verify=False) as _c:
                    _r = await _c.get(f"{_url}/api/tags")
                if _r.status_code == 200:
                    _ok = True
                    _reason = f"reachable at {_url}"
                else:
                    _reason = f"{_url}/api/tags → HTTP {_r.status_code}"
            except Exception as _e:  # noqa: BLE001
                _reason = f"unreachable: {str(_e)[:120]}"
        else:
            # Cloud providers: only verify a key is present.
            # We deliberately don't burn a real call to probe — that
            # would cost tokens on every fabric_call invocation.
            if (_p.get("api_key") or "").strip():
                _ok = True
                _reason = f"API key configured (provider={_p.get('name') or _ptype})"
            else:
                _reason = f"no API key set for {_p.get('name') or _ptype}"
        checks.append({"provider": _ptype, "healthy": _ok, "reason": _reason})
        if _ok and not chosen:
            chosen = _ptype
            detail = f"{_ptype} ready ({_reason})"

    # ── 3) Env-var Ollama fallback (LAMA_OLLAMA_BASE_URL) ────────────
    # Covers "local Ollama exists but user never added it as a Console
    # provider" — the last resort before hard-fail.
    if not chosen:
        _env_ollama = (os.environ.get("LAMA_OLLAMA_BASE_URL") or "").strip().rstrip("/")
        if _env_ollama.endswith("/v1"):
            _env_ollama = _env_ollama[:-3]
        if _env_ollama:
            _ok = False
            try:
                async with httpx.AsyncClient(timeout=2.0, verify=False) as _c:
                    _r = await _c.get(f"{_env_ollama}/api/tags")
                if _r.status_code == 200:
                    _ok = True
            except Exception:  # noqa: BLE001
                pass
            checks.append({"provider": "env-ollama", "healthy": _ok,
                           "reason": f"{_env_ollama} → HTTP {'200' if _ok else 'unreachable'}"})
            if _ok:
                chosen = "ollama"
                detail = f"env-Ollama ready ({_env_ollama})"

    if not chosen:
        detail = "no generation provider is healthy — configure Factory.ai, add a Console provider, or set LAMA_OLLAMA_BASE_URL"

    return {"chosen": chosen, "detail": detail, "checks": checks}


async def fabric_call(
    messages: List[Dict],
    agent_key: str = "",
    project_id: str = "",
    **kwargs,
) -> Dict:
    """Public LLM call entry-point. Thin wrapper around `_fabric_call_impl`
    that records a trace into the `llm_traces` collection for the
    Audit page "Detail Log Trace" feature (iter-13.101).

    Behaviour is otherwise identical to the previous fabric_call —
    same signature, same return shape, same exception propagation.
    """
    import time as _time
    from datetime import datetime as _dt
    from uuid import uuid4 as _uuid4
    trace_id = _uuid4().hex
    started_iso = _dt.utcnow().isoformat()
    started_perf = _time.perf_counter()
    try:
        result = await _fabric_call_impl(
            messages=messages,
            agent_key=agent_key,
            project_id=project_id,
            **kwargs,
        )
        if isinstance(result, dict):
            result.setdefault("trace_id", trace_id)
        await _record_llm_trace(
            trace_id=trace_id,
            messages=messages,
            response=result,
            started_iso=started_iso,
            started_perf=started_perf,
            agent_key=agent_key,
            project_id=project_id,
            kwargs=kwargs,
            status="SUCCESS",
            error_reason="",
        )
        return result
    except BaseException as exc:  # noqa: BLE001
        await _record_llm_trace(
            trace_id=trace_id,
            messages=messages,
            response=None,
            started_iso=started_iso,
            started_perf=started_perf,
            agent_key=agent_key,
            project_id=project_id,
            kwargs=kwargs,
            status="FAIL",
            error_reason=f"{type(exc).__name__}: {exc}",
        )
        raise


async def _fabric_call_impl(
    messages: List[Dict],
    agent_key: str = "",
    project_id: str = "",
    **kwargs,
) -> Dict:
    """Drop-in replacement for chat_completion. Routes through Model Fabric if providers
    are configured, else falls back to legacy chat_completion. Accepts both
    `model=...` (legacy) and `model_override=...` kwargs.

    iter-13.101 — Renamed from `fabric_call` so the public `fabric_call`
    name can be a thin instrumentation wrapper that records every LLM
    call into the `llm_traces` collection (for the Audit page's
    "Detail Log Trace" button). All internal callers in this module
    AND every `from llm import fabric_call` import in the rest of the
    codebase resolve to the wrapper — preserving the recording even
    for monkey-patched stubs in tests (`monkeypatch.setattr(llm,
    "fabric_call", ...)` replaces the wrapper, which is exactly the
    behaviour those tests rely on).
    """
    # iter-14.32 — PRE-FLIGHT GENERATION-PROVIDER HEALTH CHECK.
    # Priority chain: Factory.ai → Console providers (Anthropic → OpenAI
    # → Groq → Ollama) → env-var Ollama → HARD FAIL. Confidence scoring
    # is a separate engine (LangGraph+HF) and is unaffected.
    #
    # This runs on every fabric_call so the operator sees a clean,
    # actionable error the moment a provider goes down, instead of an
    # opaque 402 / connection-refused / auth failure buried in a stack
    # trace.
    try:
        _probe = await probe_generation_providers(project_id)
    except Exception as _pe:  # noqa: BLE001
        _probe = {"chosen": None, "detail": f"probe failed: {_pe}", "checks": []}
    if not _probe.get("chosen"):
        _lines = "; ".join(
            f"{c['provider']}={'ok' if c['healthy'] else 'no'} ({c['reason']})"
            for c in _probe.get("checks", [])
        )
        raise RuntimeError(
            "No generation provider is healthy. "
            "LAMA checked Factory.ai → Console providers (Anthropic / "
            "OpenAI / Groq / Ollama) → env-Ollama and none were reachable "
            "with a working credential. "
            "Fix ONE of: (a) top up Factory.ai credits + set "
            "`settings.factory_orchestrator.enabled=true` on the project, "
            "(b) add an Anthropic / OpenAI / Groq API key or a local "
            "Ollama URL under Console → Models, or (c) set "
            "`LAMA_OLLAMA_BASE_URL=http://host.docker.internal:11434` "
            "in .env if you run Ollama on the host. "
            f"Health report: {_lines}"
        )
    # Stash probe result so downstream error handlers can reference it
    # (via locals()) when Factory returns 402 / auth error mid-call.
    _preflight_probe = _probe
    # Infer agent_key + project_id from call stack when omitted.
    frame = None
    # iter-13.81.2 — request-scoped override beats frame-walk inference so
    # routes can pin `agent_key="datamodel.regenerate"` (etc.) before
    # spawning background jobs.
    if not agent_key:
        _ctx_key = get_current_agent_key()
        if _ctx_key:
            agent_key = _ctx_key
    if not agent_key:
        try:
            import inspect
            frame = inspect.currentframe().f_back
            mod = frame.f_globals.get("__name__", "")
            # iter-13.45 — Walk up to 8 frames to find a more specific
            # `_run_<name>_job` coroutine; the previous flat module→agent
            # mapping collapsed every Architecture sub-stage into the
            # generic `arch.chat` agent (wrong tier in Console routing AND
            # wrong Factory.ai sub-session). When the call is wrapped in
            # asyncio.create_task even the immediate f_back is the loop
            # callback — keep walking until we hit a real function name.
            _JOB_TO_AGENT = {
                # Architecture sub-stages
                "_run_recommend_job":     "arch.recommend",
                "_run_hld_job":           "arch.hld",
                "_run_lld_job":           "arch.lld",
                "_run_seq_job":           "arch.sequence",
                "_run_api_contracts_job": "arch.api_contracts",
                "arch_chat":              "arch.chat",
                # CodeGen sub-stages (best-effort — names may evolve)
                "_run_codegen_job":       "codegen.service",
                "_run_frontend_job":      "codegen.frontend",
                "_run_gap_recovery_job":  "codegen.gap_recovery",
                # iter-13.81.2 — DataModel jobs (OLTP/OLAP/migration scripts +
                # the LLM polish helpers). Before this map they all fell
                # through to the module-level fallback "datamodel.chat",
                # which routed to `datamodel.generate` even on REGENERATE,
                # so the Console's `datamodel.regenerate` model pick was
                # never observed by Factory.
                "_run_oltp_job":          "datamodel.oltp",
                "_run_olap_job":          "datamodel.olap",
                "_run_scripts_job":       "datamodel.scripts",
                "_llm_polish_enums":      "datamodel.oltp",
                "_llm_polish_comments":   "datamodel.oltp",
            }
            inferred: str | None = None
            _f = frame
            for _ in range(8):
                if _f is None:
                    break
                fn_name = _f.f_code.co_name if _f.f_code else ""
                if fn_name in _JOB_TO_AGENT:
                    inferred = _JOB_TO_AGENT[fn_name]
                    break
                _f = _f.f_back
            if inferred:
                agent_key = inferred
            else:
                agent_key = {
                    "routes.architecture": "arch.chat",
                    "routes.codegen": "codegen.chat",
                    "routes.datamodel": "datamodel.chat",
                    "routes.srs": "srs.generate",
                    "routes.chat": "srs.gap_question",
                }.get(mod, "unknown")
        except Exception:
            agent_key = "unknown"
    if not project_id:
        try:
            import inspect
            frame = frame or inspect.currentframe().f_back
            if frame:
                # Most route handlers carry project_id directly.
                local_pid = frame.f_locals.get("project_id")
                if isinstance(local_pid, str) and local_pid.strip():
                    project_id = local_pid.strip()
                # Chat/some handlers keep it under req/payload objects.
                if not project_id:
                    req = frame.f_locals.get("req")
                    if req is not None:
                        v = getattr(req, "project_id", "") or (req.get("project_id") if isinstance(req, dict) else "")
                        if isinstance(v, str) and v.strip():
                            project_id = v.strip()
                if not project_id:
                    payload = frame.f_locals.get("payload")
                    if isinstance(payload, dict):
                        v = payload.get("project_id") or payload.get("projectId") or ""
                        if isinstance(v, str) and v.strip():
                            project_id = v.strip()
        except Exception:
            pass

    # iter-13.38 — final fallback: pull from the request-scoped contextvar.
    # This is the ONLY path that survives `asyncio.create_task(chat_completion(...))`
    # because frame.f_back inside an asyncio Task points to the loop, not to
    # the route handler that created the task. DataModel jobs (and several
    # Architecture / CodeGen paths) wrap chat_completion in create_task, so
    # without this fallback their Factory.ai routing was silently skipped.
    if not project_id:
        project_id = get_current_project_id()

    # iter-15.52 — Global "Droid-is-up → always use it" fallback.
    # If NO project_id was resolved above but the Droid CLI is reachable
    # (cached fast probe from iter-15.48) AND some project has
    # `settings.factory_orchestrator.enabled=true`, borrow that project's
    # id so Factory routing kicks in. This makes Factory the default route
    # for any call site that forgot to plumb project_id (e.g. the
    # transformer pipeline pre-15.51) — no more silent Ollama fallbacks
    # when Droid is healthy.
    if not project_id:
        try:
            project_id = await _resolve_default_factory_project_id()
        except Exception:  # noqa: BLE001 — never break the LLM layer
            pass

    # iter-13.99.1 — auto-prepend the workspace-isolation directive to
    # the system message for EVERY project-scoped LLM call. This way
    # SRS / Architecture / CodeGen / DataModel / Living / Chat all
    # inherit the same workspace contract without per-stage prompt
    # edits. Best-effort: any failure passes the messages through
    # unchanged.
    if project_id and messages:
        try:
            from kb.workspace_isolation import (
                workspace_root_for_project,
                workspace_directive_for_prompt,
                workspace_kb_summary_for_prompt,
            )
            _ws_root = await workspace_root_for_project(project_id)
            _directive = workspace_directive_for_prompt(_ws_root)
            # iter-13.81.8 — also inject a KB readiness snapshot so Factory
            # Droid sees concrete file/entity counts up-front and doesn't
            # try to verify the workspace via shell tools (the path is
            # virtual; the probe always returns "no such directory" and
            # the Droid then reports "no source files discovered").
            _kb_snapshot = await workspace_kb_summary_for_prompt(project_id)
            _prepend = ""
            if _directive:
                _prepend += _directive
            if _kb_snapshot:
                _prepend += ("\n\n" if _prepend else "") + _kb_snapshot
            if _prepend:
                # Make a SHALLOW copy of messages to avoid mutating the
                # caller's list. Prepend to the first system message if
                # present; otherwise insert a new system message at [0].
                _msgs2 = list(messages)
                if _msgs2 and isinstance(_msgs2[0], dict) and _msgs2[0].get("role") == "system":
                    _first = dict(_msgs2[0])
                    _existing = _first.get("content", "") or ""
                    # Skip re-prepending if the directive is already present
                    # (e.g. SRS path that builds its own directive inline).
                    if "iter-13.99" not in _existing or "PROJECT-ISOLATED WORKSPACE" not in _existing:
                        _first["content"] = _prepend + "\n\n" + _existing
                        _msgs2[0] = _first
                else:
                    _msgs2.insert(0, {"role": "system", "content": _prepend})
                messages = _msgs2
        except Exception:  # noqa: BLE001 — never break the LLM layer
            pass

    # Optional: route ALL project LLM calls via Factory Sessions API when
    # the per-project orchestrator toggle is enabled in Console.
    if project_id:
        from factory_orchestrator import (
            route_via_factory_orchestrator,
            get_project_factory_orchestrator_config,
            _resolve_model_for_agent,
            _bucket_for_agent,
        )

        # iter-13.81.6 — CONSOLE-MODEL-PIN BYPASS (now OPT-IN, default OFF).
        # ──────────────────────────────────────────────────────────────
        # Factory.ai's REST API will not honour a per-session model override
        # (POST /sessions rejects `model` with HTTP 400; message POST
        # silently ignores it — see iter-13.82 note in
        # factory_orchestrator._create_session). The natural-language
        # MODEL ROUTING DIRECTIVE we prepend is best-effort only —
        # Factory Droid frequently disregards it and uses the Computer's
        # default model ("auto").
        #
        # iter-13.81.10 — DEFAULT FLIPPED TO OFF after the bypass caused
        # more failures than it solved. With pin honouring on, calls
        # were leaving Factory (which had credits) and hitting fabric /
        # env-OpenRouter (which didn't), causing first-section aborts.
        #
        # Enable explicitly with `LAMA_FACTORY_PIN_BYPASS=1` if you
        # actively want Console pin honoured AND have funded fabric
        # provider credits. Otherwise Factory remains the single route
        # when enabled, and the Console pin shows up only as a natural-
        # language hint Factory may or may not act on.
        _pin_bypass_enabled = (
            os.environ.get("LAMA_FACTORY_PIN_BYPASS", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            _pre_fcfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
            # iter-14.36 — HONOUR the project-level ON/OFF flag before
            # anything else. Previously the `else` branches below set
            # `project_id_for_factory = project_id` even when the operator
            # had switched Factory OFF in the Console, which caused
            # route_via_factory_orchestrator to still shell out to
            # `droid exec` (because project.mode="cli" was left in the
            # config from a prior session). Every codegen file paid a
            # ~10s "Factory CLI routing FAILED droid exec exited 1"
            # penalty for a feature the user explicitly disabled.
            _factory_enabled_flag = bool(_pre_fcfg.get("enabled"))
            if not _factory_enabled_flag:
                project_id_for_factory = ""
                _bypass_factory_fallback_pid = ""
            elif _pin_bypass_enabled and _factory_enabled_flag:
                _pinned_model = _resolve_model_for_agent(_pre_fcfg, agent_key or "")
                if _pinned_model and _pinned_model.lower() != "auto":
                    _stage_b, _mode_b = _bucket_for_agent(agent_key or "")
                    try:
                        import logging as _log
                        _log.getLogger("lama.factory").info(
                            "Factory BYPASS: agent=%s bucket=%s.%s — Console "
                            "pinned model=%s; routing via fabric to honour pin "
                            "(Factory cannot accept per-session model override).",
                            agent_key, _stage_b, _mode_b, _pinned_model,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Force fabric to use this model: pass through kwargs.
                    kwargs.setdefault("model_override", _pinned_model)
                    # Fall through to the fabric path below (skip Factory).
                    project_id_for_factory = ""  # local sentinel
                    # iter-13.81.7 — remember the original pid so we can
                    # fall BACK to Factory if fabric refuses (credits/auth/
                    # network). The bypass is a hint, not a hard route —
                    # if the operator's pinned vendor is out of credits and
                    # Factory.ai's Droid Computer IS funded, Factory should
                    # still serve the request rather than aborting the run.
                    _bypass_factory_fallback_pid = project_id
                else:
                    project_id_for_factory = project_id
                    _bypass_factory_fallback_pid = ""
            else:
                project_id_for_factory = project_id
                _bypass_factory_fallback_pid = ""
        except Exception:  # noqa: BLE001 — never break the LLM layer
            project_id_for_factory = project_id
            _bypass_factory_fallback_pid = ""

        # iter-13.36 — STRICT MODE for Factory.ai.
        # Previously a Factory failure (424 computer-asleep, session timeout,
        # rate-limit, empty assistant reply, …) silently fell through to
        # fabric routing — which then picked the medium-tier default
        # `claude-sonnet-4.6` from Console. The user's complaint: "I chose
        # Factory.ai, why is Claude Sonnet being fetched?" The honest answer
        # is that the silent fallback hid the real Factory error AND burned
        # tokens on a model they hadn't picked.
        #
        # New default: when Factory is enabled for this project, it is the
        # ONLY route. Any failure surfaces as-is so the user can fix the
        # Computer / app key / session, instead of being quietly billed for
        # a different vendor. Opt out via LAMA_FACTORY_STRICT=0 (or
        # project.settings.factory_orchestrator.allow_fallback=true) if you
        # explicitly want the old "best-effort" fallback chain back.
        _factory_strict = (os.environ.get("LAMA_FACTORY_STRICT", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
        # iter-13.81.17 — CodeGen-stage AUTO-RELAX.
        # CodeGen fans out into 100+ per-file LLM calls. When Factory.ai
        # is enabled but the Droid Computer is asleep / out of credits /
        # missing an agent binding, strict mode would re-raise every call
        # → every file becomes `// LAMA: emergency scaffold (LLM returned
        # empty 4× — run Gap Recovery to enrich)`. That's exactly the
        # "no classes at all" failure mode the user reported. For codegen
        # we ALWAYS allow the fallback chain because:
        #   • The user is not staring at a chat box — they want CODE.
        #   • The emergency scaffold is strictly worse than a real LLM
        #     response from a secondary provider (even if it's the
        #     "wrong" model per Console pin).
        # SRS / Architecture / Chat keep strict mode because they're
        # interactive and the user can fix Factory before retrying.
        _ak_lower = (agent_key or "").lower()
        if _ak_lower.startswith("codegen.") or _ak_lower in {"codegen", "codegen.chat"}:
            _factory_strict = False
        # iter-15.34 — Explicit per-call opt-out of Factory routing. Used
        # by `routes/tools.py::_run_coder` for a ONE-TIME retry when the
        # Factory Droid response for a code-generation task looks like an
        # agentic narrative ("I've updated UserService.java...") instead
        # of literal source code (see `_looks_like_code` there). Factory
        # is still tried FIRST for every Coder call — this only forces the
        # standard Console-routed fabric chain for that single retry, so a
        # narrative response never gets silently persisted as "generated
        # code" while still preferring Factory whenever it behaves.
        if kwargs.pop("skip_factory", False):
            project_id_for_factory = ""
            _bypass_factory_fallback_pid = ""
        _factory_enabled_here = False
        try:
            _fcfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
            _factory_enabled_here = bool(_fcfg.get("enabled"))
            _allow_fallback_override = bool(_fcfg.get("allow_fallback", False))
        except Exception:
            _allow_fallback_override = False
        if _allow_fallback_override:
            _factory_strict = False
        # iter-13.81.6 — when the Console pinned a specific (non-"auto")
        # model above, project_id_for_factory was blanked. Skip Factory
        # entirely so the fabric path below honours the pinned model. Strict
        # mode also relaxes in this branch because the operator's pin is
        # explicit consent to bypass Factory's auto-routing.
        if not project_id_for_factory:
            _factory_enabled_here = False
            _factory_strict = False

        try:
            routed = None
            if project_id_for_factory:
                routed = await route_via_factory_orchestrator(
                    messages=messages,
                    project_id=project_id_for_factory,
                    agent_key=agent_key,
                    timeout=kwargs.get("timeout", 120.0),
                )
            if routed is not None:
                # iter-13.99.1 — every Factory-routed response goes through
                # the shared workspace-isolation guard. Foreign file paths
                # are redacted in-place before the response leaves the
                # LLM layer, so EVERY stage (SRS / Architecture / CodeGen /
                # DataModel / Living) inherits the protection without per-
                # call-site wiring.
                routed = await _sanitize_response_inplace(
                    routed, project_id, agent_key, source="factory",
                )
                return routed
        except Exception as fe:  # noqa: BLE001
            try:
                import logging as _log
                _log.getLogger("lama.factory").warning(
                    "Factory orchestrator failed for agent=%s pid=%s strict=%s — err=%s",
                    agent_key, project_id, _factory_strict, str(fe)[:300],
                )
            except Exception:
                pass
            # iter-13.45 — Distinguish USER-FIXABLE Factory failures (bad
            # app key, Computer asleep, wrong session, missing agent) from
            # SERVER-SIDE 5xx errors that originate on Factory.ai's own
            # infrastructure. Strict mode was designed to surface the
            # former (so the user can fix their setup) — it should NOT
            # block the user when Factory's own backend returns 500/502/
            # 503/504 with the standard "An unexpected error occurred"
            # envelope. Those are transient infra problems on the vendor's
            # side and the user has no actionable fix.
            _msg = str(fe).lower()
            _is_factory_5xx = (
                "http 500" in _msg
                or "http 502" in _msg
                or "http 503" in _msg
                or "http 504" in _msg
                or "an unexpected error occurred" in _msg
                or "internal server error" in _msg
                # iter-13.91.3 — HTTP 424 "Computer disconnected during
                # request" is Factory infra (the Droid Computer went to
                # sleep / crashed). `_create_session` already retries for
                # up to 95s on wake; if it still surfaces, the Droid is
                # genuinely offline and the user has no in-app fix.
                or "http 424" in _msg
                or "computer disconnected" in _msg
            )
            # iter-14.32 — Treat "402 Payment Required" / credit-limit
            # exhaustion as a FALL-FORWARD condition, not a hard-raise.
            # If Factory.ai has no budget, we still want to serve the
            # user via the next healthy provider in the chain (Anthropic
            # / OpenAI / Groq / Ollama) rather than blocking them until
            # their org admin tops up credits. The operator explicitly
            # asked: "check Factory → Anthropic → Ollama in order; if
            # Factory can't serve, try the next."
            _is_factory_billing = (
                "http 402" in _msg
                or "payment required" in _msg
                or "credit limit" in _msg
                or "reached the credit" in _msg
                or "insufficient credit" in _msg
                or "quota exceeded" in _msg
            )
            if _factory_enabled_here and _factory_strict and _is_factory_billing:
                try:
                    _log.getLogger("lama.factory").warning(
                        "iter-14.32: Factory.ai returned 402 / credit-limit "
                        "for agent=%s — falling forward to next healthy "
                        "provider in the chain (Anthropic → OpenAI → Groq "
                        "→ Ollama) instead of hard-raising. Top up Factory "
                        "credits or fix the fallback provider to restore "
                        "exact-model routing.", agent_key,
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Fall through to fabric routing below — do NOT raise.
            elif _factory_enabled_here and _factory_strict and not _is_factory_5xx:
                # Don't silently re-route through Claude Sonnet / fabric. The
                # user explicitly picked Factory.ai; surface the real error.
                raise RuntimeError(
                    f"Factory.ai orchestrator failed for agent='{agent_key}' "
                    f"(strict mode is ON). Original error: {str(fe)[:240]}. "
                    "Fix the Factory Computer / app key / session, or set "
                    "LAMA_FACTORY_STRICT=0 (or project settings "
                    "factory_orchestrator.allow_fallback=true) to fall back "
                    "to other providers."
                ) from fe
            if _factory_enabled_here and _factory_strict and _is_factory_5xx:
                try:
                    _log.getLogger("lama.factory").warning(
                        "Factory.ai returned 5xx for agent=%s — strict mode "
                        "transparently falling back to fabric / env OpenRouter. "
                        "This is a Factory infrastructure error, not a user "
                        "config problem.", agent_key,
                    )
                except Exception:
                    pass
            # Best-effort (legacy) path — fall through to fabric / env OpenRouter.

    fabric_err: Exception | None = None
    fabric_err_was_billing: bool = False  # iter-13.34 — track for cross-vendor fallback
    fabric_provider_types: set[str] = set()
    # iter-13.81.7 — Factory-fallback-after-bypass marker (see fabric_call docstring).
    _bypass_pid = locals().get("_bypass_factory_fallback_pid", "") or ""

    async def _factory_safety_net(orig_exc: Exception) -> dict | None:
        """If the Console-pin BYPASS was active (`_bypass_pid` set) and
        every fabric / env-OpenRouter route refused the call (typically
        402 credits / 401 auth on the operator-pinned vendor), make ONE
        last attempt via Factory.ai. Factory's Droid Computer often has
        its own funded routing — losing the model pin is acceptable when
        the alternative is aborting the entire SRS run.

        Returns the routed dict on success, or None when no fallback was
        attempted / the fallback itself failed.
        """
        if not _bypass_pid:
            return None
        try:
            from factory_orchestrator import (
                route_via_factory_orchestrator as _rfo,
                get_project_factory_orchestrator_config as _gfc,
            )
            _fcfg2 = await _gfc(_bypass_pid, include_secret=False)
            if not bool(_fcfg2.get("enabled")):
                return None
            import logging as _lg
            _lg.getLogger("lama.factory").warning(
                "Factory BYPASS-FALLBACK: fabric refused agent=%s with %s — "
                "retrying via Factory.ai (losing model pin, but Computer's "
                "default route is funded). Top up the pinned vendor's "
                "credits to restore exact-model honouring.",
                agent_key, str(orig_exc)[:200],
            )
            # Drop the pinned model_override — Factory will route via Computer default.
            _kwargs2 = dict(kwargs)
            _kwargs2.pop("model_override", None)
            routed2 = await _rfo(
                messages=messages,
                project_id=_bypass_pid,
                agent_key=agent_key,
                timeout=_kwargs2.get("timeout", 120.0),
            )
            if routed2 is not None:
                routed2 = await _sanitize_response_inplace(
                    routed2, _bypass_pid, agent_key, source="factory-bypass-fallback",
                )
                return routed2
        except Exception as fe2:  # noqa: BLE001
            try:
                import logging as _lg
                _lg.getLogger("lama.factory").warning(
                    "Factory BYPASS-FALLBACK also failed: %s", str(fe2)[:200],
                )
            except Exception:  # noqa: BLE001
                pass
        return None
    try:
        from db import model_providers as mp_col
        has_providers = await mp_col.count_documents({"is_active": True}) > 0
        if has_providers:
            # Snapshot which provider vendors fabric tried so we can decide
            # whether the env-var OPENROUTER_API_KEY is a DIFFERENT vendor
            # (and therefore worth retrying) when fabric raises CreditError.
            try:
                async for _p in mp_col.find({"is_active": True}, {"_id": 0, "provider_type": 1}):
                    fabric_provider_types.add((_p.get("provider_type") or "").lower())
            except Exception:  # noqa: BLE001
                pass
            # iter 13.8.2 — use the failover variant so a single
            # provider's 402 / quota error doesn't blow up the whole
            # section. fabric_chat_with_failover walks every is_active
            # provider in priority order and raises CreditError only
            # when *every* one has refused for billing/auth reasons.
            from fabric.model_fabric import fabric_chat_with_failover, CreditError
            try:
                result = await fabric_chat_with_failover(
                    messages=messages,
                    agent_key=agent_key,
                    project_id=project_id,
                    model_override=kwargs.get("model_override", "") or kwargs.get("model", ""),
                    max_tokens=kwargs.get("max_tokens", 0) or 0,
                    temperature=kwargs.get("temperature", 0.3),
                    timeout=kwargs.get("timeout", 120.0),
                    response_format=kwargs.get("response_format"),
                )
                # If failover returned empty content (e.g., upstream 200 with no text)
                # and we have an OpenRouter env-var fallback configured, retry via legacy client.
                if (result.get("content") or "").strip() or not OPENROUTER_API_KEY:
                    # iter-13.99.1 — workspace-isolation guard.
                    result = await _sanitize_response_inplace(
                        result, project_id, agent_key, source="fabric",
                    )
                    return result
                fabric_err = RuntimeError("fabric returned empty content")
            except CreditError as ce:
                # iter-13.34 — CROSS-VENDOR ENV FALLBACK.
                # Previous logic re-raised CreditError unconditionally on
                # the assumption that env OPENROUTER_API_KEY would be the
                # SAME key already loaded into Console (so retrying would
                # burn time on a guaranteed 402). That assumption breaks
                # for the very common setup where:
                #   • Console has only Anthropic (sk-ant-…) — out of credits
                #   • Env has OPENROUTER_API_KEY (sk-or-…) — separate vendor,
                #     separate billing, still funded
                # In that case env-var OpenRouter is a perfectly valid
                # last-resort failover. We allow it iff:
                #   1. OPENROUTER_API_KEY is set
                #   2. NO active Console provider is openrouter-typed
                #      (otherwise fabric already tried this key and failed)
                if (
                    OPENROUTER_API_KEY
                    and "openrouter" not in fabric_provider_types
                ):
                    fabric_err = ce
                    fabric_err_was_billing = True
                    # fall through to env-var OpenRouter retry below
                else:
                    _r = await _factory_safety_net(ce)
                    if _r is not None:
                        return _r
                    raise
            except Exception as e:
                fabric_err = e
    except Exception as e:
        # Re-raise CreditError; only swallow generic init failures.
        from fabric.model_fabric import CreditError as _CE
        if isinstance(e, _CE):
            # Same cross-vendor escape hatch as above for the outer try.
            if (
                OPENROUTER_API_KEY
                and "openrouter" not in fabric_provider_types
            ):
                fabric_err = e
                fabric_err_was_billing = True
            else:
                _r = await _factory_safety_net(e)
                if _r is not None:
                    return _r
                raise
        else:
            fabric_err = e

    # iter-13.115 — Ollama-first fallback. Try a configured local Ollama
    # provider BEFORE the env-var OpenRouter path (operator preference:
    # "when factory is not reachable, do not try to connect openrouter,
    # better connect ollama"). No-op when no Ollama provider exists.
    if fabric_err is not None:
        _o = await _try_ollama_fallback(
            messages=messages, agent_key=agent_key,
            project_id=project_id, kwargs=kwargs,
        )
        if _o is not None:
            return _o

    # Fallback to env-var OpenRouter if fabric is not configured or fails.
    if fabric_err is not None and not OPENROUTER_API_KEY:
        _r = await _factory_safety_net(fabric_err)
        if _r is not None:
            return _r
        raise fabric_err

    # ── iter-13.114 — LOCAL-PROVIDER GUARD ────────────────────────────
    # When the operator's active default provider is LOCAL (Ollama, or a
    # custom provider whose base_url points at localhost / 127.0.0.1 /
    # host.docker.internal), env-var OPENROUTER_API_KEY is NOT a sensible
    # fallback — the user explicitly set up local routing so they don't
    # have to pay a cloud vendor. Without this guard, a transient local
    # failure (model not pulled, OOM, cold-load timeout) silently bills
    # an OpenRouter account that's often unfunded → the user sees a
    # confusing 402 instead of the real Ollama-side error.
    #
    # We only short-circuit when:
    #   • fabric_err is set (something actually failed locally), AND
    #   • the failure is NOT a billing error from a cloud provider
    #     (covered by the iter-13.34 cross-vendor path below), AND
    #   • the active default provider is local.
    # In that case we re-raise the original fabric_err so the user sees
    # what Ollama actually said — instead of routing to OpenRouter.
    if fabric_err is not None and not fabric_err_was_billing:
        _is_local = False
        _ptype = ""
        _burl = ""
        try:
            from db import model_providers as _mp_col
            _default = await _mp_col.find_one(
                {"is_default": True, "is_active": True},
                {"_id": 0, "provider_type": 1, "base_url": 1},
            )
            if _default:
                _ptype = (_default.get("provider_type") or "").lower()
                _burl = (_default.get("base_url") or "").lower()
                _is_local = (
                    _ptype == "ollama"
                    or "localhost" in _burl
                    or "127.0.0.1" in _burl
                    or "host.docker.internal" in _burl
                    or "0.0.0.0" in _burl
                )
        except Exception:  # noqa: BLE001 — DB lookup must never block the call
            _is_local = False
        # IMPORTANT: the raise must live OUTSIDE the try/except above,
        # otherwise the bare `except Exception: pass` swallows it and we
        # silently fall through to the env-var OpenRouter path (the very
        # bug iter-13.114 exists to prevent).
        if _is_local:
            import logging as _lg
            _lg.getLogger("lama.llm").warning(
                "iter-13.114: local default provider (type=%s base=%s) "
                "failed for agent=%s — NOT falling back to env-var "
                "OpenRouter. Surface the real local error so the operator "
                "can fix it (model not pulled, OOM, cold-load timeout, …). "
                "Original error: %s",
                _ptype, _burl, agent_key, str(fabric_err)[:300],
            )
            _r = await _factory_safety_net(fabric_err)
            if _r is not None:
                return _r
            raise fabric_err

    # iter-13.34 — Skip the env-var retry only when the fabric error was
    # billing/auth AND the env key would hit the same vendor (handled
    # above by re-raising). When `fabric_err_was_billing` is True, we've
    # explicitly decided env OpenRouter is a DIFFERENT vendor and should
    # be tried — so don't re-raise here.
    if fabric_err is not None and not fabric_err_was_billing:
        try:
            from fabric.model_fabric import _is_billing_error, _is_auth_error  # type: ignore
            if _is_billing_error(str(fabric_err)) or _is_auth_error(str(fabric_err)):
                _r = await _factory_safety_net(fabric_err)
                if _r is not None:
                    return _r
                raise fabric_err
        except ImportError:
            pass
    if fabric_err_was_billing:
        # iter-13.114b — Even on the cross-vendor billing-fallback path,
        # if the operator's DEFAULT provider is local (Ollama / localhost /
        # host.docker.internal), the user explicitly opted out of cloud
        # billing. Don't surprise them with a 402 from env-var OpenRouter;
        # surface the original CreditError so they know their non-default
        # cloud providers are out of credit and can either top up or
        # deactivate them.
        _default_is_local = False
        try:
            from db import model_providers as _mp_col2
            _d2 = await _mp_col2.find_one(
                {"is_default": True, "is_active": True},
                {"_id": 0, "provider_type": 1, "base_url": 1},
            )
            if _d2:
                _pt2 = (_d2.get("provider_type") or "").lower()
                _bu2 = (_d2.get("base_url") or "").lower()
                _default_is_local = (
                    _pt2 == "ollama"
                    or "localhost" in _bu2
                    or "127.0.0.1" in _bu2
                    or "host.docker.internal" in _bu2
                    or "0.0.0.0" in _bu2
                )
        except Exception:  # noqa: BLE001
            _default_is_local = False
        if _default_is_local:
            import logging as _lg
            _lg.getLogger("lama.llm").warning(
                "iter-13.114b: default provider is local but a non-default "
                "cloud provider raised CreditError. NOT falling back to env-var "
                "OpenRouter — surfacing the original error so the operator can "
                "fix it (deactivate the out-of-credit cloud provider, or top "
                "it up). Original: %s",
                str(fabric_err)[:300],
            )
            _r = await _factory_safety_net(fabric_err)
            if _r is not None:
                return _r
            raise fabric_err
        import logging as _logging
        _logging.getLogger("lama.llm").warning(
            "Fabric refused with CreditError but env OPENROUTER_API_KEY is "
            "a different vendor (fabric=%s) — retrying via env-var OpenRouter "
            "as last-resort failover.",
            sorted(fabric_provider_types) or "(none)",
        )
    # When falling back to env-var OpenRouter from a cross-vendor billing
    # error, IGNORE the original `model_override` (it's an Anthropic /
    # other-vendor model id that OpenRouter can't route). Resolve to the
    # env-var default model instead.
    if fabric_err_was_billing:
        model = _env_default_model()
    else:
        model = kwargs.get("model_override") or kwargs.get("model", "") or _env_default_model()
    # iter-13.115 — Last-chance Ollama-first attempt before the env-var
    # OpenRouter call. Covers the path where NO Console providers are
    # configured at all (fabric_err stays None so the upstream Ollama
    # check above never fires) but the user has manually added an
    # Ollama provider via the API. Operator preference: prefer Ollama
    # over OpenRouter env-var.
    _o2 = await _try_ollama_fallback(
        messages=messages, agent_key=agent_key,
        project_id=project_id, kwargs=kwargs,
    )
    if _o2 is not None:
        return _o2

    # iter-14.31 — HARD KILL of the OpenRouter env-var fallback for EVERY
    # generation path (recommend / HLD / LLD / API-contracts / CodeGen /
    # SRS / DataModel / Chat).
    #
    # Operator directive (2026-08-14): "if Factory AI is not available,
    # go with HF for confidence; for generation, no silent OpenRouter
    # fallback anywhere." OpenRouter is no longer a fallback for ANY
    # generation call — Factory.ai (via factory_cli / factory_api / fabric)
    # is the only route. If it's down, we fail LOUDLY so the operator
    # fixes the primary provider instead of quietly being billed by
    # OpenRouter.
    #
    # Rationale:
    #   • The env-var OpenRouter fallback was silently burning tokens on
    #     300K-char prompts every time droid timed out.
    #   • It also hid the real Factory error from the operator, making
    #     "why is my generation using the wrong model?" impossible to
    #     debug from the UI.
    #   • HF encoders (BGE + cross-encoder/NLI) CANNOT generate text —
    #     they only score confidence — so the "HF fallback" for
    #     generation is by definition "fail hard with a clear message
    #     and let the operator fix Factory.ai".
    #
    # Ollama (local, no billing surprise) remains a legitimate secondary
    # ONLY when explicitly added as a Console provider — handled by
    # `_try_ollama_fallback` above, which already returned above when
    # applicable.

    _r = await _factory_safety_net(fabric_err or RuntimeError("no primary route succeeded"))
    if _r is not None:
        return _r

    _reason = str(fabric_err)[:400] if fabric_err else "no active Console provider and Factory.ai orchestrator not configured"
    import logging as _lg2
    _lg2.getLogger("lama.llm").error(
        "iter-14.31: generation call FAILED HARD for agent=%s pid=%s — "
        "no OpenRouter fallback will be attempted. Underlying: %s",
        agent_key, project_id, _reason,
    )
    raise RuntimeError(
        f"Generation provider unavailable for agent='{agent_key}'. "
        "Factory.ai (or the Console-configured primary provider) failed "
        "and there is NO OpenRouter fallback (removed in iter-14.31). "
        "Configure a working provider in Console → Models, or fix the "
        "Factory Computer / app key / session, then retry. "
        f"Underlying error: {_reason}"
    )


# ── iter-13.99.1 — Cross-stage output sanitizer hook ──────────────────
# Every fabric_call result that carries a non-empty project_id gets its
# `content` (and, where relevant, `messages[].content`) scrubbed against
# the project's iter-13.99 workspace contract:
#   • Multi-component paths MUST start with /lama-workspaces/{tid}__{pid}/
#   • Bare basenames in prose fall back to a basename match
#   • Anything else → [redacted-foreign-path]
#
# This is the SINGLE point of enforcement for every stage (SRS, Arch,
# CodeGen, DataModel, Living, Chat) — no per-call-site wiring needed.
async def _sanitize_response_inplace(
    response: dict | None,
    project_id: str,
    agent_key: str,
    *,
    source: str,
) -> dict | None:
    """Mutate `response["content"]` (and `response["messages"][i]["content"]`)
    through the shared workspace-isolation guard. Best-effort: any
    plumbing failure returns the response unchanged.
    """
    if not response or not isinstance(response, dict):
        return response
    # iter-14.54 — Living generation agents legitimately emit NEW target
    # file paths (`src/test/java/…/HomePageTest.java`, `perf/plan.jmx`,
    # etc.). These are outputs of the modernisation, not legacy leaks, so
    # the workspace-prefix guard must NOT rewrite them to
    # `[redacted-foreign-path]` — that was breaking the Selenium file
    # list + JMeter multi-file split.
    if (agent_key or "") in {
        "test.selenium", "test.jmeter", "test.cases",
        "test.drift", "test.srs_diff",
    }:
        return response
    if not (project_id or get_current_project_id()):
        return response
    pid = (project_id or get_current_project_id() or "").strip()
    if not pid:
        return response
    try:
        from kb.workspace_isolation import sanitize_llm_output
    except Exception:  # noqa: BLE001 — defensive: never break the LLM layer
        return response
    try:
        if isinstance(response.get("content"), str):
            response["content"] = await sanitize_llm_output(
                response["content"], pid,
                source_label=f"{source}/{agent_key or 'unknown-agent'}",
            )
        # Multi-turn fabric responses sometimes carry the assistant text
        # in a messages[] list (Anthropic native shape). Sanitize each
        # string content found there too.
        msgs = response.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict) and isinstance(m.get("content"), str):
                    m["content"] = await sanitize_llm_output(
                        m["content"], pid,
                        source_label=f"{source}/{agent_key or 'unknown-agent'}/msg",
                    )
    except Exception as exc:  # noqa: BLE001
        try:
            import logging as _log
            _log.getLogger("lama.workspace_isolation").warning(
                "iter-13.99.1 fabric guard crashed for pid=%s src=%s: %s — "
                "returning response unchanged",
                pid, source, exc,
            )
        except Exception:
            pass
    return response


# ──────────────────────────────────────────────────────────────────────
# iter-13.41 — Streaming variant of fabric_call.
#
# Yields content delta strings. Skips Factory.ai (sessions API doesn't
# stream — call buffered then yield once) and falls back to buffered for
# providers that don't support OpenAI-style SSE.
# ────────────────────────────────────────────────────────────────���─────
async def fabric_call_stream(
    messages: List[Dict],
    agent_key: str = "",
    project_id: str = "",
    **kwargs,
):
    """Streaming variant of `fabric_call`. Async generator yielding string deltas.

    Routing:
      1. Factory.ai enabled for this project → buffered Factory call, yield once.
      2. Console provider that speaks OpenAI-compatible SSE → live stream.
      3. Anything else (Anthropic native, env-var fallback) → buffered, yield once.

    The caller MUST be prepared to receive the entire payload as a single
    chunk (case 1/3). Use ``len`` or delimiter scanning rather than chunk
    counting.
    """
    if not project_id:
        project_id = get_current_project_id()

    # ── 1. Factory.ai short-circuit ──
    if project_id:
        try:
            from factory_orchestrator import (
                get_project_factory_orchestrator_config,
                _resolve_model_for_agent,
            )
            fcfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
            if bool(fcfg.get("enabled")):
                # iter-13.81.10 — Console-pin bypass is OPT-IN (default OFF).
                # See fabric_call() for the rationale. When OFF, Factory
                # always handles the call when enabled — this is the
                # working baseline. When ON, a non-"auto" pin causes
                # fall-through to fabric streaming.
                _pin_bypass_enabled = (
                    os.environ.get("LAMA_FACTORY_PIN_BYPASS", "0") or "0"
                ).strip().lower() in {"1", "true", "yes", "on"}
                _pinned = _resolve_model_for_agent(fcfg, agent_key or "") if _pin_bypass_enabled else "auto"
                if _pin_bypass_enabled and _pinned and _pinned.lower() != "auto":
                    kwargs.setdefault("model_override", _pinned)
                    # fall through to fabric streaming
                else:
                    buf = await fabric_call(
                        messages=messages, agent_key=agent_key, project_id=project_id, **kwargs,
                    )
                    content = (buf or {}).get("content", "") or ""
                    if content:
                        yield content
                    return
        except Exception:  # noqa: BLE001
            pass

    # ── 2. Try OpenAI-compatible streaming via fabric ──
    try:
        from db import model_providers as mp_col
        has_providers = await mp_col.count_documents({"is_active": True}) > 0
    except Exception:  # noqa: BLE001
        has_providers = False

    if has_providers:
        try:
            from fabric.model_fabric import fabric_chat_stream
            try:
                async for piece in fabric_chat_stream(
                    messages=messages,
                    agent_key=agent_key,
                    project_id=project_id,
                    model_override=kwargs.get("model_override", "") or kwargs.get("model", ""),
                    max_tokens=kwargs.get("max_tokens", 0) or 0,
                    temperature=kwargs.get("temperature", 0.3),
                    timeout=kwargs.get("timeout", 600.0),
                    response_format=kwargs.get("response_format"),
                ):
                    yield piece
                return
            except NotImplementedError:
                pass  # provider type unsupported → fall through to buffered
        except Exception:  # noqa: BLE001 — any setup failure → fallback
            pass

    # ── 3. Buffered fallback ──
    buf = await fabric_call(messages=messages, agent_key=agent_key, project_id=project_id, **kwargs)
    content = (buf or {}).get("content", "") or ""
    if content:
        yield content


async def fabric_stream_supported(project_id: str = "") -> bool:
    """Cheap probe: returns True iff the active route can deliver SSE
    chunks progressively. Used by SRS to decide between batched-stream
    mode (one LLM call) and per-section mode (one call per section).

    A provider qualifies as streaming-capable when:
      • Factory.ai is NOT enabled (Factory buffers + yields once), AND
      • the default active fabric provider speaks OpenAI-compatible SSE
        (i.e. provider_type != "anthropic"; today only Anthropic native
        raises NotImplementedError in fabric_chat_stream).

    When neither fabric nor Factory is configured, we fall back to the
    env-var OpenRouter path which DOES stream → return True.
    """
    if not project_id:
        try:
            project_id = get_current_project_id()
        except Exception:  # noqa: BLE001
            project_id = ""
    # Factory.ai short-circuit → buffered single yield.
    if project_id:
        try:
            from factory_orchestrator import (
                get_project_factory_orchestrator_config,
                _resolve_model_for_agent,
            )
            fcfg = await get_project_factory_orchestrator_config(project_id, include_secret=False)
            if bool(fcfg.get("enabled")):
                # iter-13.81.10 — Factory always wins (buffered yield) unless
                # the opt-in Console-pin bypass is active AND a non-"auto"
                # model is pinned for the current bucket.
                _pin_bypass_enabled = (
                    os.environ.get("LAMA_FACTORY_PIN_BYPASS", "0") or "0"
                ).strip().lower() in {"1", "true", "yes", "on"}
                _ak = get_current_agent_key() or ""
                _pinned = _resolve_model_for_agent(fcfg, _ak) if _pin_bypass_enabled else "auto"
                if not (_pin_bypass_enabled and _pinned and _pinned.lower() != "auto"):
                    return False
        except Exception:  # noqa: BLE001
            pass
    # Inspect default fabric provider.
    try:
        from db import model_providers as mp_col
        prov = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0, "provider_type": 1})
        if prov:
            ptype = (prov.get("provider_type") or "").lower()
            # Mirror the gate in fabric.model_fabric.fabric_chat_stream.
            if ptype == "anthropic":
                return False
            return True
        # No active provider → env-var OpenRouter fallback streams fine.
        return True
    except Exception:  # noqa: BLE001
        return True


# ──────────────────────────────────────────────────────────────────────
# iter-13.100 — Session-aware wrapper around fabric_call.
#
# Why a separate function (not a parameter on fabric_call):
#   • fabric_call has 4 return paths (Factory.ai, fabric failover,
#     env-fallback, safety-net). Wrapping post-call session writes around
#     every one would be invasive and high-risk.
#   • Callers that don't want rolling memory keep calling `fabric_call`
#     unchanged — zero behavior change for SRS / DataModel / Arch /
#     CodeGen until they explicitly opt in (chat.py is Phase 2 target).
#   • The summariser inside `maybe_rollover` calls *plain* fabric_call
#     (NO session_id), which makes the infinite-recursion impossible by
#     construction.
#
# Contract:
#   • Caller passes the messages they want sent (typically: an optional
#     leading system msg + the latest user turn).
#   • If `session_id` resolves to an active AgentSession, we splice the
#     session's stored prefix (rolling summary + refs + last-K verbatim)
#     between the caller's system message(s) and the rest.
#   • Before the LLM call we proactively run `maybe_rollover` so a
#     near-full window is compressed BEFORE the next prompt exceeds it.
#   • After the call we append the last user turn + assistant response
#     to the session.
#   • If `session_id` is empty / not found / archived, behaviour is
#     identical to plain `fabric_call` (graceful no-op).
# ──────────────────────────────────────────────────────────────────────
async def _resolve_context_window(
    agent_key: str = "",
    project_id: str = "",
    model_hint: str = "",
) -> int:
    """Best-effort resolution of the resolved-model's context_window in tokens.

    Resolution order:
      1. If `model_hint` matches an entry in any active provider's
         `models[]` catalogue (Console-configured), use its `context_window`.
      2. Else resolve the agent's tier via `AGENT_COMPLEXITY` and look up
         `provider.routing[tier]` → catalogue entry.
      3. Else look up `PROVIDER_PRESETS[detected_type].model_catalogue`
         using the same model id.
      4. Conservative fallback: 32000 tokens.

    This is deliberately a CHEAP best-effort — wrong-by-2x is fine; the
    rollover threshold is a percentage so a slight over/under-estimate
    just shifts when summaries fire.
    """
    DEFAULT_WINDOW = 32000

    def _scan_catalogue(catalogue: list, mid: str) -> int:
        for m in (catalogue or []):
            if (m or {}).get("id") == mid:
                cw = int(m.get("context_window") or 0)
                if cw > 0:
                    return cw
        return 0

    # iter-13.100 — load PROVIDER_PRESETS unconditionally (pure-Python
    # dict, no Mongo). The DB-dependent paths below are wrapped in their
    # OWN try/except so a Mongo outage doesn't hide a perfectly-resolvable
    # preset answer behind the safe-default fallback.
    try:
        from fabric.model_fabric import AGENT_COMPLEXITY, PROVIDER_PRESETS
    except Exception:  # noqa: BLE001
        AGENT_COMPLEXITY, PROVIDER_PRESETS = {}, {}

    hint = (model_hint or "").strip()

    # 1a. Direct hint match against active Console providers (best signal).
    active: list = []
    try:
        from db import model_providers as mp_col
        async for p in mp_col.find({"is_active": True}, {"_id": 0}):
            active.append(p)
    except Exception:  # noqa: BLE001 — Mongo unavailable / test env
        active = []
    if hint:
        for p in active:
            cw = _scan_catalogue(p.get("models") or [], hint)
            if cw:
                return cw
        # 1b. Direct hint match against built-in presets — works even
        # when Mongo is offline / in tests.
        for preset in PROVIDER_PRESETS.values():
            cw = _scan_catalogue(preset.get("model_catalogue") or [], hint)
            if cw:
                return cw

    # 2. Agent → stage → provider.stage_routing[stage] OR tier → provider.routing[tier] → catalogue.
    # iter-13.112 — stage_routing takes precedence over complexity-based routing
    if agent_key:
        try:
            from db import agent_configs as ac_col
            agent = await ac_col.find_one({"key": agent_key}, {"_id": 0})
        except Exception:  # noqa: BLE001
            agent = None
        stage = (agent or {}).get("stage", "")
        complexity = (agent or {}).get("complexity") \
            or AGENT_COMPLEXITY.get(agent_key, "medium")
        ranked = sorted(active, key=lambda p: not p.get("is_default", False))
        for p in ranked:
            # Try stage-based routing first (if configured)
            mid = ""
            if stage:
                stage_routing = p.get("stage_routing") or {}
                mid = stage_routing.get(stage, "")
            # Fall back to complexity-based routing
            if not mid:
                mid = (p.get("routing") or {}).get(complexity, "")
            if not mid:
                continue
            cw = _scan_catalogue(p.get("models") or [], mid)
            if cw:
                return cw
            ptype = (p.get("provider_type") or "").lower()
            preset = PROVIDER_PRESETS.get(ptype) or {}
            cw = _scan_catalogue(preset.get("model_catalogue") or [], mid)
            if cw:
                return cw

    # 3. Env-var fallback model against presets.
    env_model = _env_default_model()
    if env_model:
        for preset in PROVIDER_PRESETS.values():
            cw = _scan_catalogue(preset.get("model_catalogue") or [], env_model)
            if cw:
                return cw
    return DEFAULT_WINDOW


def _split_system_and_rest(messages: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Partition messages into leading system blocks and everything else.

    Only CONSECUTIVE leading system messages are treated as "header".
    A system message appearing later in the conversation stays in `rest`
    (rare, but lets a caller inject mid-stream system directives without
    them being mistakenly hoisted above the session prefix).
    """
    head: List[Dict] = []
    i = 0
    for m in messages or []:
        if (m or {}).get("role") == "system":
            head.append(m)
            i += 1
        else:
            break
    return head, list(messages[i:] if messages else [])


async def fabric_call_with_session(
    messages: List[Dict],
    *,
    session_id: str,
    agent_key: str = "",
    project_id: str = "",
    auto_append: bool = True,
    hydrate_refs: bool = True,
    **kwargs,
) -> Dict:
    """Session-aware wrapper around `fabric_call`.

    Behaves exactly like `fabric_call` when `session_id` resolves to no
    active AgentSession (graceful no-op). Otherwise:

      1. Loads the session, runs proactive rollover if the current
         token budget would put us over the window threshold.
      2. Splices the session prefix between caller-supplied leading
         system messages and the rest of `messages`. When
         `hydrate_refs=True` (default, Phase-3 secure mode) each ref's
         CURRENT content is fetched fresh from its collection and
         inlined as its own system block — the rolling summary is
         downgraded to a "HINT only" annotation. Set `hydrate_refs=False`
         for legacy label-only behaviour (callers that hydrate refs
         themselves elsewhere, or tests that want to assert pure shape).
      3. Calls plain `fabric_call(...)` exactly as before.
      4. If `auto_append=True` (default), appends the LAST user turn
         from the original `messages` and the assistant's response to
         the session for the next call to see.

    Returns the same dict shape as `fabric_call`: `{content, model, usage, …}`.
    """
    if not session_id:
        return await fabric_call(messages=messages, agent_key=agent_key,
                                 project_id=project_id, **kwargs)

    # Import here to keep the bare `llm.py` import free of agent_memory's
    # transitive deps (helps the existing test suites that mock db.py).
    from agent_memory import (
        append_turn,
        build_prompt_prefix,
        get_session,
        maybe_rollover,
    )

    session = await get_session(session_id)
    if session is None or session.status != "active":
        # Unknown / archived session → fall back to plain fabric_call. The
        # caller almost certainly wants the LLM call to succeed even if
        # the session ID is stale (e.g. user-edited thelocalStorage).
        try:
            import logging as _log
            _log.getLogger("lama.agent_memory").info(
                "fabric_call_with_session: session_id=%s not active (status=%s) — "
                "falling through to plain fabric_call",
                session_id, getattr(session, "status", "<missing>"),
            )
        except Exception:
            pass
        return await fabric_call(messages=messages, agent_key=agent_key,
                                 project_id=project_id, **kwargs)

    # Proactive rollover BEFORE the next call so we never blow the window
    # mid-prompt. The summariser calls plain fabric_call (no session_id),
    # which is what breaks the recursion.
    try:
        cw = await _resolve_context_window(
            agent_key=agent_key or session.agent_key,
            project_id=project_id or session.project_id,
            model_hint=kwargs.get("model_override") or kwargs.get("model") or "",
        )
        session, _rolled = await maybe_rollover(
            session_id,
            resolved_context_window=cw,
            fabric_call=fabric_call,
            summary_model="",  # let Console routing pick the summariser model
        )
    except Exception:  # noqa: BLE001 — rollover failures must not block calls
        import logging as _log
        _log.getLogger("lama.agent_memory").warning(
            "proactive rollover failed for session %s — proceeding with current state",
            session_id, exc_info=True,
        )

    # Splice: caller-system + session-prefix + caller-rest.
    head_system, rest = _split_system_and_rest(messages)
    prefix = await build_prompt_prefix(session, hydrate=hydrate_refs)
    spliced = head_system + prefix + rest

    # Capture the last user turn from the CALLER's input (not the
    # spliced/prefixed version) so we don't accidentally duplicate
    # something already inside the session's live_turns.
    last_user = ""
    for m in reversed(rest):
        if (m or {}).get("role") == "user" and (m.get("content") or "").strip():
            last_user = m["content"]
            break

    result = await fabric_call(
        messages=spliced, agent_key=agent_key, project_id=project_id, **kwargs,
    )

    if auto_append:
        assistant_text = (result or {}).get("content", "") or ""
        try:
            if last_user:
                await append_turn(session_id, role="user", content=last_user)
            if assistant_text.strip():
                await append_turn(session_id, role="assistant", content=assistant_text)
        except Exception:  # noqa: BLE001 — append failure must not lose the response
            import logging as _log
            _log.getLogger("lama.agent_memory").warning(
                "post-call session append failed for session %s",
                session_id, exc_info=True,
            )

    return result

