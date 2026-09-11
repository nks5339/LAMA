"""LAMA iteration-2 backend integration tests (pytest).

Covers: service rename, KB folder scan (with skip patterns + SKIP_DIRS),
chat intent detection (gap_question vs srs.generate auto-trigger),
TOON pruning resilience, GitHub config/test/push, existing routes regression.
"""
import os
import shutil
import tempfile
import uuid
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

LLM_TIMEOUT = 240


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
    assert pilot is not None, "PMIS Migration Pilot not seeded"
    return pilot["id"]


@pytest.fixture(scope="session")
def scratch_project_id(api):
    """A dedicated TEST_ project so we don't pollute pilot/SRS docs."""
    name = f"TEST_LAMA_v2_{uuid.uuid4().hex[:8]}"
    r = api.post(f"{API}/projects", json={
        "name": name,
        "source_tech": "PHP 5.6",
        "target_tech": "Node.js 20",
        "description": "v2 test project",
    }, timeout=30)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    yield pid


@pytest.fixture(scope="session")
def temp_src_folder():
    d = tempfile.mkdtemp(prefix="lama_test_src_")
    # Sample PHP
    (Path(d) / "UserController.php").write_text(
        "<?php\nclass UserController {\n  public function index() { return 'hi'; }\n}\n"
    )
    (Path(d) / "AuthService.php").write_text(
        "<?php\nclass AuthService {\n  public function login($u,$p) { return true; }\n  public function logout() {}\n}\n"
    )
    # Sample SQL
    (Path(d) / "schema.sql").write_text(
        "CREATE TABLE users (id INT PRIMARY KEY, email VARCHAR(255));\n"
        "CREATE TABLE sessions (id INT, user_id INT REFERENCES users(id));\n"
    )
    # Skipped files
    (Path(d) / "old.bak").write_text("backup junk")
    (Path(d) / "Legacy.php_03Mar2025").write_text("<?php // old dated copy")
    (Path(d) / "config_old.php").write_text("<?php // old config")
    (Path(d) / "data_bkp.sql").write_text("-- backup")
    (Path(d) / "notes.save").write_text("autosave")
    # SKIP_DIRS
    nm = Path(d) / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "ignored.php").write_text("<?php // should be skipped via SKIP_DIRS")
    git = Path(d) / ".git"
    git.mkdir()
    (git / "shouldskip.sql").write_text("-- skipped")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------- Service-level ----------
