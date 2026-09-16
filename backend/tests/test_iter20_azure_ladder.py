"""iter-20 — spend the paid Azure account before touching a local model.

Two defects the operator reported, in one mechanism:

  * gpt-5.1 "was never seen in the picture". The Coder ran at tier `high`,
    which on their Azure account resolves to gpt-5; gpt-5.1 sits at
    `critical` and was only ever reachable as a 429 rotation target.
  * a 429 storm. `tier_siblings` rotates within a tier, but once the tier
    was exhausted the call slept and then left the provider entirely — so
    every one of hundreds of in-flight calls paid its own round-trip and
    Retry-After to rediscover that the account was throttled.

`exhaustion_ladder` is the step between those: descend the whole account,
strongest first, and only park the provider (skipping it with no HTTP at
all) once every deployment on it is cooling.
"""
from __future__ import annotations

import os
import time

# `seed` imports `db`, which reads MONGO_URL at import time. Nothing here
# talks to Mongo — the assertions are over module constants — so a default
# is enough. Same pattern as test_iter18_providers_and_journeys.py.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

import pytest  # noqa: E402

from fabric.model_fabric import (  # noqa: E402
    AGENT_COMPLEXITY,
    PROVIDER_PRESETS,
    _ladder_below,
    clear_provider_park,
    deployment_cooldown_remaining,
    exhaustion_ladder,
    mark_deployment_throttled,
    mark_provider_parked,
    provider_park_remaining,
    reset_provider_parks,
)


@pytest.fixture(autouse=True)
def _clean_park_state():
    reset_provider_parks()
    yield
    reset_provider_parks()


# ── The ladder itself ─────────────────────────────────────────────────

def test_azure_ladder_is_ordered_strongest_first():
    ladder = exhaustion_ladder("azure")
    assert ladder[0] == "gpt-5.1"
    assert ladder.index("gpt-5") < ladder.index("gpt-4.1")
    assert ladder.index("gpt-4.1") < ladder.index("gpt-4o")
    # every mini sits below every full-size model
    first_mini = min(i for i, m in enumerate(ladder) if "mini" in m)
    last_full = max(i for i, m in enumerate(ladder) if "mini" not in m)
    assert last_full < first_mini


def test_ladder_is_confined_to_the_operators_own_deployments():
    """The preset lists what Azure CAN deploy, not what this account HAS.
    Rotating onto a deployment they lack turns a recoverable throttle into
    a DeploymentNotFound — strictly worse than the throttle."""
    catalogue = [{"id": "gpt-5"}, {"id": "gpt-4o"}]
    assert exhaustion_ladder("azure", catalogue) == ["gpt-5", "gpt-4o"]


def test_an_empty_catalogue_means_unknown_not_nothing():
    """Several provider rows are created without a `models` list; treating
    that as "no deployments" would disable the ladder entirely."""
    assert exhaustion_ladder("azure", []) == exhaustion_ladder("azure")
    assert exhaustion_ladder("azure", None) == exhaustion_ladder("azure")


def test_providers_without_a_ladder_get_an_empty_one():
    """Rotating on a provider with one shared upstream quota bucket would
    just reproduce the same limit."""
    assert exhaustion_ladder("openrouter") == []
    assert exhaustion_ladder("anthropic") == []


# ── Descent position ──────────────────────────────────────────────────

def test_descent_starts_below_the_current_rung():
    """An agent already on gpt-5 must not 'descend' to gpt-5.1 — it either
    just came from there or was never entitled to it."""
    ladder = exhaustion_ladder("azure")
    below = _ladder_below(ladder, "gpt-5", [])
    assert "gpt-5.1" not in below
    assert "gpt-5" not in below
    assert below[0] == "gpt-4.1"


def test_descent_skips_the_siblings_already_tried():
    """Rotation and descent are separate remedies with separate budgets;
    re-offering a sibling would spend the descent on a known-throttled
    deployment."""
    ladder = exhaustion_ladder("azure")
    below = _ladder_below(ladder, "gpt-5", ["gpt-4.1"])
    assert "gpt-4.1" not in below
    assert "gpt-4o" in below


def test_an_unknown_current_model_offers_the_whole_ladder():
    """An operator-named deployment gives us no position to descend from,
    and any free deployment still beats leaving the paid account."""
    below = _ladder_below(exhaustion_ladder("azure"), "my-custom-deploy", [])
    assert below == exhaustion_ladder("azure")


