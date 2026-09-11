"""
iter-15.40 — Build System selection is required BEFORE the multi-agent
pipeline starts. The Code Transformer wizard now exposes:

  GET  /tools/transformer/build-tools?backend=...&frontend=...
       → suggested tools per active component (first entry = default)
  POST /tools/transformer/create/v2  now accepts a `build_tool` JSON form
       field mapping component → tool; validates against the suggestion
       list and persists to `transformation.build_tools`.

These tests hit the routes via TestClient with only the two collections
they actually touch (`transformations`, `transform_files`) faked out —
matches the pattern used across the other iter-15.* test files.
"""
import os
import sys
from io import BytesIO
from typing import Any, Dict, List
from zipfile import ZipFile

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


@pytest.fixture
def client(monkeypatch):
    from routes import tools as tools_mod

    fake_transforms = FakeCollection()
    fake_files = FakeCollection()
    fake_audit = FakeCollection()

    monkeypatch.setattr(tools_mod, "transformations", fake_transforms)
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)
    monkeypatch.setattr(tools_mod, "audit_log", fake_audit)

    app = FastAPI()
    app.include_router(tools_mod.router)
    return TestClient(app), fake_transforms


def _make_zip_bytes() -> bytes:
    buf = BytesIO()
    with ZipFile(buf, "w") as z:
        z.writestr("src/UserService.java", "public class UserService {}\n")
    return buf.getvalue()


def test_suggestions_endpoint_returns_native_defaults(client):
    tc, _ = client
    r = tc.get("/tools/transformer/build-tools", params={
        "backend": "spring-boot-3", "frontend": "react-18",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggestions"]["backend"][0] == "maven"
    assert body["suggestions"]["frontend"][0] == "npm"
    assert "gradle" in body["suggestions"]["backend"]
    assert "maven" in body["native_support"]


def test_suggestions_empty_when_nothing_picked(client):
    tc, _ = client
    r = tc.get("/tools/transformer/build-tools")
    assert r.status_code == 200
    assert r.json()["suggestions"] == {}


def test_create_v2_persists_build_tools(client):
    tc, transforms = client
    r = tc.post(
        "/tools/transformer/create/v2",
        data={
            "name": "T1",
            "transforms": '{"backend": "spring-boot-3", "frontend": "react-18"}',
            "build_tool": '{"backend": "gradle", "frontend": "yarn"}',
        },
        files={"source_files": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["build_tools"] == {"backend": "gradle", "frontend": "yarn"}
    persisted = transforms.rows[0]
    assert persisted["build_tools"] == {"backend": "gradle", "frontend": "yarn"}


def test_create_v2_defaults_when_build_tool_omitted(client):
    tc, transforms = client
    r = tc.post(
        "/tools/transformer/create/v2",
        data={
            "name": "T2",
            "transforms": '{"backend": "spring-boot-3"}',
        },
        files={"source_files": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["build_tools"] == {"backend": "maven"}


def test_create_v2_rejects_invalid_build_tool(client):
    tc, _ = client
    r = tc.post(
        "/tools/transformer/create/v2",
        data={
            "name": "T3",
            "transforms": '{"backend": "spring-boot-3"}',
            "build_tool": '{"backend": "cmake"}',
        },
        files={"source_files": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 400, r.text
    assert "cmake" in r.text


def test_create_v2_ignores_build_tool_for_undeclared_component(client):
    tc, transforms = client
    # database picked but no build_tool for it → falls back to first suggestion
    r = tc.post(
        "/tools/transformer/create/v2",
        data={
            "name": "T4",
            "transforms": '{"database": "postgresql"}',
            "build_tool": '{"backend": "maven"}',  # ignored — no backend picked
        },
        files={"source_files": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Only components that were picked get a build tool assigned.
    assert body["build_tools"] == {"database": "flyway"}