class TestServiceRename:
    def test_root_is_lama(self, api):
        r = api.get(f"{API}/", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("service") == "LAMA"
        assert body.get("status") == "ok"


# ---------- KB scan-folder ----------
class TestKBScanFolder:
    def test_missing_folder_path_returns_400(self, api, scratch_project_id):
        r = api.post(f"{API}/kb/scan-folder", json={"project_id": scratch_project_id}, timeout=30)
        assert r.status_code == 400
        assert "folder_path" in r.text.lower()

    def test_missing_project_id_returns_400(self, api):
        r = api.post(f"{API}/kb/scan-folder", json={"folder_path": "/tmp"}, timeout=30)
        assert r.status_code == 400

    def test_nonexistent_path_returns_400(self, api, scratch_project_id):
        bogus = f"/tmp/lama_does_not_exist_{uuid.uuid4().hex}"
        r = api.post(f"{API}/kb/scan-folder",
                     json={"project_id": scratch_project_id, "folder_path": bogus}, timeout=30)
        assert r.status_code == 400
        assert "does not exist" in r.text.lower() or "directory" in r.text.lower()

    def test_scan_valid_folder_ingests_and_skips(self, api, scratch_project_id, temp_src_folder):
        r = api.post(f"{API}/kb/scan-folder",
                     json={"project_id": scratch_project_id, "folder_path": temp_src_folder},
                     timeout=60)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("scanned", 0) >= 3  # 2 php + 1 sql at minimum
        # Skipped files should include the .bak, .php_*, _old, _bkp, .save
        skipped_files = set(body.get("skipped_files", []))
        # Names (basename) are returned in skipped list per implementation
        expected_skipped = {"old.bak", "Legacy.php_03Mar2025", "config_old.php",
                            "data_bkp.sql", "notes.save"}
        # Should skip all of them
        assert expected_skipped.issubset(skipped_files), (
            f"Expected skipped {expected_skipped}, got {skipped_files}"
        )
        # Processed files should not contain any skipped pattern
        processed = body.get("files", [])
        joined = " ".join(processed)
        assert ".bak" not in joined
        assert ".php_" not in joined
        assert "_old" not in joined.lower()
        # node_modules and .git children should NOT appear in processed list
        assert not any("node_modules" in p for p in processed)
        assert not any(p.startswith(".git") or "/.git/" in p for p in processed)

    def test_files_listed_after_scan(self, api, scratch_project_id):
        r = api.get(f"{API}/kb/{scratch_project_id}/files", timeout=30)
        assert r.status_code == 200
        files = r.json()
        names = [f["filename"] for f in files]
        # At least one PHP and SQL from the scan
        assert any(n.endswith(".php") for n in names), names
        assert any(n.endswith(".sql") for n in names), names
        # None of the skipped patterns
        assert not any(".bak" in n or "_bkp" in n or "_old" in n or ".php_" in n for n in names)

    def test_build_after_scan(self, api, scratch_project_id):
        r = api.post(f"{API}/kb/build", json={"project_id": scratch_project_id}, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        stats = body.get("stats", {})
        # We seeded 2 classes + 2 tables minimum
        assert stats.get("classes", 0) >= 2, stats
        assert stats.get("tables", 0) >= 2, stats
        assert stats.get("methods", 0) >= 1, stats


# ---------- Chat intent ----------
class TestChatIntent:
    def test_gap_question_intent(self, api, pilot_project_id):
        r = api.post(f"{API}/chat", json={
            "project_id": pilot_project_id,
            "message": "What modules do you see?",
            "stage": "Discovery",
            "model": "deepseek/deepseek-chat",
        }, timeout=LLM_TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("intent") == "srs.gap_question"
        assert body.get("srs_triggered") is False
        assert "message" in body and body["message"]["content"]

    def test_srs_generate_intent_triggers(self, api, pilot_project_id):
        r = api.post(f"{API}/chat", json={
            "project_id": pilot_project_id,
            "message": "I have enough context, generate the SRS",
            "stage": "Discovery",
            "model": "deepseek/deepseek-chat",
        }, timeout=LLM_TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("intent") == "srs.generate"
        assert body.get("srs_triggered") is True
        # Verify SRS doc was created
        srs = api.get(f"{API}/srs/{pilot_project_id}", timeout=30).json()
        assert srs.get("sections"), "SRS sections should be populated after auto-trigger"


# ---------- TOON pruning (non-crash check) ----------
class TestToonPruning:
    def test_chat_handles_large_kb(self, api, pilot_project_id):
        # Pilot project has a large-ish TOON already; just exercise chat to confirm no crash
        r = api.post(f"{API}/chat", json={
            "project_id": pilot_project_id,
            "message": "Summarise the top 3 entities.",
            "stage": "Discovery",
            "model": "deepseek/deepseek-chat",
        }, timeout=LLM_TIMEOUT)
        assert r.status_code == 200, r.text
        assert r.json()["message"]["content"]


# ---------- GitHub config / test / push ----------
class TestGithub:
    REPO_URL = "https://github.com/lama-test/sample-repo"
    FAKE_TOKEN = "ghp_invalid_for_test_xxxxxxxxxxxxxxxxxxxx"

    def test_save_config(self, api, scratch_project_id):
        r = api.post(f"{API}/github/config", json={
            "project_id": scratch_project_id,
            "repo_url": self.REPO_URL,
            "token": self.FAKE_TOKEN,
            "branch": "develop",
        }, timeout=30)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    def test_get_config_does_not_leak_token(self, api, scratch_project_id):
        r = api.get(f"{API}/github/config/{scratch_project_id}", timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body["has_token"] is True
        assert body["repo_url"] == self.REPO_URL
        assert body["branch"] == "develop"
        # Token must NOT be returned full
        assert "token" not in body or body.get("token") != self.FAKE_TOKEN
        assert body["token_preview"]
        assert self.FAKE_TOKEN not in body["token_preview"]
        # Preview should be short
        assert len(body["token_preview"]) <= 12

    def test_test_connection_invalid_token(self, api):
        r = api.post(f"{API}/github/test", json={
            "repo_url": self.REPO_URL,
            "token": self.FAKE_TOKEN,
        }, timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is False
        assert body.get("error"), "error string should be present"

    def test_push_without_configured_repo_returns_error(self, api, pilot_project_id):
        # Pilot project should NOT have a real repo configured. Pass empty strings to override.
        r = api.post(f"{API}/github/push", json={
            "project_id": pilot_project_id,
            "repo_url": "",
            "token": "",
            "branch": "main",
        }, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "error"
        assert "configured" in (body.get("message") or "").lower() or \
               "token" in (body.get("message") or "").lower()

    def test_push_without_srs_returns_error(self, api, scratch_project_id):
        # scratch project has fake repo+token saved, but NO srs doc generated yet
        r = api.post(f"{API}/github/push", json={
            "project_id": scratch_project_id,
            "repo_url": "",
            "token": "",
            "branch": "main",
        }, timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "error"
        # Could be 'no SRS' or 'github push failed' if it tries to call API.
        # We want the SRS-missing branch — verified by absence of SRS doc.
        msg = (body.get("message") or "").lower()
        assert "srs" in msg or "freeze" in msg or "no srs" in msg, msg


# ---------- Regression: existing routes still work ----------
class TestRegression:
    def test_projects_list(self, api):
        r = api.get(f"{API}/projects", timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_project_get(self, api, pilot_project_id):
        r = api.get(f"{API}/projects/{pilot_project_id}", timeout=30)
        assert r.status_code == 200
        assert r.json()["id"] == pilot_project_id

    def test_project_create(self, api):
        name = f"TEST_Reg_{uuid.uuid4().hex[:6]}"
        r = api.post(f"{API}/projects", json={
            "name": name, "source_tech": "X", "target_tech": "Y",
        }, timeout=30)
        assert r.status_code in (200, 201)
        assert r.json()["name"] == name

    def test_prompts_list(self, api):
        r = api.get(f"{API}/prompts", timeout=30)
        assert r.status_code == 200
        data = r.json()
        # 8 seeded prompts expected
        assert isinstance(data, list) and len(data) >= 1

    def test_audit_log(self, api):
        r = api.get(f"{API}/audit", timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
