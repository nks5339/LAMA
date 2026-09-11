"""
iter-15.45 — Three-tier test generator, coverage runner, and
scope-aware ZIP download.

Coverage:

* `_target_framework_for_tests` picks framework hints per (target stack,
  tier) — the LLM prompt seed.
* `_test_path_for_envelope` derives repo-layout test paths that line up
  with the manifest layout the compiler expects.
* `_run_test_generator` fans out per-envelope-per-tier LLM calls under
  a semaphore, persists into `transform_files` with type="test", and
  purges old runs before writing.
* `_parse_coverage_report` handles JaCoCo CSV, npm coverage-summary.json
  and pytest-cov coverage.json shapes.
* The `/transformer/{id}/download` endpoint now respects `scope=code
  |tests|all` and 404s cleanly when a scope has no files.

`fabric_call` and the audit-log helpers are monkeypatched so no
network calls happen and Mongo isn't touched.
"""
import asyncio
import io
import json
import os
import sys
import zipfile
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

from routes import tools as tools_mod  # noqa: E402


# ─────────────── FakeCollection & fixtures ───────────────
class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def sort(self, *_a, **_kw): return self
    async def to_list(self, _n=None): return list(self._rows)


class FakeCollection:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("_id")})()

    async def find_one(self, q, projection=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None

    def find(self, q=None, projection=None):
        q = q or {}
        matched = []
        for r in self.rows:
            ok = True
            for k, v in q.items():
                if isinstance(v, dict) and "$in" in v:
                    if r.get(k) not in v["$in"]:
                        ok = False; break
                elif r.get(k) != v:
                    ok = False; break
            if ok:
                matched.append(dict(r))
        return _FakeCursor(matched)

    async def delete_many(self, q):
        before = len(self.rows)
        self.rows = [r for r in self.rows if not all(
            (r.get(k) in v["$in"] if isinstance(v, dict) and "$in" in v else r.get(k) == v)
            for k, v in q.items()
        )]
        return type("R", (), {"deleted_count": before - len(self.rows)})()

    async def update_one(self, q, u, **kw): return None
    async def count_documents(self, q): return len(list(self.find(q).to_list_sync() if hasattr(self.find(q), "to_list_sync") else []))


async def _noop(*a, **kw): return None
async def _noop_str(*a, **kw): return "run-id"


@pytest.fixture(autouse=True)
def _stub_common(monkeypatch):
    monkeypatch.setattr(tools_mod, "_log_agent_run", _noop_str)
    monkeypatch.setattr(tools_mod, "_update_agent_run", _noop)
    monkeypatch.setattr(tools_mod, "_emit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_get_effective_model", _noop_str)


# ─────────────── framework + path helpers ───────────────
def test_framework_hint_switches_by_stack():
    spring_api = tools_mod._target_framework_for_tests({"backend": "spring-boot-3"}, "api")
    assert "MockMvc" in spring_api or "WebTestClient" in spring_api

    fastapi_api = tools_mod._target_framework_for_tests({"backend": "fastapi"}, "api")
    assert "pytest" in fastapi_api and "httpx" in fastapi_api

    business = tools_mod._target_framework_for_tests({"backend": "spring-boot-3"}, "business")
    assert "Gherkin" in business


def test_test_path_uses_target_layout():
    env = {"endpoint": "/api/v1/users/{id}"}
    p_spring = tools_mod._test_path_for_envelope("api", {"backend": "spring-boot-3"}, env, 0)
    assert p_spring.startswith("src/test/java/tests/api/") and p_spring.endswith("Test.java")

    p_py = tools_mod._test_path_for_envelope("integration", {"backend": "fastapi"}, env, 0)
    assert p_py.startswith("tests/integration/test_") and p_py.endswith(".py")

    p_bdd = tools_mod._test_path_for_envelope("business", {"backend": "spring-boot-3"}, env, 0)
    assert p_bdd.endswith(".feature")


# ─────────────── _run_test_generator ───────────────
def test_run_test_generator_persists_one_file_per_envelope_per_tier(monkeypatch):
    fake_files = FakeCollection()
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)

    calls = []
    async def _fake_fabric(**kw):
        calls.append(kw)
        return {"content": "// generated test file\n"}
    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric)

    envelopes = [
        {"envelope_id": "e1", "endpoint": "/api/users", "http_method": "GET"},
        {"envelope_id": "e2", "endpoint": "/api/orders", "http_method": "POST"},
    ]
    result = asyncio.run(tools_mod._run_test_generator(
        "tx-a", envelopes, {"backend": "fastapi"}, model="stub",
    ))

    # 2 envelopes × 3 tiers = 6 files
    assert result["total"] == 6
    assert result["by_tier"] == {"business": 2, "api": 2, "integration": 2}
    assert len(fake_files.rows) == 6
    tiers_seen = sorted({r["test_tier"] for r in fake_files.rows})
    assert tiers_seen == ["api", "business", "integration"]


def test_run_test_generator_skips_empty_llm_output(monkeypatch):
    fake_files = FakeCollection()
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)

    async def _blank(**kw): return {"content": "   \n  "}
    monkeypatch.setattr(tools_mod, "fabric_call", _blank)

    result = asyncio.run(tools_mod._run_test_generator(
        "tx-b", [{"envelope_id": "e1", "endpoint": "/x"}],
        {"backend": "fastapi"}, model="stub",
    ))
    assert result["total"] == 0
    assert fake_files.rows == []


