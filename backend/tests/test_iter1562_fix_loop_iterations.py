"""
iter-15.62.4 — `_run_compile_fix_loop` no longer has a hardcoded
iteration cap by default.

Before this iteration the loop always stopped after
`LAMA_COMPILE_FIX_MAX_ITER` (default 3, hard-ceilinged at 6) iterations
even if the Coder was making real progress fixing one failure at a
time — reported directly by the user as "Reached fix-loop cap of 3
iterations without a green build" while the build was still fixable.

This iteration changes the default to UNBOUNDED: the loop now runs
until the build passes, hits a genuine infra block, or — the one
automatic stop condition that is NOT an arbitrary count — a
"stagnation guard" fires because the exact same failure recurred after
a full fix round (meaning the last round of Coder edits had zero
effect, so looping further would just burn LLM calls without result).
An explicit `max_iterations` argument (or `LAMA_COMPILE_FIX_MAX_ITER`
env var) is still honoured as an OPT-IN cap.

These tests exercise `_compile_failure_signature` directly and
`_run_compile_fix_loop` end-to-end with every external dependency
(Mongo collections, `_run_compiler`, `_parse_compile_errors`, Planner/
Coder) stubbed out, so we can deterministically control how many
iterations the fake build takes to go green.
"""
import asyncio
import os
import sys
from typing import List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes import tools as tools_mod  # noqa: E402

# The autouse `_stub_common` fixture below stubs `_coder_apply_fix` for the
# loop-level tests (agent-agnostic). A couple of tests below need to
# exercise the REAL `_coder_apply_fix`/`_run_coder` to verify agent_name
# propagation — capture the genuine functions here, before any fixture
# monkeypatches them, so those tests can restore them explicitly.
_REAL_CODER_APPLY_FIX = tools_mod._coder_apply_fix
_REAL_RUN_CODER = tools_mod._run_coder


async def _noop_log_run(*a, **kw):
    return "run-id"


async def _noop_update_run(*a, **kw):
    return None


class _FakeFilesCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


@pytest.fixture(autouse=True)
def _stub_common(monkeypatch):
    monkeypatch.setattr(tools_mod, "_log_agent_run", _noop_log_run)
    monkeypatch.setattr(tools_mod, "_update_agent_run", _noop_update_run)
    monkeypatch.setattr(tools_mod, "_emit_log", lambda *a, **kw: None)
    monkeypatch.delenv("LAMA_COMPILE_FIX_MAX_ITER", raising=False)

    async def _fake_find_one(*a, **kw):
        return {"_id": "tx-1", "detected_stack": {}, "target_stack": {}}

    async def _fake_update_one(*a, **kw):
        return None

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    monkeypatch.setattr(tools_mod.transformations, "update_one", _fake_update_one)
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeFilesCursor([{"path": "backend/Main.java", "content": "class Main {}"}]),
    )
    monkeypatch.setattr(tools_mod, "_write_transformed_workspace", lambda files: "/tmp/fake-ws")

    import shutil as _sh
    monkeypatch.setattr(_sh, "rmtree", lambda *a, **kw: None)

    # Deterministic FIX-task/Coder path — irrelevant to the iteration-
    # count behaviour under test, so make it a trivial always-succeeds no-op.
    monkeypatch.setattr(tools_mod, "_parse_compile_errors", lambda *a, **kw: [
        {"path": "backend/Main.java", "component": "backend", "tool": "maven", "lines": [(1, "boom")]},
    ])

    async def _fake_planner_tasks(*a, **kw):
        return [{"target_path": "backend/Main.java"}]

    monkeypatch.setattr(tools_mod, "_planner_fix_tasks_from_errors", _fake_planner_tasks)

    async def _fake_coder_apply_fix(*a, **kw):
        return True

    monkeypatch.setattr(tools_mod, "_coder_apply_fix", _fake_coder_apply_fix)


def _failing_compile_result(reason="BUILD FAILURE: something broke"):
    return {
        "compilation_ready": False, "overall_score": 0, "summary": "failed",
        "components": [{
            "component": "backend", "tool": "maven",
            "invocations": [{"status": "failed", "reason": reason, "cwd": "backend",
                              "stdout_tail": "", "stderr_tail": reason}],
        }],
        "mode": "native",
    }


def _passing_compile_result():
    return {
        "compilation_ready": True, "overall_score": 100, "summary": "BUILD SUCCESS",
        "components": [{
            "component": "backend", "tool": "maven",
            "invocations": [{"status": "passed", "reason": "", "cwd": "backend"}],
        }],
        "mode": "native",
    }


