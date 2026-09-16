"""iter-19 — Direct Transform (DCTE) end-to-end, over HTTP.

The three sibling suites (test_iter18_dcte, test_iter1817_dcte_devops_tester,
test_iter19_droid_agent) exercise the engine and the agents directly. This
one drives the REST layer the frontend actually calls, start to finish:

    GET  /dcte/plugins
    POST /dcte/projects/detect
    GET  /dcte/fs/browse
    POST /dcte/jobs  ->  POST /dcte/jobs/{id}/start  ->  poll  ->  completed
    GET  /dcte/jobs/{id}/{report,events,transforms}
    DELETE /dcte/jobs/{id}

plus the four cases the integration brief calls for explicitly: an invalid
input, a backend-error case, an empty/edge case, and the happy path.

Offline. Mongo is replaced with the FakeCollection fixture pattern (see
test_iter1541_traceability_gate.py, the canonical example), the narrator
is stubbed so no `fabric_call` is attempted, and the job runs with
`ai_refactor=False` so the deterministic plugin layer is the only thing
that touches disk. The source tree is the shipped Helidon sample; the
destination is a tmp_path.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND))

# routes/dcte.py imports db, which reads MONGO_URL/DB_NAME at import time.
# Motor connects lazily, so a dummy value is enough and no live Mongo is
# needed — every collection is replaced by a FakeCollection below. Same
# stub the other in-process suites use.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

SAMPLES = BACKEND / "dcte" / "samples"
HELIDON = SAMPLES / "helidon_sample"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Supports the chain JobManager uses: .sort(...).limit(...) and async-for."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = list(rows)

    def sort(self, key, direction=1):
        self.rows.sort(key=lambda r: str(r.get(key, "")), reverse=direction == -1)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def __aiter__(self):
        async def gen():
            for r in self.rows:
                yield dict(r)
        return gen()


class FakeCollection:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))

        class R:
            inserted_id = doc.get("_id")
        return R()

    async def find_one(self, q=None, sort=None, *_a, **_kw):
        q = q or {}
        hits = [r for r in self.rows if all(r.get(k) == v for k, v in q.items())]
        if sort:
            key, direction = sort[0]
            hits.sort(key=lambda r: str(r.get(key, "")), reverse=direction == -1)
        return dict(hits[0]) if hits else None

    def find(self, q=None, *_a, **_kw):
        q = q or {}
        return _FakeCursor([r for r in self.rows if all(r.get(k) == v for k, v in q.items())])

    async def update_one(self, q, update, upsert=False):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                r.update((update or {}).get("$set", {}))
                return

    async def delete_one(self, q):
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if not all(r.get(k) == v for k, v in (q or {}).items())]

        class R:
            deleted_count = before - len(self.rows)
        return R()

    async def delete_many(self, q):
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if not all(r.get(k) == v for k, v in (q or {}).items())]

        class R:
            deleted_count = before - len(self.rows)
        return R()


@pytest.fixture
def client(monkeypatch):
    from routes import dcte as dcte_mod

    cols = {name: FakeCollection() for name in
            ("dcte_jobs", "dcte_transforms", "dcte_events", "dcte_reports", "audit_log")}
    for name, col in cols.items():
        monkeypatch.setattr(dcte_mod, name, col)

    # The narrator is decorative and is the only thing in the happy path
    # that would reach for an LLM. Stub the loop, not fabric_call, so a
    # regression that starts calling the model elsewhere still fails loudly.
    async def _no_narration(*_a, **_kw):
        return None
    monkeypatch.setattr("dcte.narrator.narration_loop", _no_narration)

    # model_providers is read by the ai_refactor preflight; with
    # ai_refactor=False it is never reached, but keep it deterministic.
    monkeypatch.setattr("db.model_providers", FakeCollection(), raising=False)

    app = FastAPI()
    app.include_router(dcte_mod.router, prefix="/api")
    # The context-manager form matters here. Without it Starlette spins up a
    # fresh portal (and event loop) per request and tears it down on the way
    # out, which cancels the `asyncio.create_task` the job runner holds — the
    # job then finishes but records "background task was cancelled". uvicorn
    # keeps one loop for the process lifetime; `with` is how the test client
    # reproduces that.
    with TestClient(app) as tc:
        yield tc, cols


def _poll_until_terminal(tc, job_id, timeout_s=60.0):
    """Poll GET /jobs/{id} until the status is terminal. Returns the doc."""
    terminal = {"completed", "failed", "rolled_back"}
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = tc.get(f"/api/dcte/jobs/{job_id}").json()
        if last.get("status") in terminal:
            return last
        time.sleep(0.25)
    raise AssertionError(f"job did not reach a terminal status in {timeout_s}s: {last}")


# ---------------------------------------------------------------------------
# Read-only surface
# ---------------------------------------------------------------------------
def test_plugins_lists_both_registered_transformations(client):
    tc, _ = client
    r = tc.get("/api/dcte/plugins")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["plugins"]}
    assert ids == {"helidon-mp-to-spring-boot-3", "oracle-to-postgres"}
    for p in r.json()["plugins"]:
        assert {"display_name", "source_stack", "target_stack", "version"} <= set(p)


def test_detect_fingerprints_the_shipped_helidon_sample(client):
    tc, _ = client
    r = tc.post("/api/dcte/projects/detect", json={"source_path": str(HELIDON)})
    assert r.status_code == 200
    d = r.json()
    assert d["exists"] and d["is_valid"]
    assert d["detected_stack"] == "helidon-mp"
    assert d["suggested_target"] == "spring-boot-3"
    assert d["confidence"] >= 0.9


def test_detect_reports_a_missing_path_without_erroring(client):
    """Edge case: a path the user typed wrong is data, not a 500."""
    tc, _ = client
    r = tc.post("/api/dcte/projects/detect", json={"source_path": "/no/such/tree"})
    assert r.status_code == 200
    d = r.json()
    assert d["exists"] is False and d["is_valid"] is False
    assert d["detected_stack"] == "unknown"
    assert d["hints"]


def test_detect_rejects_a_body_missing_the_required_field(client):
    """Invalid input: typed 422 from the schema, not a stack trace."""
    tc, _ = client
    r = tc.post("/api/dcte/projects/detect", json={})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "source_path"]


def test_browse_walks_a_real_directory_and_reports_a_parent(client):
    tc, _ = client
    r = tc.get("/api/dcte/fs/browse", params={"path": str(BACKEND / "dcte")})
    assert r.status_code == 200
    body = r.json()
    assert body["parent"] == str(BACKEND)
    names = {e["name"] for e in body["entries"] if e["is_dir"]}
    assert {"plugins", "samples", "templates"} <= names


def test_browse_error_paths_are_typed(client):
    tc, _ = client
    assert tc.get("/api/dcte/fs/browse", params={"path": "/no/such/tree"}).status_code == 404
    r = tc.get("/api/dcte/fs/browse", params={"path": str(BACKEND / "db.py")})
    assert r.status_code == 400
    assert "Not a directory" in r.json()["detail"]


def test_debug_env_reports_plugins_without_leaking_secrets(client):
    tc, _ = client
    body = tc.get("/api/dcte/debug/env").json()
    assert set(body) == {"workspaces_root", "ai_refactor_enabled_default", "plugins"}
    assert len(body["plugins"]) == 2


# ---------------------------------------------------------------------------
# Unknown-job handling — every /jobs/{id} route must 404, not 500
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,suffix", [
    ("get", ""),
    ("post", "/start"),
    ("post", "/resume"),
    ("post", "/rollback"),
    ("get", "/artifact?path=/tmp/x"),
])
def test_unknown_job_is_a_clean_404(client, method, suffix):
    tc, _ = client
    r = getattr(tc, method)(f"/api/dcte/jobs/nope{suffix}")
    assert r.status_code == 404
    assert r.json()["detail"] == "DCTE job not found"


def test_create_job_rejects_an_empty_service_list(client):
    """Empty case: the 400 names what is missing."""
    tc, _ = client
    r = tc.post("/api/dcte/jobs", json={"name": "empty", "services": []})
    assert r.status_code == 400
    assert "At least one service" in r.json()["detail"]


def test_events_limit_is_bounded_by_the_schema(client):
    tc, _ = client
    r = tc.get("/api/dcte/jobs/anything/events", params={"limit": 10_000})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "less_than_equal"


# ---------------------------------------------------------------------------
# The happy path, all the way through
# ---------------------------------------------------------------------------
def test_full_job_lifecycle_helidon_to_spring(client, tmp_path):
    tc, cols = client
    dest = tmp_path / "out"

    create = tc.post("/api/dcte/jobs", json={
        "name": "e2e helidon",
        "services": [{
            "name": "svc-1",
            "source_path": str(HELIDON),
            "destination_path": str(dest),
            "source_stack": "helidon-mp",
            "target_stack": "spring-boot-3",
        }],
        "ai_refactor": False,      # deterministic layer only — no LLM
        "generate_cicd": [],
    })
    assert create.status_code == 200
    job = create.json()
    jid = job["id"]
    assert job["status"] == "created"
    # Roots derived from service 1 when the caller omits them (iter-18.1).
    assert job["source_root"] == str(HELIDON)
    assert job["output_root"] == str(dest)
    assert any(r["action"] == "dcte_job_created" for r in cols["audit_log"].rows)

    assert tc.get("/api/dcte/jobs").json()["jobs"][0]["id"] == jid

    started = tc.post(f"/api/dcte/jobs/{jid}/start")
    assert started.status_code == 200
    assert started.json()["status"] == "analyzing"

    final = _poll_until_terminal(tc, jid)
    assert final["status"] == "completed", final.get("error")
    assert final["progress"] == pytest.approx(1.0)
    assert final["error"] in (None, "")

    # The engine actually wrote a Spring Boot tree.
    converted = dest / "converted-source" / "svc-1"
    assert (converted / "pom.xml").is_file()
    pom = (converted / "pom.xml").read_text(encoding="utf-8")
    assert "spring-boot-starter-parent" in pom
    assert not list(converted.rglob("*.java")) or any(
        "org.springframework" in p.read_text(encoding="utf-8", errors="ignore")
        for p in converted.rglob("*.java")
    )

    # ... and recorded it.
    transforms = tc.get(f"/api/dcte/jobs/{jid}/transforms").json()["transforms"]
    assert transforms and all(t["job_id"] == jid for t in transforms)

    reports = tc.get(f"/api/dcte/jobs/{jid}/report").json()["reports"]
    assert {r["kind"] for r in reports}
    for r in reports:
        assert Path(r["path"]).is_file()

    events = tc.get(f"/api/dcte/jobs/{jid}/events").json()["events"]
    assert events
    # Oldest-first — the contract the page's log pane and narration banner
    # both depend on (the draft's scroll handler assumed the opposite).
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    assert events[0]["phase"] == "start"

    assert any(r["action"] == "dcte_job_completed" for r in cols["audit_log"].rows)


def test_delete_job_cascades_to_its_children(client, tmp_path):
    tc, cols = client
    jid = tc.post("/api/dcte/jobs", json={
        "name": "to delete",
        "services": [{
            "name": "svc-1",
            "source_path": str(HELIDON),
            "destination_path": str(tmp_path / "out"),
            "source_stack": "helidon-mp",
            "target_stack": "spring-boot-3",
        }],
        "ai_refactor": False,
        "generate_cicd": [],
    }).json()["id"]

    for name in ("dcte_transforms", "dcte_events", "dcte_reports"):
        cols[name].rows.append({"_id": f"x-{name}", "job_id": jid})

    r = tc.delete(f"/api/dcte/jobs/{jid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert cols["dcte_jobs"].rows == []
    for name in ("dcte_transforms", "dcte_events", "dcte_reports"):
        assert cols[name].rows == [], name
    assert tc.get(f"/api/dcte/jobs/{jid}").status_code == 404


def test_artifact_download_refuses_a_path_outside_output_root(client, tmp_path):
    """Backend-error case with teeth: the guard is what stops ../../etc/passwd."""
    tc, _ = client
    dest = tmp_path / "out"
    dest.mkdir()
    jid = tc.post("/api/dcte/jobs", json={
        "name": "guarded",
        "services": [{
            "name": "svc-1",
            "source_path": str(HELIDON),
            "destination_path": str(dest),
            "source_stack": "helidon-mp",
            "target_stack": "spring-boot-3",
        }],
        "ai_refactor": False,
        "generate_cicd": [],
    }).json()["id"]

    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    r = tc.get(f"/api/dcte/jobs/{jid}/artifact", params={"path": str(outside)})
    assert r.status_code == 400
    assert "escapes output_root" in r.json()["detail"]

    inside = dest / "ok.txt"
    inside.write_text("fine", encoding="utf-8")
    r = tc.get(f"/api/dcte/jobs/{jid}/artifact", params={"path": str(inside)})
    assert r.status_code == 200
    assert r.text == "fine"

    missing = dest / "gone.txt"
    assert tc.get(f"/api/dcte/jobs/{jid}/artifact",
                  params={"path": str(missing)}).status_code == 404


def test_cicd_emit_takes_a_typed_body_and_writes_the_template(client, tmp_path):
    tc, _ = client
    dest = tmp_path / "out"
    jid = tc.post("/api/dcte/jobs", json={
        "name": "cicd",
        "services": [{
            "name": "svc-1",
            "source_path": str(HELIDON),
            "destination_path": str(dest),
            "source_stack": "helidon-mp",
            "target_stack": "spring-boot-3",
        }],
        "ai_refactor": False,
        "generate_cicd": [],
    }).json()["id"]
    dest.mkdir(parents=True, exist_ok=True)

    r = tc.post(f"/api/dcte/jobs/{jid}/cicd", json={"providers": ["github", "jenkins"]})
    assert r.status_code == 200
    emitted = r.json()["emitted"]
    assert len(emitted) == 2
    assert all(Path(p).is_file() for p in emitted)

    # An unknown provider fails with a 400 naming it, not a 500.
    r = tc.post(f"/api/dcte/jobs/{jid}/cicd", json={"providers": ["bamboo"]})
    assert r.status_code == 400
    assert "bamboo" in r.json()["detail"]

    # Omitting the body entirely falls back to the declared default.
    r = tc.post(f"/api/dcte/jobs/{jid}/cicd", json={})
    assert r.status_code == 200
    assert len(r.json()["emitted"]) == 1


def test_pause_marks_the_job_paused(client, tmp_path):
    tc, _ = client
    jid = tc.post("/api/dcte/jobs", json={
        "name": "pausable",
        "services": [{
            "name": "svc-1",
            "source_path": str(HELIDON),
            "destination_path": str(tmp_path / "out"),
            "source_stack": "helidon-mp",
            "target_stack": "spring-boot-3",
        }],
        "ai_refactor": False,
        "generate_cicd": [],
    }).json()["id"]

    r = tc.post(f"/api/dcte/jobs/{jid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert tc.get(f"/api/dcte/jobs/{jid}").json()["status"] == "paused"
