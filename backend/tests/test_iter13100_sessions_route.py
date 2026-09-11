"""iter-13.100 Phase 4 — Tests for `routes/sessions.py`.

Uses FastAPI's TestClient to exercise the REST surface end-to-end with
in-memory fake collections (no live Mongo required).
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
def client(monkeypatch):
    """Mount only the sessions router (avoid pulling in the rest of
    server.py which would trigger seed + every other dependency)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import db
    import agent_memory as am

    fakes = {
        "agent_sessions": FakeCollection(),
        "audit_log":      FakeCollection(),
        "projects":       FakeCollection(),
    }
    # Seed one project so the route's existence check passes.
    fakes["projects"].docs.append({
        "id": "p1", "name": "Pilot", "tenant_id": "tenant_alpha",
    })
    for name, fake in fakes.items():
        monkeypatch.setattr(db, name, fake, raising=True)

    monkeypatch.setattr(am, "sess_col", fakes["agent_sessions"], raising=True)
    monkeypatch.setattr(am, "audit_col", fakes["audit_log"], raising=True)
    monkeypatch.setattr(am, "projects_col", fakes["projects"], raising=True)
    monkeypatch.setattr(am, "_SIGNING_KEY_CACHE", None, raising=True)

    # Also patch the routes.sessions module's reference (it does
    # `from db import projects as projects_col`).
    import routes.sessions as sess_routes
    monkeypatch.setattr(sess_routes, "projects_col", fakes["projects"], raising=True)

    app = FastAPI()
    app.include_router(sess_routes.router, prefix="/api")
    return TestClient(app), fakes


def test_create_session_happy_path(client):
    c, fakes = client
    r = c.post("/api/sessions", json={
        "project_id": "p1", "stage": "Discovery", "agent_key": "srs.chat",
        "seed_summary": "kick-off context",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == "p1"
    assert body["stage"] == "Discovery"
    assert body["agent_key"] == "srs.chat"
    assert body["tenant_id"] == "tenant_alpha"      # picked up from project
    assert body["summary"] == "kick-off context"
    assert body["summary_sig"]                       # signed
    assert body["status"] == "active"
    # Persisted
    assert len(fakes["agent_sessions"].docs) == 1


def test_create_session_rejects_unknown_project(client):
    c, _ = client
    r = c.post("/api/sessions", json={"project_id": "does-not-exist"})
    assert r.status_code == 404


def test_create_session_rejects_blank_project_id(client):
    c, _ = client
    r = c.post("/api/sessions", json={"project_id": "   "})
    assert r.status_code == 400


def test_get_session_roundtrip(client):
    c, _ = client
    created = c.post("/api/sessions", json={"project_id": "p1"}).json()
    sid = created["id"]
    r = c.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


def test_get_session_404_for_unknown(client):
    c, _ = client
    r = c.get("/api/sessions/nonexistent")
    assert r.status_code == 404


def test_list_sessions_filters_by_stage_and_agent(client):
    c, _ = client
    c.post("/api/sessions", json={"project_id": "p1", "stage": "Discovery", "agent_key": "srs.chat"})
    c.post("/api/sessions", json={"project_id": "p1", "stage": "DataModel", "agent_key": "datamodel.chat"})
    c.post("/api/sessions", json={"project_id": "p1", "stage": "Discovery", "agent_key": "srs.gap_question"})

    r = c.get("/api/sessions", params={"project_id": "p1"})
    assert r.json()["count"] == 3

    r = c.get("/api/sessions", params={"project_id": "p1", "stage": "Discovery"})
    assert r.json()["count"] == 2

    r = c.get("/api/sessions", params={
        "project_id": "p1", "stage": "Discovery", "agent_key": "srs.chat",
    })
    assert r.json()["count"] == 1


def test_list_sessions_requires_project_id(client):
    c, _ = client
    r = c.get("/api/sessions")
    assert r.status_code == 422   # FastAPI missing-query


def test_archive_session_marks_inactive(client):
    c, _ = client
    created = c.post("/api/sessions", json={"project_id": "p1"}).json()
    sid = created["id"]
    r = c.post(f"/api/sessions/{sid}/archive", json={"reason": "stage frozen"})
    assert r.status_code == 200
    assert r.json()["status"] == "archived"
    # Default list excludes archived.
    r2 = c.get("/api/sessions", params={"project_id": "p1"})
    assert r2.json()["count"] == 0
    # include_archived=True surfaces it.
    r3 = c.get("/api/sessions", params={"project_id": "p1", "include_archived": "true"})
    assert r3.json()["count"] == 1


def test_attach_ref_dedups_and_resigns(client):
    c, _ = client
    created = c.post("/api/sessions", json={
        "project_id": "p1", "seed_summary": "seed",
    }).json()
    sid = created["id"]
    original_sig = created["summary_sig"]

    r = c.post(f"/api/sessions/{sid}/refs", json={
        "collection": "stage_context", "doc_id": "sc-1", "label": "v1", "hash": "h1",
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["refs"]) == 1
    assert body["summary_sig"] != original_sig    # re-signed

    # Re-attach same key with new hash → replace.
    r = c.post(f"/api/sessions/{sid}/refs", json={
        "collection": "stage_context", "doc_id": "sc-1", "label": "v2", "hash": "h2",
    })
    body = r.json()
    assert len(body["refs"]) == 1
    assert body["refs"][0]["hash"] == "h2"


def test_attach_ref_404_for_unknown_session(client):
    c, _ = client
    r = c.post("/api/sessions/nonexistent/refs", json={
        "collection": "stage_context", "doc_id": "sc-1",
    })
    assert r.status_code == 404


def test_archive_404_for_unknown_session(client):
    c, _ = client
    r = c.post("/api/sessions/nonexistent/archive")
    assert r.status_code == 404

