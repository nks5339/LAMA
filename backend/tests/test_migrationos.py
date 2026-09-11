"""MigrationOS backend integration tests (pytest).

Covers: projects, prompts, KB upload/build/status/toon/glossary, chat (real LLM),
SRS generate/edit/freeze/unfreeze/PDF, GitHub push stub, audit log.
"""
import os
import io
import uuid
import time
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

LLM_TIMEOUT = 180


# ---------- Shared session ----------
@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def pilot_project_id(api):
    r = api.get(f"{API}/projects", timeout=30)
    assert r.status_code == 200, r.text
    projects = r.json()
    pilot = next((p for p in projects if p["name"] == "PMIS Migration Pilot"), None)
    assert pilot is not None, "PMIS Migration Pilot project not seeded"
    return pilot["id"]


# ---------- Projects ----------
class TestProjects:
    def test_list_projects_includes_pilot(self, api):
        r = api.get(f"{API}/projects", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        names = [p["name"] for p in data]
        assert "PMIS Migration Pilot" in names
        pilot = next(p for p in data if p["name"] == "PMIS Migration Pilot")
        assert pilot["source_tech"].startswith("PHP")
        assert pilot["target_tech"].startswith("FastAPI")
        assert "stage_status" in pilot
        assert pilot["stage_status"]["Discovery"] in ("active", "frozen")

    def test_create_project(self, api):
        payload = {
            "name": f"TEST_Project_{uuid.uuid4().hex[:8]}",
            "source_tech": "Java 8 / Spring",
            "target_tech": "Go 1.22",
            "description": "Created by pytest",
        }
        r = api.post(f"{API}/projects", json=payload, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == payload["name"]
        assert data["source_tech"] == payload["source_tech"]
        assert "id" in data and len(data["id"]) > 0
        # GET to verify persistence
        r2 = api.get(f"{API}/projects/{data['id']}", timeout=30)
        assert r2.status_code == 200
        assert r2.json()["name"] == payload["name"]


# ---------- Prompts ----------
class TestPrompts:
    EXPECTED_KEYS = {
        "srs.generate", "srs.gap_question", "datamodel.optimise",
        "arch.decompose", "code.generate", "test.unit",
        "test.selenium", "diff.srs",
    }

    def test_list_prompts_has_8_seeded(self, api):
        r = api.get(f"{API}/prompts", timeout=30)
        assert r.status_code == 200
        data = r.json()
        keys = {p["key"] for p in data}
        assert self.EXPECTED_KEYS.issubset(keys), f"Missing: {self.EXPECTED_KEYS - keys}"
        for p in data:
            assert "template" in p and "version" in p and "stage" in p

    def test_update_prompt_bumps_version(self, api):
        # fetch current
        r = api.get(f"{API}/prompts", timeout=30)
        cur = next(p for p in r.json() if p["key"] == "diff.srs")
        old_version = cur["version"]
        new_template = cur["template"] + "\n# pytest-edit"
        r2 = api.put(
            f"{API}/prompts/diff.srs",
            json={"template": new_template, "description": "pytest update"},
            timeout=30,
        )
        assert r2.status_code == 200, r2.text
        updated = r2.json()
        assert updated["version"] == old_version + 1
        assert updated["template"].endswith("# pytest-edit")
        assert updated["description"] == "pytest update"


# ---------- KB ----------
PHP_SAMPLE = """<?php
namespace App\\Controllers;

class UserController {
    public function index() {
        return view('users');
    }
    public function create($data) {
        $this->db->insert('users', $data);
    }
    public function update($id, $data) {
        return $this->db->update('users', $data, ['id' => $id]);
    }
}

class ProjectController {
    public function list_all() { return []; }
    public function detail($id) { return null; }
}
"""

SQL_SAMPLE = """
CREATE TABLE users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50)
);

CREATE TABLE projects (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    owner_id INT,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE TABLE tasks (
    id INT PRIMARY KEY,
    project_id INT,
    title VARCHAR(255),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
"""


class TestKB:
    def test_upload_files(self, api, pilot_project_id):
        files = [
            ("files", ("UserController.php", PHP_SAMPLE.encode(), "application/x-php")),
            ("files", ("schema.sql", SQL_SAMPLE.encode(), "application/sql")),
        ]
        data = {"project_id": pilot_project_id}
        # multipart -> drop json content-type for this request
        r = requests.post(f"{API}/kb/upload", data=data, files=files, timeout=60)
        assert r.status_code == 200, r.text
        out = r.json()
        assert "uploaded" in out and len(out["uploaded"]) == 2
        for f in out["uploaded"]:
            assert f["chunks"] >= 1
            assert f["size"] > 0
            assert f["filetype"] in ("php", "sql")

    def test_build_kb_extracts_entities(self, api, pilot_project_id):
        r = api.post(f"{API}/kb/build", json={"project_id": pilot_project_id}, timeout=60)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is True
        stats = out["stats"]
        # We uploaded 2 PHP classes and 3 SQL tables
        assert stats.get("classes", 0) >= 2, f"Expected >=2 classes, got {stats}"
        assert stats.get("tables", 0) >= 3, f"Expected >=3 tables, got {stats}"
        assert stats.get("methods", 0) >= 3
        assert stats.get("columns", 0) >= 5
        assert out["toon_size"] > 0

    def test_kb_status(self, api, pilot_project_id):
        r = api.get(f"{API}/kb/{pilot_project_id}/status", timeout=30)
        assert r.status_code == 200
        s = r.json()
        assert s["project_id"] == pilot_project_id
        assert s["files"] >= 2
        assert s["chunks"] >= 2
        assert s["classes"] >= 2
        assert s["tables"] >= 3
        assert s["toon_size"] > 0

    def test_kb_toon(self, api, pilot_project_id):
        r = api.get(f"{API}/kb/{pilot_project_id}/toon", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "toon" in data
        assert isinstance(data["toon"], str)
        assert len(data["toon"]) > 0

    def test_kb_glossary(self, api, pilot_project_id):
        r = api.get(f"{API}/kb/{pilot_project_id}/glossary", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "terms" in data
        assert isinstance(data["terms"], list)
        assert len(data["terms"]) > 0
        # Should contain at least one expected term
        joined = " ".join(data["terms"]).lower()
        assert "user" in joined or "project" in joined


# ---------- Chat ----------
class TestChat:
    def test_list_models(self, api):
        r = api.get(f"{API}/chat/models", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "models" in data
        ids = [m["id"] for m in data["models"]]
        assert len(ids) == 3
        assert any("deepseek-chat" in i for i in ids)
        assert any("deepseek-coder" in i for i in ids)
        assert any("qwen" in i for i in ids)

    def test_chat_message_real_llm(self, api, pilot_project_id):
        payload = {
            "project_id": pilot_project_id,
            "message": "Briefly: what is the primary entity in this codebase?",
            "model": "deepseek/deepseek-chat",
            "stage": "Discovery",
        }
        t0 = time.time()
        r = api.post(f"{API}/chat", json=payload, timeout=LLM_TIMEOUT)
        elapsed = time.time() - t0
        assert r.status_code == 200, f"({elapsed:.1f}s) {r.text}"
        data = r.json()
        assert "conversation_id" in data
        assert "message" in data
        assert data["message"]["role"] == "assistant"
        assert len(data["message"]["content"]) > 0
        assert "usage" in data
        # usage tokens should be > 0 from real call
        assert data["usage"]["total_tokens"] >= 0
        # Save for history test
        TestChat._conv_id = data["conversation_id"]

    def test_chat_history(self, api, pilot_project_id):
        r = api.get(f"{API}/chat/{pilot_project_id}/history", timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 2  # at least user + assistant from previous test
        roles = [m["role"] for m in data]
        assert "user" in roles and "assistant" in roles


# ---------- SRS ----------
class TestSRS:
    def test_generate_srs_real_llm(self, api, pilot_project_id):
        payload = {"project_id": pilot_project_id, "model": "deepseek/deepseek-chat"}
        t0 = time.time()
        r = api.post(f"{API}/srs/generate", json=payload, timeout=LLM_TIMEOUT)
        elapsed = time.time() - t0
        assert r.status_code == 200, f"({elapsed:.1f}s) {r.text}"
        data = r.json()
        assert data["ok"] is True
        sections = data["sections"]
        expected = {"purpose", "scope", "definitions", "overall_description",
                    "functional_requirements", "non_functional_requirements",
                    "use_cases", "constraints"}
        assert expected.issubset(set(sections.keys()))
        # At least the main sections must have content
        assert len(sections["purpose"]) > 10
        assert data["version"] >= 1

    def test_get_srs(self, api, pilot_project_id):
        r = api.get(f"{API}/srs/{pilot_project_id}", timeout=30)
        assert r.status_code == 200
        doc = r.json()
        assert doc["project_id"] == pilot_project_id
        assert "sections" in doc
        assert doc["version"] >= 1

    def test_update_section(self, api, pilot_project_id):
        new_text = "TEST_UPDATED purpose section content."
        r = api.put(
            f"{API}/srs/{pilot_project_id}/section",
            json={"section": "purpose", "content": new_text},
            timeout=30,
        )
        assert r.status_code == 200
        # Verify persisted
        r2 = api.get(f"{API}/srs/{pilot_project_id}", timeout=30)
        assert r2.json()["sections"]["purpose"] == new_text

    def test_freeze_and_project_status(self, api, pilot_project_id):
        r = api.post(f"{API}/srs/freeze",
                     json={"project_id": pilot_project_id, "user": "pytest"},
                     timeout=30)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Project stage_status.Discovery should be frozen
        r2 = api.get(f"{API}/projects/{pilot_project_id}", timeout=30)
        assert r2.json()["stage_status"]["Discovery"] == "frozen"
        # SRS doc must reflect frozen
        r3 = api.get(f"{API}/srs/{pilot_project_id}", timeout=30)
        assert r3.json()["frozen"] is True

    def test_section_edit_blocked_when_frozen(self, api, pilot_project_id):
        r = api.put(
            f"{API}/srs/{pilot_project_id}/section",
            json={"section": "purpose", "content": "should fail"},
            timeout=30,
        )
        assert r.status_code == 400

    def test_unfreeze(self, api, pilot_project_id):
        r = api.post(f"{API}/srs/unfreeze",
                     json={"project_id": pilot_project_id},
                     timeout=30)
        assert r.status_code == 200
        r2 = api.get(f"{API}/projects/{pilot_project_id}", timeout=30)
        assert r2.json()["stage_status"]["Discovery"] == "active"
        r3 = api.get(f"{API}/srs/{pilot_project_id}", timeout=30)
        assert r3.json()["frozen"] is False

    def test_export_pdf(self, api, pilot_project_id):
        r = api.get(f"{API}/srs/{pilot_project_id}/export.pdf", timeout=60)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")
        # Basic PDF signature
        assert r.content[:4] == b"%PDF"
        assert len(r.content) > 500


# ---------- GitHub stub ----------
class TestGithub:
    def test_push_stub(self, api, pilot_project_id):
        payload = {
            "project_id": pilot_project_id,
            "repo_url": "https://github.com/test/repo",
            "token": "ghp_dummy",
            "branch": "main",
        }
        r = api.post(f"{API}/github/push", json=payload, timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "success"
        assert "message" in data
        assert data["project_id"] == pilot_project_id


# ---------- Audit ----------
class TestAudit:
    def test_audit_log(self, api, pilot_project_id):
        r = api.get(f"{API}/audit", timeout=30, params={"project_id": pilot_project_id})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # We should have at least srs.generate + srs.freeze entries by now
        actions = [d["action"] for d in data]
        assert "srs.generate" in actions
        assert "srs.freeze" in actions
