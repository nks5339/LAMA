"""iter-20 — the build loop escalates four times instead of two.

The operator reported that the DevOps agent "failed to build that in just
two try". Two was the whole ladder: `_run_compile_fix_loop` escalated
coder -> devops_expert exactly once and then stopped on the stagnation
guard. There was no third attempt to make.

A third and fourth attempt are only worth making if they can do something
the first two could not, so the new rungs differ in what the agent SEES
(the raw build log, never previously shown to anyone) and what it may
CHANGE (regenerate from the legacy original rather than patch further).

iter-20.1 — the export gate this suite also used to pin has been REMOVED.
It blocked download and GitHub push unless the build was green, which in
practice stopped the operator collecting their own code rather than
stopping bad code shipping, because the loop does not reach green
reliably enough for that to be a gate rather than a trap. The tests at the
end now guard the opposite: that no 409 can return to the export paths.
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


# ── Export is NOT gated (iter-20.1) ───────────────────────────────────
#
# iter-20 blocked download and GitHub push unless `compile_green AND
# production_ready`, so a red build could not be retrieved at all. The
# operator asked for it back: the fix loop does not reach green reliably
# enough for that to be a gate rather than a trap, and the effect was to
# stop them collecting their own code rather than to stop bad code
# shipping. Build state is still reported everywhere it was — it simply is
# not a permission.

def test_export_is_not_gated_on_build_state():
    """`_build_readiness_gate` is gone, and nothing may reintroduce a 409
    on the export paths. Asserted on source because the failure mode is a
    refusal that only shows up with a red build in front of you."""
    src = (Path(T.__file__)).read_text()
    assert "_build_readiness_gate" not in src
    assert "LAMA_ALLOW_UNVERIFIED_DOWNLOAD" not in src, (
        "the escape hatch existed only to survive the gate; with no gate "
        "it is dead configuration"
    )


def test_the_download_endpoint_refuses_only_for_real_client_errors():
    """404 for an unknown transformation, 400 for a bad scope, 404 when
    there are genuinely no files — and nothing else. A 409 here means the
    gate is back."""
    src = (Path(T.__file__)).read_text()
    start = src.index("async def download_transformed_code(")
    body = src[start:start + 2200]
    assert "409" not in body
    assert "HTTPException(404" in body
    assert "HTTPException(400" in body


def test_github_push_is_not_gated_either():
    src = (Path(T.__file__)).read_text()
    start = src.index("async def push_transformation_to_github(")
    body = src[start:start + 1600]
    assert "409" not in body


def test_the_status_projection_still_reports_build_state():
    """The export gate is gone, but the build state it read is not.

    `compile_green` was persisted and never projected until iter-20, so the
    page had to infer the build state from `status` strings. Removing the
    gate must not take that back out — the operator should still be able to
    see plainly that a build is red, they just are not blocked by it.
    """
    src = (Path(T.__file__)).read_text()
    start = src.index("async def get_transformation_status")
    projection = src[start:start + 2500]
    for field in ("compile_green", "build_tools", "dependency_audit",
                  "production_ready", "status"):
        assert f'"{field}": 1' in projection, f"{field} is no longer projected"


def test_a_malformed_file_id_is_a_client_error_not_a_server_fault():
    """Found by sweeping every GET route for 5xx, not by a unit test.

    `get_transformation_file` passed `file_id` straight to ObjectId(),
    which raises bson.errors.InvalidId on anything that is not a 24-char
    hex string — surfacing as a 500. Its sibling
    `regenerate_transformation_file` has guarded this since iter-15.10;
    this route was simply missed.
    """
    src = (Path(T.__file__)).read_text()
    # Anchor on the full signature — a bare "get_transformation_file"
    # prefix-matches the sibling LISTING route `get_transformation_files`,
    # which appears first in the file and has no ObjectId call at all, so
    # this assertion would have passed vacuously.
    start = src.index("async def get_transformation_file(transform_id: str, file_id: str)")
    body = src[start:start + 900]
    assert "except Exception" in body
    assert "Invalid file_id" in body
    assert 'ObjectId(file_id), "transform_id"' not in body, (
        "ObjectId(file_id) must be guarded before it reaches the query"
    )
