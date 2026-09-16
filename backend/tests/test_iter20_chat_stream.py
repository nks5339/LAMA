"""
iter-20 — Streaming chat over SSE.

Endpoint under test:
  POST /chat/stream
       → text/event-stream emitting phase / citation / token / complete.

What these pin:
  • the SSE wire contract the frontend's `streamMessage` parses
  • that streaming and buffered chat share ONE context pipeline
    (`_prepare_turn`), so the two transports cannot drift
  • that a non-streaming provider still works — `fabric_call_stream`
    degrades to a single buffered chunk and the route must handle that
  • that the streamed reply is persisted exactly once, and that session
    mode appends both turns (the buffered path gets this from
    `fabric_call_with_session`; the streaming path must do it itself)
  • that SRS auto-trigger failures are reported rather than swallowed
"""
import json
import os
import sys
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def sort(self, *_a, **_kw): return self
    async def to_list(self, _n=None): return list(self._rows)


class FakeCollection:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        class R: inserted_id = doc.get("_id")
        return R()

    async def find_one(self, q=None, *_a, **_kw):
        q = q or {}
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None

    def find(self, q=None, *_a, **_kw):
        q = q or {}
        return _FakeCursor([r for r in self.rows if all(r.get(k) == v for k, v in q.items())])

    async def update_one(self, q, update, upsert=False):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                r.update((update or {}).get("$set", {}))
                return
        if upsert:
            doc = dict(q or {})
            doc.update((update or {}).get("$set", {}))
            self.rows.append(doc)

    async def count_documents(self, q=None):
        q = q or {}
        return len([r for r in self.rows if all(r.get(k) == v for k, v in q.items())])


PROJECT_ID = "proj-stream-1"


def _parse_sse(body: str) -> List[Dict[str, Any]]:
    """Decode an SSE body into the list of JSON events it carried."""
    out = []
    for frame in body.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


@pytest.fixture
def client(monkeypatch):
    import routes.chat as chat

    projects = FakeCollection()
    projects.rows.append({"id": PROJECT_ID, "name": "PMIS Pilot"})
    msgs = FakeCollection()
    convos = FakeCollection()
    toon = FakeCollection()
    toon.rows.append({
        "project_id": PROJECT_ID, "toon": "TOON-SKELETON", "summary": "12 files",
    })

    monkeypatch.setattr(chat, "projects", projects)
    monkeypatch.setattr(chat, "messages", msgs)
    monkeypatch.setattr(chat, "conversations", convos)
    monkeypatch.setattr(chat, "kb_toon", toon)
    monkeypatch.setattr(chat, "prompts", FakeCollection())
    monkeypatch.setattr(chat, "project_prompts", FakeCollection())
    monkeypatch.setattr(chat, "srs_documents", FakeCollection())

    async def fake_sources(project_id, query, top_k=8):
        return [
            {"content": "class Invoice {}", "filename": "Invoice.php",
             "filetype": "php", "chunk_id": "c1", "score": 0.91},
            {"content": "CREATE TABLE invoice", "filename": "schema.sql",
             "filetype": "sql", "chunk_id": "c2", "score": 0.84},
        ]

    # Both transports must see the SAME retrieval set — they hit one Qdrant
    # with one query; only the metadata differs. Stub them consistently so
    # the parity test measures code drift, not fixture drift.
    async def fake_search(project_id, query, top_k=8):
        return [s["content"] for s in await fake_sources(project_id, query, top_k)]

    monkeypatch.setattr(chat, "search_with_sources", fake_sources)
    monkeypatch.setattr(chat, "qdrant_search", fake_search)
    monkeypatch.setattr(chat, "prune_toon", lambda t, n, s: (t or "")[:n])

    app = FastAPI()
    app.include_router(chat.router)
    return TestClient(app), chat, msgs


def _post(c, **over):
    payload = {"project_id": PROJECT_ID, "message": "how does billing work?"}
    payload.update(over)
    return c.post("/chat/stream", json=payload)


# ── wire contract ────────────────────────────────────────────────────

def test_stream_emits_phase_citations_tokens_then_complete(client, monkeypatch):
    c, chat, msgs = client

    async def fake_stream(*_a, **_kw):
        for piece in ("Billing ", "is handled ", "by Invoice.php."):
            yield piece

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)

    r = _post(c)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    kinds = [e["type"] for e in events]

    assert kinds[0] == "phase" and events[0]["phase"] == "retrieving"
    assert "citation" in kinds
    assert "generating" in [e.get("phase") for e in events if e["type"] == "phase"]
    assert kinds[-1] == "complete"

    # Deltas concatenate to the whole reply.
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Billing is handled by Invoice.php."


def test_citations_carry_filename_and_are_deduped(client, monkeypatch):
    c, chat, _ = client

    async def fake_sources(project_id, query, top_k=8):
        return [
            {"content": "a", "filename": "Invoice.php", "filetype": "php", "score": 0.9},
            {"content": "b", "filename": "Invoice.php", "filetype": "php", "score": 0.8},
            {"content": "c", "filename": "", "filetype": "", "score": 0.7},
        ]

    async def fake_stream(*_a, **_kw):
        yield "ok"

    monkeypatch.setattr(chat, "search_with_sources", fake_sources)
    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)

    cites = [e for e in _parse_sse(_post(c).text) if e["type"] == "citation"]
    # Duplicate filename collapsed; the nameless chunk is not a citation.
    assert [x["filename"] for x in cites] == ["Invoice.php"]
    assert cites[0]["filetype"] == "php"


