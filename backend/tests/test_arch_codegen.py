"""Stage 3 (Architecture) + Stage 4 (CodeGen) wiring tests."""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8382").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def existing_project_id():
    r = requests.get(f"{API}/projects", timeout=20)
    assert r.status_code == 200
    projs = r.json()
    assert isinstance(projs, list) and len(projs) > 0, "Need at least one seeded project"
    # Prefer a project with DataModel frozen
    for p in projs:
        if (p.get("stage_status", {}) or {}).get("DataModel") == "frozen":
            return p["id"]
    return projs[0]["id"]


# ------- Health -------
def test_health():
    r = requests.get(f"{API}/health", timeout=10)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ------- Architecture: jobs/start/recommend -------
def test_arch_recommend_missing_project_id():
    r = requests.post(f"{API}/architecture/jobs/start/recommend", json={}, timeout=15)
    assert r.status_code == 400
    assert "project_id" in r.json().get("detail", "").lower()


def test_arch_recommend_invalid_project_returns_400():
    r = requests.post(
        f"{API}/architecture/jobs/start/recommend",
        json={"project_id": "__nonexistent_" + uuid.uuid4().hex},
        timeout=20,
    )
    # require_stage_context returns 400 with "DataModel stage not frozen..."
    assert r.status_code in (400, 404)
    detail = r.json().get("detail", "")
    assert "DataModel" in detail or "frozen" in detail.lower() or "not found" in detail.lower()


def test_arch_hld_missing_project_id():
    r = requests.post(f"{API}/architecture/jobs/start/hld", json={}, timeout=15)
    assert r.status_code == 400


def test_arch_lld_missing_project_id():
    r = requests.post(f"{API}/architecture/jobs/start/lld", json={}, timeout=15)
    assert r.status_code == 400


def test_arch_sequence_missing_project_id():
    r = requests.post(f"{API}/architecture/jobs/start/sequence", json={}, timeout=15)
    assert r.status_code == 400


def test_arch_artifacts_fresh_project():
    pid = uuid.uuid4().hex
    r = requests.get(f"{API}/architecture/{pid}/artifacts", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body == {"artifacts": [], "services": []}


def test_arch_chat_missing_project_id():
    r = requests.post(f"{API}/architecture/chat", json={}, timeout=15)
    assert r.status_code == 400


def test_arch_chat_missing_message(existing_project_id):
    r = requests.post(f"{API}/architecture/chat",
                      json={"project_id": existing_project_id}, timeout=15)
    assert r.status_code == 400
    assert "message" in r.json().get("detail", "").lower()


def test_arch_reset_works(existing_project_id):
    # Use a fresh project_id to avoid disturbing existing state
    pid = "TEST_arch_reset_" + uuid.uuid4().hex
    r = requests.post(f"{API}/architecture/{pid}/reset", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True


# ------- CodeGen -------
def test_codegen_generate_missing_project_id():
    r = requests.post(f"{API}/codegen/jobs/start/generate", json={}, timeout=15)
    assert r.status_code == 400


def test_codegen_generate_no_arch_frozen(existing_project_id):
    """Project has DataModel frozen but NOT Architecture -> should 400 with 'Architecture must be frozen'."""
    r = requests.post(f"{API}/codegen/jobs/start/generate",
                      json={"project_id": existing_project_id}, timeout=20)
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "Architecture" in detail and "frozen" in detail.lower()


def test_codegen_files_fresh_project():
    pid = uuid.uuid4().hex
    r = requests.get(f"{API}/codegen/{pid}/files", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body == {"services": [], "total_files": 0}


def test_codegen_download_zip_empty(existing_project_id):
    r = requests.post(f"{API}/codegen/{existing_project_id}/download-zip", timeout=15)
    assert r.status_code == 400
    assert "No files" in r.json().get("detail", "")


def test_codegen_chat_missing_project_id():
    r = requests.post(f"{API}/codegen/chat", json={}, timeout=15)
    assert r.status_code == 400


def test_codegen_reset_works():
    pid = "TEST_codegen_reset_" + uuid.uuid4().hex
    r = requests.post(f"{API}/codegen/{pid}/reset", timeout=15)
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_codegen_job_not_found():
    r = requests.get(f"{API}/codegen/jobs/nope_{uuid.uuid4().hex}", timeout=10)
    assert r.status_code == 404


def test_arch_job_not_found():
    r = requests.get(f"{API}/architecture/jobs/nope_{uuid.uuid4().hex}", timeout=10)
    assert r.status_code == 404
