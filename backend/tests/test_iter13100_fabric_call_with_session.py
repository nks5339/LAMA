"""iter-13.100 — Tests for the session-aware wrapper `fabric_call_with_session`
in `backend/llm.py`.

These tests stub `fabric_call` and the agent_memory CRUD with in-memory
fakes so we don't need a live Mongo / LLM provider to assert the
splice / rollover / append contract.
"""
import os
import sys
from typing import Any, Dict, List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
os.environ.setdefault("LAMA_SESSION_SIGNING_KEY", "test-key-iter-13-100")


# Reuse the FakeCollection from the agent_memory tests.
from tests.test_iter13100_agent_memory import FakeCollection  # type: ignore


@pytest.fixture
def fake_db(monkeypatch):
    import db
    import agent_memory as am

    fake_sessions = FakeCollection()
    fake_audit = FakeCollection()
    fake_projects = FakeCollection()

    monkeypatch.setattr(db, "agent_sessions", fake_sessions, raising=True)
    monkeypatch.setattr(db, "audit_log", fake_audit, raising=True)
    monkeypatch.setattr(db, "projects", fake_projects, raising=True)
    monkeypatch.setattr(am, "sess_col", fake_sessions, raising=True)
    monkeypatch.setattr(am, "audit_col", fake_audit, raising=True)
    monkeypatch.setattr(am, "projects_col", fake_projects, raising=True)
    monkeypatch.setattr(am, "_SIGNING_KEY_CACHE", None, raising=True)

    return {"sessions": fake_sessions, "audit": fake_audit, "projects": fake_projects}


@pytest.fixture
def stub_fabric_call(monkeypatch):
    """Replace `llm.fabric_call` with a recording stub.

    Returns the recorder list (each entry = {messages, agent_key, project_id, kwargs}).
    The stub returns a deterministic assistant reply.
    """
    import llm

    calls: List[Dict[str, Any]] = []

    async def _stub(messages, agent_key="", project_id="", **kwargs):
        calls.append({
            "messages": list(messages or []),
            "agent_key": agent_key,
            "project_id": project_id,
            "kwargs": dict(kwargs),
        })
        return {
            "content": "stubbed assistant reply",
            "model": "stub/test-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }

    monkeypatch.setattr(llm, "fabric_call", _stub, raising=True)
    return calls


# ──────────────────────────────────────────────────────────────────────
# Wrapper contract tests
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_empty_session_id_is_pure_passthrough(fake_db, stub_fabric_call):
    """No session_id → wrapper must forward verbatim to fabric_call."""
    from llm import fabric_call_with_session

    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hello"},
    ]
    result = await fabric_call_with_session(
        msgs, session_id="", agent_key="srs.chat", project_id="p1",
    )
    assert result["content"] == "stubbed assistant reply"
    assert len(stub_fabric_call) == 1
    assert stub_fabric_call[0]["messages"] == msgs
    assert stub_fabric_call[0]["agent_key"] == "srs.chat"
    assert stub_fabric_call[0]["project_id"] == "p1"
    # No session was created/touched.
    assert fake_db["sessions"].docs == []


@pytest.mark.asyncio
async def test_unknown_session_id_falls_through(fake_db, stub_fabric_call):
    """Stale / wrong session_id must NOT block the LLM call."""
    from llm import fabric_call_with_session

    result = await fabric_call_with_session(
        [{"role": "user", "content": "hi"}],
        session_id="does-not-exist", agent_key="srs.chat",
    )
    assert result["content"] == "stubbed assistant reply"
    assert len(stub_fabric_call) == 1


@pytest.mark.asyncio
async def test_archived_session_falls_through(fake_db, stub_fabric_call):
    from agent_memory import archive_session, create_session
    from llm import fabric_call_with_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(project_id="p1", agent_key="srs.chat"))
    await archive_session(sess.id)

    msgs = [{"role": "user", "content": "hi"}]
    await fabric_call_with_session(msgs, session_id=sess.id, agent_key="srs.chat")
    # Plain pass-through — no prefix was spliced in.
    assert len(stub_fabric_call) == 1
    assert stub_fabric_call[0]["messages"] == msgs