def test_run_test_generator_strips_markdown_fences(monkeypatch):
    fake_files = FakeCollection()
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)

    async def _fenced(**kw):
        return {"content": "```python\ndef test_x(): assert True\n```"}
    monkeypatch.setattr(tools_mod, "fabric_call", _fenced)

    asyncio.run(tools_mod._run_test_generator(
        "tx-c", [{"envelope_id": "e1", "endpoint": "/x"}],
        {"backend": "fastapi"}, model="stub", tiers=["api"],
    ))
    assert fake_files.rows[0]["content"].startswith("def test_x")
    assert "```" not in fake_files.rows[0]["content"]


def test_run_test_generator_purges_previous_run(monkeypatch):
    fake_files = FakeCollection()
    fake_files.rows.append({
        "transform_id": "tx-d", "type": "test", "path": "stale.py",
        "content": "old",
    })
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)

    async def _ok(**kw): return {"content": "new"}
    monkeypatch.setattr(tools_mod, "fabric_call", _ok)

    asyncio.run(tools_mod._run_test_generator(
        "tx-d", [{"envelope_id": "e1", "endpoint": "/x"}],
        {"backend": "fastapi"}, model="stub", tiers=["business"],
    ))
    # Stale row purged; only the new one remains.
    remaining = [r for r in fake_files.rows if r["transform_id"] == "tx-d"]
    assert all(r["content"] == "new" for r in remaining)


# ─────────────── _parse_coverage_report ───────────────
def test_parse_coverage_jacoco_csv(tmp_path):
    f = tmp_path / "jacoco.csv"
    f.write_text(
        "GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED\n"
        "g,p,c,10,90\n"
        "g,p,c2,0,100\n"
    )
    pct = tools_mod._parse_coverage_report(str(f))
    assert pct is not None
    # (90 + 100) / (100 + 100) = 95 %
    assert abs(pct - 95.0) < 0.1


def test_parse_coverage_npm_summary(tmp_path):
    f = tmp_path / "coverage-summary.json"
    f.write_text(json.dumps({"total": {"lines": {"pct": 82.4}}}))
    assert tools_mod._parse_coverage_report(str(f)) == 82.4


def test_parse_coverage_pytest_cov(tmp_path):
    f = tmp_path / "coverage.json"
    f.write_text(json.dumps({"totals": {"percent_covered": 76.9}}))
    assert tools_mod._parse_coverage_report(str(f)) == 76.9


def test_parse_coverage_missing_returns_none(tmp_path):
    assert tools_mod._parse_coverage_report(str(tmp_path / "nope.csv")) is None


# ─────────────── scope-aware download endpoint ───────────────
@pytest.fixture
def client(monkeypatch):
    fake_transforms = FakeCollection()
    fake_files = FakeCollection()
    monkeypatch.setattr(tools_mod, "transformations", fake_transforms)
    monkeypatch.setattr(tools_mod, "transform_files", fake_files)

    app = FastAPI()
    app.include_router(tools_mod.router)
    return TestClient(app), fake_transforms, fake_files


def _seed_tx(fake_transforms, fake_files, name="pilot"):
    fake_transforms.rows.append({"_id": "tx1", "name": name})
    fake_files.rows.extend([
        {"transform_id": "tx1", "type": "transformed", "path": "src/main.py", "content": "print('hi')"},
        {"transform_id": "tx1", "type": "test", "path": "tests/api/test_x.py", "content": "def test_x(): pass"},
    ])


def test_download_scope_code_default(client):
    tc, fake_transforms, fake_files = client
    _seed_tx(fake_transforms, fake_files)
    r = tc.get("/tools/transformer/tx1/download")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "src/main.py" in names
    assert "tests/api/test_x.py" not in names
    assert r.headers["content-disposition"].endswith('_transformed.zip"')


def test_download_scope_tests_only(client):
    tc, fake_transforms, fake_files = client
    _seed_tx(fake_transforms, fake_files)
    r = tc.get("/tools/transformer/tx1/download?scope=tests")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "tests/api/test_x.py" in names
    assert "src/main.py" not in names
    assert r.headers["content-disposition"].endswith('_tests.zip"')


def test_download_scope_all_bundles_both(client):
    tc, fake_transforms, fake_files = client
    _seed_tx(fake_transforms, fake_files)
    r = tc.get("/tools/transformer/tx1/download?scope=all")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"src/main.py", "tests/api/test_x.py"} <= names
    assert r.headers["content-disposition"].endswith('_bundle.zip"')


def test_download_scope_invalid_returns_400(client):
    tc, fake_transforms, fake_files = client
    _seed_tx(fake_transforms, fake_files)
    r = tc.get("/tools/transformer/tx1/download?scope=weird")
    assert r.status_code == 400


def test_download_scope_tests_empty_returns_404(client):
    tc, fake_transforms, fake_files = client
    fake_transforms.rows.append({"_id": "tx2", "name": "empty"})
    fake_files.rows.append({"transform_id": "tx2", "type": "transformed",
                            "path": "a.py", "content": "x"})
    r = tc.get("/tools/transformer/tx2/download?scope=tests")
    assert r.status_code == 404
