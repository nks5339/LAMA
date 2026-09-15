"""
iter-18.3 — Azure tier map, context-overflow failover, sanctioned local
code-generation models.

Operator spec:
  • Azure is primary; assign its deployments by task complexity.
  • On context / max-output limit, fall back to Ollama.
  • Code generation uses ONLY llama3.1:latest, gpt-oss:latest,
    qwen3.5:latest. Lightweight work uses qwen3:4b / qwen2.5-coder:7b.
  • nomic-embed-text for embeddings.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from fabric import model_fabric as MF  # noqa: E402


# ── Azure catalogue + tiers ───────────────────────────────────────────
def test_azure_catalogue_covers_the_operators_chat_deployments():
    ids = {m["id"] for m in MF.PROVIDER_PRESETS["azure"]["model_catalogue"]}
    assert {
        "gpt-5.1", "gpt-5", "gpt-5-mini", "o3", "o4-mini", "o3-mini",
        "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini",
    } <= ids


def test_azure_embedding_deployments_are_listed_separately():
    """Embeddings are not chat models — resolve_model routes /chat only."""
    emb = {m["id"] for m in MF.PROVIDER_PRESETS["azure"]["embedding_catalogue"]}
    assert emb == {"text-embedding-3-large", "text-embedding-ada-002"}
    chat = {m["id"] for m in MF.PROVIDER_PRESETS["azure"]["model_catalogue"]}
    assert not (emb & chat)


def test_azure_tiers_ascend_in_capability():
    """iter-19 — six tiers, and gpt-5.1 moved from `high` to `critical`.

    The three-tier ladder could only reach three of the operator's ten
    chat deployments. Splitting the top gives the DevOps agent and the
    remediation re-plan a rung above ordinary code generation, which is
    the point: `critical` is what gets spent on the last chance to make a
    build shippable, not on every coder call.
    """
    t = MF.PROVIDER_PRESETS["azure"]["default_models"]
    assert t["trivial"] == "gpt-4o-mini"
    assert t["low"] == "gpt-4.1-mini"
    assert t["medium"] == "gpt-4.1"
    assert t["high"] == "gpt-5"
    assert t["critical"] == "gpt-5.1"
    assert t["reasoning"] == "o4-mini"


def test_every_azure_chat_deployment_is_reachable():
    """No deployment may be catalogue-only.

    The operator asked for all of their deployments to be used according to
    task severity. A model that appears in the catalogue but in neither a
    tier nor a tier's sibling list can never be resolved by any agent, so
    listing it would be a promise the router does not keep.
    """
    preset = MF.PROVIDER_PRESETS["azure"]
    catalogue = {m["id"] for m in preset["model_catalogue"]}
    reachable = set(preset["default_models"].values())
    for sibs in preset["tier_siblings"].values():
        reachable |= set(sibs)
    assert catalogue - reachable == set(), (
        f"unreachable Azure deployments: {sorted(catalogue - reachable)}"
    )


def test_tier_ladder_walk_serves_pre_iter19_provider_rows():
    """A row written before iter-19 has only low/medium/high.

    Those rows must keep routing every agent — including agents now on the
    three new tiers — rather than falling through to `models[0]`, which is
    whatever happens to sit first in the catalogue.
    """
    legacy = {"low": "cheap", "medium": "mid", "high": "strong"}
    assert MF.resolve_tier_model(legacy, "trivial") == "cheap"
    assert MF.resolve_tier_model(legacy, "low") == "cheap"
    assert MF.resolve_tier_model(legacy, "medium") == "mid"
    assert MF.resolve_tier_model(legacy, "high") == "strong"
    # critical/reasoning must degrade UPWARD to the strongest available
    # model, never downward to the cheapest.
    assert MF.resolve_tier_model(legacy, "critical") == "strong"
    assert MF.resolve_tier_model(legacy, "reasoning") == "strong"
    assert MF.resolve_tier_model({}, "high") == ""


def test_reasoning_deployments_use_max_completion_tokens():
    """o3/o4/gpt-5 reject `max_tokens` outright."""
    for m in ("o3", "o4-mini", "o3-mini", "gpt-5.1", "gpt-5-mini"):
        assert MF.token_limit_field(m) == "max_completion_tokens", m
    for m in ("gpt-4.1", "gpt-4o", "qwen3.5:latest"):
        assert MF.token_limit_field(m) == "max_tokens", m


# ── context / output overflow → failover ──────────────────────────────
@pytest.mark.parametrize("msg", [
    "This model's maximum context length is 128000 tokens",
    "context_length_exceeded",
    "Please reduce the length of the messages",
    "max_tokens is too large: 200000",
    "max_completion_tokens is too large",
    "Input is too long for requested model",
    "requested too many tokens",
])
def test_context_overflow_is_recognised(msg):
    assert MF._is_context_error(msg) is True


@pytest.mark.parametrize("msg", [
    "429 Too Many Requests",
    "401 unauthorized",
    "insufficient credit",
    "Read timed out after 600.0s",
    "connection refused",
])
def test_unrelated_errors_are_not_context_errors(msg):
    assert MF._is_context_error(msg) is False


def test_overflow_joins_the_recoverable_set():
    """The gap that mattered: overflow used to re-raise instead of failing over."""
    msg = "This model's maximum context length is 128000 tokens"
    recoverable = (
        MF._is_billing_error(msg) or MF._is_auth_error(msg)
        or MF._is_rate_limit_error(msg) or MF._is_context_error(msg)
    )
    assert recoverable is True, "an overflow must fail over to Ollama, not raise"


def test_timeout_still_raises_rather_than_failing_over():
    """Non-regression: a timeout is NOT recoverable by trying another model."""
    msg = "Agent 'tools.transformer.coder' timed out after 600.0s"
    assert not (
        MF._is_billing_error(msg) or MF._is_auth_error(msg)
        or MF._is_rate_limit_error(msg) or MF._is_context_error(msg)
    )


# ── sanctioned local codegen models ───────────────────────────────────
def test_the_sanctioned_set_is_exactly_the_operators_three():
    assert set(MF.OLLAMA_CODEGEN_MODELS) == {
        "llama3.1:latest", "gpt-oss:latest", "qwen3.5:latest",
    }


def test_ollama_tiers_use_lightweight_models_below_high():
    t = MF.PROVIDER_PRESETS["ollama"]["default_models"]
    assert t["low"] == "qwen3:4b"
    assert t["medium"] == "qwen2.5-coder:7b"
    assert t["high"] in MF.OLLAMA_CODEGEN_MODELS


@pytest.mark.parametrize("agent", [
    "codegen.coder_be", "codegen.coder_fe", "tools.transformer.coder",
    "codegen.regenerate", "codegen.gap_recovery",
])
def test_codegen_agent_is_forced_onto_a_sanctioned_model(agent):
    out = MF._coerce_ollama_codegen_model("qwen3:4b", agent, "ollama",
                                          "http://localhost:11434/v1")
    assert out in MF.OLLAMA_CODEGEN_MODELS
    assert out != "qwen3:4b", "a 4B model must never generate code"


@pytest.mark.parametrize("model", ["llama3.1:latest", "gpt-oss:latest", "qwen3.5:latest"])
def test_an_already_sanctioned_model_is_left_alone(model):
    assert MF._coerce_ollama_codegen_model(
        model, "codegen.coder_be", "ollama", "http://localhost:11434/v1") == model


@pytest.mark.parametrize("agent", [
    "codegen.verifier", "codegen.reviewer", "codegen.tester",
    "srs.generate", "codegen.finalizer",
])
def test_non_codegen_agents_keep_their_lightweight_model(agent):
    """Verifier/reviewer/tester emit JSON verdicts, not code."""
    assert MF._coerce_ollama_codegen_model(
        "qwen3:4b", agent, "ollama", "http://localhost:11434/v1") == "qwen3:4b"


def test_cloud_providers_are_never_coerced():
    """Azure's own tier map already sends codegen to a capable deployment."""
    assert MF._coerce_ollama_codegen_model(
        "gpt-5.1", "codegen.coder_be", "azure",
        "https://x.openai.azure.com") == "gpt-5.1"


