"""One bounded JSON repair re-ask when a structured-output call comes back prose.

Small local models routinely ignore a `response_format` request: they answer
in prose, wrap the object in ``` fences, or prepend "Sure! Here's the JSON:".
Before this, the pipeline's response was to score the file REJECT at
confidence 0.0 and make a human look at it.

The repair lives in `fabric_call` rather than at the fifteen agent call sites
because that is the single choke point every LLM call already passes through
(contract #4). One edit covers all of them and none can drift apart later.

What these pin:

* it fires only when the caller actually asked for JSON
* it is capped at exactly ONE extra call, so a model that cannot produce
  JSON costs one wasted round-trip rather than an unbounded loop
* a failed repair returns the ORIGINAL result, so this can only add a chance
  of success, never remove one
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

import llm as llm_mod  # noqa: E402
from llm import parses_as_json_object  # noqa: E402


# ── the strictness question ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"verdict":"ACCEPT","confidence":97}',
    '  {"a": 1}  ',
    '{}',
])
def test_valid_json_objects_are_accepted(text: str):
    assert parses_as_json_object(text) is True


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "Sure! Here is the JSON you asked for: {\"a\": 1}",
    '```json\n{"a": 1}\n```',
    '{"a": 1,}',                       # trailing comma
    "{'a': 1}",                        # single quotes
    '[{"a": 1}]',                      # array, not an object
    'The verdict is ACCEPT with 97% confidence.',
])
def test_malformed_or_wrapped_output_is_rejected(text: str):
    """Deliberately strict.

    The agents' own `_extract_json_object` helpers scrape the outermost
    braces out of prose, and that leniency is what let this go unnoticed:
    a model that wraps its answer in an apology still "parses", so nobody
    saw that the structured-output request was being dropped entirely.
    This asks the stricter question — did we get what we asked for.
    """
    assert parses_as_json_object(text) is False


# ── the repair pass ───────────────────────────────────────────────────

def _stub_impl(monkeypatch, replies):
    """Replace _fabric_call_impl with a scripted sequence of replies."""
    calls: list[list[dict]] = []

    async def fake(messages, agent_key="", project_id="", **kwargs):
        calls.append(messages)
        return {"content": replies[min(len(calls) - 1, len(replies) - 1)],
                "model": "stub", "usage": {}}

    monkeypatch.setattr(llm_mod, "_fabric_call_impl", fake)

    async def noop_trace(**kw):
        return None

    monkeypatch.setattr(llm_mod, "_record_llm_trace", noop_trace)
    return calls


@pytest.mark.asyncio
async def test_prose_reply_triggers_exactly_one_repair(monkeypatch):
    calls = _stub_impl(monkeypatch, [
        "Sure! The verdict is ACCEPT.",            # first: unparseable
        '{"verdict":"ACCEPT","confidence":97}',    # repair: good
    ])

    out = await llm_mod.fabric_call(
        messages=[{"role": "user", "content": "score it"}],
        agent_key="codegen.verifier",
        response_format={"type": "json_object"},
    )

    assert out["content"] == '{"verdict":"ACCEPT","confidence":97}'
    assert out["json_repaired"] is True
    assert len(calls) == 2, "expected exactly one repair re-ask"

    # The repair must show the model its own output and the parser error,
    # otherwise it is just a retry and retries of a deterministic-ish
    # failure tend to reproduce it.
    repair_turns = calls[1]
    assert repair_turns[-2]["role"] == "assistant"
    assert repair_turns[-2]["content"] == "Sure! The verdict is ACCEPT."
    assert "not valid JSON" in repair_turns[-1]["content"]


@pytest.mark.asyncio
async def test_good_reply_is_never_repaired(monkeypatch):
    calls = _stub_impl(monkeypatch, ['{"verdict":"ACCEPT"}'])

    out = await llm_mod.fabric_call(
        messages=[{"role": "user", "content": "score it"}],
        agent_key="codegen.verifier",
        response_format={"type": "json_object"},
    )

    assert len(calls) == 1
    assert "json_repaired" not in out


@pytest.mark.asyncio
async def test_no_repair_when_json_was_not_requested(monkeypatch):
    """Most calls want prose. They must not pay for a parse check."""
    calls = _stub_impl(monkeypatch, ["Here is a long markdown document..."])

    out = await llm_mod.fabric_call(
        messages=[{"role": "user", "content": "write the SRS section"}],
        agent_key="srs.generate",
    )

    assert len(calls) == 1
    assert out["content"].startswith("Here is a long markdown")


@pytest.mark.asyncio
async def test_repair_is_capped_at_one_even_when_it_also_fails(monkeypatch):
    """A model that cannot produce JSON costs ONE wasted call, not a loop."""
    calls = _stub_impl(monkeypatch, [
        "still not json",
        "still not json either",
        "and again",
    ])

    out = await llm_mod.fabric_call(
        messages=[{"role": "user", "content": "score it"}],
        agent_key="codegen.verifier",
        response_format={"type": "json_object"},
    )

    assert len(calls) == 2, "the repair must not recurse"
    # Original is returned untouched, so the caller's existing lenient
    # `_extract_json_object` fallback still gets its chance.
    assert out["content"] == "still not json"
    assert "json_repaired" not in out


@pytest.mark.asyncio
async def test_a_failing_repair_call_never_breaks_the_original(monkeypatch):
    """If the re-ask itself raises, the first result must survive."""
    state = {"n": 0}

    async def fake(messages, agent_key="", project_id="", **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return {"content": "prose", "model": "stub", "usage": {}}
        raise RuntimeError("provider went down mid-repair")

    monkeypatch.setattr(llm_mod, "_fabric_call_impl", fake)

    async def noop_trace(**kw):
        return None

    monkeypatch.setattr(llm_mod, "_record_llm_trace", noop_trace)

    out = await llm_mod.fabric_call(
        messages=[{"role": "user", "content": "score it"}],
        agent_key="codegen.verifier",
        response_format={"type": "json_object"},
    )

    assert out["content"] == "prose"