def test_descent_is_empty_when_the_ladder_is():
    assert _ladder_below([], "gpt-5", []) == []


# ── Park / probe ──────────────────────────────────────────────────────

def test_a_park_is_floored_so_it_beats_the_deployment_cooldown():
    """A 5s park saves nothing the per-deployment cooldown already does."""
    applied = mark_provider_parked("az1", 5.0)
    assert applied == 60.0
    assert 55.0 < provider_park_remaining("az1") <= 60.0


def test_a_park_honours_a_longer_retry_after():
    applied = mark_provider_parked("az1", 300.0)
    assert applied == 300.0


def test_consecutive_parks_double_and_are_capped():
    """An account that keeps refusing should be probed less often, not
    hammered — but never parked indefinitely."""
    first = mark_provider_parked("az1", 60.0)
    second = mark_provider_parked("az1", 60.0)
    third = mark_provider_parked("az1", 60.0)
    assert first == 60.0
    assert second == 120.0
    assert third == 240.0
    for _ in range(10):
        last = mark_provider_parked("az1", 60.0)
    assert last == 900.0


def test_an_unparked_provider_reports_zero():
    assert provider_park_remaining("never-parked") == 0.0


def test_a_successful_call_clears_the_park_and_the_streak():
    """The call after the window is a probe: success must return ALL
    traffic to the paid account, and reset the escalation so the next
    incident starts at the short floor."""
    mark_provider_parked("az1", 60.0)
    mark_provider_parked("az1", 60.0)      # streak -> 2
    clear_provider_park("az1")
    assert provider_park_remaining("az1") == 0.0
    assert mark_provider_parked("az1", 60.0) == 60.0   # back to the floor


def test_an_expired_park_lets_the_next_call_through(monkeypatch):
    mark_provider_parked("az1", 60.0)
    assert provider_park_remaining("az1") > 0
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61.0)
    assert provider_park_remaining("az1") == 0.0


def test_an_expired_park_keeps_the_streak_so_a_failed_probe_backs_off(monkeypatch):
    """If the probe fails, the park must resume at the LONGER interval —
    restarting at the floor would reproduce the storm at low frequency."""
    mark_provider_parked("az1", 60.0)      # streak 1, 60s
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61.0)
    assert provider_park_remaining("az1") == 0.0   # window over, probe now
    # probe 429s -> park again; must be 120s, not 60s.
    assert mark_provider_parked("az1", 60.0) == 120.0


def test_park_state_is_per_provider():
    mark_provider_parked("az1", 60.0)
    assert provider_park_remaining("az2") == 0.0


# ── Deployment cooldown still works alongside it ──────────────────────

def test_deployment_cooldown_is_independent_of_the_park():
    mark_deployment_throttled("az1", "gpt-5.1", 30.0)
    assert deployment_cooldown_remaining("az1", "gpt-5.1") > 0
    assert deployment_cooldown_remaining("az1", "gpt-5") == 0.0
    assert provider_park_remaining("az1") == 0.0


# ── Tiering: the agents that write code start at the top ──────────────

@pytest.mark.parametrize("agent", [
    "tools.transformer.coder",
    "tools.transformer.verifier",
    "tools.transformer.planner",
    "tools.transformer.devops_expert",
    "tools.transformer.devops_audit",
    "codegen.coder_be",
    "codegen.coder_fe",
])
def test_code_writing_agents_start_at_the_top_of_the_ladder(agent):
    """`critical` is what reaches gpt-5.1 on this account. The Coder sat at
    `high` (gpt-5), which is why the flagship never appeared in a run."""
    assert AGENT_COMPLEXITY[agent] == "critical"


@pytest.mark.parametrize("agent", [
    "tools.transformer.tester",
    "tools.transformer.super_agent",
    "tools.transformer.context_manager",
    "tools.transformer.validator",
    "codegen.build_tool_selector",
    "codegen.finalizer",
])
def test_light_agents_are_not_promoted(agent):
    """Starting a classification or gate call on the flagship drains the
    quota the Coder needs and brings the exhaustion point forward."""
    assert AGENT_COMPLEXITY[agent] not in ("critical", "reasoning")