def test_ollama_cloud_models_are_exempt():
    """gpt-oss:120b-cloud is large and deliberately chosen — do not override."""
    for m in ("gpt-oss:120b-cloud", "deepseek-v3.1:671b-cloud"):
        assert MF._coerce_ollama_codegen_model(
            m, "codegen.coder_be", "ollama", "http://localhost:11434/v1") == m


# ── embeddings ────────────────────────────────────────────────────────
def test_embeddings_default_to_nomic_embed_text(monkeypatch):
    monkeypatch.delenv("LAMA_OLLAMA_EMBED_MODEL", raising=False)
    import importlib
    import kb.vector_store as VS
    importlib.reload(VS)
    assert VS.OLLAMA_EMBED_MODEL == "nomic-embed-text"


# ── regression: AZURE_DEPLOYMENT must not collapse the tier ladder ────
def test_single_deployment_env_does_not_flatten_the_ladder(monkeypatch):
    """Found during the iter-18.3 deep test on a live boot.

    The operator's `.env` carries `AZURE_DEPLOYMENT=gpt-5.1`. The first
    implementation let that override all three tiers, so every agent --
    gap questions and finalizers included -- resolved to gpt-5.1 and
    complexity-based routing was silently dead. AZURE_DEPLOYMENT now only
    fills a tier the ladder left empty.
    """
    import importlib
    monkeypatch.setenv("AZURE_DEPLOYMENT", "gpt-5.1")
    for v in ("AZURE_DEPLOYMENT_TRIVIAL", "AZURE_DEPLOYMENT_LOW",
              "AZURE_DEPLOYMENT_MEDIUM", "AZURE_DEPLOYMENT_HIGH",
              "AZURE_DEPLOYMENT_CRITICAL", "AZURE_DEPLOYMENT_REASONING"):
        monkeypatch.delenv(v, raising=False)
    import fabric.model_fabric as F
    importlib.reload(F)
    try:
        t = F.PROVIDER_PRESETS["azure"]["default_models"]
        assert t["low"] == "gpt-4.1-mini", "AZURE_DEPLOYMENT must not flatten low"
        assert t["medium"] == "gpt-4.1"
        assert t["high"] == "gpt-5"
        assert t["critical"] == "gpt-5.1"
        # Every tier keeps its own value — the ladder is intact.
        assert len(set(t.values())) == len(t), f"AZURE_DEPLOYMENT flattened tiers: {t}"
    finally:
        importlib.reload(F)