# ─────────────── _compile_failure_signature ───────────────
def test_compile_failure_signature_ignores_passed_invocations():
    sig = tools_mod._compile_failure_signature(_passing_compile_result())
    assert sig == ()


def test_compile_failure_signature_stable_for_identical_failures():
    a = tools_mod._compile_failure_signature(_failing_compile_result("X"))
    b = tools_mod._compile_failure_signature(_failing_compile_result("X"))
    assert a == b


def test_compile_failure_signature_differs_for_different_reasons():
    a = tools_mod._compile_failure_signature(_failing_compile_result("X"))
    b = tools_mod._compile_failure_signature(_failing_compile_result("Y"))
    assert a != b


# ─────────────── _run_compile_fix_loop: no hardcoded cap ───────────────
def test_fix_loop_continues_past_the_old_default_cap_of_three(monkeypatch):
    """Regression test for the exact symptom reported: 5 iterations of
    genuine (differing) failures followed by a pass must NOT be cut off
    at iteration 3 anymore."""
    call_count = {"n": 0}

    async def _fake_run_compiler(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] < 5:
            return _failing_compile_result(reason=f"failure #{call_count['n']}")
        return _passing_compile_result()

    monkeypatch.setattr(tools_mod, "_run_compiler", _fake_run_compiler)

    result = asyncio.run(tools_mod._run_compile_fix_loop("tx-1", {"backend": "maven"}, None))
    assert result["compilation_ready"] is True
    assert result["iterations_used"] == 5
    assert result["attempts"][-1]["status"] == "passed"
    assert call_count["n"] == 5


# ─────────────── iter-15.62.6: DevOps Expert escalation ───────────────
def test_fix_loop_escalates_to_devops_expert_and_recovers(monkeypatch):
    """The user-reported real-world case: the default Coder's fix made
    no difference once, the loop escalates to a DevOps Expert persona
    (NOT giving up), and the DevOps Expert's fix actually resolves it."""
    call_count = {"n": 0}
    agent_calls: List[str] = []

    async def _fake_run_compiler(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            # Same failure twice in a row -> triggers escalation on #2.
            return _failing_compile_result(reason="invalid target release: 21")
        return _passing_compile_result()

    # `**kw` is load-bearing. `_coder_apply_fix` later gained an `on_stage`
    # progress callback, and this stub was never updated. The call therefore
    # raised TypeError inside the loop's per-file try/except, which logged
    # "fix_error" and moved on -- so agent_calls stayed empty and the test
    # failed while the PRODUCT was working correctly. Absorbing new keyword
    # arguments keeps the stub from going stale the next time the signature
    # grows; the assertions below still pin the behaviour that matters.
    async def _tracking_coder_apply_fix(transform_id, task_row, detected_stack, target_stack, model, agent_name="coder", **kw):
        agent_calls.append(agent_name)
        return True

    monkeypatch.setattr(tools_mod, "_run_compiler", _fake_run_compiler)
    monkeypatch.setattr(tools_mod, "_coder_apply_fix", _tracking_coder_apply_fix)

    result = asyncio.run(tools_mod._run_compile_fix_loop("tx-1", {"backend": "maven"}, None))
    assert result["compilation_ready"] is True
    # iteration 1 (coder) -> iteration 2 (escalate mid-iteration, devops_expert fixes it) -> iteration 3 passes
    assert agent_calls == ["coder", "devops_expert"]
    statuses = [a["status"] for a in result["attempts"]]
    assert statuses == ["fixes_applied", "fixes_applied", "passed"]
    assert result["attempts"][1]["escalated_to_devops"] is True
    assert result["attempts"][1]["acting_agent"] == "devops_expert"


def test_coder_apply_fix_forwards_agent_name_to_run_coder(monkeypatch):
    """`_coder_apply_fix(agent_name="devops_expert")` must propagate the
    persona all the way into `_run_coder`, not silently default back to
    "coder"."""
    captured = {}

    async def _fake_find_one(*a, **kw):
        return {"content": "pom xml content", "_id": "f1", "original_path": "backend/pom.xml"}

    monkeypatch.setattr(tools_mod.transform_files, "find_one", _fake_find_one)

    async def _fake_update_one(*a, **kw):
        return None

    monkeypatch.setattr(tools_mod.transform_files, "update_one", _fake_update_one)
    monkeypatch.setattr(tools_mod.transformer_tasks, "update_one", _fake_update_one)

    async def _fake_run_coder(transform_id, task_doc, source_content, kb_ctx, detected_stack, target_stack, model=None, envelope=None, agent_name="coder"):
        captured["agent_name"] = agent_name
        return "<fixed content>"

    async def _fake_run_verifier(*a, **kw):
        return {"verdict": "ACCEPT", "confidence": 95, "structural_check": {"ok": True}}

    monkeypatch.setattr(tools_mod, "_coder_apply_fix", _REAL_CODER_APPLY_FIX)
    monkeypatch.setattr(tools_mod, "_run_coder", _fake_run_coder)
    monkeypatch.setattr(tools_mod, "_run_verifier", _fake_run_verifier)

    ok = asyncio.run(tools_mod._coder_apply_fix(
        "tx-1", {"_id": "t1", "target_path": "backend/pom.xml"}, {}, {}, None,
        agent_name="devops_expert",
    ))
    assert ok is True
    assert captured["agent_name"] == "devops_expert"


def test_run_coder_devops_expert_uses_devops_prompt_and_agent_key(monkeypatch):
    """`_run_coder(agent_name="devops_expert")` must resolve the DevOps
    Expert's OWN prompt/model (not the Coder's) and route through the
    `tools.transformer.devops_expert` fabric agent_key (per AGENTS.md
    contract #4 — no hard-coded model at the call site, tier resolution
    keyed off agent_key)."""
    captured = {}

    async def _fake_get_effective_prompt(transform_id, agent):
        captured["prompt_agent"] = agent
        return "SYSTEM PROMPT"

    async def _fake_get_effective_model(transform_id, agent, fallback_model):
        captured["model_agent"] = agent
        return fallback_model

    async def _fake_fabric_call(**kwargs):
        captured["agent_key"] = kwargs.get("agent_key")
        return {"content": "<?xml version=\"1.0\"?><project></project>", "model": "some-model"}

    async def _fake_project_id_for_transform(transform_id):
        return "p1"

    monkeypatch.setattr(tools_mod, "_get_effective_prompt", _fake_get_effective_prompt)
    monkeypatch.setattr(tools_mod, "_get_effective_model", _fake_get_effective_model)
    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric_call)
    monkeypatch.setattr(tools_mod, "_project_id_for_transform", _fake_project_id_for_transform)

    result = asyncio.run(tools_mod._run_coder(
        "tx-1", {"task_id": "t1", "source_path": "backend/pom.xml", "notes": "fix release"},
        "<current pom content>", "(kb ctx)", {}, {}, model=None, agent_name="devops_expert",
    ))
    assert captured["prompt_agent"] == "devops_expert"
    assert captured["model_agent"] == "devops_expert"
    assert captured["agent_key"] == "tools.transformer.devops_expert"
    assert "<project>" in result


