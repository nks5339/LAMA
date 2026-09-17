"""iter-22 — Direct Transform: the catalogue drives the engine, not just the prompt.

iter-21 built a 14-source × 7-target catalogue and made the migration BRIEF
generic. It stopped there. `stacks.py` stayed a prompt-only artefact: nothing
outside `prompt_builder.py` read `suffixes`, `forbidden` or `build_cmd`, and
the engine, the residue scanner, the guardrail and the build agent all kept
the Helidon→Spring / Oracle→PG constants they were born with.

The consequence was not subtle. For 96 of the 98 selectable pairs the AI pass
was handed an EMPTY file list — the engine swept `(".java", ".sql")` while the
generic plugin staged 35 file types — so a JSP → React job copied its tree,
converted nothing, and wrote `COMPLETED`.

The iter-21 suite verifies every link in that chain except the one that was
broken: it proves the catalogue renders, the brief names the right pair, and
the plugin stages the files, and then stops. These tests cover the handoff.

Offline throughout. DCTE imports `fabric_call` INSIDE the function body, so
patching the consuming module does not reach it — patch `llm.fabric_call`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from dcte import ai_refactor as AR                      # noqa: E402
from dcte import build_agent as BA                      # noqa: E402
from dcte import devops_agent as DA                     # noqa: E402
from dcte import prompt_store as PS                     # noqa: E402
from dcte.engine import TransformationEngine            # noqa: E402
from dcte.models import DcteJob, ServiceConfig          # noqa: E402
from dcte.stacks import (                               # noqa: E402
    STACKS, ai_sweep_suffixes, residue_markers,
)


@pytest.fixture(autouse=True)
def _no_prompt_rows(monkeypatch):
    """No Mongo in these tests: every agent falls back to its module brief."""
    async def _empty(_key):
        return ""
    monkeypatch.setattr(PS, "get_dcte_prompt", _empty)
    PS.reset_cache()


# ── the catalogue now drives which files are looked at ────────────────

def test_every_stack_declares_the_extensions_its_code_lives_in():
    """A row without `suffixes` is selectable and invisible to the AI pass,
    which is exactly the state the whole catalogue was in before iter-22."""
    missing = [s.id for s in STACKS if not s.suffixes]
    assert missing == [], f"stacks with no declared suffixes: {missing}"


@pytest.mark.parametrize("pair,expected", [
    (("jsp", "react-19"), {".jsp", ".tsx"}),
    (("jquery", "angular-22"), {".js", ".ts"}),
    (("ejb", "dotnet-10"), {".java", ".cs"}),
    (("struts", "spring-boot-4"), {".jsp", ".java"}),
    (("oracle", "postgres-18"), {".sql"}),
])
def test_the_sweep_covers_both_sides_of_the_pair(pair, expected):
    """Source extensions are what needs converting; target extensions are
    what a previous pass may already have emitted and still has to be swept."""
    got = ai_sweep_suffixes(*pair)
    assert expected <= got, f"{pair}: missing {expected - got}"


def test_an_unknown_pair_still_gets_a_usable_sweep():
    """A job saved with a stack that has since left the catalogue must not
    silently sweep nothing."""
    got = ai_sweep_suffixes("no-such-stack", "also-not-real")
    assert ".java" in got and ".py" in got and ".ts" in got


def test_the_sweep_never_includes_build_manifests():
    """`dependency_migrator` and `devops_agent` own those. Two writers on one
    pom is how a repaired pom gets un-repaired."""
    for src in ("helidon-mp", "jsp", "ejb"):
        for tgt in ("spring-boot-4", "react-19", "dotnet-10"):
            assert ".json" not in ai_sweep_suffixes(src, tgt)


# ── the residue gate reads the catalogue, not a Helidon constant ──────

def test_residue_is_derived_from_the_selected_pair():
    """The gate that decides whether a file is 'migrated' or 'needs manual'
    was a Helidon+Oracle constant, so leftover Struts scanned clean."""
    struts = residue_markers("struts", "spring-boot-4")
    assert "org.apache.struts" in struts
    assert "ActionForm" in struts

    jsp = residue_markers("jsp", "react-19")
    assert "<%" in jsp, "a surviving scriptlet is the whole point"
    assert "javax.servlet." in jsp

    dotnet = residue_markers("ejb", "dotnet-10")
    assert "@Stateless" in dotnet          # from the source
    assert "System.Web." in dotnet         # from the target's own reject list


def test_the_target_can_permit_a_marker_the_source_forbids():
    """`jakarta.ws.rs.` is Helidon residue under Spring and the house style
    under Quarkus. Without the escape hatch, adding a Quarkus row would make
    every correct Quarkus file scan as half-migrated."""
    from dataclasses import replace
    from dcte import stacks as S

    quarkus = replace(
        S.get_stack("spring-boot-4"), id="quarkus-test",
        permitted=("jakarta.ws.rs.", "@ApplicationScoped"),
    )
    S._BY_ID["quarkus-test"] = quarkus
    try:
        got = residue_markers("helidon-mp", "quarkus-test")
        assert "jakarta.ws.rs." not in got
        assert "@ApplicationScoped" not in got
        assert "io.helidon." in got, "the source framework is still residue"
    finally:
        S._BY_ID.pop("quarkus-test", None)


def test_residue_scan_opens_the_files_the_pair_implies(tmp_path):
    """Before iter-22 `_scan_residue_in_file` returned [] for any suffix that
    was not .java/.sql, so a React output was never even read."""
    page = tmp_path / "Page.tsx"
    page.write_text("export default function P(){ return <%= x %>; }", encoding="utf-8")

    assert AR._scan_residue_in_file(page) == [], "no pair given -> historical behaviour"

    hits = AR._scan_residue_in_file(
        page,
        residue_markers("jsp", "react-19"),
        ai_sweep_suffixes("jsp", "react-19"),
    )
    assert "<%" in hits


def test_scan_residual_walks_a_non_java_tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export const A = () => { $(document).ready(fn); };", encoding="utf-8")
    out = AR.scan_residual(tmp_path, source_stack="jquery", target_stack="react-19")
    assert len(out) == 1
    assert "$(document).ready" in out[0]["markers"]


def test_comments_are_blanked_with_the_right_opener(tmp_path):
    """iter-20 blanks comments so the scan reads CODE. The opener was
    `--` for .sql and `//` for everything else, which is wrong for Python."""
    body = "# VARCHAR2 is only mentioned in this comment\nx = 1\n"
    assert "VARCHAR2" not in AR._blank_comments(body, ".py")
    assert "VARCHAR2" not in AR._blank_comments("-- VARCHAR2 here\nSELECT 1;", ".sql")
    assert "VARCHAR2" not in AR._blank_comments("// VARCHAR2 here\nint x;", ".java")


# ── truncation is stated, and a truncated reply is refused ────────────

def test_nothing_in_the_eligible_band_is_truncated():
    """16 KB < 40 KB meant every file in that band was cut silently and then
    size-checked against its full length — the exact defect iter-18.8 fixed
    one band lower and left here."""
    assert AR._MAX_CHARS_PER_FILE >= AR._MAX_FILE_SIZE_FOR_LLM
    assert BA._MAX_CHARS_PER_FILE >= BA._MAX_FILE_SIZE_FOR_LLM


def test_a_truncated_prompt_can_never_write_a_file(tmp_path):
    """Writing back a reply to a truncated prompt deletes everything past
    the cut. That is worse than not migrating the file at all."""
    f = tmp_path / "Big.java"
    original = "class Big {" + ("\n  // body" * 4000) + "\n}"
    f.write_text(original, encoding="utf-8")
    reply = "class Big {\n  // rewritten\n}"
    ok, reason = AR._safe_apply(f, original, reply, None, sent_len=1000)
    assert ok is False
    assert "shown to the model" in reason
    assert f.read_text(encoding="utf-8") == original, "the file must be untouched"


def test_the_size_ratio_is_measured_against_what_was_sent(tmp_path):
    """A faithful rewrite of the visible half used to score 0.55 against the
    full original and get rejected as 'too small' — or, worse, squeak past."""
    f = tmp_path / "A.java"
    body = "class A {\n" + ("  int x;\n" * 200) + "}"
    f.write_text(body, encoding="utf-8")
    good = "class A {\n" + ("  int y;\n" * 200) + "}"
    ok, reason = AR._safe_apply(f, body, good, None, sent_len=len(body))
    assert ok is True, reason


def test_a_marker_in_a_comment_is_not_residue(tmp_path):
    """The plugins emit correct code whose comments NAME the annotation they
    replaced. A raw substring check called that residue and sent a clean file
    round the fix-up loop to reword a sentence."""
    f = tmp_path / "Ctl.java"
    f.write_text("class Ctl {}", encoding="utf-8")
    new = "// was @ApplicationScoped before the migration\nclass Ctl {}\n" + "//x\n" * 20
    ok, _ = AR._safe_apply(f, "class Ctl {}\n" + "//x\n" * 20, new,
                           ("@ApplicationScoped",), 0)
    assert ok is True


# ── the JSON contract satisfies the choke-point repair ────────────────

def test_the_contract_is_an_object_so_the_repair_path_applies():
    """`llm.fabric_call` runs ONE bounded repair re-ask when a
    `response_format` reply fails `parses_as_json_object` — which requires a
    JSON OBJECT. A bare array could never qualify, which is why DCTE was the
    only subsystem that lost a file to a prose reply."""
    from llm import parses_as_json_object
    prompt = AR._system_prompt("helidon-mp", "spring-boot-3")
    assert '"files"' in prompt
    assert parses_as_json_object('{"files": []}') is True
    assert parses_as_json_object('[{"file": "x"}]') is False


def test_the_parser_accepts_the_object_and_the_legacy_array():
    """Older prompts, cached replies and small local models all emit a bare
    array. Rejecting them would trade a working path for a stricter one."""
    assert AR._coerce_file_entries({"files": [{"file": "a"}]}) == [{"file": "a"}]
    assert AR._coerce_file_entries([{"file": "a"}]) == [{"file": "a"}]
    assert AR._coerce_file_entries({"file": "a", "action": "leave"}) == [
        {"file": "a", "action": "leave"}]
    assert AR._coerce_file_entries({"nonsense": 1}) is None
    assert AR._coerce_file_entries("not json at all") is None


def test_the_sweep_asks_for_json_mode(tmp_path, monkeypatch):
    """Passing `response_format` is what arms the repair; DCTE was the only
    LLM subsystem in the app that never did."""
    import llm
    seen: list[dict] = []

    async def _fake(**kwargs):
        seen.append(kwargs)
        return {"content": json.dumps({"files": [{
            "file": str(tmp_path / "A.java"), "action": "leave",
            "changes": [], "risk": "low"}]})}

    monkeypatch.setattr(llm, "fabric_call", _fake, raising=True)
    f = tmp_path / "A.java"
    f.write_text("class A {}", encoding="utf-8")
    asyncio.run(AR.transform_files(
        [f], source_stack="helidon-mp", target_stack="spring-boot-3", concurrency=1))
    assert seen, "the model was never called"
    assert seen[0].get("response_format") == {"type": "json_object"}


# ── the engine hands the AI pass the files the pair implies ───────────

def _job(tmp_path, src_stack, tgt_stack, files):
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return DcteJob(
        name="sweep", source_root=str(src), output_root=str(tmp_path / "out"),
        services=[ServiceConfig(
            name="svc", source_path=str(src), destination_path=str(tmp_path / "out"),
            source_stack=src_stack, target_stack=tgt_stack)],
        ai_refactor=True, generate_cicd=[],
    )


def test_a_jsp_to_react_job_reaches_the_ai_pass_with_its_jsp_files(tmp_path):
    """THE regression. The engine swept `(".java", ".sql")` while the generic
    plugin staged 35 file types, so this job handed the model an empty list,
    converted nothing, and still reported COMPLETED."""
    job = _job(tmp_path, "jsp", "react-19", {
        "web/list.jsp": "<% out.println(\"hi\"); %>",
        "web/app.js": "$(document).ready(function(){});",
        "src/Servlet.java": "class Servlet {}",
        "pom.xml": "<project/>",
    })
    seen: dict = {}

    def _ai(files, source_stack="", target_stack=""):
        seen["files"] = [Path(f).name for f in files]
        seen["pair"] = (source_stack, target_stack)
        return []

    TransformationEngine().run_job(job, ai_refactor_fn=_ai)
    assert "list.jsp" in seen["files"], f"the .jsp never reached the model: {seen}"
    assert "app.js" in seen["files"]
    assert "Servlet.java" in seen["files"]
    assert "pom.xml" not in seen["files"], "manifests belong to the DevOps agent"
    assert seen["pair"] == ("jsp", "react-19")


def test_the_helidon_pair_still_sweeps_its_java(tmp_path):
    """The two deterministic pairs must not regress while the rest is fixed."""
    job = _job(tmp_path, "helidon-mp", "spring-boot-3", {
        "src/main/java/com/x/A.java": "package com.x;\nclass A {}",
    })
    seen: dict = {}

    def _ai(files, source_stack="", target_stack=""):
        seen["files"] = [Path(f).name for f in files]
        return []

    TransformationEngine().run_job(job, ai_refactor_fn=_ai)
    assert "A.java" in seen["files"]


def test_a_build_agent_exception_does_not_take_devops_down_with_it(tmp_path):
    """`br` was bound inside the build try and read unconditionally by the
    DevOps phase, so a build agent that raised made DevOps die on
    `NameError: br` — reported as 'DevOps agent errored', naming the wrong
    cause, and skipping the one phase that could have repaired the damage."""
    job = _job(tmp_path, "helidon-mp", "spring-boot-3",
               {"src/main/java/com/x/A.java": "package com.x;\nclass A {}"})
    ran: list[str] = []

    def _build(dest, svc_id, source_stack="", target_stack=""):
        raise RuntimeError("subprocess died")

    def _devops(dest, svc_id, build_result, source_stack="", target_stack=""):
        ran.append("devops")
        return {"attempted": True, "fixes_applied": 0, "gaps_found": [],
                "fixes": [], "unresolved": [], "notes": []}

    events: list = []
    TransformationEngine().run_job(
        job, event_sink=events.append, build_fix_fn=_build, devops_fn=_devops)

    assert ran == ["devops"], "DevOps must still run after a build-agent crash"
    devops_errors = [e for e in events
                     if e.phase == "devops" and "errored" in (e.message or "")]
    assert devops_errors == [], f"DevOps reported an error it did not have: {devops_errors}"


# ── the build agent speaks the target's language ──────────────────────

def test_the_build_fixer_prompt_names_the_selected_target():
    """It was a hardcoded 'Spring Boot 3.x / Java 17-21' essay handed to
    every pair, listing Spring Boot 3 starters and forbidding Helidon
    symbols — on a JSP → React job too."""
    prompt = asyncio.run(BA._build_fixer_prompt("jsp", "react-19"))
    assert "React 19" in prompt
    assert "Spring Boot 3" not in prompt
    assert "Helidon" not in prompt

    spring = asyncio.run(BA._build_fixer_prompt("helidon-mp", "spring-boot-4"))
    assert "Spring Boot 4.1" in spring
    assert "io.helidon." in spring, "the source's markers are the residue clause"


def test_the_toolchain_note_reaches_the_model(monkeypatch):
    """Compiling Java 25 source at release 17 turns every modern construct
    into a syntax error, and the fix loop then 'repairs' correct code. If we
    cannot force the right level, the model has to be told why."""
    monkeypatch.setattr(BA, "_installed_jdk_major", lambda: 17)
    _release, note = BA._release_for("spring-boot-4")
    prompt = asyncio.run(BA._build_fixer_prompt("helidon-mp", "spring-boot-4", note))
    assert "TOOLCHAIN" in prompt
    assert "Java 25" in prompt


def test_the_fix_loop_recompiles_after_its_last_round(tmp_path, monkeypatch):
    """The comment said 'Final compile check after last fix pass' and no such
    check existed: a build the last round actually repaired was reported red
    and the operator was sent to debug a green build."""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    calls = {"n": 0}

    async def _fake_build(cmd, cwd):
        calls["n"] += 1
        # Fail the first round, succeed on the post-fix final check.
        if calls["n"] == 1:
            return 1, "/x/A.java:[1,1] cannot find symbol"
        return 0, "BUILD SUCCESS"

    async def _fake_fix(fabric_call, path, errors, agent_key, system_prompt="", markers=None):
        return True, "applied", ["fixed"]

    monkeypatch.setattr(BA, "_run_build", _fake_build)
    monkeypatch.setattr(BA, "_fix_one_file", _fake_fix)
    monkeypatch.setattr(BA, "_parse_errors",
                        lambda out, root: [{"file": str(tmp_path / "A.java"),
                                            "line": 1, "message": "cannot find symbol"}]
                        if "cannot find" in out else [])
    monkeypatch.setattr(BA, "_detect_build_tool", lambda r: ("maven", tmp_path))
    monkeypatch.setattr(BA, "_maven_cmd", lambda d, release="": ["/bin/true"])

    res = asyncio.run(BA.build_and_fix(tmp_path, max_attempts=1,
                                       source_stack="helidon-mp",
                                       target_stack="spring-boot-3"))
    assert res.success is True, f"final compile never ran: {res.notes}"
    assert any("final check" in n for n in res.notes)


# ── the DevOps agent has the call site its tier was reserved for ──────

def test_dcte_devops_now_has_a_call_site():
    """DT-1: the key was registered, tiered and seeded for an escalation that
    had never been written, and the module docstring said so."""
    src = (Path(BA.__file__).parent / "devops_agent.py").read_text()
    assert "_escalate_gaps_to_llm" in src
    assert 'agent_key: str = "dcte.devops"' in src
    assert "no such call site exists yet" not in src, "the docstring still denies it"


def test_the_escalation_only_sees_what_templates_could_not_close(tmp_path, monkeypatch):
    """The deterministic patchers keep first refusal: a template is
    reproducible and cannot invent a database URL."""
    import llm
    seen: list[dict] = []

    async def _fake(**kwargs):
        seen.append(kwargs)
        return {"content": json.dumps({
            "action": "write", "file": "src/main/resources/extra.yml",
            "content": "feature:\n  flag: true\n", "why": "added the missing section"})}

    monkeypatch.setattr(llm, "fabric_call", _fake, raising=True)
    fixed, still_open = asyncio.run(DA._escalate_gaps_to_llm(
        tmp_path, [{"kind": "exotic_gap", "detail": "something no template covers"}],
        source_stack="helidon-mp", target_stack="spring-boot-4"))
    assert len(fixed) == 1
    assert fixed[0]["by"] == "llm"
    assert still_open == []
    assert (tmp_path / "src/main/resources/extra.yml").is_file()
    assert seen[0]["agent_key"] == "dcte.devops"
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_the_escalation_cannot_write_outside_the_service(tmp_path, monkeypatch):
    """The model names the path, so it has to be confined. Without this a
    `../../etc/x` reply writes outside the job."""
    import llm

    async def _fake(**kwargs):
        return {"content": json.dumps({
            "action": "write", "file": "../../escaped.yml",
            "content": "x: 1", "why": "nope"})}

    monkeypatch.setattr(llm, "fabric_call", _fake, raising=True)
    svc = tmp_path / "svc"
    svc.mkdir()
    fixed, still_open = asyncio.run(DA._escalate_gaps_to_llm(
        svc, [{"kind": "exotic_gap", "detail": "x"}]))
    assert fixed == []
    assert "outside the service" in still_open[0]["reason"]
    assert not (tmp_path / "escaped.yml").exists()


def test_a_skip_is_recorded_as_unresolved_not_as_a_fix(tmp_path, monkeypatch):
    """An invented database URL reported as fixed is worse than an honest
    skip, because nobody goes looking for it."""
    import llm

    async def _fake(**kwargs):
        return {"content": json.dumps({
            "action": "skip", "why": "I would need the real datasource"})}

    monkeypatch.setattr(llm, "fabric_call", _fake, raising=True)
    fixed, still_open = asyncio.run(DA._escalate_gaps_to_llm(
        tmp_path, [{"kind": "exotic_gap", "detail": "x"}]))
    assert fixed == []
    assert "real datasource" in still_open[0]["reason"]


def test_escalation_is_opt_out(tmp_path, monkeypatch):
    """`escalate=False` restores the purely deterministic behaviour."""
    import llm

    async def _boom(**kwargs):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(llm, "fabric_call", _boom, raising=True)
    monkeypatch.setattr(DA, "detect_structural_gaps",
                        lambda *a, **kw: [{"kind": "exotic_gap", "detail": "x"}])
    res = asyncio.run(DA.run_devops(tmp_path, escalate=False))
    assert res.fixes_applied == 0
    assert len(res.unresolved) == 1


# ── the prompts are in Prompt Library, like every other agent's ───────

@pytest.mark.parametrize("key", [
    "dcte.transformer", "dcte.build_fixer", "dcte.devops", "dcte.narrator",
])
def test_the_dcte_prompts_are_seeded(key):
    """DT-2: every other LLM agent read its prompt from the library; the
    DCTE agents held theirs as module constants, so an operator who went
    looking found four tools' prompts and not the fifth's."""
    from seed import GLOBAL_PROMPTS
    row = next((p for p in GLOBAL_PROMPTS if p["key"] == key), None)
    assert row is not None, f"{key} is not seeded"
    assert row.get("force_update") is True, "rev-bumps must reach existing installs"
    assert row["stage"] == "Tools"


@pytest.mark.parametrize("key", [
    "dcte.transformer", "dcte.build_fixer", "dcte.devops",
])
def test_the_seeded_rows_mark_where_the_computed_half_goes(key):
    """There are 98 selectable pairs; the target's idioms and the no-residue
    clause cannot be written down in a library row."""
    from seed import GLOBAL_PROMPTS
    row = next(p for p in GLOBAL_PROMPTS if p["key"] == key)
    assert PS.STACK_SECTIONS_PLACEHOLDER in row["template"]


def test_an_edited_row_never_loses_the_stack_guidance():
    """An operator who deletes the placeholder gets the computed sections
    appended, not dropped. Losing the target's conventions is worse than an
    oddly-ordered prompt."""
    out = PS.compose("MY EDITED PROMPT", "TARGET CONVENTIONS\n  Target: X.\n", "FB")
    assert "MY EDITED PROMPT" in out
    assert "TARGET CONVENTIONS" in out


def test_a_missing_row_falls_back_to_the_module_brief():
    """DCTE runs in a worker thread off the main loop; its agents must never
    fail because Mongo blinked or because the seed has not run yet."""
    assert PS.compose("", "SECTIONS", "THE MODULE BRIEF") == "THE MODULE BRIEF"


def test_the_guardrails_are_not_editable():
    """The size bounds and the residue reject list stay in code, where an
    edit to a prompt cannot silently weaken them."""
    from seed import GLOBAL_PROMPTS
    templates = " ".join(
        p["template"] for p in GLOBAL_PROMPTS if p["key"].startswith("dcte."))
    assert "_MIN_SIZE_RATIO" not in templates
    assert "0.55" not in templates
