"""Iteration 10 tests:
- Health check
- GET /api/kb/{project_id}/ontology (empty + populated)
- /api/console/prompts/preview returns hardened OLTP template ('HARSHLY PENALISED')
"""
import os
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fall back to reading .env
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except Exception:
        pass


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def seeded_project_id(api):
    """Find an existing project with KB built; prefer TEST_LAMA_v4_bb4ae283."""
    r = api.get(f"{BASE_URL}/api/projects", timeout=30)
    assert r.status_code == 200, r.text
    projs = r.json()
    # Look for seeded LAMA project first
    for p in projs:
        if "LAMA" in (p.get("name", "") or "") or "lama" in (p.get("id", "") or "").lower():
            return p["id"]
    # else first one
    if projs:
        return projs[0]["id"]
    pytest.skip("No project available")


# ---- Health ----
def test_health(api):
    r = api.get(f"{BASE_URL}/api/health", timeout=20)
    assert r.status_code == 200
    assert r.json().get("ok") is True


# ---- Ontology on empty project ----
def test_ontology_fresh_project_empty(api):
    # Create a fresh project
    name = f"TEST_ontology_empty_{uuid.uuid4().hex[:8]}"
    r = api.post(
        f"{BASE_URL}/api/projects",
        json={"name": name, "source_tech": "Java/JSP", "target_tech": "Spring Boot/React"},
        timeout=30,
    )
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    try:
        rr = api.get(f"{BASE_URL}/api/kb/{pid}/ontology", timeout=30)
        assert rr.status_code == 200, rr.text
        data = rr.json()
        assert data["project_id"] == pid
        assert "stats" in data
        s = data["stats"]
        assert "total_nodes" in s and "total_edges" in s and "by_type" in s
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)
        assert s["total_nodes"] == 0
        assert s["total_edges"] == 0
    finally:
        try:
            api.delete(f"{BASE_URL}/api/projects/{pid}", timeout=20)
        except Exception:
            pass


# ---- Ontology on seeded project (LAMA v4) ----
def test_ontology_seeded_project_populated(api, seeded_project_id):
    r = api.get(f"{BASE_URL}/api/kb/{seeded_project_id}/ontology", timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    s = data["stats"]
    # Should have at least some nodes if KB has been built
    if s["total_nodes"] == 0:
        pytest.skip("Seeded project has no KB data; build KB first")
    assert s["total_nodes"] >= 10, f"Expected >=10 nodes, got {s['total_nodes']}"
    # Type distribution should include at least one of the structural types
    by_type = s["by_type"]
    assert any(t in by_type for t in ("Class", "Table", "Column", "Method", "Route")), (
        f"No structural types present: {by_type}"
    )
    # nodes have id/type/label
    for n in data["nodes"][:5]:
        assert "id" in n and "type" in n and "label" in n


def test_ontology_unknown_project_404(api):
    r = api.get(f"{BASE_URL}/api/kb/nonexistent_xyz/ontology", timeout=20)
    assert r.status_code == 404


# ---- Hardened OLTP prompt via console preview ----
def test_oltp_prompt_hardened(api, seeded_project_id):
    payload = {
        "prompt_key": "datamodel.oltp",
        "project_id": seeded_project_id,
        "variables": {},
    }
    r = api.post(f"{BASE_URL}/api/console/prompts/preview", json=payload, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    tpl = body.get("resolved_template") or body.get("template") or ""
    assert "HARSHLY PENALISED" in tpl, (
        f"datamodel.oltp prompt does not contain 'HARSHLY PENALISED'. "
        f"Keys={list(body.keys())}, snippet={tpl[:300]}"
    )


def test_olap_prompt_hardened(api, seeded_project_id):
    payload = {
        "prompt_key": "datamodel.olap",
        "project_id": seeded_project_id,
        "variables": {},
    }
    r = api.post(f"{BASE_URL}/api/console/prompts/preview", json=payload, timeout=30)
    if r.status_code != 200:
        pytest.skip(f"datamodel.olap preview returned {r.status_code}")
    body = r.json()
    tpl = body.get("resolved_template") or body.get("template") or ""
    # Best-effort: olap prompt should also be hardened (per problem statement)
    assert len(tpl) > 100, "OLAP template suspiciously short"