def test_devops_expert_registered_in_agent_prompt_keys():
    assert tools_mod.AGENT_PROMPT_KEYS.get("devops_expert") == "tools.transformer.devops_expert"
    assert tools_mod.AGENT_LLM_BACKED.get("devops_expert") is True
    assert tools_mod.AGENT_LABELS.get("devops_expert") == "DevOps Expert"


# ─────────────── iter-15.62.8: compile-fix orphan/stall detection ───────────────
def test_status_flags_stale_non_terminal_compile_fix_progress_as_stalled(monkeypatch):
    """A `compile_fix_progress` stuck on a non-terminal phase (e.g. the
    backend restarted mid-"fixing") with no update in
    `_COMPILE_STALL_THRESHOLD_SEC` must be surfaced as stalled so the FE
    doesn't spin for its full 20-minute client-side timeout."""
    stale_ts = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - __import__("datetime").timedelta(seconds=tools_mod._COMPILE_STALL_THRESHOLD_SEC + 30)
    ).isoformat()

    async def _fake_find_one(*a, **kw):
        return {
            "compile_fix_progress": {"phase": "fixing", "iteration": 2, "updated_at": stale_ts},
            "compile_console_updated_at": stale_ts,
        }

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    result = asyncio.run(tools_mod.get_transformation_status("tx-1"))
    assert result["compile_fix_stalled"] is True


def test_status_does_not_flag_recent_non_terminal_progress_as_stalled(monkeypatch):
    fresh_ts = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()

    async def _fake_find_one(*a, **kw):
        return {
            "compile_fix_progress": {"phase": "compiling", "iteration": 1, "updated_at": fresh_ts},
            "compile_console_updated_at": fresh_ts,
        }

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    result = asyncio.run(tools_mod.get_transformation_status("tx-1"))
    assert result["compile_fix_stalled"] is False


def test_status_does_not_flag_terminal_phase_as_stalled_even_if_old(monkeypatch):
    """A "passed"/"exhausted"/etc. phase is terminal by definition — it's
    supposed to stop updating, that's not staleness."""
    stale_ts = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - __import__("datetime").timedelta(seconds=tools_mod._COMPILE_STALL_THRESHOLD_SEC + 9999)
    ).isoformat()

    async def _fake_find_one(*a, **kw):
        return {
            "compile_fix_progress": {"phase": "passed", "iteration": 1, "updated_at": stale_ts},
            "compile_console_updated_at": stale_ts,
        }

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    result = asyncio.run(tools_mod.get_transformation_status("tx-1"))
    assert result["compile_fix_stalled"] is False


