"""iter-13.100 — Tests for the rolling-memory agent_memory module.

Two test groups:

  1. Pure-logic tests — no Mongo needed. Validate HMAC signing,
     per-agent memory policy lookup, token estimation, and
     prompt-prefix building.

  2. Integration tests using an in-memory fake collection so we don't
     need a running Mongo just to assert the CRUD + rollover flow.
"""
import os
import sys
from typing import Any, Dict, List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# Make sure the agent_memory module can import db.py — which requires
# these env vars at import time. We point at a non-existent local mongo
# so motor doesn't actually try to connect; the in-memory fakes below
# replace every collection accessor before any awaited call.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
os.environ.setdefault("LAMA_SESSION_SIGNING_KEY", "test-key-iter-13-100")


# ──────────────────────────────────────────────────────────────────────
# In-memory fake collection that mimics the bits of motor we use.
# ──────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def sort(self, *_args, **_kwargs) -> "_FakeCursor":
        return self

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self) -> None:
        self.docs: List[Dict[str, Any]] = []

    async def find_one(self, q: Dict[str, Any], _proj: Dict[str, Any] | None = None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    def find(self, q: Dict[str, Any], _proj: Dict[str, Any] | None = None) -> _FakeCursor:
        rows = [dict(d) for d in self.docs if all(d.get(k) == v for k, v in q.items())]
        return _FakeCursor(rows)

    async def insert_one(self, doc: Dict[str, Any]):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("id", "")})

    async def update_one(self, q: Dict[str, Any], upd: Dict[str, Any], upsert: bool = False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                if "$set" in upd:
                    d.update(upd["$set"])
                if "$unset" in upd:
                    for k in upd["$unset"]:
                        d.pop(k, None)
                return
        if upsert:
            new = dict(q)
            if "$set" in upd:
                new.update(upd["$set"])
            self.docs.append(new)


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every collection agent_memory touches with an in-memory fake.

    Patches both `db.<collection>` and `agent_memory.<collection>` since
    agent_memory does `from db import agent_sessions as sess_col` (a name
    binding made at import time).
    """
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
    # Reset cached signing key so the env-var pickup happens fresh.
    monkeypatch.setattr(am, "_SIGNING_KEY_CACHE", None, raising=True)

    return {"sessions": fake_sessions, "audit": fake_audit, "projects": fake_projects}


# ──���─────────��─────────────────────────────────────────────────────────
# Pure-logic tests
# ──────────────────────────────────────────────────────────────────────
def test_memory_policy_returns_agent_specific_defaults():
    from agent_memory import memory_policy, DEFAULT_MEMORY_POLICY

    srs = memory_policy("srs.chat")
    assert srs["keep_last_k"] == 10
    assert srs["summary_strategy"] == "verbatim_decisions"

    # Unknown agent falls back to the module default.
    assert memory_policy("unknown.thing") == DEFAULT_MEMORY_POLICY
    assert memory_policy("") == DEFAULT_MEMORY_POLICY


def test_estimate_tokens_uses_char_over_4_with_floor():
    from agent_memory import estimate_tokens

    assert estimate_tokens("") == 1                     # floor at 1
    assert estimate_tokens("a" * 8) == 2                # 8//4
    assert estimate_tokens("a" * 4096) == 1024


@pytest.mark.asyncio
async def test_sign_and_verify_summary_roundtrip(fake_db):
    from agent_memory import sign_summary, verify_summary_sig
    from models import AgentSession, ContextRef

    refs = [
        ContextRef(collection="stage_context", doc_id="sc-1", label="Discovery v3"),
        ContextRef(collection="srs_documents", doc_id="srs-1"),
    ]
    sig = await sign_summary(
        summary="user picked Postgres over MariaDB",
        refs=refs, project_id="p1", stage="DataModel", agent_key="datamodel.chat",
    )
    assert sig and len(sig) == 64       # SHA-256 hex digest length

    sess = AgentSession(
        project_id="p1", stage="DataModel", agent_key="datamodel.chat",
        summary="user picked Postgres over MariaDB",
        summary_sig=sig, refs=refs,
    )
    assert await verify_summary_sig(sess) is True


@pytest.mark.asyncio
async def test_tampered_summary_fails_signature(fake_db):
    """The whole point of HMAC: a client-modified summary must not validate."""
    from agent_memory import sign_summary, verify_summary_sig
    from models import AgentSession, ContextRef

    refs = [ContextRef(collection="stage_context", doc_id="sc-1")]
    sig = await sign_summary(
        summary="approved Decision A", refs=refs,
        project_id="p1", stage="Discovery", agent_key="srs.chat",
    )
    sess = AgentSession(
        project_id="p1", stage="Discovery", agent_key="srs.chat",
        # Tampered: client tried to flip the decision.
        summary="approved Decision B (actually rejected)",
        summary_sig=sig, refs=refs,
    )
    assert await verify_summary_sig(sess) is False


@pytest.mark.asyncio
async def test_empty_summary_with_empty_sig_is_trivially_valid(fake_db):
    from agent_memory import verify_summary_sig
    from models import AgentSession

    sess = AgentSession(project_id="p1", summary="", summary_sig="")
    assert await verify_summary_sig(sess) is True


# ──────────────────────────────────────────────────────────────────────
# CRUD integration tests against the in-memory fake.
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_and_get_session_roundtrip(fake_db):
    from agent_memory import create_session, get_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(
        AgentSessionCreate(
            project_id="p1", stage="Discovery", agent_key="srs.chat",
            refs=[ContextRef(collection="stage_context", doc_id="sc-1", label="KB v1")],
            seed_summary="droid-1 handed off here",
        ),
        tenant_id="tenant_42",
    )
    assert sess.id and sess.tenant_id == "tenant_42"
    assert sess.summary == "droid-1 handed off here"
    assert sess.summary_sig                # seeded summary must be signed
    assert sess.rollover_count == 0

    fetched = await get_session(sess.id)
    assert fetched is not None
    assert fetched.summary == sess.summary
    assert fetched.summary_sig == sess.summary_sig

    # Audit log captured the create event.
    actions = [d["action"] for d in fake_db["audit"].docs]
    assert "session.created" in actions


@pytest.mark.asyncio
async def test_append_turn_grows_live_tail_and_token_count(fake_db):
    from agent_memory import append_turn, create_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(project_id="p1"))
    assert sess.token_count == 0

    sess2 = await append_turn(sess.id, role="user", content="hello world" * 200)
    assert sess2 is not None
    assert len(sess2.live_turns) == 1
    assert sess2.live_turns[0].role == "user"
    assert sess2.token_count > 0
    assert sess2.live_turns[0].token_count == sess2.live_turns[0].token_count  # set


@pytest.mark.asyncio
async def test_get_session_wipes_summary_when_signature_invalid(fake_db):
    """If a stored doc was tampered with on disk (or signed with a stale
    key), the next read must self-heal by wiping the summary instead of
    returning untrusted content to the LLM."""
    from agent_memory import create_session, get_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(
        project_id="p1", seed_summary="trusted summary",
    ))
    # Tamper directly in the fake store.
    for d in fake_db["sessions"].docs:
        if d["id"] == sess.id:
            d["summary"] = "MALICIOUS summary not matching sig"
            break

    reread = await get_session(sess.id)
    assert reread is not None
    assert reread.summary == ""             # wiped because sig didn't match
    assert reread.summary_sig == ""
    actions = [d["action"] for d in fake_db["audit"].docs]
    assert "session.summary_sig_invalid" in actions


@pytest.mark.asyncio
async def test_attach_ref_dedups_and_resigns(fake_db):
    from agent_memory import attach_ref, create_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(AgentSessionCreate(
        project_id="p1", seed_summary="seed",
    ))
    original_sig = sess.summary_sig

    r1 = ContextRef(collection="stage_context", doc_id="sc-1", label="v1", hash="h1")
    s = await attach_ref(sess.id, r1)
    assert s is not None and len(s.refs) == 1
    assert s.summary_sig != original_sig    # re-signed after refs digest changed

    # Re-attach same (collection, doc_id) with a NEW hash → replace, not duplicate.
    r1_v2 = ContextRef(collection="stage_context", doc_id="sc-1", label="v2", hash="h2")
    s2 = await attach_ref(sess.id, r1_v2)
    assert s2 is not None
    assert len(s2.refs) == 1
    assert s2.refs[0].hash == "h2"


@pytest.mark.asyncio
async def test_archive_session_marks_status(fake_db):
    from agent_memory import archive_session, create_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(project_id="p1"))
    out = await archive_session(sess.id, reason="stage frozen")
    assert out is not None
    assert out.status == "archived"
    actions = [d["action"] for d in fake_db["audit"].docs]
    assert "session.archived" in actions


@pytest.mark.asyncio
async def test_list_sessions_filters_by_project_and_archived(fake_db):
    from agent_memory import archive_session, create_session, list_sessions
    from models import AgentSessionCreate

    s1 = await create_session(AgentSessionCreate(project_id="p1", stage="Discovery"))
    s2 = await create_session(AgentSessionCreate(project_id="p1", stage="DataModel"))
    await create_session(AgentSessionCreate(project_id="p2", stage="Discovery"))
    await archive_session(s2.id)

    p1_active = await list_sessions(project_id="p1")
    assert {s.id for s in p1_active} == {s1.id}

    p1_all = await list_sessions(project_id="p1", include_archived=True)
    assert {s.id for s in p1_all} == {s1.id, s2.id}


@pytest.mark.asyncio
async def test_build_prompt_prefix_includes_summary_refs_and_turns(fake_db):
    from agent_memory import append_turn, build_prompt_prefix, create_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(AgentSessionCreate(
        project_id="p1",
        seed_summary="user decided PostgreSQL over MariaDB",
        refs=[ContextRef(collection="stage_context", doc_id="sc-1", label="DataModel v2")],
    ))
    await append_turn(sess.id, role="user", content="what about the audit table?")
    sess = await append_turn(sess.id, role="assistant", content="keep it append-only.")

    # Use the legacy label-only mode here — Phase 3 hydration is covered
    # in `test_iter13100_hydration.py`.
    prefix = await build_prompt_prefix(sess, hydrate=False)
    # system(refs) + system(summary) + user + assistant = 4
    assert len(prefix) == 4
    assert prefix[0]["role"] == "system"
    assert "AUTHORITATIVE CONTEXT REFS" in prefix[0]["content"]
    assert "stage_context:sc-1" in prefix[0]["content"]
    assert prefix[1]["role"] == "system"
    assert "ROLLING MEMORY SUMMARY" in prefix[1]["content"]
    assert "PostgreSQL" in prefix[1]["content"]
    assert prefix[2] == {"role": "user", "content": "what about the audit table?"}
    assert prefix[3] == {"role": "assistant", "content": "keep it append-only."}


# ──────────────────────────────────────────────────────────────────────
# Rollover — the marquee test for the iter-13.100 design.
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rollover_summarises_older_and_keeps_last_k(fake_db, monkeypatch):
    """Force a small context window so 5 turns trip the threshold.
    Keep last 2 verbatim; the older 3 must be compressed by the
    injected fake fabric_call."""
    from agent_memory import (
        append_turn, create_session, get_session, maybe_rollover, AGENT_MEMORY,
    )
    from models import AgentSessionCreate

    # Tighten policy for this test so the assertions are deterministic.
    monkeypatch.setitem(
        AGENT_MEMORY, "srs.chat",
        {"keep_last_k": 2, "window_budget_pct": 0.50, "summary_strategy": "compact"},
    )

    sess = await create_session(AgentSessionCreate(project_id="p1", agent_key="srs.chat"))
    big_msg = "x" * 4000          # ~1000 tokens each → 5 turns ≈ 5000 tokens
    for i in range(5):
        await append_turn(sess.id, role="user" if i % 2 == 0 else "assistant",
                          content=f"turn-{i} {big_msg}")

    summary_calls: List[Dict[str, Any]] = []

    async def fake_fabric(**kwargs):
        summary_calls.append(kwargs)
        return {"content": "COMPRESSED: 3 older turns rolled up.",
                "model": "test-model/summariser",
                "usage": {"total_tokens": 100}}

    # Budget so small that anything past 1024*0.50 = 512 tokens triggers rollover.
    sess_after, rolled = await maybe_rollover(
        sess.id,
        resolved_context_window=1024,
        fabric_call=fake_fabric,
    )
    assert rolled is True
    assert sess_after.rollover_count == 1
    assert sess_after.summary == "COMPRESSED: 3 older turns rolled up."
    assert sess_after.summary_sig                # signed
    assert sess_after.summary_model == "test-model/summariser"
    # Last 2 turns preserved verbatim, older 3 dropped.
    assert len(sess_after.live_turns) == 2
    assert sess_after.live_turns[0].content.startswith("turn-3 ")
    assert sess_after.live_turns[1].content.startswith("turn-4 ")
    # Summariser was called exactly once with our system prompt.
    assert len(summary_calls) == 1
    sys_msg = summary_calls[0]["messages"][0]
    assert sys_msg["role"] == "system"
    assert "MEMORY COMPRESSOR" in sys_msg["content"]
    # Audit logged the rollover event.
    actions = [d["action"] for d in fake_db["audit"].docs]
    assert "session.rolled_over" in actions

    # Re-reading must pass signature verification (would self-wipe if not).
    reread = await get_session(sess.id)
    assert reread is not None
    assert reread.summary == sess_after.summary
    assert reread.summary_sig == sess_after.summary_sig


@pytest.mark.asyncio
async def test_rollover_noop_when_under_budget(fake_db):
    from agent_memory import append_turn, create_session, maybe_rollover

    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(project_id="p1"))
    await append_turn(sess.id, role="user", content="hi")

    async def fake_fabric(**_kwargs):       # must NOT be called
        raise AssertionError("rollover should not have invoked summariser")

    sess2, rolled = await maybe_rollover(
        sess.id, resolved_context_window=200000, fabric_call=fake_fabric,
    )
    assert rolled is False
    assert sess2.rollover_count == 0


@pytest.mark.asyncio
async def test_rollover_fail_safe_keeps_old_summary(fake_db, monkeypatch):
    """If the summariser LLM call fails, the existing summary + tail must
    be preserved (graceful degradation — never lose memory on a flaky
    network call)."""
    from agent_memory import (
        append_turn, create_session, maybe_rollover, AGENT_MEMORY,
    )
    from models import AgentSessionCreate

    monkeypatch.setitem(
        AGENT_MEMORY, "srs.chat",
        {"keep_last_k": 1, "window_budget_pct": 0.10, "summary_strategy": "compact"},
    )
    sess = await create_session(AgentSessionCreate(
        project_id="p1", agent_key="srs.chat",
        seed_summary="ORIGINAL trusted summary",
    ))
    for i in range(4):
        await append_turn(sess.id, role="user", content="x" * 2000)

    original_summary = "ORIGINAL trusted summary"

    async def angry_fabric(**_kwargs):
        raise RuntimeError("upstream LLM exploded")

    sess2, rolled = await maybe_rollover(
        sess.id, resolved_context_window=1024, fabric_call=angry_fabric,
    )
    assert rolled is False
    assert sess2.summary == original_summary    # not wiped
    actions = [d["action"] for d in fake_db["audit"].docs]
    assert "session.rollover_failed" in actions


