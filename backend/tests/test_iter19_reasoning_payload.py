"""iter-19 — request shaping for reasoning-class deployments.

The defect this pins: `fabric_chat` built its payload as
`{"model", "messages", "temperature"}` with `temperature` set
unconditionally, while every LAMA agent asks for 0.1-0.3 to get
deterministic JSON. Azure's gpt-5.x and o-series deployments reject any
temperature other than the default with

    HTTP 400  Unsupported value: 'temperature' does not support 0.1.
              Only the default (1) is supported.

A 400 is not in `fabric_chat_with_failover`'s recoverable set, so it
re-raised into llm.py's Ollama fallback and the call finished on a local
model at a 600s timeout. With `critical` routed to gpt-5.1, that was a
total outage of the top of the tier ladder presenting as "the LLM is
always vacant", not as an error anyone could see.

The module already knew the family — `_is_reasoning_model` existed and was
applied to the token-limit field. Only temperature was left unguarded.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fabric.model_fabric import (  # noqa: E402
    PROVIDER_PRESETS,
    apply_temperature,
    apply_token_limit,
    _is_reasoning_model,
)


# ── the family detector ───────────────────────────────────────────────

@pytest.mark.parametrize("model_id", [
    "gpt-5", "gpt-5.1", "gpt-5-mini", "o1-preview", "o3", "o3-mini", "o4-mini",
    # Azure deployment names are operator-chosen, which is why the match is
    # a substring scan: a deployment called "prod-gpt-5-chat" is still gpt-5.
    "prod-gpt-5-chat",
    # OpenRouter-style vendor prefixes must not hide the family.
    "openai/o3-mini",
])
def test_reasoning_models_are_recognised(model_id):
    assert _is_reasoning_model(model_id) is True, model_id


@pytest.mark.parametrize("model_id", [
    "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini",
    "qwen3.5:latest", "llama3.1:latest", "gpt-oss:latest", "qwen2.5-coder:7b",
    "",
])
def test_ordinary_models_are_not_misclassified(model_id):
    assert _is_reasoning_model(model_id) is False, model_id


# ── temperature ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model_id", ["gpt-5.1", "gpt-5", "o3", "o4-mini", "o3-mini"])
def test_reasoning_deployments_get_no_temperature(model_id):
    """Omitted, not coerced to 1.

    The default is what these models use anyway, and an absent field cannot
    be rejected by a gateway that validates the parameter differently.
    """
    payload = apply_temperature({"model": model_id}, model_id, 0.1)
    assert "temperature" not in payload


@pytest.mark.parametrize("model_id", ["gpt-4.1", "gpt-4o-mini", "qwen3.5:latest"])
def test_ordinary_deployments_keep_the_callers_temperature(model_id):
    payload = apply_temperature({"model": model_id}, model_id, 0.1)
    assert payload["temperature"] == 0.1


def test_a_smuggled_temperature_cannot_survive():
    """A payload that already carries `temperature` must not slip it past
    the guard — the same contract `apply_token_limit` holds for
    `max_tokens`, and for the same reason: one leaked key is a 400."""
    payload = apply_temperature({"model": "o3", "temperature": 0.7}, "o3", 0.7)
    assert "temperature" not in payload


# ── both constraints together ─────────────────────────────────────────

# ── the reasoning-token budget ────────────────────────────────────────

def test_a_small_budget_is_raised_for_reasoning_models():
    """Reasoning tokens are spent from the SAME allowance as visible output.

    Measured against the live deployments: at max_completion_tokens=16 both
    o4-mini and gpt-5 report completion_tokens=16 and return content="";
    at 256 the identical prompt answers in 19. That is worse than an error
    — HTTP 200 with an empty string makes the agent look like it produced
    nothing rather than like it failed.
    """
    payload = apply_token_limit({}, "o4-mini", 16)
    assert payload["max_completion_tokens"] >= 2000


def test_a_generous_budget_is_left_alone():
    """The floor raises, never lowers — a caller asking for room to write a
    whole file must still get it."""
    payload = apply_token_limit({}, "gpt-5.1", 12000)
    assert payload["max_completion_tokens"] == 12000


def test_ordinary_models_keep_the_exact_budget_asked_for():
    """The floor is a property of reasoning models, not a global minimum;
    raising it for gpt-4.1-mini would change cost for no benefit."""
    assert apply_token_limit({}, "gpt-4.1-mini", 16)["max_tokens"] == 16


def test_no_budget_means_no_field():
    """A caller passing 0 means "provider default" — the floor must not
    invent a ceiling where none was asked for."""
    assert apply_token_limit({}, "o3", 0) == {}


def test_reasoning_payload_carries_neither_rejected_field():
    """The shape actually sent to a gpt-5.1 deployment."""
    payload = {"model": "gpt-5.1", "messages": []}
    payload = apply_token_limit(payload, "gpt-5.1", 8000)
    payload = apply_temperature(payload, "gpt-5.1", 0.1)
    assert payload["max_completion_tokens"] == 8000
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_ordinary_payload_is_unchanged_in_shape():
    payload = {"model": "gpt-4.1", "messages": []}
    payload = apply_token_limit(payload, "gpt-4.1", 8000)
    payload = apply_temperature(payload, "gpt-4.1", 0.2)
    assert payload["max_tokens"] == 8000
    assert "max_completion_tokens" not in payload
    assert payload["temperature"] == 0.2


def test_every_reasoning_tier_target_is_actually_reasoning_shaped():
    """Guard against a future edit that points `critical` or `reasoning` at
    a model the payload shaper does not recognise — the tier would then
    send a temperature the deployment rejects, silently reopening this
    outage."""
    tiers = PROVIDER_PRESETS["azure"]["default_models"]
    for tier in ("critical", "reasoning"):
        assert _is_reasoning_model(tiers[tier]), (
            f"azure tier {tier!r} points at {tiers[tier]!r}, which the payload "
            "shaper does not treat as reasoning-class"
        )