def test_status_no_stall_flag_when_no_compile_fix_progress_yet(monkeypatch):
    async def _fake_find_one(*a, **kw):
        return {"status": "running"}

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    result = asyncio.run(tools_mod.get_transformation_status("tx-1"))
    assert result["compile_fix_stalled"] is False


def test_fix_loop_stagnation_guard_stops_when_fix_has_no_effect(monkeypatch):
    """If the exact same failure recurs after a fix round, the loop climbs
    one rung of the escalation ladder; only when the ladder is exhausted
    does it stop (NOT because of a hardcoded count) rather than looping
    forever burning LLM calls.

    iter-20 — the ladder grew from 2 rungs to 4. It used to be coder ->
    devops_expert -> stop, which is the "not fixed in 2 iterations" the
    operator reported: the third attempt was never made. The rungs now
    differ in what the agent SEES and may CHANGE, not just which prompt is
    used, because repeating the same view with a different persona is why
    the second attempt so often reproduced the first.
    """
    async def _fake_run_compiler(*a, **kw):
        return _failing_compile_result(reason="same failure every time")

    monkeypatch.setattr(tools_mod, "_run_compiler", _fake_run_compiler)

    result = asyncio.run(tools_mod._run_compile_fix_loop("tx-1", {"backend": "maven"}, None))
    assert result["compilation_ready"] is False
    # 1: coder. 2: same sig -> devops_expert. 3: same sig -> devops_expert
    # with the raw build log. 4: same sig -> regenerator. 5: stagnant.
    assert result["attempts"][-1]["status"] == "stagnant"
    assert result["iterations_used"] == 5
    assert [a.get("acting_agent") for a in result["attempts"][:4]] == [
        "coder", "devops_expert", "devops_expert_raw", "regenerator",
    ]
    assert result["attempts"][1]["escalated_to_devops"] is True


def test_the_escalation_ladder_rungs_are_distinct_remedies(monkeypatch):
    """Each rung must be able to do something the previous could not.

    A ladder of four identical attempts is four times the cost for the
    same answer — that was the failure mode of the old third attempt.
    """
    ladder = tools_mod._ESCALATION_LADDER
    assert [r[0] for r in ladder] == [
        "coder", "devops_expert", "devops_expert_raw", "regenerator",
    ]
    # Every rung carries an operator-facing label and a reason, both of
    # which surface in the progress stream.
    for agent, label, why in ladder:
        assert agent and label and why
    # The last rung is the only one that can abandon the failed file.
    assert "original" in ladder[-1][2] or "legacy" in ladder[-1][2]


def test_the_default_iteration_cap_leaves_room_to_climb_the_ladder():
    """iter-15.62.4 made this unbounded-until-stagnant, which sounds
    generous but ended runs at two in practice. A 4-rung ladder needs at
    least 5 iterations to be walked to the end."""
    assert tools_mod._COMPILE_FIX_DEFAULT_MAX_ITER >= len(tools_mod._ESCALATION_LADDER) + 1


def test_fix_loop_respects_explicit_max_iterations_when_caller_opts_in(monkeypatch):
    """An explicit cap must still work — it's just no longer the default."""
    call_count = {"n": 0}

    async def _fake_run_compiler(*a, **kw):
        call_count["n"] += 1
        return _failing_compile_result(reason=f"failure #{call_count['n']}")  # always different, never passes/stagnates

    monkeypatch.setattr(tools_mod, "_run_compiler", _fake_run_compiler)

    result = asyncio.run(tools_mod._run_compile_fix_loop("tx-1", {"backend": "maven"}, None, max_iterations=2))
    assert result["compilation_ready"] is False
    assert result["iterations_used"] == 2
    assert call_count["n"] == 2


def test_fix_loop_respects_env_var_cap_when_no_explicit_arg(monkeypatch):
    monkeypatch.setenv("LAMA_COMPILE_FIX_MAX_ITER", "2")
    call_count = {"n": 0}

    async def _fake_run_compiler(*a, **kw):
        call_count["n"] += 1
        return _failing_compile_result(reason=f"failure #{call_count['n']}")

    monkeypatch.setattr(tools_mod, "_run_compiler", _fake_run_compiler)

    result = asyncio.run(tools_mod._run_compile_fix_loop("tx-1", {"backend": "maven"}, None))
    assert result["iterations_used"] == 2
    assert call_count["n"] == 2
