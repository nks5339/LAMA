"""Iteration 11: Stage 5 Living gate-checks + Ontology snapshot CRUD + diff."""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to local for inside-container runs (still uses public if set)
    BASE_URL = "http://localhost:8001"

API = f"{BASE_URL}/api"
# Seeded LAMA project with ontology nodes (Discovery+DataModel frozen, Architecture available)
LAMA_PROJECT = "1376022c-a9b1-4f65-82ec-8b9cb07c9f0e"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ───────── Health ─────────
def test_health(session):
    r = session.get(f"{API}/health", timeout=15)
    assert r.status_code == 200
    assert r.json().get("ok") is True


# ───────── Ontology snapshot CRUD + diff ─────────
class TestOntologySnapshots:
    def test_full_snapshot_lifecycle(self, session):
        # 1. List snapshots — should respond OK with count int
        r = session.get(f"{API}/kb/{LAMA_PROJECT}/ontology/snapshots", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "snapshots" in data and "count" in data
        assert isinstance(data["snapshots"], list)
        initial_count = data["count"]

        # 2. Create a snapshot
        name = f"TEST_snap_{uuid.uuid4().hex[:8]}"
        r = session.post(f"{API}/kb/{LAMA_PROJECT}/ontology/snapshot",
                         json={"name": name}, timeout=30)
        assert r.status_code == 200, r.text
        snap = r.json()
        assert "snapshot_id" in snap
        assert snap["name"] == name
        assert "created_at" in snap
        assert "stats" in snap and "total_nodes" in snap["stats"]
        snap_id = snap["snapshot_id"]
        nodes_in_snap = snap["stats"]["total_nodes"]

        # 3. List snapshots — should now include our new one, and masked (no nodes/edges)
        r = session.get(f"{API}/kb/{LAMA_PROJECT}/ontology/snapshots", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == initial_count + 1
        found = [s for s in body["snapshots"] if s["id"] == snap_id]
        assert len(found) == 1
        # nodes/edges must be masked out
        assert "nodes" not in found[0]
        assert "edges" not in found[0]

        # 4. Diff vs current — should be near-zero since we just snapshotted
        r = session.get(f"{API}/kb/{LAMA_PROJECT}/ontology/diff",
                        params={"a": snap_id, "b": "current"}, timeout=30)
        assert r.status_code == 200, r.text
        diff = r.json()
        assert diff["a"]["id"] == snap_id
        assert diff["b"]["id"] == "current"
        assert "summary" in diff
        s = diff["summary"]
        for k in ("added_nodes", "removed_nodes", "unchanged_nodes",
                  "added_edges", "removed_edges", "by_type_delta"):
            assert k in s, f"missing {k}"
        assert s["added_nodes"] == 0
        assert s["removed_nodes"] == 0
        assert s["unchanged_nodes"] == nodes_in_snap
        assert isinstance(diff["added_nodes"], list)
        assert isinstance(diff["removed_nodes"], list)

        # 5. Diff missing 'a' -> 400
        r = session.get(f"{API}/kb/{LAMA_PROJECT}/ontology/diff", timeout=15)
        assert r.status_code == 400

        # 6. Diff with nonexistent snapshot_id -> 404
        r = session.get(f"{API}/kb/{LAMA_PROJECT}/ontology/diff",
                        params={"a": "nonexistent_xyz"}, timeout=15)
        assert r.status_code == 404
        assert "Snapshot A not found" in r.text

        # 7. Delete the snapshot
        r = session.delete(f"{API}/kb/{LAMA_PROJECT}/ontology/snapshot/{snap_id}", timeout=15)
        assert r.status_code == 200
        assert r.json().get("ok") is True

        # 8. Second delete -> 404
        r = session.delete(f"{API}/kb/{LAMA_PROJECT}/ontology/snapshot/{snap_id}", timeout=15)
        assert r.status_code == 404


# ───────── Living gate-checks (CodeGen NOT frozen on this project) ─────────
class TestLivingGates:
    def test_selenium_gate_400(self, session):
        # Use a project where CodeGen is NOT frozen (LAMA_PROJECT has CodeGen=locked)
        r = session.post(f"{API}/living/jobs/start/selenium",
                         json={"project_id": LAMA_PROJECT}, timeout=15)
        assert r.status_code == 400
        body = r.text
        assert "CodeGen" in body or "frozen" in body.lower()

    def test_selenium_missing_project_400(self, session):
        r = session.post(f"{API}/living/jobs/start/selenium",
                         json={"project_id": "missing_xyz_123"}, timeout=15)
        assert r.status_code in (400, 404)

    def test_jmeter_gate_400(self, session):
        r = session.post(f"{API}/living/jobs/start/jmeter",
                         json={"project_id": LAMA_PROJECT}, timeout=15)
        assert r.status_code == 400

    def test_drift_gate_400(self, session):
        r = session.post(f"{API}/living/jobs/start/drift",
                         json={"project_id": LAMA_PROJECT,
                               "live_signals": "p99=900ms"}, timeout=15)
        assert r.status_code == 400

    def test_srs_diff_requires_inputs(self, session):
        # SRS-diff intentionally does NOT have the codegen gate (per code review)
        # — payload validation runs first. Missing srs_a/srs_b -> 400.
        r = session.post(f"{API}/living/jobs/start/srs-diff",
                         json={"project_id": LAMA_PROJECT}, timeout=15)
        assert r.status_code == 400
        assert "srs_a" in r.text or "required" in r.text.lower()

    def test_living_no_project_id(self, session):
        r = session.post(f"{API}/living/jobs/start/jmeter", json={}, timeout=15)
        assert r.status_code == 400


# ───────── Living artifacts list / freeze / reset ─────────
class TestLivingArtifactsAndStage:
    @pytest.fixture(scope="class")
    def fresh_project(self, session):
        # Create a fresh TEST project for stage manipulation
        name = f"TEST_living_iter11_{uuid.uuid4().hex[:8]}"
        r = session.post(f"{API}/projects", json={
            "name": name, "description": "iter11",
            "source_tech": "PHP", "target_tech": "FastAPI + React",
        }, timeout=15)
        assert r.status_code in (200, 201), r.text
        pid = r.json().get("id") or r.json().get("project", {}).get("id")
        assert pid
        yield pid
        # cleanup
        try:
            session.delete(f"{API}/projects/{pid}", timeout=10)
        except Exception:
            pass

    def test_list_artifacts_empty(self, session, fresh_project):
        r = session.get(f"{API}/living/{fresh_project}/artifacts", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data == {"artifacts": [], "count": 0} or (data["count"] == 0 and data["artifacts"] == [])

    def test_freeze_then_verify(self, session, fresh_project):
        r = session.post(f"{API}/living/{fresh_project}/freeze", timeout=15)
        assert r.status_code == 200
        assert r.json().get("ok") is True

        # Verify via GET /api/projects
        r = session.get(f"{API}/projects", timeout=15)
        proj = next((p for p in r.json() if p["id"] == fresh_project), None)
        assert proj is not None
        assert proj.get("stage_status", {}).get("Living") == "frozen"

    def test_reset_clears(self, session, fresh_project):
        r = session.post(f"{API}/living/{fresh_project}/reset", timeout=15)
        assert r.status_code == 200
        assert r.json().get("ok") is True
        # Living should be 'available' again
        r = session.get(f"{API}/projects", timeout=15)
        proj = next((p for p in r.json() if p["id"] == fresh_project), None)
        assert proj.get("stage_status", {}).get("Living") == "available"

    def test_artifact_not_found(self, session, fresh_project):
        r = session.get(f"{API}/living/{fresh_project}/artifact/nonexistent_id", timeout=15)
        assert r.status_code == 404


# ───────── No regression checks ─────────
def test_projects_list_still_ok(session):
    r = session.get(f"{API}/projects", timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
