"""iter-14.25.3 (SUPERSEDED by iter-14.25.4) — historical opt-out test.

Iter-14.25.3 dropped ``srs`` from the default Journey-KB stages
allowlist because the CGHS pilot showed no SRS-quality gain.
Iter-14.25.4 reverted that decision after adding two deterministic
roster guards (UC roster from workflows + glossary whitelist) that
attack the *actual* root cause (LLM free-invention of §5 UCs and
§1.3 acronyms). Journey grounding stays ON for SRS by default now.

The tests below still cover the toggle mechanics (env override,
per-project override wins) so operators keep a rollback path, but
the default assertions were updated to reflect iter-14.25.4.
"""

from __future__ import annotations

import os
import sys
import pathlib

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from kb import journey_config as jc  # noqa: E402


def test_default_stages_is_codegen_only():
    """iter-14.25.8: SRS removed from journey KB defaults again (CGHS §5)."""
    assert jc._DEFAULT_STAGES == ("codegen",)


def test_env_default_stages_returns_codegen_when_env_unset(monkeypatch):
    monkeypatch.delenv("LAMA_JOURNEY_KB_STAGES", raising=False)
    assert jc._env_default_stages() == ["codegen"]


def test_env_default_stages_can_opt_in_srs(monkeypatch):
    """Rollback path preserved: env can turn SRS journey grounding on."""
    monkeypatch.setenv("LAMA_JOURNEY_KB_STAGES", "srs,codegen")
    assert jc._env_default_stages() == ["srs", "codegen"]


@pytest.mark.asyncio
async def test_is_stage_enabled_default_is_off_for_srs(monkeypatch):
    """With master toggle ON and DEFAULT stages, SRS must be OFF (iter-14.25.8)."""
    monkeypatch.delenv("LAMA_JOURNEY_KB_STAGES", raising=False)

    async def _enabled(pid):  # noqa: ARG001
        return True

    async def _stages(pid):  # noqa: ARG001
        return jc._env_default_stages()

    monkeypatch.setattr(jc, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(jc, "journey_kb_stages", _stages)
    assert await jc.is_stage_enabled("p1", "srs") is False
    assert await jc.is_stage_enabled("p1", "codegen") is True


@pytest.mark.asyncio
async def test_is_stage_enabled_srs_can_still_be_opted_in(monkeypatch):
    """Rollback path preserved: env can turn SRS journey grounding back on."""
    monkeypatch.setenv("LAMA_JOURNEY_KB_STAGES", "srs,codegen")

    async def _enabled(pid):  # noqa: ARG001
        return True

    async def _stages(pid):  # noqa: ARG001
        return jc._env_default_stages()

    monkeypatch.setattr(jc, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(jc, "journey_kb_stages", _stages)
    assert await jc.is_stage_enabled("p1", "srs") is True
    assert await jc.is_stage_enabled("p1", "codegen") is True
