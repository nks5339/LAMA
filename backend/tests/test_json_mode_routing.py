"""JSON mode reaches the provider, in that provider's own wire format.

Phase 0 recon found that fifteen agent call sites across
``routes/codegen.py`` and ``routes/tools.py`` pass
``response_format={"type": "json_object"}`` into ``fabric_call``, and that
not one of them ever got JSON mode. ``llm.py`` forwarded exactly two kwargs
(``max_tokens`` and ``temperature``) at each of its three hand-off sites, so
the kwarg was dropped before any payload was built. Every structural agent
— verifier, reviewer, tester, planner, traceability gate — asked for strict
JSON, silently received prose, and fell back to scraping the outermost
``{...}`` out of it. On a small local model that is exactly how a perfectly
good generated file ends up scored REJECT at confidence 0.0.

These tests pin two things:

1.  The translation itself, per provider, because providers do not agree on
    the wire format and sending OpenAI's field to one that rejects unknown
    keys turns a working call into a 400.
2.  That the kwarg survives the trip from ``fabric_call`` down to the
    payload, which is the part that was actually broken. A unit test of the
    translator alone would have passed happily for the entire time the bug
    was live, so the plumbing is tested end-to-end against a fake transport.
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
    apply_json_mode,
    supports_json_mode,
)

JSON_OBJECT = {"type": "json_object"}


# ── the translation table ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "ptype",
    ["openai", "azure", "groq", "openrouter", "ollama", "custom"],
)
def test_openai_style_providers_get_the_native_field(ptype: str):
    """These speak OpenAI's structured-output field verbatim.

    ``ollama`` belongs here because LAMA only ever posts to
    /v1/chat/completions, its OpenAI-compatibility surface, which has
    accepted ``response_format`` since v0.5 — NOT the top-level
    ``format: "json"`` of the native /api/chat route. ``custom`` covers
    LM Studio, vLLM and llama.cpp, all of which implement the same shape.
    """
    # The prompt already says "JSON", which is the common case for LAMA's
    # structural agents and avoids entangling this with the word-injection
    # guard covered separately below.
    original = [{"role": "user", "content": "Return JSON."}]
    payload = {"model": "m", "messages": [dict(m) for m in original]}
    out = apply_json_mode(payload, ptype, JSON_OBJECT)

    assert out["response_format"] == JSON_OBJECT
    assert supports_json_mode(ptype) is True
    # A prompt that already qualifies must be passed through untouched.
    assert out["messages"] == original


def test_openai_style_gets_the_json_word_injected_when_absent():
    """OpenAI and Azure refuse json_object mode unless "json" is in the messages.

        HTTP 400  'messages' must contain the word 'json' in some form, to
                  use 'response_format' of type 'json_object'.

    Verified against a live Azure gpt-5.1 deployment. Without the guard,
    turning JSON mode on converts a working call into a hard 400 for any
    agent whose prompt does not happen to say "json" — an accuracy fix that
    ships as an outage. The code-path fallbacks in routes/codegen.py (for
    example ``"You are the CodeGen Reviewer."``, used when a prompt row is
    missing) are exactly such prompts, and they fire when something else
    has already gone wrong.
    """
    payload = {
        "model": "gpt-5.1",
        "messages": [
            {"role": "system", "content": "You are the CodeGen Verifier."},
            {"role": "user", "content": "Emit a verdict of ACCEPT."},
        ],
    }
    out = apply_json_mode(payload, "azure", JSON_OBJECT)

    assert out["response_format"] == JSON_OBJECT
    blob = " ".join(m["content"] for m in out["messages"]).lower()
    assert "json" in blob


def test_openai_style_leaves_messages_alone_when_json_already_present():
    """Do not pad a prompt that already says it.

    The caller's own wording is more specific than ours, and every injected
    token is one the operator pays for on a prompt that did not need it.
    """
    original = [
        {"role": "system", "content": "Return ONLY the JSON described below."},
        {"role": "user", "content": "go"},
    ]
    payload = {"model": "gpt-4o", "messages": [dict(m) for m in original]}
    out = apply_json_mode(payload, "openai", JSON_OBJECT)

    assert out["messages"] == original


def test_json_word_detection_is_case_insensitive():
    """Prompts say "JSON" far more often than "json"."""
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": "Return STRICT JSON ONLY."}],
    }
    out = apply_json_mode(payload, "openai", JSON_OBJECT)

    assert out["messages"] == [{"role": "system", "content": "Return STRICT JSON ONLY."}]


def test_anthropic_gets_a_system_instruction_instead():
    """Anthropic's /v1/messages has no equivalent field and rejects unknown keys.

    The only portable way to ask is in the prompt, so the instruction is
    appended to the existing system message rather than sent as a field.
    """
    payload = {
        "model": "claude",
        "messages": [
            {"role": "system", "content": "You are the Verifier."},
            {"role": "user", "content": "check this"},
        ],
    }
    out = apply_json_mode(payload, "anthropic", JSON_OBJECT)

    assert "response_format" not in out, (
        "anthropic must not receive the OpenAI field — it 400s on unknown keys"
    )
    assert supports_json_mode("anthropic") is False
    system = out["messages"][0]["content"]
    assert system.startswith("You are the Verifier."), (
        "the caller's own instructions must survive and come first — they are "
        "more specific than ours and should win"
    )
    assert "single valid JSON object" in system
    # The user turn is untouched.
    assert out["messages"][1] == {"role": "user", "content": "check this"}


def test_anthropic_with_no_system_message_gets_one_prepended():
    payload = {"model": "claude", "messages": [{"role": "user", "content": "go"}]}
    out = apply_json_mode(payload, "anthropic", JSON_OBJECT)

    assert out["messages"][0]["role"] == "system"
    assert "single valid JSON object" in out["messages"][0]["content"]
    assert out["messages"][1] == {"role": "user", "content": "go"}


@pytest.mark.parametrize("falsy", [None, {}, False])
def test_no_response_format_is_a_no_op(falsy):
    """Safe to call unconditionally on every request.

    The overwhelming majority of calls do not want JSON mode, so the
    translator has to be inert for them rather than something the payload
    builder must remember to guard.
    """
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    before = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_json_mode(payload, "openai", falsy)

    assert out == before
    assert "response_format" not in out


def test_unknown_provider_falls_back_to_the_instruction():
    """An unrecognised provider must not receive a field it may reject.

    Failing closed here means a newly added provider degrades to a prompt
    instruction rather than 400-ing every structural call until someone
    notices.
    """
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_json_mode(payload, "some-new-vendor", JSON_OBJECT)

    assert "response_format" not in out
    assert out["messages"][0]["role"] == "system"


# ── the plumbing, end to end ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_response_format_survives_fabric_chat_into_the_payload(monkeypatch):
    """The regression test for the actual defect.

    Pins that a caller passing ``response_format`` to ``fabric_chat`` results
    in it appearing in the JSON body that reaches the transport. This is the
    hop that was broken; the translator above was never the problem.
    """
    from fabric import model_fabric as mf

    captured: dict = {}

    async def fake_resolve(agent_key):
        return (
            "gpt-4o",
            "https://api.openai.com/v1",
            {"Authorization": "Bearer k", "Content-Type": "application/json"},
            {"provider_type": "openai", "request_params": {}},
        )

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None, params=None):
            captured["url"] = url
            captured["json"] = json
            captured["params"] = params
            return _Resp()

    class _Col:
        async def find_one(self, *a, **kw):
            return {}

        async def insert_one(self, *a, **kw):
            return None

        async def update_one(self, *a, **kw):
            return None

    monkeypatch.setattr(mf, "resolve_model", fake_resolve)
    monkeypatch.setattr(mf.httpx, "AsyncClient", _Client)
    monkeypatch.setitem(
        sys.modules, "db",
        type("db", (), {"agent_configs": _Col(), "token_usage_log": _Col(),
                        "model_providers": _Col()})(),
    )

    await mf.fabric_chat(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="codegen.verifier",
        response_format=JSON_OBJECT,
    )

    assert captured["json"]["response_format"] == JSON_OBJECT, (
        "response_format did not reach the provider payload — this is the "
        "exact defect the fix addresses"
    )


def test_every_structural_agent_call_site_still_asks_for_json():
    """Guard the fifteen call sites that motivated the fix.

    If someone removes ``response_format`` from these agents believing it to
    be inert — which it genuinely was before this change — the structural
    agents quietly go back to parsing prose. Counting them is crude but it
    is the cheapest thing that notices.
    """
    codegen = (_BACKEND / "routes" / "codegen.py").read_text(encoding="utf-8")
    tools = (_BACKEND / "routes" / "tools.py").read_text(encoding="utf-8")
    total = codegen.count("response_format=") + tools.count("response_format=")

    assert total >= 15, (
        f"expected at least the 15 structural-agent call sites recorded in "
        f"Phase 0 recon, found {total}"
    )
