"""
iter-18.1 / 18.2 — Journey-derived task division + Azure/Ollama seeding.

18.1  Envelopes come from `kb_journeys` (the deterministic graph walk)
      before falling back to `arch_services`. The journey carries the
      inherited chain for one route -- controller -> service ->
      repository -> tables -> columns, plus roles and business-rule ids.
      The BR ids matter: the traceability gate scores
      `envelope.br_ids & task.br_ids`, so the old always-empty `br_ids`
      made it vacuously 100%.

18.2  Azure is seeded primary and Ollama the fallback, WITHOUT ever
      overwriting what the operator already configured.
"""
import os
import sys
from typing import Any, Dict, List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes import codegen as CG  # noqa: E402


# ── 18.1 journey envelopes ────────────────────────────────────────────
JOURNEY = {
    "journey_id": "AJ-post-api-register",
    "kind": "api",
    "pivot_route": "POST /api/register",
    "classes_touched": ["AuthCtrl", "UserService", "UserRepo"],
    "tables": ["users"],
    "columns": ["users.email_id", "users.password_hash"],
    "roles": ["guest"],
    "business_rules": ["BR-AUTH-001", "BR-AUTH-002"],
    "trace": [{"name": "AuthCtrl.register"}, {"name": "UserService.create"}],
}


def test_pivot_route_split():
    assert CG._split_pivot_route("POST /api/register") == ("POST", "/api/register")
    assert CG._split_pivot_route("/api/health") == ("ANY", "/api/health")
    assert CG._split_pivot_route("") == ("ANY", "")


