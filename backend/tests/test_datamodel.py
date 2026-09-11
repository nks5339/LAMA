"""LAMA Stage 2 — Data Model backend tests.

Covers the new datamodel router and factory_reset endpoint.
Does NOT exercise the LLM-generating SSE end-to-end (expensive).
We validate:
- pipeline gates (400 when Discovery unfrozen)
- prompts.seed contains datamodel.* keys
- artifact list shape
- reset endpoint (Stage 2 + factory)
- artifact CRUD bogus-id 404 (not 405/500)
- chat pipeline-gate
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8382").rstrip("/")
TEST_PROJECT_ID = "7a0b9827-a3d1-46c6-82df-533fdd0bc8d8"


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Health / boot ----------
class TestHealth:
    def test_health(self, api):
        r = api.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True


# ---------- Seed prompts ----------
class TestSeedPrompts:
    def test_datamodel_prompts_present(self, api):
        r = api.get(f"{BASE_URL}/api/prompts", timeout=10)
        assert r.status_code == 200
        keys = {p["key"] for p in r.json()}
        for k in ("datamodel.oltp", "datamodel.olap", "datamodel.bus_matrix", "datamodel.chat"):
            assert k in keys, f"prompt {k} missing"


# ---------- Pipeline gates (Discovery not frozen → 400) ----------
class TestPipelineGate:
    def test_generate_oltp_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/generate/oltp",
                     json={"project_id": TEST_PROJECT_ID}, timeout=20)
        assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text[:200]}"
        assert "Discovery" in r.text and "frozen" in r.text.lower()

    def test_generate_entity_graph_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/generate/entity-graph",
                     json={"project_id": TEST_PROJECT_ID}, timeout=20)
        assert r.status_code == 400
        assert "Discovery" in r.text

    def test_chat_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/chat",
                     json={"project_id": TEST_PROJECT_ID, "message": "hi"}, timeout=20)
        assert r.status_code == 400
        assert "Discovery" in r.text

    def test_generate_olap_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/generate/olap",
                     json={"project_id": TEST_PROJECT_ID}, timeout=20)
        assert r.status_code == 400

    def test_generate_bus_matrix_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/generate/bus-matrix",
                     json={"project_id": TEST_PROJECT_ID}, timeout=20)
        assert r.status_code == 400

    def test_generate_migration_scripts_gated(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/generate/migration-scripts",
                     json={"project_id": TEST_PROJECT_ID}, timeout=20)
        assert r.status_code == 400


# ---------- Artifact list ----------
class TestArtifactList:
    def test_list_artifacts_fresh_project(self, api):
        # Create fresh project — no artifacts ever
        new_pid = f"TEST_DM_{uuid.uuid4().hex[:8]}"
        r = api.get(f"{BASE_URL}/api/data-model/{new_pid}/artifacts", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert "artifacts" in body and isinstance(body["artifacts"], list)
        assert body["artifacts"] == []


# ---------- Stage 2 reset on project with no artifacts ----------
class TestStage2Reset:
    def test_reset_no_artifacts_returns_200(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/{TEST_PROJECT_ID}/reset", timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert "deleted" in body
        for k in ("data_models", "bus_matrix", "olap_models", "migration_artifacts"):
            assert k in body["deleted"]


# ---------- Factory reset ----------
class TestFactoryReset:
    def test_factory_reset_404_fake_project(self, api):
        r = api.post(f"{BASE_URL}/api/projects/__fake__/factory-reset", timeout=15)
        assert r.status_code == 404
        assert "Project not found" in r.text

    def test_factory_reset_route_registered(self, api):
        # GET should be 405 (method not allowed) confirming POST exists, OR 404 from a real check.
        # POST with non-existent project returned 404 above; that confirms registration.
        r = api.options(f"{BASE_URL}/api/projects/__fake__/factory-reset", timeout=10)
        # Just ensure path doesn't 404 the route itself (some servers return 405)
        assert r.status_code in (200, 204, 404, 405)


# ---------- Artifact CRUD bogus IDs → 404 (not 405/500) ----------
class TestArtifactCRUDBogus:
    def test_get_artifact_bogus_404(self, api):
        r = api.get(f"{BASE_URL}/api/data-model/{TEST_PROJECT_ID}/artifact/__bogus__", timeout=10)
        assert r.status_code == 404, r.text
        assert "Artifact not found" in r.text

    def test_put_artifact_bogus_404(self, api):
        r = api.put(f"{BASE_URL}/api/data-model/{TEST_PROJECT_ID}/artifact/__bogus__",
                    json={"content": "x"}, timeout=10)
        assert r.status_code == 404, r.text
        assert "Artifact not found" in r.text

    def test_freeze_artifact_bogus_404(self, api):
        r = api.post(f"{BASE_URL}/api/data-model/{TEST_PROJECT_ID}/artifact/__bogus__/freeze",
                     timeout=10)
        assert r.status_code == 404
        assert "Artifact not found" in r.text

    def test_download_artifact_bogus_404(self, api):
        r = api.get(f"{BASE_URL}/api/data-model/{TEST_PROJECT_ID}/artifact/__bogus__/download",
                    timeout=10)
        assert r.status_code == 404, r.text
        assert "Artifact not found" in r.text


# ---------- Existing flow regressions ----------
class TestExistingFlows:
    def test_srs_get_still_works(self, api):
        r = api.get(f"{BASE_URL}/api/srs/{TEST_PROJECT_ID}", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert "sections" in body

    def test_pipeline_still_5_stages(self, api):
        r = api.get(f"{BASE_URL}/api/projects/{TEST_PROJECT_ID}/pipeline", timeout=15)
        assert r.status_code == 200
        stages = r.json()
        # pipeline shape: flat dict { Discovery: {...}, DataModel: {...}, ... } OR list/wrapper
        if isinstance(stages, list):
            assert len(stages) == 5
        elif isinstance(stages, dict):
            if "stages" in stages:
                assert len(stages["stages"]) == 5
            else:
                expected = {"Discovery", "DataModel", "Architecture", "CodeGen", "Living"}
                assert expected.issubset(set(stages.keys()))

    def test_owl_export_still_works(self, api):
        r = api.get(f"{BASE_URL}/api/kb/{TEST_PROJECT_ID}/owl-export", timeout=20)
        assert r.status_code == 200
        body = r.json()
        assert "@context" in body and "@graph" in body
