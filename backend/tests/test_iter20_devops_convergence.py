"""iter-20 — the build loop converges, and nothing ships until it does.

The operator reported two things about a failed Helidon -> Spring Boot run:
the DevOps agent "failed to build that in just two try", and they still
ended up with a downloadable ZIP of code that does not compile.

Both were true by design:

  * `_run_compile_fix_loop` escalated coder -> devops_expert exactly ONCE
    and then stopped on the stagnation guard. Two attempts was the whole
    ladder.
  * `download_transformed_code` checked only that the transformation
    existed and the scope string was valid. A red build exported exactly
    like a green one.

A third and fourth attempt are only worth making if they can do something
the first two could not, so the new rungs differ in what the agent SEES
(the raw build log, never previously shown to anyone) and what it may
CHANGE (regenerate from the legacy original rather than patch further).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

import routes.tools as T  # noqa: E402


# ── The escalation ladder ─────────────────────────────────────────────

def test_the_ladder_has_four_distinct_rungs():
    """Two was the old ladder, and the reported symptom."""
    assert len(T._ESCALATION_LADDER) == 4
    assert [r[0] for r in T._ESCALATION_LADDER] == [
        "coder", "devops_expert", "devops_expert_raw", "regenerator",
    ]


def test_the_default_cap_allows_the_whole_ladder_to_be_walked():
    assert T._COMPILE_FIX_DEFAULT_MAX_ITER >= len(T._ESCALATION_LADDER) + 1


def test_the_regenerator_is_a_registered_agent():
    """A prompt with no AGENT_PROMPT_KEYS entry resolves to "" and the
    agent silently never runs — the exact defect iter-18 found in the
    Validator, which had been seeded but unreachable since iter-16."""
    assert T.AGENT_PROMPT_KEYS["regenerator"] == "tools.transformer.regenerator"
    assert T.AGENT_LLM_BACKED["regenerator"] is True
    assert "regenerator" in T.AGENT_LABELS


def test_the_regenerator_prompt_is_actually_seeded():
    import seed

    keys = {p["key"] for p in seed.GLOBAL_PROMPTS}
    assert "tools.transformer.regenerator" in keys


def test_the_regenerator_prompt_carries_the_operators_own_rules():
    """The brief that worked for them by hand, encoded: do not alter
    business logic, do not finish with broken code, be token-efficient
    without losing accuracy."""
    import seed

    tpl = next(
        p["template"] for p in seed.GLOBAL_PROMPTS
        if p["key"] == "tools.transformer.regenerator"
    ).lower()
    assert "do not alter business logic" in tpl
    assert "broken code" in tpl
    assert "complete file" in tpl
    # It must be told the history, or it behaves like another patch pass.
    assert "last" in tpl and "escalation" in tpl


def test_the_regenerator_runs_on_the_strongest_tier():
    """It only ever runs after three cheaper attempts have each failed, so
    the marginal cost of the best model is justified several times over by
    the time it is reached."""
    from fabric.model_fabric import AGENT_COMPLEXITY

    assert AGENT_COMPLEXITY["tools.transformer.regenerator"] == "critical"


# ── Rung 2: the raw build log ─────────────────────────────────────────

def _compile_result_with_log(stderr: str = "", stdout: str = "") -> dict:
    return {
        "compilation_ready": False,
        "components": [{
            "component": "backend", "tool": "maven",
            "invocations": [{
                "tool": "maven", "cwd": "svc", "status": "failed",
                "stderr_tail": stderr, "stdout_tail": stdout,
            }],
        }],
    }


def test_the_raw_build_log_is_extracted_for_the_escalated_agent():
    """Until iter-20 NOTHING in this loop ever put raw build output in
    front of an agent — only the Planner's prose summary of it. A detail
    the Planner elided was unavailable to every repair attempt."""
    out = T._raw_build_log_excerpt(_compile_result_with_log(
        stderr="[ERROR] Failed to execute goal ... resolve dependencies",
    ))
    assert "RAW BUILD OUTPUT" in out
    assert "Failed to execute goal" in out
    assert "maven" in out


def test_passing_invocations_are_not_included():
    """A green module's output is noise in a prompt about a red one."""
    cr = _compile_result_with_log(stderr="boom")
    cr["components"][0]["invocations"].append({
        "tool": "maven", "cwd": "other", "status": "passed",
        "stdout_tail": "BUILD SUCCESS on the other module",
    })
    out = T._raw_build_log_excerpt(cr)
    assert "boom" in out
    assert "BUILD SUCCESS" not in out


def test_an_empty_log_yields_no_section():
    """The rung must degrade to a plain devops_expert retry rather than
    emitting a heading that reads as 'the build printed nothing'."""
    assert T._raw_build_log_excerpt({}) == ""
    assert T._raw_build_log_excerpt(_compile_result_with_log(stderr="  ")) == ""


def test_the_log_is_bounded():
    """A Maven reactor dump is mostly download progress; the diagnosis is
    in the tail, and the whole thing would crowd out the file itself."""
    out = T._raw_build_log_excerpt(_compile_result_with_log(stderr="x" * 500_000))
    assert len(out) < 20_000


def test_the_log_keeps_the_tail_not_the_head():
    body = "noise\n" * 5000 + "[ERROR] the actual root cause"
    out = T._raw_build_log_excerpt(_compile_result_with_log(stderr=body))
    assert "the actual root cause" in out


# ── Rung 3: regeneration ──────────────────────────────────────────────

