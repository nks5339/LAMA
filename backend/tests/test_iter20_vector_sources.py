"""
iter-20 — `kb.vector_store.search_with_sources`.

Added so the streaming chat route can show the user which files an answer
was grounded in. `filename` was always in the Qdrant payload; the existing
`search()` threw it away and returned bare content strings.

These pin the two things that matter:
  • it degrades exactly like `search()` — [] on every failure path, so a
    caller falls back to TOON-only rather than raising
  • it preserves the payload metadata a citation needs
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

import kb.vector_store as vs  # noqa: E402


class _Point:
    def __init__(self, payload, score=0.9):
        self.payload = payload
        self.score = score


class _FakeClient:
    """Mimics qdrant-client >= 1.10, which exposes query_points."""

    def __init__(self, points):
        self._points = points
        self.calls = []

    def query_points(self, **kw):
        self.calls.append(kw)

        class R:
            points = self._points

        return R()


PAYLOADS = [
    {"content": "class Invoice {}", "filename": "Invoice.php", "filetype": "php",
     "chunk_id": "c1", "project_id": "p1"},
    {"content": "CREATE TABLE invoice", "filename": "schema.sql", "filetype": "sql",
     "chunk_id": "c2", "project_id": "p1"},
]


@pytest.fixture
def wired(monkeypatch):
    client = _FakeClient([_Point(PAYLOADS[0], 0.91), _Point(PAYLOADS[1], 0.84)])
    monkeypatch.setattr(vs, "_enabled", lambda: True)
    monkeypatch.setattr(vs, "get_client", lambda: client)
    monkeypatch.setattr(vs, "get_embedder", lambda: object())
    monkeypatch.setattr(vs, "_embed_batch", lambda texts: [[0.1, 0.2, 0.3]])
    return client


@pytest.mark.asyncio
async def test_returns_content_with_citation_metadata(wired):
    rows = await vs.search_with_sources("p1", "how does billing work?", top_k=5)
    assert [r["filename"] for r in rows] == ["Invoice.php", "schema.sql"]
    assert rows[0]["filetype"] == "php"
    assert rows[0]["content"] == "class Invoice {}"
    assert rows[0]["score"] == pytest.approx(0.91)
    assert rows[0]["chunk_id"] == "c1"


@pytest.mark.asyncio
async def test_content_matches_what_plain_search_would_return(wired, monkeypatch):
    """The buffered and streaming chat paths must retrieve the SAME chunks —
    only the metadata differs. If these diverge, the two transports stop
    being interchangeable."""
    rows = await vs.search_with_sources("p1", "billing")
    monkeypatch.setattr(vs, "get_client", lambda: _FakeClient(
        [_Point(PAYLOADS[0], 0.91), _Point(PAYLOADS[1], 0.84)]
    ))
    plain = await vs.search("p1", "billing")
    assert [r["content"] for r in rows] == plain


@pytest.mark.asyncio
async def test_scopes_the_query_to_the_project(wired):
    await vs.search_with_sources("p1", "billing", top_k=3)
    kw = wired.calls[0]
    assert kw["limit"] == 3
    assert kw["with_payload"] is True
    assert kw["query_filter"] is not None


@pytest.mark.asyncio
async def test_tolerates_a_payload_missing_filename(monkeypatch):
    monkeypatch.setattr(vs, "_enabled", lambda: True)
    monkeypatch.setattr(vs, "get_client", lambda: _FakeClient([
        _Point({"content": "orphan chunk"}),
        _Point(None),
    ]))
    monkeypatch.setattr(vs, "get_embedder", lambda: object())
    monkeypatch.setattr(vs, "_embed_batch", lambda texts: [[0.1]])

    rows = await vs.search_with_sources("p1", "q")
    # The nameless chunk still contributes content; the empty payload is
    # dropped. The route filters nameless rows out of the citation list.
    assert len(rows) == 1
    assert rows[0]["filename"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disable",
    [
        lambda mp: mp.setattr(vs, "_enabled", lambda: False),
        lambda mp: mp.setattr(vs, "get_client", lambda: None),
        lambda mp: mp.setattr(vs, "get_embedder", lambda: None),
    ],
    ids=["vector-store-off", "no-client", "no-embedder"],
)
async def test_degrades_to_empty_like_plain_search(monkeypatch, disable):
    """Qdrant is inert unless QDRANT_URL or QDRANT_PATH is set. Every
    disabled path must return [] so callers fall back to TOON-only."""
    monkeypatch.setattr(vs, "_enabled", lambda: True)
    monkeypatch.setattr(vs, "get_client", lambda: _FakeClient([]))
    monkeypatch.setattr(vs, "get_embedder", lambda: object())
    disable(monkeypatch)
    assert await vs.search_with_sources("p1", "q") == []


@pytest.mark.asyncio
async def test_empty_query_returns_empty(monkeypatch):
    monkeypatch.setattr(vs, "_enabled", lambda: True)
    assert await vs.search_with_sources("p1", "") == []


@pytest.mark.asyncio
async def test_swallows_a_backend_error_rather_than_raising(monkeypatch):
    class Boom:
        def query_points(self, **kw):
            raise RuntimeError("qdrant is down")

    monkeypatch.setattr(vs, "_enabled", lambda: True)
    monkeypatch.setattr(vs, "get_client", lambda: Boom())
    monkeypatch.setattr(vs, "get_embedder", lambda: object())
    monkeypatch.setattr(vs, "_embed_batch", lambda texts: [[0.1]])

    # A retrieval outage must degrade the answer, never fail the request.
    assert await vs.search_with_sources("p1", "q") == []
