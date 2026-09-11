"""iter-14.7 — Regression test for the factory_cli timeout floor bug.

Forensic context:

    User reported that SRS section generation via the factory_cli path
    took a huge amount of time from LAMA, even though direct
    `droid exec` calls returned in seconds. Log analysis showed every
    attempt timing out at exactly 300017ms:

        Factory CLI routing TIMEOUT agent=srs.regenerate elapsed=300017ms

    Root cause: `route_via_factory_cli` computed
        effective_timeout = max(timeout, FACTORY_CLI_TIMEOUT_SEC)
    while the module-level env-var was documented as a **hard cap**
    ("so a runaway agent can't burn the box for an hour"). `max`
    turned the cap into a **floor**, so the caller's per-attempt
    schedule (240s / 200s / 150s from routes/srs.py) was ignored and
    every attempt waited a full 300s before failing.

    Consequence: 3 attempts × 300s = 900s of hard timeout before the
    OpenRouter fallback kicked in — and when OPENROUTER_API_KEY was
    unset, users saw ~15 minutes of "loading" followed by a
    misleading error.

    Fix: caller's `timeout` is now authoritative and
    `FACTORY_CLI_TIMEOUT_SEC` only clamps as an UPPER bound (with a
    fallback to the env cap when the caller passes 0/None).

Tests here pin the intended semantics so this can't silently regress.
"""
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from fabric import factory_cli  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_env_cap(monkeypatch):
    """Pin the module-level cap to a known value for the test window."""
    monkeypatch.setattr(factory_cli, "FACTORY_CLI_TIMEOUT_SEC", 300.0)
    yield


def _install_fake_run(monkeypatch, captured):
    async def _fake_run(**kwargs):
        captured["timeout"] = kwargs["timeout"]
        return {"result": "OK"}

    monkeypatch.setattr(factory_cli, "_run_droid_exec", _fake_run)
    monkeypatch.setattr(factory_cli, "_binary_resolves", lambda _p: True)


def test_caller_timeout_below_env_cap_is_honored(monkeypatch):
    """Caller says 90s, env cap is 300s → effective timeout must be 90s.

    This is the exact SRS `fast_mode` case; the old code applied the
    300s floor, so a droid hang used 300s instead of 90s per attempt.
    """
    captured = {}
    _install_fake_run(monkeypatch, captured)

    result = asyncio.run(factory_cli.route_via_factory_cli(
        messages=[{"role": "user", "content": "ping"}],
        project_id="p1",
        agent_key="srs.regenerate",
        timeout=90.0,
    ))
    assert result is not None
    assert captured["timeout"] == 90.0, (
        f"caller timeout 90s must be honored (got {captured['timeout']}s); "
        "regression: env cap treated as floor again"
    )


def test_caller_timeout_above_env_cap_is_clamped(monkeypatch):
    """Caller says 600s, env cap is 300s → effective timeout is 300s.

    Pins the "hard cap" contract: no caller may burn the box for
    longer than the env-var allows.
    """
    captured = {}
    _install_fake_run(monkeypatch, captured)

    asyncio.run(factory_cli.route_via_factory_cli(
        messages=[{"role": "user", "content": "ping"}],
        project_id="p1",
        agent_key="srs.regenerate",
        timeout=600.0,
    ))
    assert captured["timeout"] == 300.0, (
        f"cap must clamp caller timeout down (got {captured['timeout']}s)"
    )


def test_missing_caller_timeout_falls_back_to_env_cap(monkeypatch):
    """timeout=0 → use env cap (300s). Preserves prior default behaviour
    for callers that don't pass an explicit schedule."""
    captured = {}
    _install_fake_run(monkeypatch, captured)

    asyncio.run(factory_cli.route_via_factory_cli(
        messages=[{"role": "user", "content": "ping"}],
        project_id="p1",
        agent_key="srs.regenerate",
        timeout=0.0,
    ))
    assert captured["timeout"] == 300.0