@pytest.mark.asyncio
async def test_active_session_splices_prefix_and_appends_after(fake_db, stub_fabric_call):
    """The marquee test: prefix is spliced between caller-system and
    caller-rest, AND both the new user turn + assistant reply are
    appended to the session for next time."""
    from agent_memory import create_session, get_session
    from llm import fabric_call_with_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(AgentSessionCreate(
        project_id="p1", agent_key="srs.chat",
        seed_summary="we decided on Postgres",
        refs=[ContextRef(collection="stage_context", doc_id="sc-1", label="DataModel v2")],
    ))
    caller_msgs = [
        {"role": "system", "content": "STAGE BRIEF: Discovery"},
        {"role": "user", "content": "what about audit tables?"},
    ]
    result = await fabric_call_with_session(
        caller_msgs, session_id=sess.id,
        agent_key="srs.chat", project_id="p1",
        # Phase 3: hydration would try to fetch stage_context/sc-1 which
        # doesn't exist in fake_db — exercise pure splice shape here.
        hydrate_refs=False,
    )
    assert result["content"] == "stubbed assistant reply"

    # Inspect what fabric_call actually received.
    assert len(stub_fabric_call) == 1
    sent = stub_fabric_call[0]["messages"]
    roles = [m["role"] for m in sent]
    # Phase 3 reorders to: caller-system, refs-system (label-only here),
    # summary-system, caller-user.
    assert roles[0] == "system" and "STAGE BRIEF" in sent[0]["content"]
    assert roles[1] == "system" and "AUTHORITATIVE CONTEXT REFS" in sent[1]["content"]
    assert "stage_context:sc-1" in sent[1]["content"]
    assert roles[2] == "system" and "ROLLING MEMORY SUMMARY" in sent[2]["content"]
    assert "Postgres" in sent[2]["content"]
    assert roles[-1] == "user" and sent[-1]["content"] == "what about audit tables?"

    # After the call, session should have grown by 2 turns (user + assistant).
    sess_after = await get_session(sess.id)
    assert sess_after is not None
    contents = [t.content for t in sess_after.live_turns]
    assert contents == ["what about audit tables?", "stubbed assistant reply"]


@pytest.mark.asyncio
async def test_auto_append_false_does_not_grow_session(fake_db, stub_fabric_call):
    from agent_memory import create_session, get_session
    from llm import fabric_call_with_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(project_id="p1", agent_key="srs.chat"))
    await fabric_call_with_session(
        [{"role": "user", "content": "anything"}],
        session_id=sess.id, agent_key="srs.chat",
        auto_append=False,
    )
    sess_after = await get_session(sess.id)
    assert sess_after is not None
    assert sess_after.live_turns == []


@pytest.mark.asyncio
async def test_wrapper_runs_rollover_before_calling_fabric(fake_db, stub_fabric_call, monkeypatch):
    """Force the session over budget so the wrapper triggers rollover
    BEFORE the stubbed fabric_call is invoked for the real user turn.

    The rollover summariser itself calls plain fabric_call (no session_id)
    — that's the second entry in our recorder; the real user turn is the
    third."""
    from agent_memory import AGENT_MEMORY, append_turn, create_session, get_session
    from llm import fabric_call_with_session
    from models import AgentSessionCreate

    # Make the rollover threshold tiny.
    monkeypatch.setitem(
        AGENT_MEMORY, "srs.chat",
        {"keep_last_k": 2, "window_budget_pct": 0.10, "summary_strategy": "compact"},
    )
    # Make the resolved context window tiny too — force rollover.
    async def _tiny_window(*_args, **_kwargs):
        return 1024
    import llm
    monkeypatch.setattr(llm, "_resolve_context_window", _tiny_window, raising=True)

    sess = await create_session(AgentSessionCreate(project_id="p1", agent_key="srs.chat"))
    for i in range(5):
        await append_turn(sess.id, role="user" if i % 2 == 0 else "assistant",
                          content="x" * 4000)
    pre_rollover_count = (await get_session(sess.id)).rollover_count
    assert pre_rollover_count == 0

    await fabric_call_with_session(
        [{"role": "user", "content": "next question"}],
        session_id=sess.id, agent_key="srs.chat",
    )

    # Two fabric_call invocations expected: one for the rollover summariser,
    # one for the real user turn.
    assert len(stub_fabric_call) == 2

    sess_after = await get_session(sess.id)
    assert sess_after is not None
    assert sess_after.rollover_count == 1
    # After rollover we kept last 2 verbatim, then appended new user +
    # assistant → 4 live turns total.
    assert len(sess_after.live_turns) == 4
    assert sess_after.summary == "stubbed assistant reply"  # rollover used the stub
    assert sess_after.summary_sig                            # signed


@pytest.mark.asyncio
async def test_split_system_and_rest_handles_edges():
    from llm import _split_system_and_rest

    assert _split_system_and_rest([]) == ([], [])
    assert _split_system_and_rest([{"role": "user", "content": "x"}]) == (
        [], [{"role": "user", "content": "x"}],
    )
    head, rest = _split_system_and_rest([
        {"role": "system", "content": "a"},
        {"role": "system", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "system", "content": "mid"},
        {"role": "assistant", "content": "d"},
    ])
    assert [m["content"] for m in head] == ["a", "b"]
    # Mid-stream system message stays in rest (not hoisted).
    assert [m["role"] for m in rest] == ["user", "system", "assistant"]


@pytest.mark.asyncio
async def test_resolve_context_window_falls_back_when_db_empty(fake_db):
    """With no providers configured and no hint, must return the safe default."""
    from llm import _resolve_context_window

    cw = await _resolve_context_window(agent_key="srs.chat", project_id="p1")
    assert cw == 32000


@pytest.mark.asyncio
async def test_resolve_context_window_uses_preset_when_hint_matches(fake_db):
    """A model hint that exists in PROVIDER_PRESETS must drive the answer."""
    from llm import _resolve_context_window

    # claude-opus-4.7 sits in the anthropic + openrouter presets with 200000.
    cw = await _resolve_context_window(model_hint="anthropic/claude-opus-4.7")
    assert cw == 200000


