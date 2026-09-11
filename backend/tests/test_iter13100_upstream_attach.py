"""iter-13.100 Phase 5 — Auto-attach upstream-refs + per-stage chat isolation.

The two contracts under test:

  1. `auto_attach_upstream_refs(project_id, stage)` returns a refs list
     pointing at every frozen `stage_context` doc upstream of `stage`.
     Empty when no upstream stages are frozen, partial when only some
     are, never raises.

  2. `create_session(req, auto_attach_upstream=True)` (the default) wires
     that helper in transparently — a session created in stage N+1
     starts with stages 1..N's frozen handoffs already attached as
     authoritative refs, hashed for drift detection.

  3. Per-(project, stage, agent_key) sessions are isolated: rolling
     over the Discovery session does not affect the DataModel session
     for the same project.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
os.environ.setdefault("LAMA_SESSION_SIGNING_KEY", "test-key-iter-13-100")

from tests.test_iter13100_agent_memory import FakeCollection  # type: ignore


@pytest.fixture
def fake_db(monkeypatch):
    import db
    import agent_memory as am

    fakes = {
        "agent_sessions": FakeCollection(),
        "audit_log":      FakeCollection(),
        "projects":       FakeCollection(),
        "stage_context":  FakeCollection(),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(db, name, fake, raising=True)

    monkeypatch.setattr(am, "sess_col", fakes["agent_sessions"], raising=True)
    monkeypatch.setattr(am, "audit_col", fakes["audit_log"], raising=True)
    monkeypatch.setattr(am, "projects_col", fakes["projects"], raising=True)
    monkeypatch.setattr(am, "_SIGNING_KEY_CACHE", None, raising=True)

    return fakes


# ─────────────────────────────────────────────────────────────────────
# _upstream_stages
# ─────────────────────────────────────────────────────────────────────
def test_upstream_stages_for_each_stage():
    from agent_memory import _upstream_stages

    assert _upstream_stages("Discovery") == ()
    assert _upstream_stages("DataModel") == ("Discovery",)
    assert _upstream_stages("Architecture") == ("Discovery", "DataModel")
    assert _upstream_stages("CodeGen") == ("Discovery", "DataModel", "Architecture")
    assert _upstream_stages("Living") == ("Discovery", "DataModel", "Architecture", "CodeGen")
    # Unknown / blank / lowercase → empty (no spurious refs).
    assert _upstream_stages("nonsense") == ()
    assert _upstream_stages("") == ()
    assert _upstream_stages("discovery") == ()


# ─────────────────────────────────────────────────────────────────────
# auto_attach_upstream_refs
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_auto_attach_empty_for_discovery(fake_db):
    from agent_memory import auto_attach_upstream_refs

    refs = await auto_attach_upstream_refs("p1", "Discovery")
    assert refs == []


@pytest.mark.asyncio
async def test_auto_attach_returns_only_frozen_upstream(fake_db):
    """CodeGen requires Discovery + DataModel + Architecture frozen. If
    only Discovery + DataModel are frozen the helper returns refs for
    those two; it must NOT raise."""
    from agent_memory import auto_attach_upstream_refs

    fake_db["stage_context"].docs.extend([
        {"id": "sc-disc-v3", "project_id": "p1", "stage": "Discovery", "version": 3,
         "frozen_at": "2026-06-01T00:00:00Z", "outputs": {"x": 1}, "toon_summary": "ts"},
        {"id": "sc-dm-v1", "project_id": "p1", "stage": "DataModel", "version": 1,
         "frozen_at": "2026-06-02T00:00:00Z", "outputs": {"y": 2}, "toon_summary": "tdm"},
        # Architecture intentionally NOT frozen.
    ])

    refs = await auto_attach_upstream_refs("p1", "CodeGen")
    assert len(refs) == 2
    ids = {r.doc_id for r in refs}
    assert ids == {"sc-disc-v3", "sc-dm-v1"}
    # Each ref carries a non-empty hash (for drift detection).
    assert all(r.hash for r in refs)
    # Labels include stage name + version.
    labels = {r.label for r in refs}
    assert "Discovery frozen v3" in labels
    assert "DataModel frozen v1" in labels


@pytest.mark.asyncio
async def test_auto_attach_scopes_to_project(fake_db):
    """A frozen stage_context for project p2 must NOT leak into p1's
    auto-attached refs."""
    from agent_memory import auto_attach_upstream_refs

    fake_db["stage_context"].docs.extend([
        {"id": "sc-p2-disc", "project_id": "p2", "stage": "Discovery", "version": 1,
         "outputs": {}, "toon_summary": ""},
    ])
    refs = await auto_attach_upstream_refs("p1", "DataModel")
    assert refs == []


@pytest.mark.asyncio
async def test_auto_attach_first_hydration_reports_no_drift(fake_db):
    """The hash captured by auto-attach must match what the hydrator
    re-computes on the very next read — otherwise every fresh session
    would scream "DRIFTED" on its first call."""
    from agent_memory import auto_attach_upstream_refs, hydrate_refs

    fake_db["stage_context"].docs.append({
        "id": "sc-1", "project_id": "p1", "stage": "Discovery", "version": 1,
        "frozen_at": "2026-06-01T00:00:00Z", "outputs": {"a": 1}, "toon_summary": "s",
    })
    refs = await auto_attach_upstream_refs("p1", "DataModel")
    assert len(refs) == 1

    hydrated = await hydrate_refs(refs)
    assert hydrated[0]["status"] == "ok"
    assert hydrated[0]["drifted"] is False


# ─────────────────────────────────────────────────────────────────────
# create_session integration
# ��────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_session_auto_attaches_upstream_refs_by_default(fake_db):
    from agent_memory import create_session
    from models import AgentSessionCreate

    fake_db["stage_context"].docs.extend([
        {"id": "sc-disc", "project_id": "p1", "stage": "Discovery", "version": 5,
         "outputs": {}, "toon_summary": ""},
        {"id": "sc-dm", "project_id": "p1", "stage": "DataModel", "version": 2,
         "outputs": {}, "toon_summary": ""},
    ])

    sess = await create_session(AgentSessionCreate(
        project_id="p1", stage="Architecture", agent_key="arch.chat",
    ))
    # Both upstream frozen stage_contexts auto-attached.
    assert {r.doc_id for r in sess.refs} == {"sc-disc", "sc-dm"}
    # Audit log records the count for transparency.
    create_event = [a for a in fake_db["audit_log"].docs if a["action"] == "session.created"][0]
    assert create_event["auto_attached_refs"] == 2


@pytest.mark.asyncio
async def test_explicit_refs_suppress_auto_attach(fake_db):
    """When the caller supplies refs explicitly, we must NOT also
    auto-attach upstream ones (caller knows best)."""
    from agent_memory import create_session
    from models import AgentSessionCreate, ContextRef

    fake_db["stage_context"].docs.append({
        "id": "sc-disc", "project_id": "p1", "stage": "Discovery", "version": 5,
        "outputs": {}, "toon_summary": "",
    })
    sess = await create_session(AgentSessionCreate(
        project_id="p1", stage="DataModel", agent_key="datamodel.chat",
        refs=[ContextRef(collection="kb_files", doc_id="kb-1", label="hand-picked")],
    ))
    assert len(sess.refs) == 1
    assert sess.refs[0].doc_id == "kb-1"


@pytest.mark.asyncio
async def test_auto_attach_can_be_disabled_explicitly(fake_db):
    from agent_memory import create_session
    from models import AgentSessionCreate

    fake_db["stage_context"].docs.append({
        "id": "sc-disc", "project_id": "p1", "stage": "Discovery", "version": 1,
        "outputs": {}, "toon_summary": "",
    })
    sess = await create_session(
        AgentSessionCreate(project_id="p1", stage="DataModel"),
        auto_attach_upstream=False,
    )
    assert sess.refs == []


@pytest.mark.asyncio
async def test_discovery_session_starts_with_empty_refs(fake_db):
    """No upstream stages exist for Discovery — auto-attach must produce
    an empty refs list, not a spurious self-reference."""
    from agent_memory import create_session
    from models import AgentSessionCreate

    sess = await create_session(AgentSessionCreate(
        project_id="p1", stage="Discovery", agent_key="srs.chat",
    ))
    assert sess.refs == []


# ─────────────────────────────────────────────────────────────────────
# Per-stage isolation
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_per_stage_sessions_are_isolated(fake_db):
    """A rollover on the Discovery session must not change the DataModel
    session's live_turns / summary, even though they share project_id."""
    from agent_memory import (
        AGENT_MEMORY, append_turn, create_session, get_session, maybe_rollover,
    )
    from models import AgentSessionCreate

    # Tiny budget on srs.chat so 4 turns trigger a rollover.
    import agent_memory as am
    am.AGENT_MEMORY["srs.chat"] = {"keep_last_k": 1, "window_budget_pct": 0.10, "summary_strategy": "compact"}

    discovery = await create_session(AgentSessionCreate(
        project_id="p1", stage="Discovery", agent_key="srs.chat",
    ))
    datamodel = await create_session(AgentSessionCreate(
        project_id="p1", stage="DataModel", agent_key="datamodel.chat",
    ))
    for i in range(4):
        await append_turn(discovery.id, role="user", content="x" * 2000)
    await append_turn(datamodel.id, role="user", content="DM untouched")

    async def fake_fabric(**_kwargs):
        return {"content": "ROLLED", "model": "m", "usage": {"total_tokens": 1}}

    rolled_disc, did_roll = await maybe_rollover(
        discovery.id, resolved_context_window=1024, fabric_call=fake_fabric,
    )
    assert did_roll is True
    assert rolled_disc.rollover_count == 1

    dm_after = await get_session(datamodel.id)
    assert dm_after.rollover_count == 0
    assert len(dm_after.live_turns) == 1
    assert dm_after.live_turns[0].content == "DM untouched"
    assert dm_after.summary == ""