def _stub_journeys(monkeypatch, journeys, enabled=True):
    import kb.journey_config as JC
    import kb.journey_materializer as JM
    async def _enabled(_pid): return enabled
    async def _load(_pid, **_kw): return list(journeys)
    monkeypatch.setattr(JC, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(JM, "load_journeys", _load)


@pytest.mark.asyncio
async def test_journey_envelope_inherits_the_graph_chain(monkeypatch):
    _stub_journeys(monkeypatch, [JOURNEY])
    envs = await CG._journey_codegen_envelopes("p1")

    assert len(envs) == 1
    e = envs[0]
    assert e["envelope_id"] == "AJ-post-api-register"
    assert (e["endpoint_method"], e["endpoint_path"]) == ("POST", "/api/register")
    # the inheritance chain, in order
    assert e["controller_class"] == "AuthCtrl"
    assert e["service_class"] == "UserService"
    assert e["repository_class"] == "UserRepo"
    assert e["db_tables"] == ["users"]
    assert e["side"] == "backend"


@pytest.mark.asyncio
async def test_journey_envelope_carries_br_ids(monkeypatch):
    """The whole point — the old arch_services path always sent []."""
    _stub_journeys(monkeypatch, [JOURNEY])
    envs = await CG._journey_codegen_envelopes("p1")
    assert envs[0]["br_ids"] == ["BR-AUTH-001", "BR-AUTH-002"]
    assert any("BR-AUTH-001" in c for c in envs[0]["acceptance_criteria"])


@pytest.mark.asyncio
async def test_ui_journey_becomes_a_frontend_envelope(monkeypatch):
    ui = dict(JOURNEY, journey_id="UJ-register-form", kind="ui",
              ui_fields=["email", "password"])
    _stub_journeys(monkeypatch, [ui])
    e = (await CG._journey_codegen_envelopes("p1"))[0]
    assert e["side"] == "frontend" and e["layer"] == "page"
    assert any("email" in c for c in e["acceptance_criteria"])


@pytest.mark.asyncio
async def test_column_journeys_are_not_codegen_work(monkeypatch):
    """`column` journeys belong to DataModel, not CodeGen."""
    _stub_journeys(monkeypatch, [dict(JOURNEY, kind="column", journey_id="CJ-x")])
    assert await CG._journey_codegen_envelopes("p1") == []


@pytest.mark.asyncio
async def test_disabled_toggle_falls_through(monkeypatch):
    _stub_journeys(monkeypatch, [JOURNEY], enabled=False)
    assert await CG._journey_codegen_envelopes("p1") == []


@pytest.mark.asyncio
async def test_graph_failure_never_blocks_codegen(monkeypatch):
    import kb.journey_config as JC
    async def _boom(_pid): raise RuntimeError("graph missing")
    monkeypatch.setattr(JC, "is_journey_kb_enabled", _boom)
    assert await CG._journey_codegen_envelopes("p1") == []


def test_journey_kb_now_defaults_on(monkeypatch):
    import kb.journey_config as JC
    monkeypatch.delenv("LAMA_USE_JOURNEY_KB", raising=False)
    assert JC._env_default_enabled() is True
    monkeypatch.setenv("LAMA_USE_JOURNEY_KB", "0")
    assert JC._env_default_enabled() is False


# ── 18.2 provider seeding ─────────────────────────────────────────────
class FakeProviders:
    def __init__(self, rows=None): self.rows: List[Dict[str, Any]] = list(rows or [])
    def find(self, *_a, **_kw):
        rows = self.rows
        class _C:
            async def to_list(self, _n=None): return [dict(r) for r in rows]
        return _C()
    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        class R: inserted_id = doc.get("id")
        return R()


def _seed_env(monkeypatch, **kw):
    for k in ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_ENDPOINT",
              "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT", "AZURE_API_VERSION",
              "LAMA_OLLAMA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


@pytest.mark.asyncio
async def test_seeds_azure_primary_and_ollama_fallback(monkeypatch):
    import seed as S
    _seed_env(monkeypatch, AZURE_API_KEY="k", AZURE_ENDPOINT="https://x.openai.azure.com",
              AZURE_DEPLOYMENT="gpt-4o")
    fake = FakeProviders()
    monkeypatch.setattr("db.model_providers", fake, raising=False)
    import db as _db
    monkeypatch.setattr(_db, "model_providers", fake, raising=False)

    await S.seed_providers()

    by_type = {r["provider_type"]: r for r in fake.rows}
    assert set(by_type) == {"azure", "ollama"}
    assert by_type["azure"]["is_default"] is True, "Azure is the primary"
    assert by_type["azure"]["is_active"] is True, "credentials present -> active"
    assert by_type["azure"]["priority"] < by_type["ollama"]["priority"]
    assert by_type["ollama"]["is_active"] is True, "local needs no key"
    assert by_type["ollama"]["is_default"] is False


@pytest.mark.asyncio
async def test_azure_without_credentials_is_seeded_inactive(monkeypatch):
    """An active provider with no key reproduces the documented 401 cascade."""
    import seed as S
    _seed_env(monkeypatch)
    fake = FakeProviders()
    import db as _db
    monkeypatch.setattr(_db, "model_providers", fake, raising=False)

    await S.seed_providers()
    az = [r for r in fake.rows if r["provider_type"] == "azure"][0]
    assert az["is_active"] is False


@pytest.mark.asyncio
async def test_operator_configuration_is_never_overwritten(monkeypatch):
    """The contract: existing rows untouched, existing default not stolen."""
    import seed as S
    _seed_env(monkeypatch, AZURE_API_KEY="k", AZURE_ENDPOINT="https://x")
    mine = {"id": "p-own", "provider_type": "anthropic", "name": "My Anthropic",
            "is_default": True, "is_active": True, "api_key": "sk-ant-secret"}
    fake = FakeProviders([mine])
    import db as _db
    monkeypatch.setattr(_db, "model_providers", fake, raising=False)

    await S.seed_providers()

    still = [r for r in fake.rows if r["id"] == "p-own"][0]
    assert still == mine, "an existing provider must not be edited at all"
    az = [r for r in fake.rows if r["provider_type"] == "azure"][0]
    assert az["is_default"] is False, "the operator's default is not stolen"


@pytest.mark.asyncio
async def test_seeding_is_idempotent(monkeypatch):
    import seed as S
    _seed_env(monkeypatch, AZURE_API_KEY="k", AZURE_ENDPOINT="https://x")
    fake = FakeProviders()
    import db as _db
    monkeypatch.setattr(_db, "model_providers", fake, raising=False)

    await S.seed_providers()
    await S.seed_providers()
    await S.seed_providers()
    assert len(fake.rows) == 2, "re-running startup must not duplicate providers"