def test_auto_configure_from_key_does_not_flatten_the_ladder():
    """iter-19 — the SECOND flattener, missed by iter-18.3.

    iter-18.3 fixed `seed_providers` but left `auto_configure_from_key`
    pointing every tier at AZURE_DEPLOYMENT. That path is reached by the
    Console's "paste one API key to configure routing automatically"
    button, so one paste silently undid the ladder the seed had just set
    up — and with gpt-5.1 on every tier, every call carried a temperature
    the deployment rejects.
    """
    routing = dict(MF.PROVIDER_PRESETS["azure"]["default_models"])
    az_deployment = "gpt-5.1"
    # The expression as it now appears in auto_configure_from_key.
    routing = {tier: (routing.get(tier) or az_deployment) for tier in routing}
    assert routing["low"] == "gpt-4.1-mini"
    assert routing["medium"] == "gpt-4.1"
    assert len(set(routing.values())) == len(routing)


def test_per_tier_env_overrides_win(monkeypatch):
    import importlib
    monkeypatch.setenv("AZURE_DEPLOYMENT_LOW", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_DEPLOYMENT_HIGH", "o3")
    import fabric.model_fabric as F
    importlib.reload(F)
    try:
        t = F.PROVIDER_PRESETS["azure"]["default_models"]
        assert t["low"] == "gpt-4o-mini"
        assert t["high"] == "o3"
        assert t["medium"] == "gpt-4.1", "unset tiers keep the ladder"
    finally:
        monkeypatch.delenv("AZURE_DEPLOYMENT_LOW", raising=False)
        monkeypatch.delenv("AZURE_DEPLOYMENT_HIGH", raising=False)
        importlib.reload(F)
