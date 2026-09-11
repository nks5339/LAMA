"""
iter-15.41 — Mandatory traceability confirmation gate between Context
Manager and Planner.

Endpoints under test:
  GET  /tools/transformer/{tid}/traceability
       → mode-aware traceability data (BE hop chains + FE screen→APIs)
  POST /tools/transformer/{tid}/confirm-traceability
       → approves envelopes, stamps `traceability_confirmed_at`, schedules
         the Planner background task, returns confirmation timestamp.
"""
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

    async def update_many(self, q, update):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                r.update((update or {}).get("$set", {}))

    async def count_documents(self, q=None):
        q = q or {}
        return sum(1 for r in self.rows if all(r.get(k) == v for k, v in q.items()))


@pytest.fixture
def client(monkeypatch):
    from routes import tools as tools_mod

    fake_transforms = FakeCollection()
    fake_envelopes = FakeCollection()
    fake_files = FakeCollection()
    fake_audit = FakeCollection()

    monkeypatch.setattr(tools_mod, "transformations", fake_transforms)
    monkeypatch.setattr(tools_mod, "transformer_envelopes", fake_envelopes)
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)
    monkeypatch.setattr(tools_mod, "audit_log", fake_audit)

    # No-op the background task so tests don't try to run the real
    # Planner. FastAPI's BackgroundTasks accepts any awaitable/callable —
    # replace `_continue_multi_agent_after_confirm` with a coroutine
    # that just records it was invoked.
    invoked = {"count": 0, "last_args": None}

    async def _fake_continue(tid, model=None):
        invoked["count"] += 1
        invoked["last_args"] = (tid, model)

    monkeypatch.setattr(tools_mod, "_continue_multi_agent_after_confirm", _fake_continue)

    app = FastAPI()
    app.include_router(tools_mod.router)
    return TestClient(app), fake_transforms, fake_envelopes, fake_files, invoked


def _seed_backend_transform(transforms, envelopes, tid="t-be"):
    transforms.rows.append({
        "_id": tid,
        "name": "BE Only",
        "status": "awaiting_confirmation",
        "transforms": {"backend": "spring-boot-3"},
    })
    envelopes.rows.append({
        "transform_id": tid,
        "envelope_id": "e1",
        "endpoint_method": "GET",
        "endpoint_path": "/api/users",
        "controller_class": "UserController",
        "service_class": "UserService",
        "repository_class": "UserRepo",
        "db_tables": ["users"],
        "business_logic_summary": "List users",
        "request": {"dto_class": "ListUsersReq"},
        "response": {"result_type": "List<UserDto>"},
        "data_layer": {"table_trace": [{"from": "UserService", "to": "UserRepo", "via": "findAll"}]},
    })


def test_traceability_backend_mode(client):
    tc, transforms, envelopes, _files, _inv = client
    _seed_backend_transform(transforms, envelopes)

    r = tc.get("/tools/transformer/t-be/traceability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "backend"
    assert body["backend_total"] == 1
    assert body["frontend_total"] == 0
    row = body["backend_rows"][0]
    assert row["endpoint_path"] == "/api/users"
    assert row["db_tables"] == ["users"]
    assert body["confirmed_at"] is None


def test_traceability_frontend_mode_extracts_screens(client):
    tc, transforms, envelopes, files, _inv = client
    transforms.rows.append({
        "_id": "t-fe",
        "status": "awaiting_confirmation",
        "transforms": {"frontend": "react-18"},
    })
    # No BE envelopes for pure-FE run — that's fine.
    files.rows.append({
        "transform_id": "t-fe",
        "type": "source",
        "path": "src/components/UserList.jsx",
        "content": """
            export default function UserList(){
              return (<div>
                <button id="refresh-btn">Refresh</button>
                <input placeholder="Search users" />
                <table id="user-table"><tbody></tbody></table>
              </div>);
            }
            const load = () => fetch("/api/users");
            axios.get("/api/roles");
        """,
    })

    r = tc.get("/tools/transformer/t-fe/traceability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "frontend"
    assert body["backend_total"] == 0
    assert body["frontend_total"] == 1
    screen = body["frontend_rows"][0]
    assert screen["screen"] == "UserList.jsx"
    urls = sorted([c["url"] for c in screen["api_calls"]])
    assert urls == ["/api/roles", "/api/users"]
    kinds = {e["kind"] for e in screen["elements"]}
    assert "button" in kinds
    assert "input" in kinds
    assert "table" in kinds


def test_traceability_fullstack_mode(client):
    tc, transforms, envelopes, files, _inv = client
    transforms.rows.append({
        "_id": "t-full",
        "status": "awaiting_confirmation",
        "transforms": {"backend": "spring-boot-3", "frontend": "react-18"},
    })
    envelopes.rows.append({
        "transform_id": "t-full", "envelope_id": "e1",
        "endpoint_path": "/api/orders", "controller_class": "OrderCtrl",
        "db_tables": ["orders"], "data_layer": {}, "request": {}, "response": {},
    })
    files.rows.append({
        "transform_id": "t-full", "type": "source",
        "path": "app/screens/orders.jsx",
        "content": 'fetch("/api/orders")',
    })
    r = tc.get("/tools/transformer/t-full/traceability")
    body = r.json()
    assert body["mode"] == "fullstack"
    assert body["backend_total"] == 1
    assert body["frontend_total"] == 1


def test_confirm_traceability_happy_path(client):
    tc, transforms, envelopes, _files, invoked = client
    _seed_backend_transform(transforms, envelopes)

    r = tc.post("/tools/transformer/t-be/confirm-traceability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["traceability_confirmed_at"]
    # Transformation doc updated
    tdoc = transforms.rows[0]
    assert tdoc["status"] == "running"
    assert tdoc["phase"] == "planner"
    assert tdoc.get("traceability_confirmed_at")
    # Envelope approved
    assert envelopes.rows[0].get("status") == "approved"
    # Background continuation scheduled once
    assert invoked["count"] == 1


def test_confirm_traceability_rejects_wrong_status(client):
    tc, transforms, envelopes, _files, _inv = client
    _seed_backend_transform(transforms, envelopes)
    transforms.rows[0]["status"] = "running"

    r = tc.post("/tools/transformer/t-be/confirm-traceability")
    assert r.status_code == 400
    assert "awaiting_confirmation" in r.text or "traceability" in r.text.lower()


def test_confirm_traceability_rejects_unknown_transform(client):
    tc, *_ = client
    r = tc.post("/tools/transformer/does-not-exist/confirm-traceability")
    assert r.status_code == 404


def test_traceability_confirmed_at_surfaces_on_get(client):
    tc, transforms, envelopes, _files, _inv = client
    _seed_backend_transform(transforms, envelopes)
    tc.post("/tools/transformer/t-be/confirm-traceability")
    # Fake the status back to awaiting_confirmation so we can re-GET
    # (real pipeline moves it forward; here we just want to prove the
    # timestamp was persisted and reads back).
    transforms.rows[0]["status"] = "awaiting_confirmation"
    r = tc.get("/tools/transformer/t-be/traceability")
    assert r.json()["confirmed_at"] is not None
