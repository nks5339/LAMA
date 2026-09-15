"""iter-19 — provider pacing: sibling rotation, cooldown, concurrency.

The operator's log showed `tools.transformer.tester` in a 429 storm
against one Azure deployment: concurrent calls each hammering gpt-4.1,
each independently sleeping its own `Retry-After` (observed at 45-52s),
each then falling through to a local model with a 600s timeout.

Nothing in the fabric coordinated those calls. There was no semaphore, no
shared knowledge of a throttle, and no way to use the account's other nine
deployments — each of which carries its own quota bucket.

These tests pin the three mechanisms that replaced that, and in particular
the ordering rule that makes the difference: ROTATE FIRST, SLEEP LAST.
Sleeping is what stalls a wave; rotating is what completes it.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fabric.model_fabric as MF  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_pacing_state():
    """Cooldowns and semaphores are module-level and in-process, so a test
    must not inherit another's."""
    MF._DEPLOYMENT_COOLDOWN.clear()
    MF._PROVIDER_SEMAPHORES.clear()
    yield
    MF._DEPLOYMENT_COOLDOWN.clear()
    MF._PROVIDER_SEMAPHORES.clear()


# ── sibling rotation ──────────────────────────────────────────────────

def test_azure_tiers_have_siblings_to_rotate_to():
    sibs = MF.PROVIDER_PRESETS["azure"]["tier_siblings"]
    for tier in ("trivial", "low", "medium", "high", "critical", "reasoning"):
        assert sibs.get(tier), f"tier {tier} has no sibling to rotate to on a 429"


def test_a_tier_never_lists_itself_as_its_own_sibling():
    """Rotating to the deployment that just returned 429 would burn an
    attempt on a guaranteed repeat."""
    tiers = MF.PROVIDER_PRESETS["azure"]["default_models"]
    for tier, primary in tiers.items():
        assert primary not in MF.tier_siblings("azure", tier, primary)


def test_rotation_is_confined_to_deployments_the_operator_actually_has():
    """The preset lists what the vendor CAN deploy, not what this account
    HAS. Rotating onto a deployment the operator lacks turns a recoverable
    429 into a 404 (DeploymentNotFound) — strictly worse than the throttle
    we were routing around."""
    catalogue = [{"id": "gpt-4.1"}, {"id": "gpt-4o"}]   # no gpt-5-mini
    sibs = MF.tier_siblings("azure", "medium", "gpt-4.1", catalogue)
    assert sibs == ["gpt-4o"]


def test_an_empty_catalogue_means_unknown_not_nothing():
    """Several provider rows are created without a `models` list. Treating
    that as "no deployments available" would disable rotation entirely for
    them, which is the opposite of what an absent catalogue implies."""
    assert MF.tier_siblings("azure", "medium", "gpt-4.1", []) == ["gpt-4o", "gpt-5-mini"]
    assert MF.tier_siblings("azure", "medium", "gpt-4.1", None) == ["gpt-4o", "gpt-5-mini"]


def test_the_seeded_azure_catalogue_permits_every_sibling():
    """The shipped preset must be self-consistent: every sibling it names
    has to exist in the catalogue it ships, or rotation is dead on arrival
    for a default install."""
    preset = MF.PROVIDER_PRESETS["azure"]
    catalogue = list(preset["model_catalogue"])
    for tier, primary in preset["default_models"].items():
        declared = [s for s in preset["tier_siblings"].get(tier, []) if s != primary]
        assert MF.tier_siblings("azure", tier, primary, catalogue) == declared, tier


# ── context overflow: rotate up before leaving the provider ───────────

def test_overflow_rotates_to_the_roomiest_sibling():
    """A context overflow is not a throttle, but a sibling may still solve
    it — within `medium`, gpt-4.1 has ~8x the window of gpt-4o. Without
    this the call leaves the provider and lands on a local 32k model, which
    is the LEAST likely thing to fit a prompt that just overflowed 128k."""
    assert MF._roomiest_sibling("azure", "gpt-4o", ["gpt-5-mini", "gpt-4.1"]) == "gpt-4.1"


def test_no_rotation_when_nothing_is_roomier():
    """Rotating sideways or downward after an overflow reproduces it, so
    the call should leave the provider instead."""
    assert MF._roomiest_sibling("azure", "gpt-4.1", ["gpt-4o", "gpt-5-mini"]) == ""
    assert MF._roomiest_sibling("azure", "gpt-4o", []) == ""


def test_unknown_context_windows_do_not_trigger_a_rotation():
    """Published limits drift, so an unknown window must not be treated as
    0 and thus "anything is roomier"."""
    assert MF._roomiest_sibling("azure", "made-up-model", ["also-made-up"]) == ""


def test_context_window_reads_the_catalogue():
    assert MF._context_window("azure", "gpt-4.1") > MF._context_window("azure", "gpt-4o")
    assert MF._context_window("azure", "nope") == 0


def test_cooldown_map_sweeps_expired_entries():
    """A long-lived worker should not carry a row per model it has ever
    seen throttled."""
    for i in range(40):
        MF.mark_deployment_throttled("p", f"m{i}", 0.0)   # already expired
    MF.mark_deployment_throttled("p", "live", 30.0)
    assert len(MF._DEPLOYMENT_COOLDOWN) < 40
    assert MF.deployment_cooldown_remaining("p", "live") > 0


