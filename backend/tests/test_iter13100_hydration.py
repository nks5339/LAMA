"""iter-13.100 Phase 3 — Hydration & trust-boundary tests.

Locks in the contract that:
  • Refs are resolved by fetching the CURRENT doc from its collection
    on every call — summaries are HINTS, refs are TRUTH.
  • The `HYDRATABLE_COLLECTIONS` allowlist is the single security
    boundary; non-allowlisted collections surface as `forbidden` and
    are visible to the LLM as a refusal notice (NOT silently dropped).
  • Drift (content changed since `ref.hash` was captured) is announced
    inline so the LLM can react.
  • Missing docs become explicit `REF MISSING` notices.
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

# Re-use the in-memory fake collection from Phase 1.
from tests.test_iter13100_agent_memory import FakeCollection  # type: ignore


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every collection the hydrator may touch with an in-memory fake."""
    import db
    import agent_memory as am

    fakes = {
        "agent_sessions":  FakeCollection(),
        "audit_log":       FakeCollection(),
        "projects":        FakeCollection(),
        "stage_context":   FakeCollection(),
        "srs_documents":   FakeCollection(),
        "kb_files":        FakeCollection(),
        "arch_documents":  FakeCollection(),
        "data_models":     FakeCollection(),
        # Intentionally NOT faking: agent_configs, users, model_providers.
        # The hydrator's allowlist must refuse those without ever
        # touching the underlying collection.
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(db, name, fake, raising=True)

    monkeypatch.setattr(am, "sess_col", fakes["agent_sessions"], raising=True)
    monkeypatch.setattr(am, "audit_col", fakes["audit_log"], raising=True)
    monkeypatch.setattr(am, "projects_col", fakes["projects"], raising=True)
    monkeypatch.setattr(am, "_SIGNING_KEY_CACHE", None, raising=True)

    return fakes


# ─────────────────────────────────────────────────────────────────────
# hydrate_refs
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hydrate_ok_returns_projected_doc(fake_db):
    from agent_memory import hydrate_refs
    from models import ContextRef

    fake_db["stage_context"].docs.append({
        "id": "sc-1", "project_id": "p1", "stage": "DataModel", "version": 3,
        "frozen_at": "2026-06-23T10:00:00Z",
        "outputs": {"oltp_ddl": "CREATE TABLE foo (...)"},
        "toon_summary": "tables: foo, bar",
        # Field NOT in allowlist — must be excluded from inlined content.
        "internal_secret": "hunter2",
    })

    [h] = await hydrate_refs([ContextRef(
        collection="stage_context", doc_id="sc-1", label="DataModel v3",
    )])
    assert h["status"] == "ok"
    assert "CREATE TABLE foo" in h["content"]
    assert "tables: foo, bar" in h["content"]
    # Allowlist enforced — non-listed field MUST NOT appear in the prompt.
    assert "hunter2" not in h["content"]
    assert "internal_secret" not in h["content"]
    assert h["drifted"] is False
    assert h["current_hash"]


@pytest.mark.asyncio
async def test_hydrate_missing_doc_returns_missing_status(fake_db):
    from agent_memory import hydrate_refs
    from models import ContextRef

    [h] = await hydrate_refs([ContextRef(
        collection="stage_context", doc_id="does-not-exist", label="ghost",
    )])
    assert h["status"] == "missing"
    assert h["content"] == ""


@pytest.mark.asyncio
async def test_hydrate_refuses_non_allowlisted_collection(fake_db):
    """Trust boundary: a tampered session pointing at `agent_configs`
    (API keys) or `users` (password hashes) must be refused at hydration
    time — never reach the underlying collection at all."""
    from agent_memory import hydrate_refs
    from models import ContextRef

    bad_refs = [
        ContextRef(collection="agent_configs", doc_id="any", label="leak attempt"),
        ContextRef(collection="users", doc_id="any", label="pw exfil"),
        ContextRef(collection="model_providers", doc_id="any", label="api-key exfil"),
        ContextRef(collection="", doc_id="any"),
    ]
    out = await hydrate_refs(bad_refs)
    assert all(h["status"] == "forbidden" for h in out)
    assert all(h["content"] == "" for h in out)


@pytest.mark.asyncio
async def test_hydrate_detects_drift_against_attached_hash(fake_db):
    """When the on-disk content has changed since ref.hash was captured,
    the hydrator MUST flag `drifted=True` so the prefix builder can
    annotate the prompt."""
    from agent_memory import _content_for_ref, _ref_hash, hydrate_refs
    from models import ContextRef

    fake_db["arch_documents"].docs.append({
        "id": "arch-1", "type": "HLD", "version": 1,
        "frozen": True, "content": "v1 content",
    })
    # Compute the hash that WOULD have been correct for v1.
    v1_hash = _ref_hash(_content_for_ref(
        fake_db["arch_documents"].docs[0],
        ("type", "content", "version", "frozen"),
    ))

    # Now mutate the doc on disk → v2.
    fake_db["arch_documents"].docs[0]["content"] = "v2 content (mutated)"
    fake_db["arch_documents"].docs[0]["version"] = 2

    [h] = await hydrate_refs([ContextRef(
        collection="arch_documents", doc_id="arch-1",
        label="HLD frozen", hash=v1_hash,
    )])
    assert h["status"] == "ok"
    assert h["drifted"] is True
    assert "v2 content" in h["content"]
    assert h["current_hash"] != v1_hash


@pytest.mark.asyncio
async def test_hydrate_no_drift_when_ref_hash_empty(fake_db):
    """Refs attached without a hash (legacy / cheap-attach) should never
    surface as drifted — only refs with a captured baseline can drift."""
    from agent_memory import hydrate_refs
    from models import ContextRef

    fake_db["srs_documents"].docs.append({
        "id": "srs-1", "version": 1, "sections": {"intro": "hello"}, "frozen": True,
    })
    [h] = await hydrate_refs([ContextRef(
        collection="srs_documents", doc_id="srs-1", label="SRS v1",  # no hash
    )])
    assert h["status"] == "ok"
    assert h["drifted"] is False


@pytest.mark.asyncio
async def test_hydrate_truncates_oversized_content(fake_db, monkeypatch):
    """Per-ref content must be capped to keep prompt size predictable."""
    import agent_memory as am
    from agent_memory import hydrate_refs
    from models import ContextRef

    monkeypatch.setattr(am, "HYDRATED_REF_MAX_CHARS", 200, raising=True)
    huge = "x" * 5000
    fake_db["kb_files"].docs.append({
        "id": "kb-1", "filename": "huge.php", "filetype": "php",
        "kind": "legacy_code", "kind_notes": huge,
    })
    [h] = await hydrate_refs([ContextRef(
        collection="kb_files", doc_id="kb-1", label="huge",
    )])
    assert h["status"] == "ok"
    assert "truncated" in h["content"]
    assert len(h["content"]) < 600


# ─────────────────────────────────────────────────────────────────────
# build_prompt_prefix (hydrated mode = default)
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hydrated_prefix_inlines_ref_content_as_authoritative(fake_db):
    from agent_memory import build_prompt_prefix, create_session
    from models import AgentSessionCreate, ContextRef

    fake_db["stage_context"].docs.append({
        "id": "sc-1", "stage": "DataModel", "version": 2, "frozen_at": "ts",
        "outputs": {"ddl": "CREATE TABLE invoices (id UUID PRIMARY KEY)"},
        "toon_summary": "tables: invoices",
    })

    sess = await create_session(AgentSessionCreate(
        project_id="p1", agent_key="srs.chat",
        seed_summary="user said keep audit trail",
        refs=[ContextRef(collection="stage_context", doc_id="sc-1", label="DataModel v2")],
    ))
    prefix = await build_prompt_prefix(sess)   # hydrate=True by default
    # Expect: refs-system(s) first, then summary-system marked as HINT,
    # then no live turns (none yet).
    assert prefix[0]["role"] == "system"
    assert "AUTHORITATIVE REF" in prefix[0]["content"]
    assert "stage_context/sc-1" in prefix[0]["content"]
    assert "CREATE TABLE invoices" in prefix[0]["content"]
    # Summary downgraded explicitly.
    assert prefix[1]["role"] == "system"
    assert "HINT only" in prefix[1]["content"]
    assert "audit trail" in prefix[1]["content"]


@pytest.mark.asyncio
async def test_hydrated_prefix_annotates_drift_inline(fake_db):
    from agent_memory import (
        _content_for_ref, _ref_hash, build_prompt_prefix, create_session,
    )
    from models import AgentSessionCreate, ContextRef

    fake_db["arch_documents"].docs.append({
        "id": "arch-1", "type": "HLD", "version": 1, "frozen": True,
        "content": "v1 design",
    })
    old_hash = _ref_hash(_content_for_ref(
        fake_db["arch_documents"].docs[0],
        ("type", "content", "version", "frozen"),
    ))
    # Drift it.
    fake_db["arch_documents"].docs[0]["content"] = "v2 design"

    sess = await create_session(AgentSessionCreate(
        project_id="p1",
        refs=[ContextRef(
            collection="arch_documents", doc_id="arch-1",
            label="HLD", hash=old_hash,
        )],
    ))
    prefix = await build_prompt_prefix(sess)
    assert "[DRIFTED since attached]" in prefix[0]["content"]
    assert "v2 design" in prefix[0]["content"]


@pytest.mark.asyncio
async def test_hydrated_prefix_surfaces_missing_ref_as_notice(fake_db):
    from agent_memory import build_prompt_prefix, create_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(AgentSessionCreate(
        project_id="p1",
        refs=[ContextRef(
            collection="srs_documents", doc_id="ghost", label="deleted SRS",
        )],
    ))
    prefix = await build_prompt_prefix(sess)
    assert prefix[0]["role"] == "system"
    assert "REF MISSING" in prefix[0]["content"]
    assert "do not invent" in prefix[0]["content"].lower()


@pytest.mark.asyncio
async def test_hydrated_prefix_surfaces_forbidden_ref_as_notice(fake_db):
    from agent_memory import build_prompt_prefix, create_session
    from models import AgentSessionCreate, ContextRef

    sess = await create_session(AgentSessionCreate(
        project_id="p1",
        refs=[ContextRef(collection="agent_configs", doc_id="leak", label="api key")],
    ))
    prefix = await build_prompt_prefix(sess)
    assert prefix[0]["role"] == "system"
    assert "REF FORBIDDEN" in prefix[0]["content"]
    # Allowlisted-only contract: the LLM gets a visible refusal, NOT
    # the doc content.
    assert "leak attempt" not in prefix[0]["content"]
    assert "api_key" not in prefix[0]["content"]


@pytest.mark.asyncio
async def test_hydrated_prefix_ordering_refs_before_summary_before_turns(fake_db):
    """The whole point of Phase 3: refs are authoritative (first), summary
    is a hint (second), live turns last."""
    from agent_memory import (
        append_turn, build_prompt_prefix, create_session,
    )
    from models import AgentSessionCreate, ContextRef

    fake_db["srs_documents"].docs.append({
        "id": "srs-1", "version": 1, "sections": {"intro": "hi"}, "frozen": True,
    })
    sess = await create_session(AgentSessionCreate(
        project_id="p1",
        seed_summary="prior discussion",
        refs=[ContextRef(collection="srs_documents", doc_id="srs-1", label="SRS")],
    ))
    await append_turn(sess.id, role="user", content="next")

    from agent_memory import get_session
    sess = await get_session(sess.id)
    prefix = await build_prompt_prefix(sess)
    roles_and_tags = [
        (m["role"], m["content"][:32]) for m in prefix
    ]
    # Index 0 = refs (system), 1 = summary (system, HINT), 2 = user turn
    assert prefix[0]["role"] == "system" and "AUTHORITATIVE REF" in prefix[0]["content"]
    assert prefix[1]["role"] == "system" and "HINT only" in prefix[1]["content"]
    assert prefix[2] == {"role": "user", "content": "next"}, roles_and_tags


