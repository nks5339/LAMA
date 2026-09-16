"""Model Fabric — unified LLM client supporting any provider.

Key behaviours:
1. User pastes one API key → system auto-detects provider and sets up
   complexity routing automatically.
2. Every LLM call goes through resolve_model() which picks the right
   model based on agent complexity + provider routing.
3. Token usage is logged after every call.
4. Falls back to env-var config if no DB providers configured.
"""
import asyncio
import os
import time
import httpx
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

# Centralised SSL-verify policy (LAMA_DISABLE_SSL_VERIFY / LAMA_CA_BUNDLE)
from llm import _http_verify


# ── Provider presets ──────────────────────────────────────────────────
PROVIDER_PRESETS: Dict[str, Dict] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_prefix": "sk-or-",
        "default_models": {
            "low": "deepseek/deepseek-chat",
            "medium": "anthropic/claude-sonnet-4.6",
            "high": "anthropic/claude-opus-4.7",
        },
        "model_catalogue": [
            {"id": "deepseek/deepseek-chat", "label": "DeepSeek Chat (low)",
             "context_window": 128000, "cost_per_1k_input": 0.00027, "cost_per_1k_output": 0.0011},
            {"id": "qwen/qwen-2.5-72b-instruct", "label": "Qwen 2.5 72B (medium)",
             "context_window": 32000, "cost_per_1k_input": 0.00040, "cost_per_1k_output": 0.00040},
            {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Llama 3.3 70B (medium)",
             "context_window": 128000, "cost_per_1k_input": 0.00059, "cost_per_1k_output": 0.00079},
            {"id": "anthropic/claude-sonnet-4", "label": "Claude Sonnet 4 (high)",
             "context_window": 200000, "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
            # — iter 13.2: SRS-generation + verification flagships ——————————
            {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6 (medium/high) — SRS verification",
             "context_window": 200000, "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
            {"id": "anthropic/claude-opus-4.7", "label": "Claude Opus 4.7 (flagship) — SRS generation",
             "context_window": 200000, "cost_per_1k_input": 0.015, "cost_per_1k_output": 0.075},
            {"id": "openai/gpt-4o", "label": "GPT-4o (high)",
             "context_window": 128000, "cost_per_1k_input": 0.005, "cost_per_1k_output": 0.015},
        ],
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "key_prefix": "sk-ant-",
        "default_models": {"low": "claude-haiku-4-5", "medium": "claude-sonnet-4-6", "high": "claude-opus-4-7"},
        "model_catalogue": [
            {"id": "claude-haiku-4-5", "label": "Claude Haiku (low)",
             "context_window": 200000, "cost_per_1k_input": 0.00025, "cost_per_1k_output": 0.00125},
            {"id": "claude-sonnet-4", "label": "Claude Sonnet 4 (high)",
             "context_window": 200000, "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
            # — iter 13.2: SRS-generation + verification flagships ——————————
            {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (medium/high) — SRS verification",
             "context_window": 200000, "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
            {"id": "claude-opus-4-7", "label": "Claude Opus 4.7 (flagship) — SRS generation",
             "context_window": 200000, "cost_per_1k_input": 0.015, "cost_per_1k_output": 0.075},
        ],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_prefix": "sk-",
        "default_models": {"low": "gpt-4o-mini", "medium": "gpt-4o", "high": "gpt-4o"},
        "model_catalogue": [
            {"id": "gpt-4o-mini", "label": "GPT-4o Mini (low)",
             "context_window": 128000, "cost_per_1k_input": 0.00015, "cost_per_1k_output": 0.0006},
            {"id": "gpt-4o", "label": "GPT-4o (high)",
             "context_window": 128000, "cost_per_1k_input": 0.005, "cost_per_1k_output": 0.015},
        ],
    },
    "azure": {
        # Azure OpenAI. `base_url` is per-tenant and has no sensible
        # default, so it comes from the operator (Console) or AZURE_ENDPOINT.
        # It is the ACCOUNT root; resolve_model() appends
        # /openai/deployments/<deployment> and adds ?api-version=<version>.
        #
        # Note the endpoint is not always *.openai.azure.com — enterprise
        # deployments routinely sit behind a corporate gateway with an
        # arbitrary host and path prefix. resolve_model() therefore only
        # appends the deployment path when the URL does not already contain
        # one, so a pre-scoped gateway URL is left alone.
        "base_url": os.environ.get("AZURE_ENDPOINT", ""),
        # Azure keys are opaque 32-char strings with no distinguishing
        # prefix, so they cannot be auto-detected from the key alone.
        # Selecting "azure" in the Console is required.
        "key_prefix": "",
        # iter-18.3 — Tier map for the operator's actual deployment set.
        # On Azure the routable identifier is the DEPLOYMENT name; these
        # are the conventional names matching the published model ids, so
        # they work as-is when deployments are named after their model.
        # AZURE_DEPLOYMENT still overrides all three when set, which keeps
        # a single-deployment account working exactly as before.
        # Precedence, highest first:
        #   1. AZURE_DEPLOYMENT_{LOW,MEDIUM,HIGH} — per-tier, explicit.
        #   2. the ladder below — the point of complexity-based routing.
        #   3. AZURE_DEPLOYMENT — single-deployment accounts only, and it
        #      fills a tier ONLY if the ladder left it empty.
        # AZURE_DEPLOYMENT deliberately does NOT collapse all three tiers:
        # doing so silently defeats tier routing on a multi-deployment
        # account, which is the normal case for this operator.
        "default_models": {
            "trivial": os.environ.get("AZURE_DEPLOYMENT_TRIVIAL", "") or "gpt-4o-mini",
            "low": os.environ.get("AZURE_DEPLOYMENT_LOW", "") or "gpt-4.1-mini",
            "medium": os.environ.get("AZURE_DEPLOYMENT_MEDIUM", "") or "gpt-4.1",
            "high": os.environ.get("AZURE_DEPLOYMENT_HIGH", "") or "gpt-5",
            "critical": os.environ.get("AZURE_DEPLOYMENT_CRITICAL", "") or "gpt-5.1",
            "reasoning": os.environ.get("AZURE_DEPLOYMENT_REASONING", "") or "o4-mini",
        },
        # iter-19 — Sibling deployments for the same tier, tried in order
        # when the primary returns 429. Each Azure deployment has its own
        # quota bucket, so rotating to a sibling on the SAME account costs
        # nothing and lands immediately, where sleeping the provider's
        # `Retry-After` (observed up to 52s) stalls the whole wave and
        # falling through to local Ollama costs a 600s timeout.
        "tier_siblings": {
            "trivial": ["gpt-4.1-mini"],
            "low": ["gpt-4o-mini"],
            "medium": ["gpt-4o", "gpt-5-mini"],
            "high": ["gpt-5.1", "gpt-4.1"],
            "critical": ["gpt-5"],
            "reasoning": ["o3-mini", "o3"],
        },
        # iter-20 — Account-wide descent order, strongest first.
        #
        # `tier_siblings` rotates WITHIN a tier. That is the right first
        # move (an equal-capability deployment with its own quota bucket),
        # but when every sibling is throttled the old code slept, and then
        # eventually let the call leave Azure entirely for a local Ollama
        # model. On a PAID account that is backwards: a degraded Azure
        # deployment beats a 4B local model on every axis that matters for
        # code generation, and the operator is paying for the quota either
        # way.
        #
        # So once the tier is exhausted we walk DOWN this list instead,
        # and only leave the provider when every chat deployment on the
        # account is cooling. Ordered by capability rather than by tier so
        # a `critical` agent degrades gpt-5.1 -> gpt-5 -> gpt-4.1 rather
        # than dropping straight to a mini.
        #
        # Intersected with the provider row's own `models` catalogue at
        # use time (see `exhaustion_ladder()`), so an account that has not
        # deployed gpt-5.1 never rotates onto a 404.
        "exhaustion_ladder": [
            "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o",
            "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini",
        ],
        # Cost is billed per-deployment at rates that depend on the
        # operator's agreement, so zeros here are honest rather than a
        # guess. The token counts in token_usage_log stay accurate; only
        # the dollar column is unknown until the operator fills it in.
        #
        # `context_window` is the published limit and drives only the
        # Console display and pre-flight sizing. It is deliberately NOT
        # what triggers the Ollama fallback — that keys off the provider's
        # own context/output error (`_is_context_error`), which is
        # authoritative and cannot drift out of date.
        "model_catalogue": [
            {"id": "gpt-5.1", "label": "GPT-5.1 (high) — newest, 2025-11-13",
             "context_window": 400000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-5", "label": "GPT-5 (high) — 2025-08-07",
             "context_window": 400000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-5-mini", "label": "GPT-5 mini (low/medium) — 2025-08-07",
             "context_window": 400000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "o3", "label": "o3 (high) — reasoning, 2025-04-16",
             "context_window": 200000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "o4-mini", "label": "o4 mini (medium) — reasoning, 2025-04-16",
             "context_window": 200000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "o3-mini", "label": "o3 mini (medium) — reasoning, 2025-01-31",
             "context_window": 200000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-4.1", "label": "GPT-4.1 (medium) — 2025-04-14",
             "context_window": 1047576, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini (low) — 2025-04-14",
             "context_window": 1047576, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-4o", "label": "GPT-4o (medium) — 2024-08-06",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-4o-mini", "label": "GPT-4o mini (low) — 2024-07-18",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
        ],
        # Embedding deployments on the same account. Not part of the chat
        # catalogue — `resolve_model` routes /chat/completions only. Kept
        # here so the Console can offer them once an embeddings backend
        # that targets Azure exists (today embeddings run through
        # sentence-transformers or Ollama/nomic-embed-text).
        "embedding_catalogue": [
            {"id": "text-embedding-3-large", "dimensions": 3072},
            {"id": "text-embedding-ada-002", "dimensions": 1536},
        ],
    },
    "gemini": {
        # Google's OpenAI-compatibility surface, NOT the native
        # generativelanguage REST shape. Using it means Gemini flows
        # through the same fabric_chat path as every other provider
        # (Bearer auth, /chat/completions, the same SSE streaming) instead
        # of needing a second transport.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_prefix": "AIza",
        "default_models": {
            "low": "gemini-2.0-flash",
            "medium": "gemini-2.5-flash",
            "high": "gemini-2.5-pro",
        },
        "model_catalogue": [
            {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash (low)",
             "context_window": 1000000, "cost_per_1k_input": 0.0001, "cost_per_1k_output": 0.0004},
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash (medium)",
             "context_window": 1000000, "cost_per_1k_input": 0.0003, "cost_per_1k_output": 0.0025},
            {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro (high)",
             "context_window": 1000000, "cost_per_1k_input": 0.00125, "cost_per_1k_output": 0.010},
            {"id": "gemini-1.5-pro", "label": "Gemini 1.5 Pro (legacy)",
             "context_window": 2000000, "cost_per_1k_input": 0.00125, "cost_per_1k_output": 0.005},
        ],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_prefix": "gsk_",
        "default_models": {"low": "llama-3.1-8b-instant", "medium": "llama-3.3-70b-versatile", "high": "llama-3.3-70b-versatile"},
        "model_catalogue": [
            {"id": "llama-3.1-8b-instant", "label": "Llama 3.1 8B (low)",
             "context_window": 128000, "cost_per_1k_input": 0.00005, "cost_per_1k_output": 0.00008},
            {"id": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B (medium/high)",
             "context_window": 128000, "cost_per_1k_input": 0.00059, "cost_per_1k_output": 0.00079},
        ],
    },
    "ollama": {
        # Local Ollama runtime — OpenAI-compatible /v1 endpoint.
        # No API key required; users set a base URL only (defaults below).
        # Override per-deployment via the Console base-URL field or by
        # setting LAMA_OLLAMA_BASE_URL in the env.
        "base_url": os.environ.get("LAMA_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        # iter-14.21 — Cloud Ollama endpoint. Auto-selected when the
        # configured model tag ends with `-cloud`, or the provider has
        # an api_key set, or LAMA_OLLAMA_CLOUD=1 (see
        # _resolve_ollama_endpoint). Cloud Ollama exposes the same
        # OpenAI-compatible surface but requires Authorization: Bearer.
        "cloud_base_url": os.environ.get(
            "LAMA_OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1",
        ),
        "key_prefix": "",
        # iter-14.23 — Updated defaults based on available models Aug 2026.
        # iter-14.24 — Defaulting to LOCAL models only (no subscription needed).
        # Cloud models (-cloud suffix) require Ollama subscription.
        # iter-18.3 — Operator-approved local set.
        #   high    -> code generation. Only llama3.1:latest, gpt-oss:latest
        #              and qwen3.5:latest are sanctioned for generating code;
        #              `_coerce_ollama_codegen_model` enforces that for every
        #              codegen agent regardless of what routing says.
        #   low/med -> lightweight work (classification, short JSON, gates).
        # iter-19 — six tiers, same vocabulary as every other preset. The
        # sanctioned-code-generation set (qwen3.5, llama3.1, gpt-oss) backs
        # the three top tiers; the lightweight pair backs the bottom three.
        "default_models": {
            "trivial": "qwen3:4b",
            "low": "qwen3:4b",
            "medium": "qwen2.5-coder:7b",
            "high": "qwen3.5:latest",
            "critical": "gpt-oss:latest",
            "reasoning": "gpt-oss:latest",
        },
        "tier_siblings": {
            "high": ["llama3.1:latest"],
            "critical": ["qwen3.5:latest"],
            "reasoning": ["llama3.1:latest"],
        },
        # Seed the catalogue with validated models only. Retired models
        # (qwen3-coder:480b-cloud) removed. Run `ollama list` to verify
        # local availability; cloud models require Ollama Cloud auth.
        "model_catalogue": [
            {"id": "qwen3.5:latest",
             "label": "Qwen 3.5 (high) \u2014 sanctioned for code generation",
             "context_window": 32000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "llama3.1:latest",
             "label": "Llama 3.1 (high) \u2014 sanctioned for code generation",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-oss:latest",
             "label": "GPT-OSS (high) \u2014 sanctioned for code generation",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "nomic-embed-text:latest",
             "label": "Nomic Embed Text \u2014 embeddings only",
             "context_window": 8192, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            # ── Local models (cost=0, run on your GPU) ────────────────────
            {"id": "qwen3:4b", "label": "Qwen 3 4B (low) — fast local",
             "context_window": 32000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B (medium) — code-aware",
             "context_window": 32000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "qwen2.5-coder:32b", "label": "Qwen 2.5 Coder 32B (high) — best local coder",
             "context_window": 32000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "qwen3-coder:30b", "label": "Qwen 3 Coder 30B (high) — local codegen",
             "context_window": 32000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "llama3.1:8b", "label": "Llama 3.1 8B — general purpose",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "llama3:latest", "label": "Llama 3 (latest) — general purpose",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            # ── Cloud models (run on Ollama Cloud, require auth) ──────────
            {"id": "kimi-k2.7-code:cloud", "label": "Kimi K2.7 Code (cloud) — newest coder ⭐",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "deepseek-v3.1:671b-cloud", "label": "DeepSeek v3.1 671B (cloud) — best reasoning",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-oss:120b-cloud", "label": "GPT OSS 120B (cloud) — strong general",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            {"id": "gpt-oss:20b-cloud", "label": "GPT OSS 20B (cloud) — fast cloud",
             "context_window": 128000, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0},
            # NOTE: qwen3-coder:480b-cloud RETIRED 2026-07-15. Check
            # `curl https://ollama.com/v1/models` before adding new cloud models.
        ],
    },
    "custom": {
        "base_url": "",
        "key_prefix": "",
        "default_models": {"low": "", "medium": "", "high": ""},
        "model_catalogue": [],
    },
}


# ── Tier ladder ───────────────────────────────────────────────────────
# iter-19 — The three-tier vocabulary (low/medium/high) could not express
# the spread of an account with ten chat deployments: seven of the
# operator's Azure deployments were unreachable because no agent could
# ever resolve to them. Six tiers do express it.
#
# Ordered cheapest → most capable, with `reasoning` at the end as a
# SIDEWAYS step rather than a seventh rung: it is not "better than
# critical", it is a different shape of model (o-series) for work that is
# diagnostic rather than generative.
TIER_ORDER = ("trivial", "low", "medium", "high", "critical", "reasoning")

# iter-19 — the floor for the reasoning deployments (gpt-5.x, o1/o3/o4) and
# for `response_format: json_object`. Exported because `routes/console.py`'s
# Test Connection endpoint must use the SAME value: its contract is that it
# tells the operator whether real calls will work, so a second literal that
# drifts would make the button pass while generation fails.
AZURE_API_VERSION_DEFAULT = "2024-12-01-preview"

# What to fall back to when a provider row has no entry for a tier. Every
# row written before iter-19 has exactly low/medium/high, and operators
# may leave new tiers blank on purpose, so resolution must degrade rather
# than fail. Each list is tried in order; the walk always terminates at a
# tier that pre-iter-19 rows are guaranteed to carry.
TIER_FALLBACK_CHAIN: Dict[str, Tuple[str, ...]] = {
    "trivial":   ("low", "medium"),
    "low":       ("medium",),
    "medium":    ("high", "low"),
    "high":      ("critical", "medium"),
    "critical":  ("high", "medium"),
    # A provider with no dedicated reasoning deployment should get its
    # strongest general model, not its cheapest.
    "reasoning": ("critical", "high", "medium"),
}


def resolve_tier_model(routing: Dict[str, str], tier: str) -> str:
    """Pick the model for `tier` from a provider's routing map.

    Walks `TIER_FALLBACK_CHAIN` when the tier is absent or blank, so a
    provider row that predates the six-tier vocabulary keeps routing every
    agent correctly instead of falling through to `models[0]`.
    """
    routing = routing or {}
    direct = (routing.get(tier) or "").strip()
    if direct:
        return direct
    for candidate in TIER_FALLBACK_CHAIN.get(tier, ()):
        val = (routing.get(candidate) or "").strip()
        if val:
            return val
    return ""


def tier_siblings(
    provider_type: str,
    tier: str,
    primary: str,
    available_models: Optional[List[Dict]] = None,
) -> List[str]:
    """Alternate deployments for `tier`, in the order to try them.

    Only meaningful where one account exposes several deployments that are
    interchangeable for a tier — Azure is the motivating case, since each
    deployment carries its own quota bucket. Returns `[]` for providers
    where rotating would just hit the same upstream limit.

    `available_models` is the provider row's own `models` catalogue; pass it
    so rotation is confined to deployments the operator actually has.
    """
    preset = PROVIDER_PRESETS.get((provider_type or "").lower(), {})
    sibs = [s for s in (preset.get("tier_siblings") or {}).get(tier, []) if s and s != primary]
    if not sibs:
        return []

    # Only rotate to something this provider row actually offers. The
    # preset lists what the vendor CAN deploy, not what this operator HAS:
    # rotating onto a deployment their account lacks would turn a
    # recoverable 429 into a 404 (`DeploymentNotFound`), which is worse
    # than the throttle we were routing around. An operator who curates
    # `models` therefore constrains rotation for free.
    #
    # An empty catalogue means "unknown", not "nothing" — several rows are
    # created without one — so in that case the preset list stands.
    catalogue = {(m or {}).get("id", "") for m in (available_models or [])}
    catalogue.discard("")
    if not catalogue:
        return sibs
    return [s for s in sibs if s in catalogue]


def exhaustion_ladder(
    provider_type: str,
    available_models: Optional[List[Dict]] = None,
) -> List[str]:
    """Account-wide descent order for this provider, strongest first.

    iter-20 — the step AFTER `tier_siblings`. Siblings are equals within a
    tier; this is the whole account ordered by capability, walked only
    once the tier's own siblings are all throttled.

    The point is to exhaust a PAID account before falling back to a local
    engine. Previously a fully-throttled tier slept and then let the call
    leave the provider, which on this operator's setup meant a 4B local
    model served work the Azure account still had headroom for on a
    slightly weaker deployment.

    Intersected with the provider row's own catalogue for the same reason
    `tier_siblings` is: the preset lists what the vendor CAN deploy, not
    what this operator HAS, and rotating onto a deployment they lack turns
    a recoverable throttle into a `DeploymentNotFound`. An empty catalogue
    means "unknown", not "nothing", so the preset list stands.
    """
    preset = PROVIDER_PRESETS.get((provider_type or "").lower(), {})
    ladder = [m for m in (preset.get("exhaustion_ladder") or []) if m]
    if not ladder:
        return []
    catalogue = {(m or {}).get("id", "") for m in (available_models or [])}
    catalogue.discard("")
    if not catalogue:
        return ladder
    return [m for m in ladder if m in catalogue]


# ── Complexity map for all agent keys ────────────────────────────────
# iter-13.76 — Split first-pass GENERATION (Sonnet / medium) from
# REGENERATION (Opus / high) on the user's two highest-cost stages
# (SRS + CodeGen). The default Console routing for Anthropic is
# {low: haiku, medium: sonnet-4.6, high: opus-4.7} (see
# PROVIDER_PRESETS above), so:
#
#   • First-time generation runs through `srs.generate` / `codegen.service`
#     / `codegen.frontend` → MEDIUM tier → Claude Sonnet 4.6.
#   • The user clicking "Regenerate" routes through `srs.regenerate` /
#     `codegen.regenerate` / `codegen.gap_recovery` → HIGH tier →
#     Claude Opus 4.7.
#   • The independent second-opinion pass (`srs.revalidation`) is also
#     HIGH so the revalidator never re-uses the cheaper model that wrote
#     the original — same vendor, stronger tier.
#
# These are tier hints — the actual model still resolves through
# `Console.routing[tier]` so an admin can rebind them without code.
AGENT_COMPLEXITY: Dict[str, str] = {
    # ── Discovery / SRS ──────────────────────────────────────────────
    "srs.gap_question":   "low",      # tiny clarification call
    "srs.generate":       "medium",   # first-time SRS section → Sonnet
    "srs.regenerate":     "high",     # user-triggered section regen → Opus
    "srs.revalidation":   "high",     # independent 2nd pass → Opus
    "srs.edit":           "medium",   # in-line edit → Sonnet
    "srs.diff":           "medium",
    # ── DataModel ────────────────────────────────────────────────────
    "datamodel.oltp":     "high",     # one-shot, deeply structural → Opus
    "datamodel.olap":     "medium",
    "datamodel.bus_matrix": "low",
    "datamodel.chat":     "medium",
    # ── Architecture ─────────────────────────────────────────────────
    "arch.recommend":     "high",
    "arch.hld":           "high",
    "arch.lld":           "medium",
    "arch.sequence":      "medium",
    "arch.api_contracts": "high",
    "arch.chat":          "medium",
    # ── CodeGen ──────────────────────────────────────────────────────
    "codegen.service":     "medium",  # first-time backend file → Sonnet
    "codegen.frontend":    "medium",  # first-time frontend file → Sonnet
    "codegen.regenerate":  "high",    # user-triggered file regen → Opus
    "codegen.gap_recovery":"high",    # regeneration of an incomplete file → Opus
    "codegen.chat":        "medium",
    "codegen.docs":        "low",
    # ── Orchestrators ────────────────────────────────────────────────
    "orchestrator.discovery":    "medium",
    "orchestrator.datamodel":    "medium",
    "orchestrator.architecture": "medium",
    "orchestrator.codegen":      "medium",
    "orchestrator.living":       "medium",
    # iter-13.32 — Graphify enrichment pass (runs during Build KB). Tier "high"
    # so by default it routes to the Anthropic Opus / equivalent flagship via
    # Console.routing[high]; downstream SRS / Arch / CodeGen then consume the
    # enriched subgraph through subgraph_yaml_for_*.
    "kb.graphify": "high",
    # ── Code Transformer / Gap Analyzer ("Tools" projects) ────────────
    # iter-16.x — these agent_keys were previously MISSING from this map,
    # so every one of them silently fell through to the generic "medium"
    # fallback in `resolve_model()` — including the Coder itself, which
    # does the heaviest lift (writing an entire migrated file from
    # scratch) and most needs the strongest tier available. Coder /
    # Pattern-Applier / Devops-Expert are "high" so that once a stronger
    # provider is configured for this tier, transformation quality
    # improves automatically without per-agent overrides. Verifier stays
    # "high" too — it's the sole quality gate and a weak grader is
    # exactly how bad transforms slip through as false ACCEPTs. Planner /
    # Context-Manager / Validator are "medium" (structural, not
    # generative). Tester is "low" — iter-15.44 made it a thin narrative
    # supplement to the real subprocess compiler, not the source of truth.
    #
    # iter-19 — re-tiered onto the six-tier ladder. `resolve_model` reads
    # `agent_configs.complexity` BEFORE this map, and the seeded rows had
    # drifted apart from it on five of these ten keys (tester ran `medium`
    # / gpt-4.1 here while this map said `low`). `seed.py`'s
    # `migrate_transformer_tiers_19` reconciles the rows to these values,
    # guarded on the old default so Console overrides survive.
    #
    # The DevOps agent gets `critical` in both its modes: it is the last
    # gate before an operator is told the build is production-ready, and
    # its re-plan round is the pipeline's one chance to repair a manifest
    # the Coder already failed to fix.
    #
    # iter-20 — Coder / Verifier / Planner promoted high -> critical.
    #
    # On this operator's Azure account `high` resolves to gpt-5 and
    # `critical` to gpt-5.1, so the strongest deployment they pay for was
    # never reached by the agents doing the migration itself — it was only
    # ever touched as a 429 rotation target. Since the account is a paid
    # key that should be spent top-down (and now degrades DOWN the whole
    # account before any local model is used — see `exhaustion_ladder`),
    # the agents that write and judge the migrated code start at the top.
    #
    # Light agents below are deliberately NOT promoted: starting a
    # classification or gate call on the flagship drains the same quota
    # the Coder needs, and brings the exhaustion point forward.
    "tools.transformer.coder":           "critical",
    "tools.transformer.pattern":         "high",
    "tools.transformer.devops_expert":   "critical",
    "tools.transformer.devops_audit":    "critical",
    "tools.transformer.verifier":        "critical",
    "tools.transformer.planner":         "critical",
    "tools.transformer.context_manager": "medium",
    "tools.transformer.validator":       "medium",
    "tools.transformer.super_agent":     "low",
    "tools.transformer.tester":          "low",
    # Diagnosis, not generation — reads a wall of build output and works
    # out what actually broke. That is what the o-series is for.
    "tools.transformer.diagnostician":   "reasoning",
    "tools.gap_analyzer":            "high",
    "tools.gap_verifier":            "high",
    "tools.gap_analyzer.doc_parser": "medium",
    # ── iter-17 — Multi-Agent CodeGen pipeline ───────────────────────
    # Legacy_migration Stage-4 mirror of the tools.transformer.* fabric,
    # but with two coder specialists (BE / FE) instead of one and with
    # extra roles (build_tool_selector, traceability_gate, finalizer).
    # See routes/codegen.py `_run_multi_agent_codegen` for orchestration.
    "codegen.super_agent":          "high",
    "codegen.context_manager":      "high",
    "codegen.planner":              "high",
    # iter-20 — the two agents that actually write source, promoted to
    # `critical` for the same reason as their tools.transformer twins.
    "codegen.coder_be":             "critical",
    "codegen.coder_fe":             "critical",
    "codegen.verifier":             "medium",
    "codegen.reviewer":             "medium",
    "codegen.tester":               "medium",
    "codegen.build_tool_selector":  "low",
    "codegen.traceability_gate":    "medium",
    "codegen.finalizer":            "low",
}


def estimate_cost(model_id: str, prompt_tokens: int, completion_tokens: int,
                  provider_type: str = "openrouter") -> float:
    preset = PROVIDER_PRESETS.get(provider_type, {})
    for m in preset.get("model_catalogue", []):
        if m["id"] == model_id:
            return (prompt_tokens / 1000 * m["cost_per_1k_input"]
                    + completion_tokens / 1000 * m["cost_per_1k_output"])
    return 0.0


def detect_provider_from_key(api_key: str) -> str:
    """Infer a provider type from an API key's prefix.

    Every value returned here MUST be a key of PROVIDER_PRESETS. An
    "AIza..." key used to return "google", which was never a preset, so
    setup_default_provider fell through to the "custom" preset — a row with
    an empty base_url and an empty model catalogue that could not route a
    single call. Google keys now return "gemini", which exists.

    Azure is deliberately absent: its keys are opaque 32-char strings with
    no distinguishing prefix, so they are indistinguishable from "custom".
    Selecting Azure explicitly in the Console is required.
    """
    key = (api_key or "").strip()
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    if not key:
        return "ollama"
    return "custom"


# ── JSON mode ─────────────────────────────────────────────────────────
# Fifteen agent call sites (every structural agent in routes/codegen.py
# and routes/tools.py) ask for `response_format={"type": "json_object"}`.
# Until this landed the kwarg was dropped in llm.py's hand-off to
# fabric_chat, so NOT ONE of them ever got JSON mode: each asked for
# strict JSON, silently received prose, and fell back to scraping the
# outermost {...} out of it with `_extract_json_object`. On a small local
# model that is exactly how a good file ends up scored REJECT / 0.0.
#
# Providers do not agree on the wire format, so the translation lives
# here and nowhere else. Adding a provider means adding one line.
#
#   openai / azure / groq / openrouter / custom
#       OpenAI's own field. `custom` covers LM Studio, vLLM and
#       llama.cpp, all of which implement the OpenAI shape.
#   ollama
#       Its /v1 OpenAI-compatibility layer has accepted `response_format`
#       since v0.5. LAMA only ever posts to /v1/chat/completions (see
#       fabric_chat), so the OpenAI field is the correct one here — NOT
#       the top-level `format: "json"` of the native /api/chat route.
#   anthropic
#       /v1/messages has no equivalent field and rejects unknown keys.
#       Callers get a system-message instruction instead; see
#       `json_mode_system_suffix`.
_JSON_MODE_OPENAI_STYLE = frozenset({
    "openai", "azure", "groq", "openrouter", "ollama", "custom",
})

# Appended to the system message for providers with no native JSON mode.
# Deliberately terse: a long lecture competes with the caller's own schema
# instructions, which are more specific and should win.
_JSON_MODE_SYSTEM_SUFFIX = (
    "\n\nRespond with a single valid JSON object and nothing else. "
    "No prose before or after it, no markdown code fences."
)


# ── Reasoning-class models ────────────────────────────────────────────
# OpenAI's reasoning families (o1, o3, o4, gpt-5) renamed the output-token
# budget to `max_completion_tokens` and REJECT `max_tokens` outright:
#
#   HTTP 400  Unsupported parameter: 'max_tokens' is not supported with
#             this model. Use 'max_completion_tokens' instead.
#
# fabric_chat sends `max_tokens` on every request, so without this every
# single call to such a deployment fails — verified against a live Azure
# gpt-5.1 deployment, where it is a total outage rather than a degradation.
#
# Matched on the model id because the constraint follows the MODEL, not the
# provider: the same rule applies to OpenAI direct, Azure, and any gateway
# in front of them. Azure makes it easier to hit because the deployment name
# is operator-chosen, which is why the match is a substring scan rather than
# an exact list — a deployment called "prod-gpt-5-chat" is still gpt-5.
_REASONING_MODEL_MARKERS = ("gpt-5", "o1-", "o3-", "o4-")


def _is_reasoning_model(model_id: str) -> bool:
    m = (model_id or "").strip().lower()
    if not m:
        return False
    # Strip a vendor prefix like "openai/" so OpenRouter-style ids match too.
    tail = m.rsplit("/", 1)[-1]
    return (
        any(marker in tail for marker in _REASONING_MODEL_MARKERS)
        or tail in {"o1", "o3", "o4"}
    )


def token_limit_field(model_id: str) -> str:
    """Name of the output-token-budget field this model accepts."""
    return "max_completion_tokens" if _is_reasoning_model(model_id) else "max_tokens"


def apply_temperature(payload: Dict, model_id: str, temperature: float) -> Dict:
    """Set the sampling temperature, or omit it for models that reject one.

    The same reasoning families that renamed the token budget also fixed
    temperature at 1 and reject any other value:

      HTTP 400  Unsupported value: 'temperature' does not support 0.1.
                Only the default (1) is supported.

    Every LAMA agent asks for a low temperature (0.1-0.3) because it wants
    deterministic JSON, so without this guard a gpt-5 / o-series deployment
    is a total outage — not a degradation — exactly like the `max_tokens`
    case above. A 400 is not in `fabric_chat_with_failover`'s recoverable
    set either, so it escapes to the Ollama fallback and the whole high
    tier silently collapses onto a local model.

    Omitting the key is preferred over sending `temperature: 1`: the
    default is what these models use anyway, and an absent field cannot be
    rejected by a gateway that validates the parameter differently.
    """
    payload.pop("temperature", None)
    if not _is_reasoning_model(model_id):
        payload["temperature"] = temperature
    return payload


# A reasoning model spends `max_completion_tokens` on its INTERNAL reasoning
# first and only then on visible output, so a budget sized for the answer
# alone is silently consumed before a single character is emitted. Verified
# against the live deployments: at max_completion_tokens=16 both o4-mini and
# gpt-5 report completion_tokens=16 and return content="". At 256 the same
# prompt answers in 19.
#
# That failure mode is worse than an error — the call returns HTTP 200 with
# an empty string, so `_extract_json_object` yields {} and the agent looks
# like it produced nothing rather than like it failed. Every caller's number
# was chosen assuming the budget was all visible output, so the correction is
# applied here rather than by editing dozens of call sites. Nothing is
# overpaid: billing follows tokens actually generated, not the ceiling.
#
# iter-19.2 — a flat floor was not enough. `codegen.planner` asks for 3000
# and routes to gpt-5; measured live, it spent ALL 3000 reasoning and
# returned "". Given 8000 it used 5035 and produced valid JSON — so roughly
# 3500 of those tokens were reasoning for a trivial input.
#
# The reserve is ADDITIVE rather than multiplicative because reasoning
# effort tracks how hard the PROBLEM is, not how long the answer is:
# doubling a 12k coder budget would reserve 12k for thinking it does not
# need, while adding nothing to a small call that needs it most.
_REASONING_OUTPUT_FLOOR = 2000
_REASONING_TOKEN_RESERVE = 6000


def apply_token_limit(payload: Dict, model_id: str, max_tokens: int) -> Dict:
    """Set the output-token budget under whichever name the model accepts.

    Mutates and returns `payload`. Always removes the other spelling, so a
    payload that already carries `max_tokens` cannot smuggle it through to a
    model that rejects it. For reasoning models the budget gains
    `_REASONING_TOKEN_RESERVE` on top of what the caller asked for, so
    thinking cannot eat the allowance meant for the answer.
    """
    field = token_limit_field(model_id)
    payload.pop("max_tokens", None)
    payload.pop("max_completion_tokens", None)
    if max_tokens:
        if field == "max_completion_tokens":
            max_tokens = max(
                max_tokens + _REASONING_TOKEN_RESERVE,
                _REASONING_OUTPUT_FLOOR,
            )
        payload[field] = max_tokens
    return payload


def supports_json_mode(provider_type: str) -> bool:
    """True when the provider accepts a native structured-output field.

    False does not mean JSON is unobtainable — it means the caller must
    fall back to instructing the model in the prompt.
    """
    return (provider_type or "").strip().lower() in _JSON_MODE_OPENAI_STYLE


def _with_json_instruction(messages: List[Dict]) -> List[Dict]:
    """Return `messages` with the JSON instruction attached to the system turn.

    Appends to the first system message so the caller's own schema
    instructions still come first and win — they are more specific than
    ours. Prepends a system message when there is none.
    """
    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            msg["content"] = (msg.get("content") or "") + _JSON_MODE_SYSTEM_SUFFIX
            return out
    return [{"role": "system", "content": _JSON_MODE_SYSTEM_SUFFIX.strip()}, *out]


def apply_json_mode(
    payload: Dict,
    provider_type: str,
    response_format: Dict | None,
) -> Dict:
    """Translate a caller's `response_format` into this provider's wire format.

    Mutates and returns `payload`. A falsy `response_format` is a no-op, so
    this is safe to call unconditionally on every request.

    For a provider with no native JSON mode the instruction is appended to
    the first system message (or a system message is prepended if there is
    none), which is the only portable way to ask.
    """
    if not response_format:
        return payload

    ptype = (provider_type or "").strip().lower()
    messages = payload.get("messages") or []

    if ptype in _JSON_MODE_OPENAI_STYLE:
        payload["response_format"] = response_format
        # OpenAI and Azure REFUSE json_object mode unless the word "json"
        # appears somewhere in the messages:
        #
        #   HTTP 400  'messages' must contain the word 'json' in some form,
        #             to use 'response_format' of type 'json_object'.
        #
        # Verified against a live Azure gpt-5.1 deployment. Without this
        # guard, switching JSON mode on would convert a working call into a
        # hard 400 for any agent whose prompt happens not to say "json" —
        # turning an accuracy fix into an outage. Most seeded LAMA prompts
        # do say it, but the code-path fallbacks ("You are the CodeGen
        # Reviewer.", used when a prompt row is missing) do not, and those
        # fire exactly when something else has already gone wrong.
        if not any("json" in (m.get("content") or "").lower() for m in messages):
            payload["messages"] = _with_json_instruction(messages)
        return payload

    # No native field on this provider — instructing the model in the prompt
    # is the only portable way to ask.
    payload["messages"] = _with_json_instruction(messages)
    return payload


# iter-14.21 — Ollama cloud vs local endpoint resolver.
# User reported: "Selected an Ollama :...-cloud model tag AND set
# OLLAMA_API_KEY on the provider, but LAMA still hit the local endpoint."
# Root cause: `PROVIDER_PRESETS['ollama'].base_url` defaults to
# localhost, and resolve_model had NO auto-switch when the caller
# implied cloud usage. Cloud Ollama needs:
#   • base_url = https://ollama.com/v1
#   • Authorization: Bearer <api_key>
#   • No localhost→host.docker.internal rewrite
#   • Skip the 10-minute local-inference timeout floor.
#
# Detection order (any of these means "route as cloud"):
#   1. provider.base_url already points at ollama.com
#   2. model_id ends with `-cloud` (Ollama's convention for cloud tags)
#   3. LAMA_OLLAMA_CLOUD env-var truthy
#   4. provider has a non-empty api_key AND provider.base_url is empty
#      or still pointing at localhost (user set the key but forgot to
#      swap the URL — most common case in the field).
def _resolve_ollama_endpoint(
    provider: Dict, model_id: str,
) -> Tuple[str, bool]:
    """Return (base_url, is_cloud) for an Ollama provider row.

    is_cloud=True implies the caller MUST add Authorization: Bearer
    <api_key> and MUST skip the localhost-in-Docker rewrite + local
    timeout floor.
    """
    base_url = (provider or {}).get("base_url", "") or ""
    # iter-14.34 — user-controlled key toggle. When key_enabled=False,
    # treat the row as if it had no api_key: no cloud auto-flip, no Bearer.
    _key_on = bool((provider or {}).get("key_enabled", True))
    api_key = ((provider or {}).get("api_key", "") or "") if _key_on else ""
    mid = (model_id or "").strip().lower()
    env_flag = (os.environ.get("LAMA_OLLAMA_CLOUD") or "").strip().lower()
    env_cloud = env_flag in {"1", "true", "yes", "on"}

    is_cloud = False
    _b = base_url.lower()
    if "ollama.com" in _b:
        is_cloud = True
    elif mid.endswith("-cloud"):
        is_cloud = True
    elif env_cloud:
        is_cloud = True
    elif api_key and (
        not base_url
        or any(h in _b for h in ("localhost", "127.0.0.1",
                                 "host.docker.internal", "0.0.0.0"))
    ):
        # User set a key AND the URL still points at localhost — the
        # ONLY sensible interpretation is "cloud". A local Ollama
        # would never need a key.
        is_cloud = True

    if is_cloud:
        cloud_url = (PROVIDER_PRESETS["ollama"].get("cloud_base_url")
                     or "https://ollama.com/v1")
        # Respect an operator-supplied cloud URL only if it's already a
        # cloud host; otherwise force the canonical cloud endpoint.
        return (base_url if "ollama.com" in _b else cloud_url, True)
    return (base_url, False)



# When LAMA's backend is inside Docker, http://localhost:11434 points at
# the CONTAINER's loopback, not the operator's host. Rewrite to
# host.docker.internal so the daemon running on the host is reached.
# Detection is best-effort: /.dockerenv exists OR LAMA_IN_DOCKER=1 OR
# /proc/1/cgroup mentions docker/containerd/kubepods. On Linux you also
# need `extra_hosts: ["host.docker.internal:host-gateway"]` in compose
# — already wired in docker-compose.yml.
def _rewrite_local_url_for_docker(url: str) -> str:
    if not url:
        return url
    in_docker = (
        os.path.exists("/.dockerenv")
        or (os.environ.get("LAMA_IN_DOCKER", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    )
    if not in_docker:
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8") as fh:
                in_docker = any(tok in fh.read() for tok in ("docker", "containerd", "kubepods"))
        except Exception:  # noqa: BLE001
            in_docker = False
    if not in_docker:
        return url
    return (
        url.replace("//localhost", "//host.docker.internal")
           .replace("//127.0.0.1", "//host.docker.internal")
    )


async def setup_default_provider(api_key: str, name: str = "", base_url: str = "",
                                 provider_type: str = "",
                                 azure_deployment: str = "",
                                 azure_api_version: str = "") -> Dict:
    """Auto-configure a provider from a single API key. Called when user pastes a key in Console.

    `provider_type` (iter-13.40) — when supplied, overrides the auto-detect.
    Used by the Console's one-click "Add Ollama" button (empty key + explicit
    type) so the row is consistently labelled "Ollama" instead of falling
    through to the "custom" preset.

    Iter 13.x fix: previously this *always* inserted a fresh document, so
    pasting a new Anthropic key three times yielded three `Anthropic (auto)`
    rows — all `is_active=True` — and the failover walker would keep hitting
    the OLD exhausted ones before reaching the funded one. We now:

    1. Deactivate every prior provider of the same `provider_type` (the old
       duplicates stay in the DB for audit but are no longer routable).
    2. Clear every stale `agent_configs.provider_id` pin so the next call
       resolves cleanly from the new default (see resolve_model — pin takes
       priority over default).
    """
    from db import model_providers as mp_col, agent_configs as ac_col
    from models import ModelProvider
    ptype = (provider_type or "").strip().lower() or detect_provider_from_key(api_key)
    preset = PROVIDER_PRESETS.get(ptype, PROVIDER_PRESETS["custom"])
    # iter-13.112 — Ollama localhost rewrite. The Console button posts
    # http://localhost:11434/v1 which is unreachable from inside Docker;
    # _rewrite_local_url_for_docker swaps it for host.docker.internal so
    # the daemon running on the host is actually reached. Non-Ollama
    # providers keep whatever the caller (or preset) supplied.
    effective_base_url = base_url or preset["base_url"]
    # iter-13.114 — rewrite localhost→host.docker.internal for ANY provider
    # whose base_url points at the loopback (Ollama, LM Studio, llama.cpp,
    # MLX, vLLM, custom). Previous gate only covered ptype=="ollama" so a
    # "custom"-typed local LM Studio row stayed unreachable from Docker.
    _b = (effective_base_url or "").lower()
    if any(h in _b for h in ("localhost", "127.0.0.1", "0.0.0.0")):
        effective_base_url = _rewrite_local_url_for_docker(effective_base_url)
    now = datetime.now(timezone.utc).isoformat()
    # Deactivate previous defaults AND deactivate prior same-type auto rows so
    # the failover walker doesn't keep re-trying the exhausted ones.
    await mp_col.update_many({}, {"$set": {"is_default": False}})
    await mp_col.update_many(
        {"provider_type": ptype, "is_active": True},
        {"$set": {"is_active": False, "deactivated_at": now,
                  "deactivated_reason": "superseded by new auto-configure"}},
    )
    # Azure carries two extra pieces of routing state that no other
    # provider needs: the deployment name (which sits in the URL path) and
    # the API version (a query parameter). Fall back to the environment so
    # a deployment configured in .env works without re-typing it in the UI.
    az_deployment = (azure_deployment or os.environ.get("AZURE_DEPLOYMENT", "")).strip()
    az_api_version = (azure_api_version or os.environ.get("AZURE_API_VERSION", "")).strip()

    routing = preset["default_models"].copy()
    catalogue = list(preset.get("model_catalogue", []))
    if ptype == "azure" and az_deployment:
        # The deployment IS the routable id on Azure, and the operator may
        # have supplied one that differs from AZURE_DEPLOYMENT in the env.
        # iter-19 — but it fills only the tiers the preset ladder left
        # EMPTY. Pointing every tier at one deployment (what this did
        # before) silently destroys complexity routing on a
        # multi-deployment account, and this path is reached by the
        # Console's "paste one API key to configure routing automatically"
        # button — so a single paste used to undo the tier ladder that
        # `seed_providers` had just set up correctly.
        routing = {tier: (routing.get(tier) or az_deployment) for tier in routing}
        if not any(m.get("id") == az_deployment for m in catalogue):
            catalogue.append({
                "id": az_deployment,
                "label": f"{az_deployment} (Azure deployment)",
                "context_window": 128000,
                "cost_per_1k_input": 0.0,
                "cost_per_1k_output": 0.0,
            })

    doc = ModelProvider(
        name=name or f"{ptype.title()} (auto)",
        provider_type=ptype,
        base_url=effective_base_url,
        api_key=api_key,
        is_default=True,
        detected_from_key=(api_key[:8] + "...") if api_key else (f"{ptype}-local" if ptype == "ollama" else ""),
        models=catalogue,
        routing=routing,
        azure_deployment=az_deployment if ptype == "azure" else "",
        azure_api_version=az_api_version if ptype == "azure" else "",
    )
    d = doc.model_dump()
    d["updated_at"] = now
    await mp_col.insert_one(d)
    # Clear every stale agent pin — otherwise resolve_model would keep
    # routing to whichever provider_id the previous failover succeeded on,
    # even if it's now inactive or exhausted.
    try:
        await ac_col.update_many({}, {"$set": {"provider_id": ""}})
    except Exception:
        pass
    d.pop("_id", None)
    return d


# iter-18.3 — Local models sanctioned for GENERATING CODE. A 4B general
# model can hold a conversation but produces code that does not compile,
# so codegen agents are pinned to this set even if routing/an override
# says otherwise. Ordered: first entry is the default substitute.
OLLAMA_CODEGEN_MODELS = ("qwen3.5:latest", "llama3.1:latest", "gpt-oss:latest")

# Agent keys whose OUTPUT IS SOURCE CODE. Verifier/reviewer/tester read
# code but emit JSON verdicts, so they are deliberately absent — they are
# light enough for the small models and gain nothing from a coder model.
_CODEGEN_AGENT_MARKERS = (
    "codegen.coder", "codegen.service", "codegen.frontend",
    "codegen.regenerate", "codegen.gap_recovery",
    "tools.transformer.coder", "tools.transformer.pattern",
    "tools.transformer.devops_expert",
)


def _is_codegen_agent(agent_key: str) -> bool:
    k = (agent_key or "").lower()
    return any(k.startswith(m) or k == m for m in _CODEGEN_AGENT_MARKERS)


def _coerce_ollama_codegen_model(model_id: str, agent_key: str, ptype: str,
                                 base_url: str = "") -> str:
    """Force a codegen agent onto a sanctioned local coder model.

    Only applies to Ollama-shaped providers. Cloud providers are left
    alone — Azure's own tier map already sends codegen to a capable
    deployment, and second-guessing an operator's cloud choice here would
    be overreach.
    """
    if not _is_codegen_agent(agent_key):
        return model_id
    # Ollama CLOUD models are a deliberate operator choice requiring cloud
    # auth, and they are large (gpt-oss:120b-cloud, deepseek-v3.1:671b).
    # The restriction exists to keep a 4B LOCAL model off code generation,
    # not to override a cloud deployment. Same `-cloud` exemption that
    # `_check_and_upgrade_model_for_context` already makes.
    if (model_id or "").strip().endswith("-cloud"):
        return model_id
    burl = (base_url or "").lower()
    looks_local = (ptype or "").lower() == "ollama" or any(
        h in burl for h in ("localhost", "127.0.0.1", "host.docker.internal", "0.0.0.0")
    )
    if not looks_local:
        return model_id
    if (model_id or "").strip() in OLLAMA_CODEGEN_MODELS:
        return model_id
    return OLLAMA_CODEGEN_MODELS[0]


async def resolve_model(
    agent_key: str,
    provider_override: Dict | None = None,
) -> Tuple[str, str, Dict, Dict]:
    """Resolve which model and provider to use for an agent.

    Returns ``(model_id, provider_base_url, provider_headers, meta)`` where
    ``meta`` carries what the caller needs to actually shape the request:

      ``provider_type``   the type of the provider we resolved to. NOT
                          necessarily the default provider's type — an agent
                          may be pinned to a different row via ``provider_id``,
                          and JSON-mode translation has to follow the provider
                          we are really talking to.
      ``request_params``  query parameters to send with the POST. Empty for
                          every provider except Azure, which carries its API
                          version in the query string rather than a header.

    ``meta`` was added when Azure landed. Azure is the first provider whose
    request cannot be described by (url, headers) alone, and JSON mode needed
    a trustworthy provider type at the payload-building site; both wanted the
    same extra return value, so they share one.

    iter-13.112 — Stage-based routing with generate/regenerate modes (like Factory).
    Determines if this is a first-time generation or regeneration based on agent_key
    patterns, then checks the appropriate stage_routing_* field.
    """
    from db import agent_configs as ac_col, model_providers as mp_col
    agent = await ac_col.find_one({"key": agent_key}, {"_id": 0})
    stage = (agent or {}).get("stage", "")
    complexity = (agent or {}).get("complexity") or AGENT_COMPLEXITY.get(agent_key, "medium")
    model_override = (agent or {}).get("model_override", "")
    provider_id = (agent or {}).get("provider_id", "")

    # Determine if this is a regeneration call based on agent_key patterns
    is_regeneration = any(pattern in (agent_key or "").lower() for pattern in [
        ".regenerate", ".gap_recovery", ".revalidation", "regen", "gap_recovery"
    ])

    # iter-19 — an explicit provider from the caller wins outright and skips
    # the pin lookup entirely. `fabric_chat_with_failover` used to express
    # "try this other provider" by WRITING `agent_configs.provider_id` and
    # restoring it afterwards; with several coroutines sharing one agent_key
    # (a test-gen or coder wave is exactly that) they clobbered each other's
    # pin mid-flight and calls landed on whichever provider the last writer
    # happened to name. Passing the provider down the call chain removes the
    # shared mutable state rather than trying to serialise access to it.
    provider = dict(provider_override) if provider_override else None
    if provider is None and provider_id:
        provider = await mp_col.find_one({"id": provider_id, "is_active": True}, {"_id": 0})
        # iter-13.34 fix — STALE-PIN CLEANUP. If the pinned provider is gone
        # or has been deactivated (Console reconfigure / new key paste /
        # provider removed) the lookup above returns None. Without this
        # cleanup, the pin would survive and `fabric_chat_with_failover`
        # would keep listing this dead provider as the "first attempt" on
        # every call — wasting one round-trip per LLM call and (when the
        # original key has now been reissued elsewhere) producing
        # confusing telemetry. Wipe the dead pin so future calls fall
        # through to the active default cleanly.
        if not provider:
            try:
                await ac_col.update_one(
                    {"key": agent_key},
                    {"$set": {"provider_id": ""}},
                )
            except Exception:  # noqa: BLE001
                pass
    if not provider:
        provider = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0})

    if not provider:
        # Fallback to env vars (legacy OpenRouter)
        return (
            os.environ.get("LAMA_DEFAULT_MODEL", "deepseek/deepseek-chat"),
            os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            {
                "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://lama.local",
                "X-Title": "LAMA",
            },
            {"provider_type": "openrouter", "request_params": {}},
        )

    # iter-13.112 — Try stage_routing_generate or stage_routing_regenerate first,
    # then fall back to complexity-based routing
    model_id = model_override
    if not model_id and stage:
        if is_regeneration:
            model_id = (provider.get("stage_routing_regenerate") or {}).get(stage, "")
        else:
            model_id = (provider.get("stage_routing_generate") or {}).get(stage, "")
    if not model_id:
        # iter-19 — walk the tier ladder rather than a bare dict lookup, so
        # a provider row written before the six-tier vocabulary (or one
        # where the operator left a new tier blank) degrades to the nearest
        # sensible tier instead of dropping to `models[0]`.
        model_id = resolve_tier_model(provider.get("routing") or {}, complexity)
    if not model_id:
        model_id = (provider.get("models") or [{}])[0].get("id", "")

    ptype = provider.get("provider_type", "openrouter")
    base_url = provider.get("base_url", "")
    # iter-18.3 — a codegen agent on a local provider must use a model
    # sanctioned for generating code, whatever routing resolved to.
    _coerced = _coerce_ollama_codegen_model(model_id, agent_key, ptype, base_url)
    if _coerced != model_id:
        try:
            logging.getLogger("lama.fabric").info(
                "iter-18.3: agent=%s is a codegen agent \u2014 substituting local "
                "model %s with sanctioned %s", agent_key, model_id, _coerced,
            )
        except Exception:  # noqa: BLE001
            pass
        model_id = _coerced
    # iter-14.34 — honour the key_enabled on/off toggle at call time.
    api_key = provider.get("api_key", "") if provider.get("key_enabled", True) else ""
    is_ollama_cloud = False
    if ptype == "ollama":
        # iter-14.21 — Route to cloud endpoint + Bearer auth when the
        # caller implies cloud (see _resolve_ollama_endpoint). Local
        # runs keep the localhost rewrite path below.
        base_url, is_ollama_cloud = _resolve_ollama_endpoint(provider, model_id)
        # iter-14.47 — Cloud-without-auth downgrade. If the stage/tier routing
        # pointed at a cloud tag (e.g. `gpt-oss:120b-cloud` for Living) but
        # the operator has `key_enabled=False` OR no api_key on the row, the
        # call would 401 with `no auth credentials`. Downgrade transparently
        # to the best available LOCAL Ollama tag: prefer routing[complexity],
        # then any non-cloud entry in `models[]`, then a sensible default.
        if is_ollama_cloud and not api_key:
            _fallback = ""
            _tier_fb = (provider.get("routing") or {}).get(complexity, "") or ""
            if _tier_fb and not _tier_fb.lower().endswith("-cloud"):
                _fallback = _tier_fb
            if not _fallback:
                for _m in (provider.get("models") or []):
                    _mid = (_m.get("id") or "")
                    if _mid and not _mid.lower().endswith("-cloud"):
                        _fallback = _mid
                        break
            if not _fallback:
                _fallback = "qwen2.5-coder:7b"  # canonical local coder
            logging.getLogger(__name__).warning(
                "iter-14.47 Ollama cloud tag %r requested but no API key "
                "(key_enabled=%s) — downgrading to local model %r",
                model_id, provider.get("key_enabled"), _fallback,
            )
            model_id = _fallback
            base_url, is_ollama_cloud = _resolve_ollama_endpoint(provider, model_id)
    # iter-13.114 — runtime localhost rewrite for already-stored rows.
    # Provider rows created before iter-13.112 (or via the API rather than
    # the Console one-click button) keep their original localhost URL on
    # disk. Apply the same rewrite at call time so they keep working from
    # inside Docker without the operator having to delete + re-add.
    _bl = (base_url or "").lower()
    if not is_ollama_cloud and any(h in _bl for h in ("localhost", "127.0.0.1", "0.0.0.0")):
        base_url = _rewrite_local_url_for_docker(base_url)
    request_params: Dict = {}
    if ptype == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    elif ptype == "azure":
        # Azure OpenAI differs from the OpenAI shape in three ways, all of
        # them structural rather than cosmetic:
        #   1. the deployment name lives in the URL path, not in `model`
        #   2. the API version is a query parameter
        #   3. auth is an `api-key` header, not `Authorization: Bearer`
        #
        # Callers append "/chat/completions" to base_url, so base_url has to
        # end at the deployment. `model_id` here is the DEPLOYMENT name — on
        # Azure that is operator-chosen and need not match any published
        # model name, which is why it is also left in the payload untouched
        # (Azure ignores it; some gateways echo it back for routing).
        deployment = model_id or provider.get("azure_deployment", "")
        # iter-19 — the previous 2024-02-15-preview default predated the
        # reasoning deployments and json_object mode, so a correctly-routed
        # gpt-5.1 call still failed at the api-version gate before the model
        # ever saw it. See AZURE_API_VERSION_DEFAULT.
        api_version = (
            provider.get("azure_api_version")
            or os.environ.get("AZURE_API_VERSION", "")
            or AZURE_API_VERSION_DEFAULT
        )
        root = (base_url or "").rstrip("/")
        # An operator may paste either the account root or a URL that already
        # points at the deployment. Only append the path we are missing, so a
        # corporate gateway URL that is already deployment-scoped is not
        # mangled into .../deployments/x/openai/deployments/x.
        if deployment and "/deployments/" not in root:
            root = f"{root}/openai/deployments/{deployment}"
        base_url = root
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        request_params = {"api-version": api_version}
    elif ptype == "ollama":
        # iter-14.21 — cloud Ollama needs Bearer auth; local doesn't.
        if is_ollama_cloud and api_key:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        else:
            headers = {"Content-Type": "application/json"}
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://lama.local",
            "X-Title": "LAMA",
        }
    return model_id, base_url, headers, {
        "provider_type": ptype,
        "request_params": request_params,
        # iter-19 — what the 429 path needs to rotate within the tier
        # instead of sleeping. `provider_id` keys the per-provider
        # concurrency gate and the shared throttle cooldown.
        "tier": complexity,
        "tier_siblings": tier_siblings(
            ptype, complexity, model_id, provider.get("models"),
        ),
        # iter-20 — the account-wide descent order, resolved here so the
        # 429 path does not have to re-read the provider row mid-call.
        # Confined to this operator's own catalogue for the same reason
        # `tier_siblings` is.
        "exhaustion_ladder": exhaustion_ladder(ptype, provider.get("models")),
        "provider_id": provider.get("id", ""),
    }


async def fabric_chat(
    messages: List[Dict],
    agent_key: str,
    project_id: str = "",
    model_override: str = "",
    max_tokens: int = 0,
    temperature: float = 0.3,
    timeout: float = 120.0,
    response_format: Dict | None = None,
    provider_override: Dict | None = None,
) -> Dict:
    """Single entry point for all LLM calls. Resolves model, applies wraps, logs usage.

    `provider_override` lets a caller (today only
    `fabric_chat_with_failover`) name the provider row to use for THIS call
    without mutating shared state — see the note in `resolve_model`.
    """
    from db import agent_configs as ac_col, token_usage_log as log_col, model_providers as mp_col
    from models import TokenUsageLog
    now = datetime.now(timezone.utc).isoformat()
    agent = await ac_col.find_one({"key": agent_key}, {"_id": 0})
    status = (agent or {}).get("status", "enabled")
    if status == "disabled":
        return {
            "content": "", "model": "disabled",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "cost_usd": 0.0, "skipped": True,
        }

    if model_override:
        _, base_url, headers, _meta = await resolve_model(agent_key, provider_override)
        model_id = model_override
    else:
        model_id, base_url, headers, _meta = await resolve_model(agent_key, provider_override)
    resolved_ptype = _meta.get("provider_type", "openrouter")
    request_params = _meta.get("request_params") or {}
    # iter-19 — pacing state. `_provider_row_id` keys both the concurrency
    # gate and the shared throttle cooldown; `_siblings` are the alternate
    # deployments this tier can rotate to on a 429.
    _provider_row_id = _meta.get("provider_id", "") or ""
    # An explicit model_override is the caller naming one specific model,
    # so honouring tier siblings would silently substitute a different one.
    _siblings: List[str] = [] if model_override else list(_meta.get("tier_siblings") or [])

    # Apply wrap prefix/suffix to first system message
    if status == "wrapped" and agent:
        prefix = agent.get("wrap_prefix", "")
        suffix = agent.get("wrap_suffix", "")
        new_messages = []
        wrapped = False
        for msg in messages:
            if not wrapped and msg.get("role") == "system":
                new_messages.append({
                    "role": "system",
                    "content": f"{prefix}\n\n{msg['content']}\n\n{suffix}".strip(),
                })
                wrapped = True
            else:
                new_messages.append(msg)
        messages = new_messages
    elif status == "replaced" and agent and agent.get("replaced_template"):
        # Replace first system message with replaced_template; keep user messages intact
        repl = agent["replaced_template"]
        replaced = False
        new_messages = []
        for msg in messages:
            if not replaced and msg.get("role") == "system":
                new_messages.append({"role": "system", "content": repl})
                replaced = True
            else:
                new_messages.append(msg)
        if not replaced:
            new_messages.insert(0, {"role": "system", "content": repl})
        messages = new_messages

    # Token budget
    budget = (agent or {}).get("token_budget_total", 0)
    used = (agent or {}).get("tokens_used_all_time", 0)
    if budget > 0 and used >= budget:
        raise RuntimeError(
            f"Agent '{agent_key}' has exceeded token budget ({used}/{budget}). "
            "Reset in Console → Agents."
        )

    effective_max = max_tokens or (agent or {}).get("max_tokens", 4096)
    
    # iter-13.100 — Context-aware model selection for local Ollama models.
    # If the prompt exceeds the model's context window, auto-upgrade to a
    # cloud model with larger context.
    _bl = (base_url or "").lower()
    _is_ollama = "ollama" in _bl or "localhost" in _bl or "127.0.0.1" in _bl or "host.docker.internal" in _bl
    if _is_ollama and not model_id.endswith("-cloud"):
        try:
            from llm import _check_and_upgrade_model_for_context
            upgraded_model, was_upgraded = await _check_and_upgrade_model_for_context(
                messages=messages,
                model_id=model_id,
                base_url=base_url,
                agent_key=agent_key,
            )
            if was_upgraded:
                import logging
                logging.getLogger("lama.fabric").info(
                    "iter-13.100: context-auto-upgrade %s → %s for agent=%s",
                    model_id, upgraded_model, agent_key,
                )
                model_id = upgraded_model
        except Exception as ctx_exc:
            import logging
            logging.getLogger("lama.fabric").warning(
                "iter-13.100: context check failed: %s — proceeding with original model",
                str(ctx_exc)[:200],
            )
    
    payload = {"model": model_id, "messages": messages}
    # Output-token budget, under whichever name this model accepts. The
    # reasoning families (gpt-5, o1/o3/o4) reject `max_tokens` with a 400,
    # so hardcoding it made those deployments unusable rather than merely
    # degraded.
    payload = apply_token_limit(payload, model_id, effective_max)
    # Same story for sampling temperature — those models accept only the
    # default. Set here rather than in the literal above so both
    # model-shaped constraints are applied in one place.
    payload = apply_temperature(payload, model_id, temperature)
    # Structured output. Applied against the provider we actually RESOLVED
    # to, not the default provider row read below — an agent pinned via
    # `provider_id` can be talking to a different vendor entirely, and
    # sending OpenAI's `response_format` to one that rejects unknown keys
    # turns a working call into a 400.
    payload = apply_json_mode(payload, resolved_ptype, response_format)
    # iter-13.115 fix — `ptype` was previously only assigned inside the
    # `finally:` block (after the HTTP call), but the iter-13.114 local-
    # timeout-floor check below read it BEFORE the call → UnboundLocalError
    # that bubbled up as a fake "ALL LLM PROVIDERS REFUSED" error for every
    # codegen file. Resolve provider_type up-front so both the timeout
    # check AND the usage-logging finally block see the same value.
    _default_provider = await mp_col.find_one({"is_default": True}, {"_id": 0}) or {}
    ptype = (_default_provider.get("provider_type") or "openrouter").lower()
    # iter-13.114 — TIMEOUT FLOOR for local providers (Ollama/LM Studio/etc).
    # A local 7B–30B model doing an 8k-token codegen prompt routinely takes
    # 3–6 minutes on consumer hardware (CPU inference, no flash-attn).
    # The default 120s timeout was a stale carry-over from cloud-only days
    # and caused every codegen-stage section to fail half-way through, which
    # the user then experienced as "the codebase isn't being generated".
    # We raise the floor to 600s (10 min) for local providers, configurable
    # via LAMA_LOCAL_LLM_TIMEOUT. Cloud calls are untouched.
    _bl = (base_url or "").lower()
    _is_local_call = (
        # iter-14.21 — Cloud Ollama uses ollama.com; exclude it from the
        # local-inference timeout floor. Only real localhost / docker-loop
        # addresses count as local now.
        (ptype == "ollama" and "ollama.com" not in _bl)
        or any(h in _bl for h in ("localhost", "127.0.0.1",
                                  "host.docker.internal", "0.0.0.0"))
    )
    # iter-14.75 — Ollama requires `num_ctx` (context window) in options.
    # Without explicit num_ctx, even 32K-capable models default to 4K context,
    # which silently truncates large prompts (KB dumps, codegen templates).
    if _is_local_call:
        _msg_chars = sum(len(m.get("content", "")) for m in messages)
        # ~4 chars/token estimate, plus 50% headroom for output
        _est_ctx = max(8192, int((_msg_chars / 4) * 1.5))
        # Cap at 32K for safety (higher can OOM on consumer GPUs)
        _est_ctx = min(_est_ctx, 32768)
        # Ollama OpenAI-compatible endpoint uses `options` for model params
        payload["options"] = {"num_ctx": _est_ctx}
        import logging
        logging.getLogger("lama.fabric").info(
            "iter-14.75: Ollama num_ctx=%d for %d-char prompt, model=%s",
            _est_ctx, _msg_chars, model_id,
        )
    if _is_local_call:
        try:
            _local_floor = float(os.environ.get("LAMA_LOCAL_LLM_TIMEOUT", "600") or 600)
        except ValueError:
            _local_floor = 600.0
        if timeout < _local_floor:
            timeout = _local_floor
    t0 = time.time()
    error_msg = ""
    call_status = "success"
    content = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        # iter-14.75 — Split timeout: short connect, long read for local LLM inference
        if _is_local_call:
            _timeout_cfg = httpx.Timeout(connect=10.0, read=timeout, write=60.0, pool=30.0)
        else:
            _timeout_cfg = timeout
        import logging
        logging.getLogger("lama.fabric").info(
            "iter-14.75: calling %s model=%s timeout=%s prompt_chars=%d",
            base_url[:40], model_id, timeout, sum(len(m.get("content", "")) for m in messages),
        )
        # iter-19 — a deployment another coroutine just learned is throttled
        # is not worth a round-trip to rediscover. Rotate to a free sibling
        # BEFORE the first attempt when one is available.
        _cool = deployment_cooldown_remaining(_provider_row_id, model_id)
        if _cool > 0 and _siblings:
            for _sib in _siblings:
                if deployment_cooldown_remaining(_provider_row_id, _sib) <= 0:
                    logging.getLogger("lama.fabric").info(
                        "iter-19: %s is cooling down for %.1fs — starting agent=%s "
                        "on tier sibling %s instead",
                        model_id, _cool, agent_key, _sib,
                    )
                    base_url = _swap_azure_deployment(base_url, model_id, _sib)
                    model_id = _sib
                    payload["model"] = _sib
                    payload = apply_token_limit(payload, _sib, effective_max)
                    payload = apply_temperature(payload, _sib, temperature)
                    break

        async with httpx.AsyncClient(timeout=_timeout_cfg, verify=_http_verify()) as client, \
                _provider_semaphore(_provider_row_id, _is_local_call):
            # A 429 states its own remedy ("try again in 67 seconds"), so wait
            # it out here rather than letting it escape as a failure. Escaping
            # is what sent transient throttles into the billing-failover path,
            # which permanently pinned agents to whichever provider answered
            # next. Bounded: a small number of attempts, each capped, so a
            # persistently throttled provider still surfaces as an error
            # instead of hanging the wave.
            #
            # iter-19 — but waiting is now the SECOND choice. Each Azure
            # deployment carries its own quota bucket, so a throttle on
            # gpt-4.1 says nothing about gpt-4o. Rotating to a tier sibling
            # turns a 52-second stall into a call that lands; we only sleep
            # once every sibling is throttled too.
            # Rotations and sleeps are counted SEPARATELY. Sharing one budget
            # (the first cut of this) meant two siblings consumed all three
            # attempts, so a call that rotated could never also wait — and on
            # a busy endpoint it failed outright where the old code would at
            # least have slept once. Rotation is a different remedy from
            # waiting, so it gets its own allowance; the total is still
            # bounded by len(siblings) + retries + 1.
            _remaining_siblings = list(_siblings)
            _sleeps_used = 0
            _roomier_tried = False
            # iter-20 — the account-wide descent, tried after the tier's
            # own siblings and before any sleep. Starts below the current
            # model so a `critical` agent degrades 5.1 -> 5 -> 4.1 rather
            # than re-trying rungs it has already passed, and excludes the
            # siblings so the two remedies do not duplicate each other.
            # An explicit model_override names one specific model, so
            # descending would silently substitute a different one — the
            # same reason tier rotation is disabled for an override.
            _ladder = [] if model_override else list(_meta.get("exhaustion_ladder") or [])
            _remaining_ladder = _ladder_below(_ladder, model_id, _siblings)
            # The longest Retry-After the provider quoted during this
            # call, used as the park length if the whole account runs out.
            _max_wait_seen = 0.0
            while True:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers, json=payload,
                    params=request_params or None,
                )
                if resp.status_code != 429:
                    # iter-19 — a context overflow is not a throttle, but a
                    # tier sibling may still solve it: within `medium`,
                    # gpt-4.1 has ~8x the window of gpt-4o. Without this the
                    # call leaves the provider entirely and lands on a local
                    # 32k model, which is the LEAST likely thing to fit a
                    # prompt that just overflowed 128k.
                    if resp.status_code != 200 and not _roomier_tried:
                        _err = resp.text[:300]
                        if _is_context_error(_err):
                            _roomier = _roomiest_sibling(
                                resolved_ptype, model_id, _remaining_siblings,
                            )
                            if _roomier:
                                _roomier_tried = True
                                logging.getLogger("lama.fabric").warning(
                                    "context overflow on %s for agent=%s — retrying on "
                                    "tier sibling %s, which has a larger window",
                                    model_id, agent_key, _roomier,
                                )
                                _remaining_siblings = [
                                    s for s in _remaining_siblings if s != _roomier
                                ]
                                base_url = _swap_azure_deployment(base_url, model_id, _roomier)
                                model_id = _roomier
                                payload["model"] = _roomier
                                payload = apply_token_limit(payload, _roomier, effective_max)
                                payload = apply_temperature(payload, _roomier, temperature)
                                continue
                    break
                _body = resp.text[:300]
                if _is_billing_error(_body):
                    break  # a hard quota cap does not heal by waiting
                _wait = _retry_after_seconds(_body)
                # Tell every other in-flight coroutine what we just learned,
                # so they route around this deployment instead of each
                # spending a round-trip to find out for themselves.
                mark_deployment_throttled(_provider_row_id, model_id, _wait)

                # Prefer a sibling that is not itself in cooldown — another
                # coroutine may already have found it throttled.
                _next = ""
                while _remaining_siblings:
                    _cand = _remaining_siblings.pop(0)
                    if deployment_cooldown_remaining(_provider_row_id, _cand) <= 0:
                        _next = _cand
                        break
                if _next:
                    logging.getLogger("lama.fabric").warning(
                        "429 from %s for agent=%s on %s — rotating to tier "
                        "sibling %s (no wait; separate quota bucket)",
                        ptype or "provider", agent_key, model_id, _next,
                    )
                    base_url = _swap_azure_deployment(base_url, model_id, _next)
                    model_id = _next
                    payload["model"] = _next
                    payload = apply_token_limit(payload, _next, effective_max)
                    payload = apply_temperature(payload, _next, temperature)
                    continue

                # iter-20 — the tier is spent. Before sleeping, DESCEND the
                # account: a weaker Azure deployment with free quota beats
                # both a 52-second stall and the local-Ollama fallback that
                # waiting eventually leads to. The operator is paying for
                # this account either way, so it should be drained before
                # anything leaves it.
                _max_used = max(_max_wait_seen, _wait)
                _rung = ""
                while _remaining_ladder:
                    _cand = _remaining_ladder.pop(0)
                    if _cand == model_id:
                        continue
                    if deployment_cooldown_remaining(_provider_row_id, _cand) <= 0:
                        _rung = _cand
                        break
                if _rung:
                    _max_wait_seen = _max_used
                    logging.getLogger("lama.fabric").warning(
                        "429 from %s for agent=%s — tier exhausted on %s, "
                        "descending the account ladder to %s rather than "
                        "waiting or leaving the provider",
                        ptype or "provider", agent_key, model_id, _rung,
                    )
                    base_url = _swap_azure_deployment(base_url, model_id, _rung)
                    model_id = _rung
                    payload["model"] = _rung
                    payload = apply_token_limit(payload, _rung, effective_max)
                    payload = apply_temperature(payload, _rung, temperature)
                    continue

                # Every deployment we know about on this account is
                # throttled. Park the whole provider so the NEXT call skips
                # it without a round-trip, instead of every coroutine
                # rediscovering the same thing — that rediscovery is what
                # made the 429 storm.
                if _ladder:
                    mark_provider_parked(_provider_row_id, _max_used)

                if _sleeps_used >= _RATE_LIMIT_MAX_RETRIES:
                    break
                _sleeps_used += 1
                logging.getLogger("lama.fabric").warning(
                    "429 from %s for agent=%s — every tier sibling is throttled, "
                    "waiting %.1fs (attempt %d/%d)",
                    ptype or "provider", agent_key, _wait,
                    _sleeps_used, _RATE_LIMIT_MAX_RETRIES,
                )
                await asyncio.sleep(_wait)
            if resp.status_code != 200:
                call_status = "error"
                error_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                raise RuntimeError(error_msg)
            # iter-20 — the account answered. If it was parked, this call
            # was the probe: clear the park so ALL traffic returns to the
            # paid provider immediately rather than draining the park.
            clear_provider_park(_provider_row_id)
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
            raw_usage = data.get("usage", {})
            usage = {
                "prompt_tokens": raw_usage.get("prompt_tokens", 0),
                "completion_tokens": raw_usage.get("completion_tokens", 0),
                "total_tokens": raw_usage.get("total_tokens", 0),
            }
    except httpx.TimeoutException:
        call_status = "timeout"
        error_msg = f"Request timed out after {timeout}s"
        raise RuntimeError(f"Agent '{agent_key}' timed out after {timeout}s")
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        # iter-13.115 fix — `ptype` is already resolved above (before the
        # call) so the timeout-floor check and usage logging share the
        # same value. Re-querying here was the original ordering bug.
        cost = estimate_cost(model_id, usage["prompt_tokens"], usage["completion_tokens"], ptype)
        log = TokenUsageLog(
            project_id=project_id, agent_key=agent_key,
            stage=(agent or {}).get("stage", ""),
            model=model_id, provider_type=ptype,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cost_usd=cost, duration_ms=duration_ms,
            status=call_status, error=error_msg,
        )
        try:
            await log_col.insert_one(log.model_dump())
        except Exception:
            pass
        if agent:
            try:
                await ac_col.update_one(
                    {"key": agent_key},
                    {"$set": {
                        "tokens_used_last_run": usage["total_tokens"],
                        "tokens_used_all_time": used + usage["total_tokens"],
                        "last_run_at": now, "last_run_model": model_id,
                        "last_run_input_tokens": usage["prompt_tokens"],
                        "last_run_output_tokens": usage["completion_tokens"],
                        "last_run_cost_usd": cost, "updated_at": now,
                    }},
                )
            except Exception:
                pass

    return {"content": content, "model": model_id, "usage": usage, "cost_usd": cost, "skipped": False}


# ──────────────────────────────────────────────────────────────────────
# Iter 13.8.2 — automatic provider failover on credit/quota/auth errors.
#
# Why: a single OpenRouter 402 ("Insufficient credits") used to bubble
# straight up to the SRS section and render as a raw JSON error in the
# document. With several is_active=True providers configured in the
# Model Fabric (Anthropic native, Groq, OpenAI, OpenRouter, Ollama, …)
# we should silently try the next one before giving up.
#
# The function tries the *default* provider first (via fabric_chat),
# then walks the remaining is_active providers in priority order.
# Returns a typed CreditError when ALL providers are exhausted so the
# caller can produce a user-actionable message.
# ──────────────────────────────────────────────────────────────────────
class CreditError(RuntimeError):
    """Raised when every configured provider has refused for billing
    reasons (402 / quota exceeded / insufficient credits / payment
    required). Carries the per-provider error chain so the UI can show
    the user exactly which keys ran out."""
    def __init__(self, attempts: List[Dict[str, str]]):
        self.attempts = attempts
        pretty = "\n".join(f"  • {a['provider']} → {a['error'][:200]}" for a in attempts)
        super().__init__("All configured LLM providers refused due to "
                         "credits/quota/auth.\n" + pretty)


_BILLING_MARKERS = (
    "insufficient credits",
    "insufficient_quota",
    "402",
    "quota exceeded",
    "billing",
    "payment required",
    "credit balance",
    "you've exceeded",
    "exceeded your current quota",
)

# A THROTTLE, not a spend problem. Kept strictly separate from the billing
# markers above, which used to contain "rate limit" and "rate-limited".
#
# That conflation had a real cost. `fabric_chat_with_failover` treats a
# billing error as grounds to walk to another provider and, on success,
# LEAVES THE AGENT PINNED there — permanently, silently, across restarts.
# So one transient 429 from a rate-limited enterprise deployment
# permanently demoted agents to whatever answered next. Observed on a live
# run: `codegen.verifier`, the quality gate for every generated file,
# ended up pinned from Azure gpt-5.1 to a local 4B model with nothing said
# to the operator. With the wave fan-out issuing six coder calls at once,
# a 429 is the expected case, not an edge case.
#
# A 429 is also the one failure that states its own remedy — the body says
# "try again in 67 seconds". Waiting is correct; changing vendor forever
# is not.
#
# "quota exceeded" deliberately stays in the BILLING list even though it
# often arrives as a 429: a hard cap does not heal by waiting.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate-limited",
    "rate_limit_exceeded",
    "too many requests",
)


def _is_billing_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(tok in m for tok in _BILLING_MARKERS)


def _is_rate_limit_error(msg: str) -> bool:
    """True for a transient throttle that should be WAITED OUT, not failed over.

    A message that is also a genuine billing failure ("quota exceeded"
    arriving as a 429) is NOT a rate limit: it will not clear by waiting.
    """
    m = (msg or "").lower()
    if any(tok in m for tok in _BILLING_MARKERS):
        return False
    return any(tok in m for tok in _RATE_LIMIT_MARKERS)


# Providers state their own backoff in prose rather than a header we can
# see here, and each phrases it differently.
_RETRY_AFTER_PATTERNS = (
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s",
    r"retry after\s+([0-9]+(?:\.[0-9]+)?)\s*s",
    r"please try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s",
    r"in\s+([0-9]+(?:\.[0-9]+)?)\s*(?:seconds|secs|s)\b",
)

_RETRY_AFTER_DEFAULT = 5.0
_RETRY_AFTER_CAP = 120.0

# Bounded so a persistently throttled provider surfaces as a real error
# rather than hanging a wave forever. Tunable for deployments on a tighter
# Azure quota than the default.
try:
    _RATE_LIMIT_MAX_RETRIES = max(0, int(os.environ.get("LAMA_RATE_LIMIT_RETRIES", "2") or 2))
except ValueError:
    _RATE_LIMIT_MAX_RETRIES = 2


def _retry_after_seconds(msg: str) -> float:
    """How long the provider asked us to wait, in seconds.

    Falls back to a short default when the message says nothing useful, and
    is capped so a provider claiming a two-hour wait cannot hang a whole
    wave. Never returns None — the caller always has a number to sleep on.
    """
    import re as _re
    m = (msg or "").lower()
    for pat in _RETRY_AFTER_PATTERNS:
        hit = _re.search(pat, m)
        if hit:
            try:
                return min(float(hit.group(1)), _RETRY_AFTER_CAP)
            except (TypeError, ValueError):
                continue
    return _RETRY_AFTER_DEFAULT


# ── iter-19: provider pacing ──────────────────────────────────────────
#
# Before this, nothing in the fabric coordinated concurrent calls. A
# wave fanned out N coroutines; each independently hit the same
# deployment, independently got a 429, and independently slept its own
# `Retry-After`. With waits observed up to 52s and two retries each, a
# single call could hold a concurrency slot ~100s before it even reached
# the failover path — which is what the operator experienced as "the LLM
# is vacant".
#
# Three coordinated pieces replace that:
#   1. a per-provider semaphore, so we stop issuing more in-flight
#      requests than the endpoint will actually serve;
#   2. a shared cooldown map, so when ONE coroutine learns a deployment
#      is throttled the others skip it instead of each rediscovering it;
#   3. deployment rotation within the tier (see `tier_siblings`), which
#      is the only one of the three that turns a throttle into a
#      completed call rather than a shorter wait.
#
# All three are in-process and deliberately not persisted: they describe
# the state of THIS worker's traffic, and a stale cooldown surviving a
# restart would be worse than rediscovering it.
_PROVIDER_SEMAPHORES: Dict[str, "asyncio.Semaphore"] = {}
_DEPLOYMENT_COOLDOWN: Dict[str, float] = {}

_LOCAL_PROVIDER_CONCURRENCY = 2
_CLOUD_PROVIDER_CONCURRENCY = 4


def _provider_semaphore(provider_id: str, is_local: bool) -> "asyncio.Semaphore":
    """One semaphore per provider row, created on first use.

    Local engines serve far fewer concurrent generations and QUEUE the
    rest — and a queued request burns its own timeout while it waits, so
    over-fanning a local Ollama converts throughput into 600s timeouts.
    """
    key = provider_id or "__default__"
    sem = _PROVIDER_SEMAPHORES.get(key)
    if sem is None:
        env = os.environ.get("LAMA_PROVIDER_MAX_CONCURRENCY", "").strip()
        if env:
            try:
                limit = max(1, int(env))
            except ValueError:
                limit = _CLOUD_PROVIDER_CONCURRENCY
        else:
            limit = _LOCAL_PROVIDER_CONCURRENCY if is_local else _CLOUD_PROVIDER_CONCURRENCY
        sem = asyncio.Semaphore(limit)
        _PROVIDER_SEMAPHORES[key] = sem
    return sem


def _cooldown_key(provider_id: str, model_id: str) -> str:
    return f"{provider_id or '_'}::{model_id or '_'}"


def mark_deployment_throttled(provider_id: str, model_id: str, seconds: float) -> None:
    """Record that this deployment is throttled until `now + seconds`.

    Expired entries are swept on write. The map is bounded by
    providers × deployments so it was never going to grow without limit,
    but a long-lived worker should not carry a row per model it has ever
    seen throttled either — and the sweep costs nothing at this size.
    """
    now = time.time()
    if len(_DEPLOYMENT_COOLDOWN) > 32:
        for k, until in list(_DEPLOYMENT_COOLDOWN.items()):
            if until <= now:
                _DEPLOYMENT_COOLDOWN.pop(k, None)
    _DEPLOYMENT_COOLDOWN[_cooldown_key(provider_id, model_id)] = now + max(0.0, seconds)


def deployment_cooldown_remaining(provider_id: str, model_id: str) -> float:
    """Seconds left on this deployment's cooldown; 0.0 when it is free."""
    until = _DEPLOYMENT_COOLDOWN.get(_cooldown_key(provider_id, model_id), 0.0)
    return max(0.0, until - time.time())


# ──────────────────────────────────────────────────────────────────────
# iter-20 — Provider-level park (circuit breaker).
#
# `_DEPLOYMENT_COOLDOWN` is per-deployment: it routes around ONE throttled
# model. When an entire account is rate-limited, every call still paid a
# full round-trip to rediscover that — which is what produced the 429
# storm in the operator's logs, hundreds of them, each costing a request
# and a `Retry-After` wait before the failover even began.
#
# A park records "this whole provider is out for N seconds" so subsequent
# calls skip it with ZERO HTTP traffic and go straight to the next
# provider (locally, Ollama). The first call after the window is a PROBE:
# if it succeeds the park clears and all traffic returns to the paid
# account, which is the operator's stated preference; if it 429s again the
# park is re-armed at double the length, capped.
#
# Deliberately in-process and not persisted, for the same reason the
# deployment cooldown is not: it describes THIS worker's traffic, and a
# stale park surviving a restart would strand an account that had since
# recovered.
# ──────────────────────────────────────────────────────────────────────
_PROVIDER_PARK: Dict[str, float] = {}
_PROVIDER_PARK_STREAK: Dict[str, int] = {}

# A park shorter than this is not worth the bookkeeping — the deployment
# cooldown already covers brief throttles.
_PROVIDER_PARK_FLOOR_SEC = 60.0
_PROVIDER_PARK_CAP_SEC = 900.0


def mark_provider_parked(provider_id: str, seconds: float = 0.0) -> float:
    """Park a whole provider; returns the park length actually applied.

    `seconds` is normally the longest `Retry-After` the provider itself
    quoted, so the wait is the provider's own stated remedy rather than a
    number we invented. It is floored (a 5s park saves nothing the
    deployment cooldown does not already) and doubled on each consecutive
    park, so an account that keeps refusing is probed with decreasing
    frequency instead of being hammered.
    """
    key = provider_id or "__default__"
    streak = _PROVIDER_PARK_STREAK.get(key, 0)
    base = max(float(seconds or 0.0), _PROVIDER_PARK_FLOOR_SEC)
    park = min(base * (2 ** streak), _PROVIDER_PARK_CAP_SEC)
    _PROVIDER_PARK[key] = time.time() + park
    _PROVIDER_PARK_STREAK[key] = streak + 1
    logging.getLogger("lama.fabric").warning(
        "iter-20: every deployment on provider=%s is throttled — parking it "
        "for %.0fs (consecutive park #%d); traffic falls back to the next "
        "provider until then",
        key, park, streak + 1,
    )
    return park


def provider_park_remaining(provider_id: str) -> float:
    """Seconds left on this provider's park; 0.0 when it is available."""
    key = provider_id or "__default__"
    until = _PROVIDER_PARK.get(key, 0.0)
    left = max(0.0, until - time.time())
    if left <= 0.0 and key in _PROVIDER_PARK:
        # Expired. Drop the row but KEEP the streak: the next call is a
        # probe, and if it fails the park must resume at the longer
        # interval rather than restarting at the floor.
        _PROVIDER_PARK.pop(key, None)
    return left


def clear_provider_park(provider_id: str) -> None:
    """A call succeeded — the account has recovered. Reset everything.

    Resetting the streak too is what lets the next incident start again at
    the short floor instead of inheriting an old escalation.
    """
    key = provider_id or "__default__"
    had = _PROVIDER_PARK.pop(key, None) is not None or _PROVIDER_PARK_STREAK.pop(key, 0) > 0
    if had:
        logging.getLogger("lama.fabric").info(
            "iter-20: provider=%s answered again — park cleared, traffic returns",
            key,
        )
    _PROVIDER_PARK_STREAK.pop(key, None)


def reset_provider_parks() -> None:
    """Test hook — clear all park state."""
    _PROVIDER_PARK.clear()
    _PROVIDER_PARK_STREAK.clear()


def _context_window(provider_type: str, model_id: str) -> int:
    """Published context window for a model, from its preset catalogue.

    0 when unknown. Used only to ORDER candidates on an overflow — never to
    pre-emptively reject a call, because published limits drift and the
    provider's own error is the authority (see `_is_context_error`).
    """
    preset = PROVIDER_PRESETS.get((provider_type or "").lower(), {})
    for m in preset.get("model_catalogue") or []:
        if (m or {}).get("id") == model_id:
            try:
                return int(m.get("context_window") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _roomiest_sibling(provider_type: str, current: str, candidates: List[str]) -> str:
    """The candidate with the largest context window, if any beats `current`.

    Returns "" when none is roomier — rotating sideways or downward after an
    overflow would just reproduce it.
    """
    here = _context_window(provider_type, current)
    best, best_ctx = "", here
    for c in candidates:
        ctx = _context_window(provider_type, c)
        if ctx > best_ctx:
            best, best_ctx = c, ctx
    return best


def _ladder_below(ladder: List[str], current: str, exclude: List[str]) -> List[str]:
    """The ladder entries strictly WEAKER than `current`, in descent order.

    iter-20 — starting the descent below the current rung matters: an
    agent already running on gpt-5 must not "descend" to gpt-5.1, which
    it either just came from or was never entitled to. When `current` is
    not on the ladder at all (a deployment the operator named themselves)
    the whole ladder is offered, since we have no position to descend
    from and any free deployment beats leaving the account.

    `exclude` drops the tier siblings, which the caller has already tried
    — rotation and descent are separate remedies and should not spend
    each other's budget.
    """
    if not ladder:
        return []
    skip = {s for s in (exclude or []) if s}
    skip.add(current)
    try:
        start = ladder.index(current) + 1
    except ValueError:
        start = 0
    return [m for m in ladder[start:] if m not in skip]


def _swap_azure_deployment(base_url: str, old_model: str, new_model: str) -> str:
    """Point an Azure base URL at a different deployment.

    Azure is the one provider where the model id is part of the URL path
    (`.../openai/deployments/<name>`), so rotating the model without
    rewriting the URL would keep calling the throttled deployment with a
    different name in the body.
    """
    if not base_url or not old_model or not new_model:
        return base_url
    marker = f"/deployments/{old_model}"
    if marker not in base_url:
        return base_url
    return base_url.replace(marker, f"/deployments/{new_model}", 1)


_AUTH_MARKERS = ("401", "unauthorized", "invalid api key", "incorrect api key", "no auth credentials")

# iter-18.3 — The prompt (or the requested completion) does not fit the
# model. Unlike a timeout this IS worth failing over: a different model
# with a bigger window, or the local Ollama fallback, may well serve it.
# Keyed off the provider's own error rather than a hardcoded context
# table, because published limits drift and the provider is authoritative.
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "maximum context",
    "context window",
    "context length",
    "too many tokens",
    "reduce the length",
    "reduce your prompt",
    "prompt is too long",
    "input is too long",
    "input too long",
    "string too long",
    "max_tokens is too large",
    "max_completion_tokens is too large",
    "exceeds the maximum",
    "requested too many tokens",
)


def _is_context_error(msg: str) -> bool:
    """True when the request exceeded the model's context or output budget.

    Distinct from a rate limit (wait and retry) and from a timeout (the
    provider never answered). Here the provider answered clearly: this
    prompt will never fit this model, so retrying it unchanged against
    the same deployment is pointless — fail over to the next provider,
    which on this deployment means the local Ollama fallback.
    """
    return any(tok in (msg or "").lower() for tok in _CONTEXT_MARKERS)


def _is_auth_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(tok in m for tok in _AUTH_MARKERS)


async def fabric_chat_with_failover(
    messages: List[Dict],
    agent_key: str,
    project_id: str = "",
    model_override: str = "",
    max_tokens: int = 0,
    temperature: float = 0.3,
    timeout: float = 120.0,
    response_format: Dict | None = None,
) -> Dict:
    """Wrap fabric_chat with automatic provider failover on 402/401/quota.

    Iteration plan: default provider → other is_active providers in
    priority order (`priority` ASC, then `created_at` DESC). Successful
    response wins. If all fail with billing-style errors, raises
    CreditError with the per-provider attempt list.
    """
    from db import model_providers as mp_col, agent_configs as ac_col

    # Look up the agent's CURRENT pin (if any) BEFORE the first attempt so
    # we can correctly mark it as "already tried" in the walk below.
    pre_agent = await ac_col.find_one({"key": agent_key}, {"_id": 0})
    pinned_id = (pre_agent or {}).get("provider_id", "") or ""

    # iter-20 — if the primary provider is PARKED (every deployment on the
    # account throttled within the last window), skip it entirely rather
    # than spending a round-trip to rediscover that. This is the fix for
    # the 429 storm: previously each of hundreds of in-flight calls paid
    # its own request and Retry-After wait before failing over.
    #
    # The park expires on its own, and `provider_park_remaining` reports 0
    # once it has — so the first call after the window goes to Azure as
    # normal and acts as the probe.
    _primary_row = await mp_col.find_one(
        {"id": pinned_id, "is_active": True}, {"_id": 0},
    ) if pinned_id else None
    if _primary_row is None:
        _primary_row = await mp_col.find_one(
            {"is_default": True, "is_active": True}, {"_id": 0},
        )
    _parked = provider_park_remaining((_primary_row or {}).get("id", ""))

    # First attempt — whatever resolve_model picks (pinned provider_id first,
    # else the default provider).
    attempts: List[Dict[str, str]] = []
    _first_error_was_rate_limit = False
    first_provider = None

    if _parked:
        logging.getLogger("lama.fabric").info(
            "iter-20: primary provider %s is parked for another %.0fs — routing "
            "agent=%s to the next provider with no round-trip",
            (_primary_row or {}).get("name") or "?", _parked, agent_key,
        )
        first_provider = _primary_row
        attempts.append({
            "provider": (_primary_row or {}).get("name") or "default",
            "type":     (_primary_row or {}).get("provider_type", ""),
            "error":    (
                f"skipped — every deployment on this account was throttled; "
                f"parked for another {_parked:.0f}s"
            ),
        })
        # A park is a throttle, so no pin is written on the way out — the
        # next call after the window returns to the primary on its own.
        _first_error_was_rate_limit = True
    else:
        try:
            return await fabric_chat(
                messages=messages, agent_key=agent_key, project_id=project_id,
                model_override=model_override, max_tokens=max_tokens,
                temperature=temperature, timeout=timeout,
                response_format=response_format,
            )
        except Exception as exc:
            msg = str(exc)
            # Only failover for *recoverable* errors. Bugs, 5xx and timeouts get
            # re-raised so they're not silently masked.
            #
            # A rate limit is recoverable too, but differently: `fabric_chat` has
            # already waited and retried it a bounded number of times before the
            # error reaches here, so by now the provider is persistently throttled.
            # Falling over lets the wave finish; the pin is released afterwards on
            # the success path so the operator's primary stays primary.
            # iter-18.3 — context/output overflow joins the recoverable set.
            # It cannot clear by waiting, but a different provider (or the
            # local Ollama fallback) may have the headroom to serve it.
            if not (_is_billing_error(msg) or _is_auth_error(msg)
                    or _is_rate_limit_error(msg) or _is_context_error(msg)):
                raise
            # Record which provider failed first so the user sees the chain.
            first_provider = _primary_row
            attempts.append({
                "provider": (first_provider or {}).get("name") or "default",
                "type":     (first_provider or {}).get("provider_type", "openrouter"),
                "error":    msg,
            })
            _first_error_was_rate_limit = _is_rate_limit_error(msg)

    # Walk every other active provider, skipping ANY id we already tried
    # (both the default AND the pre-existing pin — otherwise a pinned-but-
    # exhausted provider gets retried a second time and the walk wastes a
    # round-trip on a guaranteed 402).
    tried_ids = {
        (first_provider or {}).get("id", ""),
        pinned_id,
    }
    tried_ids.discard("")
    others = await mp_col.find(
        {"is_active": True},
        {"_id": 0},
    ).sort([("priority", 1), ("created_at", -1)]).to_list(20)

    for prov in others:
        if prov.get("id") in tried_ids:
            continue
        # iter-19 — hand the provider to fabric_chat directly instead of
        # writing `agent_configs.provider_id` and restoring it afterwards.
        # The old write was global mutable state keyed by agent_key, and a
        # wave runs many coroutines under ONE agent_key: their pins
        # interleaved, so a call could be routed by a sibling's in-flight
        # failover and the "restore" could land while another was still
        # using the pin. Passing it as an argument makes the choice local
        # to this call, which is what it always meant.
        try:
            result = await fabric_chat(
                messages=messages, agent_key=agent_key, project_id=project_id,
                model_override="",   # let resolve_model pick from this provider's routing
                max_tokens=max_tokens, temperature=temperature, timeout=timeout,
                response_format=response_format,
                provider_override=prov,
            )
            # Success. Persist a pin ONLY when the first provider failed for
            # a durable reason (billing, auth) — then re-paying the failover
            # tax on every later call would be waste.
            #
            # A throttle is not durable. Pinning after one 429 permanently
            # demoted agents to whichever provider happened to answer,
            # silently and across restarts: a live run left codegen.verifier
            # (the quality gate for every generated file) pinned from Azure
            # gpt-5.1 to a local 4B model. So a rate-limit failover now
            # writes nothing at all — this call used `prov`, and the next
            # call resolves the operator's primary again on its own.
            if _first_error_was_rate_limit:
                logging.getLogger("lama.fabric").warning(
                    "agent=%s fell back to %s for ONE call after a 429; no pin "
                    "written, so the next call returns to the primary provider",
                    agent_key, prov.get("name") or prov.get("id") or "?",
                )
            else:
                await ac_col.update_one(
                    {"key": agent_key},
                    {"$set": {"provider_id": prov.get("id", "")}},
                    upsert=True,
                )
            return result
        except Exception as exc:
            attempts.append({
                "provider": prov.get("name") or prov.get("id") or "?",
                "type":     prov.get("provider_type", ""),
                "error":    str(exc),
            })
            # iter-18.3 — keep walking on a context/output overflow too: the
            # next provider in priority order may have the headroom.
            if not (_is_billing_error(str(exc)) or _is_auth_error(str(exc))
                    or _is_context_error(str(exc))):
                # Re-raise so the caller sees the actual fault. iter-19 — no
                # pin to undo: the walk passes the provider as an argument
                # now, so there is nothing written here to roll back. The
                # blanket `provider_id: ""` that used to sit here also
                # erased a DELIBERATE operator pin whenever a call happened
                # to fail, which was never the intent.
                raise

    # iter-20 — We skipped the primary because it was parked, and no other
    # provider could serve the call. On a single-provider install that is
    # EVERY provider, so honouring the park here would take the account
    # offline for the whole window rather than merely deprioritising it.
    #
    # A park is an optimisation — avoid a round-trip we expect to fail —
    # not a policy. When there is nothing else left, a likely 429 is
    # strictly better than a certain CreditError, so try it anyway.
    if _parked:
        logging.getLogger("lama.fabric").warning(
            "iter-20: provider %s is parked but no other provider could serve "
            "agent=%s — trying the parked primary rather than failing outright",
            (_primary_row or {}).get("name") or "?", agent_key,
        )
        try:
            return await fabric_chat(
                messages=messages, agent_key=agent_key, project_id=project_id,
                model_override=model_override, max_tokens=max_tokens,
                temperature=temperature, timeout=timeout,
                response_format=response_format,
            )
        except Exception as exc:
            attempts.append({
                "provider": (_primary_row or {}).get("name") or "default",
                "type":     (_primary_row or {}).get("provider_type", ""),
                "error":    str(exc),
            })

    # Every provider exhausted.
    raise CreditError(attempts)


# ──────────────────────────────────────────────────────────────────────
# iter-13.41 — Streaming fabric call.
#
# Yields content delta strings as they arrive from the provider. Used by
# the batched SRS generator (single LLM call returning all 11 sections
# with `<<<SECTION:key>>>` delimiters) so the frontend can render each
# section the instant the closing delimiter streams through, instead of
# waiting for the whole document.
#
# Provider support:
#   • OpenAI-compatible (`openrouter`, `openai`, `groq`, `ollama`, `custom`)
#     → native OpenAI SSE stream  (`data: {"choices":[{"delta":{...}}]}`).
#   • `anthropic` native API → NotImplementedError; callers fall back to
#     buffered `fabric_chat` and yield the whole content once.
# ──────────────────────────────────────────────────────────────────────
async def fabric_chat_stream(
    messages: List[Dict],
    agent_key: str,
    project_id: str = "",
    model_override: str = "",
    max_tokens: int = 0,
    temperature: float = 0.3,
    timeout: float = 600.0,
    response_format: Dict | None = None,
):
    """Async generator yielding content delta strings.

    Raises ``NotImplementedError`` if the resolved provider doesn't speak
    OpenAI-compatible SSE (currently: ``anthropic`` native). Callers should
    catch and fall back to the buffered ``fabric_chat`` path.
    """
    import json as _json

    from db import agent_configs as ac_col, token_usage_log as log_col, model_providers as mp_col
    from models import TokenUsageLog

    now = datetime.now(timezone.utc).isoformat()
    agent = await ac_col.find_one({"key": agent_key}, {"_id": 0})
    if (agent or {}).get("status") == "disabled":
        return

    if model_override:
        _, base_url, headers, _meta = await resolve_model(agent_key)
        model_id = model_override
    else:
        model_id, base_url, headers, _meta = await resolve_model(agent_key)
    request_params = _meta.get("request_params") or {}

    # Provider gating — we only know how to stream OpenAI-compatible.
    # Prefer the type of the provider we actually resolved to; the default
    # row is only a fallback for when resolve_model took the env-var path.
    provider = await mp_col.find_one({"is_default": True}, {"_id": 0})
    ptype = (
        _meta.get("provider_type")
        or (provider or {}).get("provider_type", "openrouter")
    ).lower()
    if ptype == "anthropic":
        raise NotImplementedError(
            "fabric_chat_stream: native Anthropic /v1/messages SSE not yet "
            "implemented — caller should fall back to buffered fabric_chat."
        )

    effective_max = max_tokens or (agent or {}).get("max_tokens", 8192)
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": True,
        # Many OpenAI-compatible providers honour this; safely ignored by the rest.
        "stream_options": {"include_usage": True},
    }
    payload = apply_token_limit(payload, model_id, effective_max)
    # Streaming hits the same model-shaped constraint as the buffered path:
    # a reasoning deployment rejects an explicit temperature whether or not
    # the response is streamed.
    payload = apply_temperature(payload, model_id, temperature)
    # Structured output works alongside streaming on the OpenAI shape: the
    # deltas simply arrive already constrained to JSON. Anthropic never
    # reaches here (it raises NotImplementedError above), so in practice
    # this always takes the native-field branch.
    payload = apply_json_mode(payload, ptype, response_format)

    t0 = time.time()
    error_msg = ""
    call_status = "success"
    accumulated: List[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    try:
        async with httpx.AsyncClient(timeout=timeout, verify=_http_verify()) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions",
                headers=headers, json=payload,
                params=request_params or None,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    call_status = "error"
                    error_msg = f"HTTP {resp.status_code}: {body}"
                    raise RuntimeError(error_msg)
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(data)
                    except Exception:  # noqa: BLE001 — tolerate keepalive
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = (choices[0] or {}).get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            accumulated.append(piece)
                            yield piece
                    # Trailing usage chunk (stream_options.include_usage=true).
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        usage = {
                            "prompt_tokens": u.get("prompt_tokens", 0),
                            "completion_tokens": u.get("completion_tokens", 0),
                            "total_tokens": u.get("total_tokens", 0),
                        }
    except httpx.TimeoutException:
        call_status = "timeout"
        error_msg = f"Stream timed out after {timeout}s"
        raise RuntimeError(f"Agent '{agent_key}' stream timed out after {timeout}s")
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        if usage["total_tokens"] == 0:
            est_out = estimate_tokens("".join(accumulated))
            est_in = estimate_prompt_tokens(messages)
            usage = {"prompt_tokens": est_in, "completion_tokens": est_out,
                     "total_tokens": est_in + est_out}
        cost = estimate_cost(model_id, usage["prompt_tokens"], usage["completion_tokens"], ptype)
        log = TokenUsageLog(
            project_id=project_id, agent_key=agent_key,
            stage=(agent or {}).get("stage", ""),
            model=model_id, provider_type=ptype,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cost_usd=cost, duration_ms=duration_ms,
            status=call_status, error=error_msg,
        )
        try:
            await log_col.insert_one(log.model_dump())
        except Exception:  # noqa: BLE001
            pass
        if agent:
            try:
                used_prev = (agent or {}).get("tokens_used_all_time", 0)
                await ac_col.update_one(
                    {"key": agent_key},
                    {"$set": {
                        "tokens_used_last_run": usage["total_tokens"],
                        "tokens_used_all_time": used_prev + usage["total_tokens"],
                        "last_run_at": now, "last_run_model": model_id,
                        "last_run_input_tokens": usage["prompt_tokens"],
                        "last_run_output_tokens": usage["completion_tokens"],
                        "last_run_cost_usd": cost, "updated_at": now,
                    }},
                )
            except Exception:  # noqa: BLE001
                pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def estimate_prompt_tokens(messages: List[Dict]) -> int:
    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    return total + 4 * len(messages)
