"""A transient 429 must not permanently demote an agent to another provider.

Found by running the real pipeline against a rate-limited Azure deployment.
Two agents ended the run silently pinned to local Ollama:

    codegen.coder_fe  -> Ollama (local)
    codegen.verifier  -> Ollama (local)

with `token_usage_log` showing why:

    codegen.verifier  azure  gpt-5.1  error ::
        HTTP 429: {"statusCode": 429,
                   "message": "Rate limit is exceeded. Try again in 67 seconds."}

`_BILLING_MARKERS` contained "rate limit", so `_is_billing_error` returned
True for a 429. That routed a TRANSIENT throttle into the billing-failover
path, which on success leaves the agent pinned to whichever provider
answered — permanently, silently, and across restarts.

The consequences are not subtle. The verifier is the quality gate for every
generated file, and it was demoted from a frontier model to a 4B local one
without a word to the operator. With the wave fan-out running six coder
calls at once, a 429 on a rate-limited enterprise deployment is not an edge
case, it is the expected case.

A 429 is also the one error that tells you exactly what to do: the body says
"try again in 67 seconds". Waiting is the correct response. Switching
vendors forever is not.

Three things are pinned here:
  * a 429 is classified as a rate limit, NOT as a billing error
  * a real billing or auth failure is still classified as before
  * a retry delay is parsed from the provider's own message
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
    _is_auth_error,
    _is_billing_error,
    _is_rate_limit_error,
    _retry_after_seconds,
)

AZURE_429 = (
    'HTTP 429: { "statusCode": 429, "message": "Rate limit is exceeded. '
    'Try again in 67 seconds." }'
)
OPENAI_429 = "HTTP 429: Rate limit reached for gpt-4o in organization org-x"
GROQ_429 = "HTTP 429: rate_limit_exceeded: Please try again in 4.2s"


# ── a 429 is a rate limit, not a billing failure ──────────────────────

@pytest.mark.parametrize("msg", [AZURE_429, OPENAI_429, GROQ_429])
def test_429_is_a_rate_limit(msg: str):
    assert _is_rate_limit_error(msg) is True


@pytest.mark.parametrize("msg", [AZURE_429, OPENAI_429, GROQ_429])
def test_429_is_not_a_billing_error(msg: str):
    """The regression. Billing classification is what triggered the pin."""
    assert _is_billing_error(msg) is False, (
        "a transient 429 must not enter the billing-failover path — that is "
        "what permanently demoted the verifier to a local 4B model"
    )


@pytest.mark.parametrize("msg", [AZURE_429, OPENAI_429, GROQ_429])
def test_429_is_not_an_auth_error(msg: str):
    assert _is_auth_error(msg) is False


# ── genuine billing and auth failures still classify correctly ────────

@pytest.mark.parametrize("msg", [
    "HTTP 402: Insufficient credits",
    "insufficient_quota: You exceeded your current quota",
    "Payment required",
    "Your credit balance is too low",
    "billing hard limit reached",
])
def test_real_billing_errors_still_classified(msg: str):
    assert _is_billing_error(msg) is True
    assert _is_rate_limit_error(msg) is False


@pytest.mark.parametrize("msg", [
    "HTTP 401: Unauthorized",
    "invalid api key provided",
    "no auth credentials found",
])
def test_auth_errors_still_classified(msg: str):
    assert _is_auth_error(msg) is True
    assert _is_rate_limit_error(msg) is False


def test_quota_exceeded_is_billing_not_rate_limit():
    """"quota exceeded" is a hard cap, not a throttle — it does not heal
    by waiting, so failover remains the right response."""
    msg = "HTTP 429: You exceeded your current quota, please check your plan"
    assert _is_billing_error(msg) is True


# ── the provider tells us how long to wait; use it ────────────────────

@pytest.mark.parametrize("msg,expected", [
    (AZURE_429, 67.0),
    (GROQ_429, 4.2),
    ("Rate limit is exceeded. Try again in 4 seconds.", 4.0),
    ("Please retry after 12 seconds", 12.0),
])
def test_retry_delay_parsed_from_the_provider_message(msg, expected):
    assert _retry_after_seconds(msg) == pytest.approx(expected, rel=0.01)


def test_retry_delay_falls_back_when_unparseable():
    d = _retry_after_seconds("HTTP 429: too many requests")
    assert d is not None and 0 < d <= 60


def test_retry_delay_is_capped():
    """A provider claiming a 2-hour wait must not hang a wave forever."""
    d = _retry_after_seconds("Try again in 7200 seconds")
    assert d is not None and d <= 120


# ── the failover must not leave a permanent pin after a throttle ──────

@pytest.mark.asyncio
async def test_rate_limit_failover_releases_the_pin(monkeypatch):
    """The whole point: one 429 must not permanently change an agent's provider.

    Pins survive restarts, so a pin set by a momentary throttle is
    effectively forever. This asserts the pin is cleared once the
    fallback call succeeds, so the next call returns to the primary.
    """
    from fabric import model_fabric as mf

    writes: list = []

    class _AC:
        async def find_one(self, *a, **kw):
            return {"key": "codegen.verifier", "provider_id": ""}

        async def update_one(self, flt, upd, **kw):
            writes.append(upd.get("$set", {}).get("provider_id"))

    class _Cursor:
        def sort(self, *a, **kw):
            return self

        async def to_list(self, n):
            return [{"id": "p-ollama", "name": "Ollama (local)",
                     "provider_type": "ollama", "is_active": True}]

    class _MP:
        async def find_one(self, *a, **kw):
            return {"id": "p-azure", "name": "Azure", "provider_type": "azure",
                    "is_default": True, "is_active": True}

        def find(self, *a, **kw):
            return _Cursor()

    monkeypatch.setitem(sys.modules, "db",
                        type("db", (), {"agent_configs": _AC(), "model_providers": _MP()})())

    calls = {"n": 0}

    async def fake_fabric_chat(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(AZURE_429)
        return {"content": "{}", "model": "qwen3:4b", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)

    out = await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="codegen.verifier",
    )

    assert out["content"] == "{}"
    assert writes, "the failover never touched the pin at all"
    assert writes[-1] == "", (
        f"a 429 failover left the agent pinned to {writes[-1]!r}; the pin "
        "must be released so the next call returns to the primary provider"
    )
