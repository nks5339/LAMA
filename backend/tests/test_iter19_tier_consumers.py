"""iter-19 — every consumer of `routing` must walk the tier ladder.

Widening the tier vocabulary from three to six created a class of bug that
is invisible in the fabric itself: code elsewhere reads `routing[tier]`
with a bare dict lookup. Against a provider row written before iter-19 —
which has only low/medium/high — that returns "" for the three new tiers,
and every such site then reports or pings the wrong thing.

Each test below pins one site that got it wrong. They are grouped here
rather than with the fabric tests because the defect is not in
`resolve_tier_model`; it is in remembering to call it.
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from fabric.model_fabric import (  # noqa: E402
    AZURE_API_VERSION_DEFAULT,
    PROVIDER_PRESETS,
    TIER_ORDER,
    resolve_tier_model,
)

BACKEND = Path(__file__).resolve().parent.parent
LEGACY_ROW = {"low": "cheap", "medium": "mid", "high": "strong"}


# ── the Console must not show a blank model for a real route ──────────

def test_console_agent_list_resolves_new_tiers_on_a_legacy_row():
    """`/api/console/agents` computes `resolved_model` for display. With a
    bare lookup it showed BLANK for every agent on trivial/critical/
    reasoning — so the Console claimed the DevOps agent had no model while
    it was routing perfectly well."""
    for tier in TIER_ORDER:
        assert resolve_tier_model(LEGACY_ROW, tier), f"tier {tier} resolved to nothing"


def test_console_agent_list_uses_the_ladder():
    """Guard the call site itself, not just the helper: a future edit that
    reintroduces `routing.get(complexity)` would pass the test above."""
    src = (BACKEND / "routes" / "console.py").read_text()
    assert "resolve_tier_model(" in src
    assert not re.search(r'routing"?\s*\)?\s*or\s*\{\}\)\.get\(\s*a\.get\("complexity"', src), (
        "console.py is reading routing[complexity] directly again"
    )


# ── the Test Connection button must match what really runs ────────────

def test_provider_validate_shares_one_api_version_default():
    """`/providers/validate/{id}`'s own comment says it mirrors
    resolve_model "exactly", because its whole contract is telling the
    operator whether real calls will work. Two literals drifted apart once
    already — resolve_model moved to 2024-12-01-preview while the button
    stayed on 2024-02-15-preview, which would pass against gpt-4.1 and then
    fail against gpt-5.1."""
    src = (BACKEND / "routes" / "console.py").read_text()
    assert "AZURE_API_VERSION_DEFAULT" in src
    assert "2024-02-15-preview" not in src, "a stale api-version literal is back"


def test_test_connection_shapes_the_payload_for_the_deployment_not_the_body_model():
    """On Azure the URL names the deployment, so the deployment decides
    which parameters are legal — the `model` string in the body is
    decorative.

    With `azure_deployment=gpt-5.1` and a cheap-tier `model_id`, this
    endpoint shaped the payload for the CHEAP model (max_tokens,
    temperature=0.1) and posted it to the gpt-5.1 deployment, which rejects
    both. Verified live: it returned `ok:false` with
    "Unsupported parameter: 'max_tokens'" against a perfectly healthy
    account, and reported `model_used` as a model it had never called.
    """
    src = (BACKEND / "routes" / "console.py").read_text()
    assert "wire_model" in src, "the endpoint no longer tracks what actually answers"
    for call in ("apply_token_limit(payload, wire_model", "apply_temperature(payload, wire_model"):
        assert call in src, f"payload is not shaped for the wire model: {call}"
    assert '"model_used": model_id' not in src, (
        "model_used must report the deployment that answered, not the body field"
    )


def test_test_connection_does_not_hardcode_a_temperature():
    """`temperature: 0.1` was a literal in the payload dict, so the button
    400'd against every reasoning deployment regardless of tier."""
    src = (BACKEND / "routes" / "console.py").read_text()
    assert '"temperature": 0.1}' not in src


def test_the_shared_default_can_serve_reasoning_deployments():
    """The value itself has to be new enough. 2024-02-15-preview predates
    gpt-5.x, the o-series and response_format=json_object."""
    assert AZURE_API_VERSION_DEFAULT >= "2024-12-01-preview"


# ── the seed must not rebuild routing with fewer keys ─────────────────

def test_seed_preserves_every_tier_when_azure_deployment_is_set():
    """`seed_providers` rebuilt the routing dict from a hardcoded
    ("low","medium","high") tuple, silently DROPPING the three new tiers on
    a fresh install that has AZURE_DEPLOYMENT set. The backfill migration
    repaired it on the next boot, which hid the bug — the seed should not
    be writing a row the migration has to clean up."""
    routing = dict(PROVIDER_PRESETS["azure"]["default_models"])
    az_deploy = "gpt-5.1"
    # The expression as it now appears in seed_providers.
    routing = {t: (routing.get(t) or az_deploy) for t in routing}
    assert set(routing) == set(TIER_ORDER)


def test_seed_source_has_no_hardcoded_three_tier_tuple():
    src = (BACKEND / "seed.py").read_text()
    assert '("low", "medium", "high")' not in src, (
        "seed.py is rebuilding routing from a hardcoded 3-tier tuple again"
    )


# ── the stored model default ──────────────────────────────────────────

def test_a_new_provider_row_declares_all_six_tiers():
    """A row created through the API (not the seed) should present all six
    selects in the Console, not three."""
    from models import ModelProvider
    row = ModelProvider(name="x", provider_type="custom", base_url="", api_key="")
    assert set(row.routing) == set(TIER_ORDER)


@pytest.mark.parametrize("tier", TIER_ORDER)
def test_agent_complexity_accepts_every_tier(tier):
    """`complexity` is deliberately a plain str, not an Enum: a stored value
    the code no longer recognises must degrade through the tier ladder
    rather than 500 on read."""
    from models import AgentConfig
    a = AgentConfig(key="k", agent_type="task", stage="Tools", label="L", complexity=tier)
    assert a.complexity == tier