def test_non_streaming_provider_yields_one_chunk(client, monkeypatch):
    """fabric_call_stream degrades to a single buffered yield on providers
    that cannot do SSE. The route must still produce a valid stream."""
    c, chat, msgs = client

    async def buffered(*_a, **_kw):
        yield "the entire answer arrives at once"

    monkeypatch.setattr(chat, "fabric_call_stream", buffered)

    events = _parse_sse(_post(c).text)
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 1
    assert events[-1]["type"] == "complete"
    assert len([m for m in msgs.rows if m["role"] == "assistant"]) == 1


# ── persistence ──────────────────────────────────────────────────────

def test_user_and_assistant_messages_persisted_once(client, monkeypatch):
    c, chat, msgs = client

    async def fake_stream(*_a, **_kw):
        yield "reply"

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)
    _post(c)

    users = [m for m in msgs.rows if m["role"] == "user"]
    bots = [m for m in msgs.rows if m["role"] == "assistant"]
    assert len(users) == 1 and users[0]["content"] == "how does billing work?"
    assert len(bots) == 1 and bots[0]["content"] == "reply"
    assert users[0]["conversation_id"] == bots[0]["conversation_id"]


def test_empty_model_response_emits_error_and_persists_nothing(client, monkeypatch):
    c, chat, msgs = client

    async def empty(*_a, **_kw):
        if False:
            yield ""
        return

    monkeypatch.setattr(chat, "fabric_call_stream", empty)

    events = _parse_sse(_post(c).text)
    assert events[-1]["type"] == "error"
    assert not [m for m in msgs.rows if m["role"] == "assistant"]


def test_session_mode_appends_both_turns(client, monkeypatch):
    """The buffered path gets this from fabric_call_with_session. The
    streaming path bypasses that wrapper, so it must append explicitly —
    otherwise every streamed exchange vanishes from session memory."""
    c, chat, _ = client
    appended = []

    async def fake_stream(*_a, **_kw):
        yield "streamed answer"

    async def fake_append(session_id, *, role, content):
        appended.append((session_id, role, content))

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)
    import agent_memory
    monkeypatch.setattr(agent_memory, "append_turn", fake_append)

    _post(c, session_id="sess-9")

    assert [(r, ct) for _sid, r, ct in appended] == [
        ("user", "how does billing work?"),
        ("assistant", "streamed answer"),
    ]
    assert all(sid == "sess-9" for sid, _r, _c in appended)


# ── SRS auto-trigger reporting (was silently swallowed) ──────────────

def test_srs_trigger_failure_is_reported_not_swallowed(client, monkeypatch):
    c, chat, _ = client

    async def fake_stream(*_a, **_kw):
        yield "ok"

    async def boom(_payload):
        raise RuntimeError("Discovery is not frozen")

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)
    import routes.srs as srs
    monkeypatch.setattr(srs, "generate_srs", boom)

    # "generate srs" trips detect_intent → srs.generate in Discovery.
    events = _parse_sse(_post(c, message="generate srs now").text)
    done = events[-1]
    assert done["type"] == "complete"
    assert done["srs_triggered"] is False
    assert "Discovery is not frozen" in done["srs_error"]


def test_intent_detection_matches_buffered_path(client, monkeypatch):
    c, chat, _ = client

    async def fake_stream(*_a, **_kw):
        yield "ok"

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)

    gap = _parse_sse(_post(c, message="what tables exist?").text)[-1]
    assert gap["intent"] == "srs.gap_question"
    assert chat.detect_intent("please generate srs") == "srs.generate"


def test_unknown_project_is_404_before_any_streaming(client, monkeypatch):
    c, chat, msgs = client

    async def fake_stream(*_a, **_kw):
        yield "should not run"

    monkeypatch.setattr(chat, "fabric_call_stream", fake_stream)

    r = c.post("/chat/stream", json={"project_id": "nope", "message": "hi"})
    assert r.status_code == 404
    assert not msgs.rows


# ── shared pipeline ──────────────────────────────────────────────────

def test_both_transports_build_the_same_llm_messages(client, monkeypatch):
    """_prepare_turn is the single source of context for both routes.
    If someone reimplements assembly in one path, this fails."""
    import asyncio
    from models import ChatRequest
    _c, chat, _ = client

    req = ChatRequest(project_id=PROJECT_ID, message="how does billing work?")

    async def both():
        a = await chat._prepare_turn(req, want_sources=False)
        b = await chat._prepare_turn(req, want_sources=True)
        return a, b

    buffered, streamed = asyncio.run(both())

    assert buffered["llm_messages"] == streamed["llm_messages"]
    assert buffered["intent"] == streamed["intent"]
    # Only the streaming path pays for citation metadata.
    assert buffered["sources"] == [] and len(streamed["sources"]) == 2
