"""Tests for the new Console (Model Fabric, Agent Fabric, Prompt Engineering)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def project_id(s):
    # find any seeded project, else create one
    r = s.get(f"{API}/projects")
    if r.ok and r.json():
        return r.json()[0]["id"]
    cr = s.post(f"{API}/projects", json={"name": "TEST_console", "description": "x",
                                          "source_tech": "java", "target_tech": "python"})
    return cr.json()["id"]


# ── Health ──────────────────────────────────────────────────────────
def test_health(s):
    r = s.get(f"{API}/health")
    assert r.status_code == 200


# ── Provider Auto-detect ────────────────────────────────────────────
def test_providers_initial_listing(s):
    # cleanup any prior TEST providers
    r = s.get(f"{API}/console/providers")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data
    for p in data["providers"]:
        if p.get("name", "").startswith("TEST_") or "auto" in p.get("name", "").lower():
            s.delete(f"{API}/console/providers/{p['id']}")


def test_setup_openrouter_from_key(s):
    r = s.post(f"{API}/console/providers/setup",
               json={"api_key": "sk-or-test-key-1234567890", "name": "TEST_OR"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    p = data["provider"]
    assert p["provider_type"] == "openrouter"
    assert p["is_default"] is True
    # Masked api_key never raw
    assert "test-key" not in p["api_key"]
    assert "..." in p["api_key"]
    # Routing seeded
    assert "low" in p["routing"]
    assert "medium" in p["routing"]
    assert "high" in p["routing"]


def test_setup_anthropic_from_key(s):
    r = s.post(f"{API}/console/providers/setup",
               json={"api_key": "sk-ant-test-1234567", "name": "TEST_ANT"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"]["provider_type"] == "anthropic"


def test_setup_groq_from_key(s):
    r = s.post(f"{API}/console/providers/setup",
               json={"api_key": "gsk_test1234", "name": "TEST_GRQ"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"]["provider_type"] == "groq"


def test_providers_list_has_masked_keys(s):
    r = s.get(f"{API}/console/providers")
    assert r.status_code == 200
    providers = r.json()["providers"]
    assert len(providers) >= 1
    for p in providers:
        assert "test-key" not in p.get("api_key", "")
        assert "test1234" not in p.get("api_key", "")
        # Either masked or short
        assert "..." in p.get("api_key", "") or p.get("api_key") == "***"


# ── Provider Updates ────────────────────────────────────────────────
def test_update_provider_routing_and_default(s):
    providers = s.get(f"{API}/console/providers").json()["providers"]
    target = next((p for p in providers if p["provider_type"] == "anthropic"), providers[0])
    pid = target["id"]
    r = s.put(f"{API}/console/providers/{pid}",
              json={"is_default": True, "routing": {"low": "claude-haiku-4-5",
                                                     "medium": "claude-sonnet-4",
                                                     "high": "claude-sonnet-4"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # verify
    after = s.get(f"{API}/console/providers").json()["providers"]
    upd = next(p for p in after if p["id"] == pid)
    assert upd["is_default"] is True
    assert upd["routing"]["low"] == "claude-haiku-4-5"


def test_update_provider_key(s):
    providers = s.get(f"{API}/console/providers").json()["providers"]
    pid = providers[0]["id"]
    old_masked = providers[0]["api_key"]
    r = s.put(f"{API}/console/providers/{pid}/key",
              json={"api_key": "sk-or-rotated-key-9999999999"})
    assert r.status_code == 200
    after = s.get(f"{API}/console/providers").json()["providers"]
    new_p = next(p for p in after if p["id"] == pid)
    assert "rotated" not in new_p["api_key"]  # masked
    assert new_p["api_key"] != old_masked or "sk-or-r" in new_p["api_key"]


def test_provider_test_endpoint_with_fake_key(s):
    providers = s.get(f"{API}/console/providers").json()["providers"]
    pid = providers[0]["id"]
    r = s.post(f"{API}/console/providers/{pid}/test")
    assert r.status_code == 200
    body = r.json()
    # ok=false expected, but endpoint must respond cleanly with latency_ms
    assert "ok" in body
    assert "latency_ms" in body or body.get("ok") is False


def test_delete_last_active_provider_fails(s):
    # Find or arrange a single-active state by deleting until 1 remains
    providers = s.get(f"{API}/console/providers").json()["providers"]
    # delete all but last
    keep = providers[-1]["id"]
    for p in providers[:-1]:
        s.delete(f"{API}/console/providers/{p['id']}")
    r = s.delete(f"{API}/console/providers/{keep}")
    assert r.status_code == 400


# ── Agents ──────────────────────────────────────────────────────────
def test_agents_grouped_by_stage(s):
    r = s.get(f"{API}/console/agents")
    assert r.status_code == 200
    data = r.json()
    for stage in ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"]:
        assert stage in data, f"missing stage {stage}"
        assert "orchestrator" in data[stage]
        assert "tasks" in data[stage]
    total = sum(len(s["orchestrator"]) + len(s["tasks"]) for s in data.values())
    assert total == 22, f"expected 22 agents, got {total}"
    # Discovery: 1 orch + 3 tasks
    assert len(data["Discovery"]["orchestrator"]) == 1
    assert len(data["Discovery"]["tasks"]) == 3
    # Architecture: 1 orch + 5 tasks
    assert len(data["Architecture"]["orchestrator"]) == 1
    assert len(data["Architecture"]["tasks"]) == 5


def test_get_agent_srs_generate(s):
    r = s.get(f"{API}/console/agents/srs.generate")
    assert r.status_code == 200
    a = r.json()
    assert a["complexity"] == "high"
    assert a["max_tokens"] == 8000
    assert a["status"] == "enabled"


def test_update_agent(s):
    r = s.put(f"{API}/console/agents/srs.generate",
              json={"status": "disabled", "max_tokens": 512})
    assert r.status_code == 200
    after = s.get(f"{API}/console/agents/srs.generate").json()
    assert after["status"] == "disabled"
    assert after["max_tokens"] == 512
    # Restore
    s.put(f"{API}/console/agents/srs.generate", json={"status": "enabled", "max_tokens": 8000})


def test_reset_budget(s):
    r = s.post(f"{API}/console/agents/srs.generate/reset-budget")
    assert r.status_code == 200
    after = s.get(f"{API}/console/agents/srs.generate").json()
    assert after.get("tokens_used_all_time", 0) == 0


def test_usage_summary_shape(s):
    r = s.get(f"{API}/console/usage/summary")
    assert r.status_code == 200
    data = r.json()
    for key in ("total_tokens", "total_cost_usd", "by_stage", "by_agent", "by_model", "by_day"):
        assert key in data


# ── Prompt Preview ──────────────────────────────────────────────────
def test_prompt_preview(s, project_id):
    r = s.post(f"{API}/console/prompts/preview",
               json={"prompt_key": "srs.generate", "project_id": project_id})
    assert r.status_code == 200, r.text
    data = r.json()
    for key in ("resolved_template", "variables", "total_token_estimate",
                "cost_estimate_usd", "model_that_will_run"):
        assert key in data


def test_prompt_preview_unknown_key(s, project_id):
    r = s.post(f"{API}/console/prompts/preview",
               json={"prompt_key": "doesnt.exist.xyz", "project_id": project_id})
    assert r.status_code == 404


# ── Github token-only test (Part 11 Fix 1) ──────────────────────────
def test_github_test_token_only_valid_length(s):
    r = s.post(f"{API}/github/test",
               json={"repo_url": "", "token": "ghp_" + "a" * 36})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert "Token format valid" in body.get("message", "") or "format valid" in body.get("message", "").lower()


def test_github_test_token_too_short(s):
    r = s.post(f"{API}/github/test", json={"repo_url": "", "token": "abc"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert "Invalid token format" in body.get("error", "")