def test_siblings_are_empty_for_providers_where_rotation_cannot_help():
    """Rotating within a provider only helps when the alternates have
    SEPARATE quota. On OpenRouter the limit is per-account, so rotating
    models would hit the same ceiling and just waste a round-trip."""
    assert MF.tier_siblings("openrouter", "high", "anything") == []
    assert MF.tier_siblings("groq", "medium", "anything") == []


# ── the Azure URL carries the deployment ──────────────────────────────

def test_rotation_rewrites_the_azure_deployment_path():
    """Azure is the one provider where the model id is part of the URL. A
    rotation that changed only `payload["model"]` would keep calling the
    throttled deployment under a different name in the body."""
    url = "https://acct.openai.azure.com/openai/deployments/gpt-4.1"
    assert MF._swap_azure_deployment(url, "gpt-4.1", "gpt-4o") == (
        "https://acct.openai.azure.com/openai/deployments/gpt-4o"
    )


def test_rotation_leaves_non_azure_urls_alone():
    url = "https://api.openai.com/v1"
    assert MF._swap_azure_deployment(url, "gpt-4.1", "gpt-4o") == url


def test_rotation_rewrites_only_the_deployment_segment():
    """A gateway path that happens to repeat the model name elsewhere must
    not be mangled."""
    url = "https://gw/gpt-4.1/proxy/openai/deployments/gpt-4.1"
    out = MF._swap_azure_deployment(url, "gpt-4.1", "gpt-4o")
    assert out == "https://gw/gpt-4.1/proxy/openai/deployments/gpt-4o"


# ── shared cooldown ───────────────────────────────────────────────────

def test_cooldown_is_visible_to_other_callers():
    """The point of the shared map: one coroutine learns the deployment is
    throttled and every other coroutine routes around it, instead of each
    spending its own round-trip to discover the same thing."""
    assert MF.deployment_cooldown_remaining("p-azure", "gpt-4.1") == 0.0
    MF.mark_deployment_throttled("p-azure", "gpt-4.1", 30.0)
    assert 25.0 < MF.deployment_cooldown_remaining("p-azure", "gpt-4.1") <= 30.0
    # A sibling on the same provider is unaffected — separate quota bucket.
    assert MF.deployment_cooldown_remaining("p-azure", "gpt-4o") == 0.0
    # And so is the same model name on a different provider row.
    assert MF.deployment_cooldown_remaining("p-other", "gpt-4.1") == 0.0


def test_cooldown_expires():
    MF.mark_deployment_throttled("p-azure", "gpt-4.1", 0.0)
    assert MF.deployment_cooldown_remaining("p-azure", "gpt-4.1") == 0.0


# ── per-provider concurrency ──────────────────────────────────────────

def test_local_providers_get_a_smaller_gate(monkeypatch):
    """A local engine serves few concurrent generations and QUEUES the
    rest — and a queued request burns its own timeout while it waits, so
    over-fanning converts throughput into 600s timeouts."""
    monkeypatch.delenv("LAMA_PROVIDER_MAX_CONCURRENCY", raising=False)
    MF._PROVIDER_SEMAPHORES.clear()
    local = MF._provider_semaphore("p-ollama", is_local=True)
    cloud = MF._provider_semaphore("p-azure", is_local=False)
    assert local._value == MF._LOCAL_PROVIDER_CONCURRENCY
    assert cloud._value == MF._CLOUD_PROVIDER_CONCURRENCY
    assert local._value < cloud._value


def test_the_gate_is_per_provider_not_global():
    """A throttled cloud provider must not stop local fallback traffic."""
    a = MF._provider_semaphore("p-azure", is_local=False)
    b = MF._provider_semaphore("p-ollama", is_local=True)
    assert a is not b
    assert MF._provider_semaphore("p-azure", is_local=False) is a


def test_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("LAMA_PROVIDER_MAX_CONCURRENCY", "9")
    MF._PROVIDER_SEMAPHORES.clear()
    assert MF._provider_semaphore("p-ollama", is_local=True)._value == 9


def test_a_bad_env_value_does_not_crash_the_call(monkeypatch):
    monkeypatch.setenv("LAMA_PROVIDER_MAX_CONCURRENCY", "not-a-number")
    MF._PROVIDER_SEMAPHORES.clear()
    assert MF._provider_semaphore("p-azure", is_local=False)._value == MF._CLOUD_PROVIDER_CONCURRENCY


@pytest.mark.asyncio
async def test_the_gate_actually_bounds_in_flight_calls(monkeypatch):
    """Not just the arithmetic — the semaphore must really cap concurrency,
    since that is the whole mechanism keeping a wave from stampeding one
    endpoint."""
    monkeypatch.setenv("LAMA_PROVIDER_MAX_CONCURRENCY", "2")
    MF._PROVIDER_SEMAPHORES.clear()
    peak = {"n": 0, "cur": 0}

    async def worker():
        async with MF._provider_semaphore("p-azure", is_local=False):
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
            await asyncio.sleep(0.01)
            peak["cur"] -= 1

    await asyncio.gather(*[worker() for _ in range(10)])
    assert peak["n"] <= 2, f"{peak['n']} calls were in flight against one provider"
