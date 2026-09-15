"""Azure OpenAI and Gemini as native providers.

Azure is the first provider whose request cannot be described by
(base_url, headers, model) alone:

  * the deployment name lives in the URL path, not in the ``model`` field
  * the API version is a query parameter
  * auth is an ``api-key`` header, not ``Authorization: Bearer``

So this pins the URL the fabric actually builds, not just that a preset
exists. A preset with the right keys and a wrong URL fails identically to
having no preset at all, only later and more confusingly.

Gemini is included because it was a latent defect rather than a new
feature: ``detect_provider_from_key`` already recognised "AIza" keys and
returned ``"google"``, which has never been a key of ``PROVIDER_PRESETS``.
Pasting a Gemini key therefore silently produced a "custom" row with an
empty base URL that could not route a single call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from fabric.model_fabric import (  # noqa: E402
    PROVIDER_PRESETS,
    TIER_ORDER,
    detect_provider_from_key,
    resolve_model,
)


# ── presets exist and are internally consistent ───────────────────────

@pytest.mark.parametrize("ptype", ["azure", "gemini"])
def test_preset_exists_with_required_shape(ptype: str):
    """Every preset must carry the three ORIGINAL tiers.

    iter-19 widened the vocabulary from {low, medium, high} to six tiers,
    so this is a superset check now rather than an equality one. The three
    original tiers stay REQUIRED: every provider row written before iter-19
    has exactly those, and `resolve_tier_model`'s fallback chains all
    terminate on one of them — a preset that dropped them would strand
    those rows.
    """
    preset = PROVIDER_PRESETS[ptype]
    assert set(preset) >= {"base_url", "key_prefix", "default_models", "model_catalogue"}
    assert set(preset["default_models"]) >= {"low", "medium", "high"}
    assert set(preset["default_models"]) <= set(TIER_ORDER)


def test_every_detected_provider_type_has_a_preset():
    """The defect that motivated this: detect returned a type with no preset.

    Any key prefix the detector recognises must map to a preset, otherwise
    setup_default_provider falls through to "custom" and builds a row with
    no base URL and no catalogue — a provider that exists in the UI and
    cannot route a call.
    """
    samples = {
        "sk-ant-abc": "anthropic",
        "sk-or-abc": "openrouter",
        "gsk_abc": "groq",
        "AIzaSyABC": "gemini",
        "sk-abc": "openai",
        "": "ollama",
        "opaque-corporate-key": "custom",
    }
    for key, expected in samples.items():
        detected = detect_provider_from_key(key)
        assert detected == expected, f"{key!r} detected as {detected!r}"
        assert detected in PROVIDER_PRESETS, (
            f"detect_provider_from_key returned {detected!r} for {key!r}, "
            f"which is not a key of PROVIDER_PRESETS"
        )


def test_gemini_uses_the_openai_compatibility_surface():
    """Gemini routes through the OpenAI-compatible endpoint deliberately.

    Using it means Gemini flows through the same fabric_chat path as every
    other provider rather than needing a second transport for the native
    generativelanguage REST shape.
    """
    assert PROVIDER_PRESETS["gemini"]["base_url"].endswith("/v1beta/openai")


# ── the part that actually matters: the URL Azure builds ──────────────

async def _resolve_with(provider: dict, agent: dict | None = None):
    """Drive resolve_model against one in-memory provider row."""
    from unittest.mock import AsyncMock, MagicMock, patch

    ac_col = MagicMock()
    ac_col.find_one = AsyncMock(return_value=agent or {"key": "codegen.verifier",
                                                       "complexity": "medium"})
    ac_col.update_one = AsyncMock()
    mp_col = MagicMock()
    mp_col.find_one = AsyncMock(return_value=provider)

    with patch("db.agent_configs", ac_col), patch("db.model_providers", mp_col):
        return await resolve_model("codegen.verifier")


_AZURE_ROW = {
    "id": "p-azure",
    "provider_type": "azure",
    # Deliberately an enterprise gateway host rather than *.openai.azure.com:
    # real Azure deployments routinely sit behind one, and assuming the
    # public hostname shape is how a URL builder quietly breaks for them.
    "base_url": "https://eyq-incubator.example.com/eyq/as/api",
    "api_key": "azure-key-xyz",
    "key_enabled": True,
    "azure_deployment": "gpt-5.1",
    "azure_api_version": "2023-07-01-preview",
    "routing": {"low": "gpt-5.1", "medium": "gpt-5.1", "high": "gpt-5.1"},
    "models": [{"id": "gpt-5.1"}],
}


@pytest.mark.asyncio
async def test_azure_builds_the_deployment_scoped_url():
    model_id, base_url, headers, meta = await _resolve_with(_AZURE_ROW)

    # Callers append "/chat/completions", so base_url must end at the
    # deployment for the final URL to be correct.
    assert base_url == (
        "https://eyq-incubator.example.com/eyq/as/api/openai/deployments/gpt-5.1"
    )
    assert f"{base_url}/chat/completions".endswith(
        "/openai/deployments/gpt-5.1/chat/completions"
    )
    assert model_id == "gpt-5.1"


@pytest.mark.asyncio
async def test_azure_uses_api_key_header_not_bearer():
    _, _, headers, _ = await _resolve_with(_AZURE_ROW)

    assert headers["api-key"] == "azure-key-xyz"
    assert "Authorization" not in headers, (
        "Azure authenticates with an api-key header; a Bearer token is "
        "rejected"
    )


@pytest.mark.asyncio
async def test_azure_carries_api_version_as_a_query_param():
    _, _, _, meta = await _resolve_with(_AZURE_ROW)

    assert meta["provider_type"] == "azure"
    assert meta["request_params"] == {"api-version": "2023-07-01-preview"}


@pytest.mark.asyncio
async def test_azure_does_not_double_append_an_already_scoped_url():
    """An operator may paste a URL that already points at the deployment.

    Appending unconditionally would produce
    .../deployments/gpt-5.1/openai/deployments/gpt-5.1 and 404 on every
    call, with nothing in the error naming the real cause.
    """
    row = dict(_AZURE_ROW)
    row["base_url"] = (
        "https://eyq-incubator.example.com/eyq/as/api/openai/deployments/gpt-5.1"
    )
    _, base_url, _, _ = await _resolve_with(row)

    assert base_url.count("/deployments/") == 1
    assert base_url.endswith("/openai/deployments/gpt-5.1")


@pytest.mark.asyncio
async def test_azure_falls_back_to_env_api_version(monkeypatch):
    """A row saved before the version field existed must still route."""
    monkeypatch.setenv("AZURE_API_VERSION", "2024-10-01-preview")
    row = dict(_AZURE_ROW)
    row["azure_api_version"] = ""

    _, _, _, meta = await _resolve_with(row)
    assert meta["request_params"] == {"api-version": "2024-10-01-preview"}


@pytest.mark.asyncio
async def test_non_azure_providers_carry_no_query_params():
    """Guard against the Azure branch leaking into every other provider."""
    row = {
        "id": "p-openai",
        "provider_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-abc",
        "key_enabled": True,
        "routing": {"low": "gpt-4o-mini", "medium": "gpt-4o", "high": "gpt-4o"},
        "models": [{"id": "gpt-4o"}],
    }
    _, base_url, headers, meta = await _resolve_with(row)

    assert base_url == "https://api.openai.com/v1"
    assert headers["Authorization"] == "Bearer sk-abc"
    assert meta["request_params"] == {}
    assert meta["provider_type"] == "openai"


@pytest.mark.asyncio
async def test_azure_honours_the_key_enabled_toggle():
    """key_enabled=False must blank the credential, as it does elsewhere."""
    row = dict(_AZURE_ROW)
    row["key_enabled"] = False

    _, _, headers, _ = await _resolve_with(row)
    assert headers["api-key"] == ""


# ── reasoning-class models and the output-token budget ────────────────
#
# Found by calling the live Azure gpt-5.1 deployment: it rejects the
# `max_tokens` that fabric_chat has always sent, with
#
#   HTTP 400  Unsupported parameter: 'max_tokens' is not supported with
#             this model. Use 'max_completion_tokens' instead.
#
# That is a total outage for such a deployment, not a degradation, so it is
# pinned here rather than left to be rediscovered.

from fabric.model_fabric import (  # noqa: E402
    apply_token_limit,
    token_limit_field,
)


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5.1",
        "gpt-5",
        "gpt-5.1-2025-11-13",
        "prod-gpt-5-chat",      # operator-named Azure deployment
        "openai/gpt-5.1",       # OpenRouter-style vendor prefix
        "o1-preview",
        "o3-mini",
        "o4-mini",
    ],
)
def test_reasoning_models_use_max_completion_tokens(model_id: str):
    assert token_limit_field(model_id) == "max_completion_tokens"


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "claude-opus-4-7",
        "qwen2.5-coder:7b",
        "llama-3.3-70b-versatile",
        "deepseek/deepseek-chat",
        "gemini-2.5-pro",
        "",
    ],
)
def test_conventional_models_keep_max_tokens(model_id: str):
    assert token_limit_field(model_id) == "max_tokens"


def test_apply_token_limit_never_leaves_both_spellings():
    """A payload carrying the wrong spelling must not smuggle it through.

    Sending both is itself a 400 on the reasoning families, so the helper
    always clears the other name rather than only adding the right one.
    """
    payload = {"model": "gpt-5.1", "max_tokens": 4096}
    out = apply_token_limit(payload, "gpt-5.1", 2000)

    # The exact number is not this test's business — iter-19.2 adds a
    # reasoning reserve on top of the caller's figure, and pinning the
    # literal here made an unrelated test fail for the right change. What
    # matters is that only ONE spelling survives.
    assert set(out) == {"model", "max_completion_tokens"}
    assert "max_tokens" not in out
    assert out["max_completion_tokens"] >= 2000


def test_apply_token_limit_omits_the_field_entirely_when_zero():
    """max_tokens=0 means "no explicit budget", not "a budget of zero"."""
    out = apply_token_limit({"model": "gpt-4o"}, "gpt-4o", 0)

    assert "max_tokens" not in out
    assert "max_completion_tokens" not in out