def test_critical_tier_resolves_to_the_flagship_on_azure():
    assert PROVIDER_PRESETS["azure"]["default_models"]["critical"] == "gpt-5.1"


def test_every_ladder_entry_is_a_real_deployment_in_the_catalogue():
    """A typo here would rotate onto a 404 on every account that has not
    curated its `models` list."""
    known = {m["id"] for m in PROVIDER_PRESETS["azure"]["model_catalogue"]}
    for model in PROVIDER_PRESETS["azure"]["exhaustion_ladder"]:
        assert model in known, model


def test_every_tier_sibling_is_also_on_the_ladder_or_a_reasoning_model():
    """The two mechanisms must agree about what exists on the account."""
    ladder = set(PROVIDER_PRESETS["azure"]["exhaustion_ladder"])
    for tier, sibs in PROVIDER_PRESETS["azure"]["tier_siblings"].items():
        for s in sibs:
            if tier == "reasoning" or s.startswith("o"):
                continue   # o-series is a sideways step, not a ladder rung
            assert s in ladder, f"{tier} sibling {s} is not on the ladder"


def test_default_tier_models_are_ladder_entries_or_reasoning():
    dm = PROVIDER_PRESETS["azure"]["default_models"]
    ladder = set(PROVIDER_PRESETS["azure"]["exhaustion_ladder"])
    for tier, model in dm.items():
        if tier == "reasoning":
            continue
        assert model in ladder, f"{tier} -> {model} is not on the ladder"


# ── The migration that makes this reach a live install ────────────────

def test_the_tier_promotion_is_migrated_not_just_declared():
    """`resolve_model` reads `agent_configs.complexity` BEFORE
    AGENT_COMPLEXITY, so the code change alone would have no effect on the
    operator's existing database — the one that reported the problem."""
    import seed

    keys = {s["key"] for s in seed.HEAVY_AGENT_TIER_MIGRATION_20}
    assert keys == {
        "tools.transformer.coder",
        "tools.transformer.verifier",
        "tools.transformer.planner",
        "codegen.coder_be",
        "codegen.coder_fe",
    }
    for spec in seed.HEAVY_AGENT_TIER_MIGRATION_20:
        # Guarded on the old SEED value so a Console override survives.
        assert spec["old_c"] == "high"
        assert spec["new_c"] == "critical"
        assert AGENT_COMPLEXITY[spec["key"]] == spec["new_c"]


# ── The park, end to end through fabric_chat_with_failover ────────────
#
# The unit tests above prove the park bookkeeping. These prove the thing
# that actually fixes the operator's log: a parked provider is skipped
# with NO call to it at all. Harness copied from
# test_rate_limit_handling.py, which pins the sibling behaviour.

import sys  # noqa: E402

AZURE_429 = (
    'HTTP 429: {"statusCode": 429, '
    '"message": "Rate limit is exceeded. Try again in 67 seconds."}'
)


def _fake_db(monkeypatch, writes):
    class _AC:
        async def find_one(self, *a, **kw):
            return {"key": "tools.transformer.coder", "provider_id": ""}

        async def update_one(self, flt, upd, **kw):
            writes.append(upd.get("$set", {}).get("provider_id"))

    class _Cursor:
        def sort(self, *a, **kw):
            return self

        async def to_list(self, n):
            return [
                {"id": "p-azure", "name": "Azure", "provider_type": "azure",
                 "is_default": True, "is_active": True},
                {"id": "p-ollama", "name": "Ollama (local)",
                 "provider_type": "ollama", "is_active": True},
            ]

    class _MP:
        async def find_one(self, *a, **kw):
            return {"id": "p-azure", "name": "Azure", "provider_type": "azure",
                    "is_default": True, "is_active": True}

        def find(self, *a, **kw):
            return _Cursor()

    monkeypatch.setitem(
        sys.modules, "db",
        type("db", (), {"agent_configs": _AC(), "model_providers": _MP()})(),
    )