def test_the_regeneration_brief_states_the_operators_strict_rules():
    brief = T._regeneration_brief(
        target_path="svc/src/main/java/A.java",
        failed_content="class A {",
        compile_result={},
        detected_stack={"framework": "helidon-mp", "language": "java"},
        target_stack={"backend": "spring-boot-3"},
        regenerating_from_original=True,
    )
    low = brief.lower()
    assert "do not alter business logic" in low
    assert "do not finish with broken code" in low
    assert "complete file" in low
    assert "senior backend developer" in low


def test_the_brief_carries_the_target_playbook():
    brief = T._regeneration_brief(
        target_path="A.java", failed_content="", compile_result={},
        detected_stack={"framework": "helidon"},
        target_stack={"backend": "spring-boot-3"},
        regenerating_from_original=True,
    )
    assert "@RestController" in brief


def test_the_brief_lists_the_forbidden_source_tokens():
    brief = T._regeneration_brief(
        target_path="A.java", failed_content="", compile_result={},
        detected_stack={"framework": "helidon-mp"},
        target_stack={"backend": "spring-boot-3"},
        regenerating_from_original=True,
    )
    assert "io.helidon" in brief


def test_the_brief_says_the_failed_attempt_is_not_a_template():
    """Rungs 0-2 all edit the failed file. This rung exists to abandon it,
    so it must be shown as a warning, never as a starting point."""
    brief = T._regeneration_brief(
        target_path="A.java", failed_content="class Broken {",
        compile_result={}, detected_stack={}, target_stack={"backend": "spring-boot-3"},
        regenerating_from_original=True,
    )
    assert "do NOT copy its" in brief or "do not copy" in brief.lower()
    assert "class Broken {" in brief


def test_the_brief_is_honest_when_no_original_survives():
    """If the legacy file cannot be found we must not claim to be handing
    over the original — the model would then distrust what it sees."""
    brief = T._regeneration_brief(
        target_path="A.java", failed_content="x", compile_result={},
        detected_stack={}, target_stack={"backend": "spring-boot-3"},
        regenerating_from_original=False,
    )
    assert "not available" in brief
    assert "ORIGINAL LEGACY FILE" not in brief


# ── The download gate ─────────────────────────────────────────────────

GREEN = {
    "status": "completed",
    "build_tools": {"backend": "maven"},
    "compile_green": True,
    "dependency_audit": {"production_ready": True, "findings": []},
}


def test_a_green_build_downloads():
    assert T._build_readiness_gate(GREEN) == ""


def test_a_red_build_is_blocked():
    """The reported symptom: a broken tree exported as cleanly as a
    working one."""
    doc = dict(GREEN, compile_green=False,
               compilation_result={"summary": "3 modules failed to compile"})
    reason = T._build_readiness_gate(doc)
    assert reason
    assert "3 modules failed to compile" in reason


def test_a_devops_blocked_build_is_blocked_even_though_it_compiles():
    """`production_ready` is load-bearing since iter-19: a pom Maven
    cannot resolve is not shippable just because javac was happy."""
    doc = dict(GREEN, dependency_audit={
        "production_ready": False,
        "findings": [{"severity": "critical", "issue": "helidon-microprofile has no version"}],
    })
    reason = T._build_readiness_gate(doc)
    assert reason
    assert "helidon-microprofile has no version" in reason


def test_a_run_still_in_flight_is_blocked():
    assert T._build_readiness_gate(dict(GREEN, status="running"))


def test_a_job_with_no_build_tool_is_not_blocked():
    """We cannot assert a build is broken when no build was ever asked
    for — that would make the gate unpassable for database-only or
    frontend-only transforms."""
    assert T._build_readiness_gate({"status": "completed", "build_tools": {}}) == ""


def test_the_env_escape_hatch_lifts_the_gate(monkeypatch):
    """Off by default and not surfaced in the UI. It exists so an
    environmental failure (no JDK in the container, a blocked repository)
    cannot permanently strand a user's own code."""
    doc = dict(GREEN, compile_green=False)
    assert T._build_readiness_gate(doc)
    monkeypatch.setenv("LAMA_ALLOW_UNVERIFIED_DOWNLOAD", "1")
    assert T._build_readiness_gate(doc) == ""


def test_the_reason_tells_the_operator_what_to_do_next():
    """A blocked download with no next step is just a dead end."""
    doc = dict(GREEN, compile_green=False)
    assert "Rerun compile" in T._build_readiness_gate(doc)


@pytest.mark.parametrize("status", [
    "pending", "awaiting_confirmation", "awaiting_task_confirmation",
])
def test_every_pre_terminal_status_is_blocked(status):
    assert T._build_readiness_gate(dict(GREEN, status=status))


def test_the_status_projection_fetches_every_field_the_gate_reads():
    """Caught live, not by a unit test.

    `/status` computes `download_blocked_reason` with the same helper the
    download endpoint enforces, but it feeds that helper a PROJECTED
    document. The projection omitted `compile_green` and `build_tools`, so
    the gate saw a doc with no build tool, concluded "nothing to gate on"
    and reported None — while the download endpoint, reading the full
    document, was correctly refusing the same job with a 409.

    The UI would have shown a download button that 409s when clicked.
    """
    src = (Path(T.__file__)).read_text()
    start = src.index("async def get_transformation_status")
    projection = src[start:start + 2500]
    for field in ("compile_green", "build_tools", "dependency_audit",
                  "production_ready", "status"):
        assert f'"{field}": 1' in projection, (
            f"_build_readiness_gate reads {field!r}; the /status projection "
            f"must fetch it or the button and the 409 will disagree"
        )