@pytest.mark.asyncio
async def test_a_parked_provider_is_skipped_with_no_round_trip(monkeypatch):
    """The fix for the 429 storm.

    Previously every one of hundreds of in-flight calls paid its own
    request and Retry-After to rediscover that the account was throttled.
    Once parked, the primary must not be called at all.
    """
    from fabric import model_fabric as mf

    writes: list = []
    _fake_db(monkeypatch, writes)
    mark_provider_parked("p-azure", 300.0)

    seen: list = []

    async def fake_fabric_chat(**kw):
        prov = (kw.get("provider_override") or {}).get("id", "PRIMARY")
        seen.append(prov)
        return {"content": "ok", "model": "gpt-oss:latest", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)

    out = await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
    )

    assert out["content"] == "ok"
    # The primary was never called — no "PRIMARY" entry, and p-azure was
    # not passed as an override either.
    assert "PRIMARY" not in seen
    assert "p-azure" not in seen
    assert seen == ["p-ollama"]


@pytest.mark.asyncio
async def test_a_park_writes_no_pin_so_traffic_returns_on_its_own(monkeypatch):
    """A park is a throttle, not a durable failure. Pinning would demote
    the agent to Ollama permanently — the exact bug iter-19 fixed for the
    429 path, which this new skip path must not reintroduce."""
    from fabric import model_fabric as mf

    writes: list = []
    _fake_db(monkeypatch, writes)
    mark_provider_parked("p-azure", 300.0)

    async def fake_fabric_chat(**kw):
        return {"content": "ok", "model": "gpt-oss:latest", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)
    await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
    )
    assert writes == [], (
        f"the park path wrote provider_id {writes!r}; a throttle must not "
        "pin the agent away from the operator's paid primary"
    )


@pytest.mark.asyncio
async def test_an_unparked_provider_is_still_tried_first(monkeypatch):
    """The park must not change the happy path: with no park in force the
    primary is called directly, with no provider_override."""
    from fabric import model_fabric as mf

    writes: list = []
    _fake_db(monkeypatch, writes)

    seen: list = []

    async def fake_fabric_chat(**kw):
        seen.append((kw.get("provider_override") or {}).get("id", "PRIMARY"))
        return {"content": "ok", "model": "gpt-5.1", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)
    await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
    )
    assert seen == ["PRIMARY"]


@pytest.mark.asyncio
async def test_the_park_expires_and_the_primary_is_probed(monkeypatch):
    """Once the window is over the next call goes to the paid account
    again — that call IS the probe."""
    from fabric import model_fabric as mf

    writes: list = []
    _fake_db(monkeypatch, writes)
    mark_provider_parked("p-azure", 60.0)

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61.0)

    seen: list = []

    async def fake_fabric_chat(**kw):
        seen.append((kw.get("provider_override") or {}).get("id", "PRIMARY"))
        return {"content": "ok", "model": "gpt-5.1", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)
    await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
    )
    assert seen == ["PRIMARY"], "an expired park must let the probe through"


@pytest.mark.asyncio
async def test_a_park_never_takes_a_single_provider_install_offline(monkeypatch):
    """A park is an optimisation, not a policy.

    On an install where Azure is the ONLY provider, skipping it would make
    every call fail with CreditError for the whole window — turning a
    likely 429 into a certain outage. With nothing else left, the parked
    primary must still be tried.
    """
    from fabric import model_fabric as mf

    class _AC:
        async def find_one(self, *a, **kw):
            return {"key": "tools.transformer.coder", "provider_id": ""}

        async def update_one(self, flt, upd, **kw):
            pass

    class _Cursor:
        def sort(self, *a, **kw):
            return self

        async def to_list(self, n):
            return [{"id": "p-azure", "name": "Azure", "provider_type": "azure",
                     "is_default": True, "is_active": True}]

    class _MP:
        async def find_one(self, *a, **kw):
            return {"id": "p-azure", "name": "Azure", "provider_type": "azure",
                    "is_default": True, "is_active": True}

        def find(self, *a, **kw):
            return _Cursor()

    monkeypatch.setitem(
        sys.modules, "db",
        type("db", (), {"agent_configs": _AC(), "model_providers": _MP()})(),
    )
    mark_provider_parked("p-azure", 300.0)

    calls = {"n": 0}

    async def fake_fabric_chat(**kw):
        calls["n"] += 1
        return {"content": "ok", "model": "gpt-5.1", "usage": {}}

    monkeypatch.setattr(mf, "fabric_chat", fake_fabric_chat)

    out = await mf.fabric_chat_with_failover(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
    )
    assert out["content"] == "ok"
    assert calls["n"] == 1, "the parked primary was never retried as a last resort"
