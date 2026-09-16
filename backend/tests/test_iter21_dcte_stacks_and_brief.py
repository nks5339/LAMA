"""iter-21 — Direct Transform: stack catalogue, generic brief, generic plugin.

Three things land together because they are one feature: you cannot offer a
stack in the dropdown unless the prompt knows what it is AND some plugin can
run the pair.

What this pins:

  1. **The catalogue** — the stacks the operator asked for are selectable,
     each in the right dropdown, and every pinned version is the one the
     vendor currently ships (checked Sept 2026, recorded in `stacks.py`).

  2. **The brief is generic** — it is built from the selected pair, so a
     JSP -> React job is no longer told to migrate Helidon. The role, the
     three tasks and the three strict rules are identical for every pair;
     only the stack-specific sections change.

  3. **Every selectable pair runs** — the engine raises on an unresolved
     pair, so `resolve` falls back to the generic AI plugin. Without that,
     each stack added to the dropdown would be selectable and dead on start.

Offline: pure data and string building, no LLM, no Mongo, no subprocess.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND))

from dcte import stacks as stacks_mod  # noqa: E402
from dcte.plugin_registry import PluginRegistry, get_registry, reset_registry  # noqa: E402
from dcte.plugin_base import TransformContext  # noqa: E402
from dcte.plugins.generic_ai.plugin import GenericAiPlugin  # noqa: E402
from dcte.prompt_builder import (  # noqa: E402
    BRIEF_REV,
    build_migration_brief,
    build_transformer_brief,
    build_fixup_directive,
)

# The five the operator asked for, plus the pair that already worked.
REQUESTED = ("jsp", "react-19", "angular-22", "dotnet-10", "spring-boot-4")


# ---------------------------------------------------------------------------
# 1 — catalogue
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stack_id", REQUESTED)
def test_requested_stacks_are_in_the_catalogue(stack_id):
    s = stacks_mod.get_stack(stack_id)
    assert s is not None, f"{stack_id} missing from the catalogue"
    assert s.label and s.family and s.language


def test_jsp_is_a_source_and_the_modern_targets_are_targets():
    """JSP is migrate-FROM only; nothing should offer 'migrate TO JSP'."""
    source_ids = {s.id for s in stacks_mod.sources()}
    target_ids = {s.id for s in stacks_mod.targets()}
    assert "jsp" in source_ids
    assert "jsp" not in target_ids
    for sid in ("react-19", "angular-22", "dotnet-10", "spring-boot-4"):
        assert sid in target_ids, f"{sid} should be selectable as a target"


def test_no_stack_offers_migrating_to_a_dead_framework():
    """Struts/EJB/jQuery/Helidon are legacy: sources only."""
    target_ids = {s.id for s in stacks_mod.targets()}
    for sid in ("struts", "ejb", "jquery", "helidon-mp", "helidon-se", "oracle"):
        assert sid not in target_ids, f"{sid} must not be offered as a target"


def test_pinned_versions_are_the_ones_researched_in_sept_2026():
    """A version the vendor already retired is worse than none: it tells the
    model to emit code that is out of support the day it is generated."""
    expected = {
        "spring-boot-4": "4.1",     # 4.1.1 GA Aug 2026
        "dotnet-10": "10.0",        # LTS Nov 2025 -> Nov 2028
        "angular-22": "22",         # released Jun 2026
        "react-19": "19",           # 19.3.0, Sept 2026
        "postgres-18": "18",        # supported to Nov 2030
        "jsp": "4.0",               # Jakarta Pages 4.0, Jakarta EE 11
    }
    for sid, version in expected.items():
        assert stacks_mod.get_stack(sid).version == version


def test_modern_targets_name_their_lts_runtime_in_the_label():
    """The label is the only thing the operator reads in the dropdown, so the
    runtime they are committing to has to be in it."""
    assert "Java 25" in stacks_mod.get_stack("spring-boot-4").label
    assert "LTS" in stacks_mod.get_stack("dotnet-10").label
    for sid in ("react-19", "angular-22"):
        assert "Node 26" in stacks_mod.get_stack(sid).label


def test_legacy_ids_still_resolve_so_old_jobs_keep_working():
    """A job saved before iter-21 must still resolve its stacks."""
    for sid in ("helidon-mp", "spring-boot-3", "oracle", "postgres-15"):
        assert stacks_mod.get_stack(sid) is not None


def test_aliases_and_loose_formatting_resolve():
    assert stacks_mod.get_stack("springboot").id == "spring-boot-4"
    assert stacks_mod.get_stack("DOTNET").id == "dotnet-10"
    assert stacks_mod.get_stack("react").id == "react-19"
    assert stacks_mod.get_stack("  Angular  ").id == "angular-22"
    assert stacks_mod.get_stack("plsql").id == "oracle"


def test_unknown_stack_degrades_instead_of_raising():
    assert stacks_mod.get_stack("cobol-85") is None
    assert stacks_mod.get_stack("") is None
    # ...but it still has to render somewhere.
    assert stacks_mod.stack_label("cobol-85") == "cobol-85"
    assert stacks_mod.stack_label("") == "unknown"


def test_catalogue_payload_is_the_shape_the_dropdowns_consume():
    c = stacks_mod.describe_catalogue()
    assert set(c) == {"families", "sources", "targets"}
    for row in c["sources"] + c["targets"]:
        assert set(row) == {"id", "label", "family", "language", "version", "role"}
    # Prompt-only fields must not leak to the browser.
    assert all("forbidden" not in r and "idioms" not in r
               for r in c["sources"] + c["targets"])


def test_every_family_in_the_catalogue_has_a_declared_order():
    families = {s.family for s in stacks_mod.STACKS}
    assert families <= set(stacks_mod.FAMILY_ORDER)


# ---------------------------------------------------------------------------
# 2 — the brief is generic
# ---------------------------------------------------------------------------
def test_brief_names_the_selected_pair_not_helidon():
    """The defect this replaces: every job got a Helidon -> Spring essay."""
    brief = build_migration_brief("jsp", "react-19")
    assert "JSP" in brief and "React 19" in brief
    assert "Helidon" not in brief
    assert "@ApplicationScoped" not in brief


@pytest.mark.parametrize("pair", [
    ("helidon-mp", "spring-boot-4"),
    ("jsp", "react-19"),
    ("jsp", "angular-22"),
    ("struts", "spring-boot-4"),
    ("ejb", "dotnet-10"),
    ("oracle", "postgres-18"),
    ("jquery", "react-19"),
])
def test_operator_contract_is_identical_for_every_pair(pair):
    """Role, the three tasks and the three strict rules are the operator's
    contract — they must not drift between stack pairs."""
    src, tgt = pair
    for brief in (build_migration_brief(src, tgt), build_transformer_brief(src, tgt)):
        assert brief.startswith(
            "You are a Senior Backend Developer and Legacy-to-Modernization expert.")
        assert "TASK" in brief
        assert "Implement Swagger / OpenAPI." in brief
        assert "error free" in brief or "build-error free" in brief
        assert "STRICT RULES" in brief
        assert "Do NOT alter any business logic" in brief
        assert "Do NOT conclude with broken code" in brief
        assert "token efficiency with 100% accuracy" in brief


def test_brief_carries_the_targets_idioms_and_manifest():
    brief = build_migration_brief("helidon-mp", "spring-boot-4")
    assert "@RestController" in brief
    assert "spring-boot-starter-parent 4.1.x" in brief
    assert "mvn -q -DskipTests compile" in brief

    dotnet = build_migration_brief("ejb", "dotnet-10")
    assert "ASP.NET Core minimal APIs" in dotnet
    assert "net10.0" in dotnet
    assert "dotnet build" in dotnet


def test_brief_forbids_the_sources_markers_not_another_stacks():
    """The residue clause comes from the SOURCE, which is what makes a
    half-migrated file detectable."""
    jsp = build_migration_brief("jsp", "react-19")
    assert "`<%`" in jsp and "`<jsp:`" in jsp
    assert "io.helidon" not in jsp

    helidon = build_migration_brief("helidon-mp", "spring-boot-4")
    assert "`io.helidon.`" in helidon
    assert "<jsp:" not in helidon


def test_unknown_stack_gets_a_usable_brief_with_no_borrowed_rules():
    """An empty stack-specific section beats another stack's rules."""
    brief = build_migration_brief("cobol-85", "rust-axum")
    assert "cobol-85" in brief and "rust-axum" in brief
    assert "STRICT RULES" in brief
    # No other stack's vocabulary leaked in.
    for alien in ("@RestController", "io.helidon", "VARCHAR2", "ASP.NET"):
        assert alien not in brief


def test_transformer_brief_is_per_file_and_agentic_brief_has_a_shell():
    """Same content, two renderings — only the agentic one gets a workflow."""
    t = build_transformer_brief("helidon-mp", "spring-boot-4")
    a = build_migration_brief("helidon-mp", "spring-boot-4")
    assert "--cwd" in a and "DROID_AGENT_OK" in a
    assert "--cwd" not in t and "DROID_AGENT_OK" not in t


def test_fixup_directive_forbids_leave_and_names_the_source():
    d = build_fixup_directive("helidon-mp", "spring-boot-4")
    assert '"action":"rewrite"' in d
    assert "NOT acceptable" in d
    assert "Helidon" in d


def test_brief_rev_is_recorded_so_a_stale_brief_is_identifiable():
    assert isinstance(BRIEF_REV, int) and BRIEF_REV >= 2


def test_ai_refactor_composes_the_brief_with_the_json_contract():
    """The parser's schema has to survive the prompt becoming generic."""
    from dcte.ai_refactor import _system_prompt, _fixup_prompt
    p = _system_prompt("jsp", "angular-22")
    assert "Angular 22" in p
    assert '"action": "rewrite" | "leave"' in p
    assert "STRICT JSON" in p
    f = _fixup_prompt("jsp", "angular-22")
    assert "RESIDUE REMEDIATION" in f and "STRICT JSON" in f


# ---------------------------------------------------------------------------
# 3 — every selectable pair actually runs
# ---------------------------------------------------------------------------
def test_deterministic_pairs_keep_their_own_plugin():
    reg = get_registry()
    assert reg.resolve("helidon-mp", "spring-boot-3").id == "helidon-mp-to-spring-boot-3"
    assert reg.resolve("oracle", "postgres-15").id == "oracle-to-postgres"


@pytest.mark.parametrize("pair", [
    ("jsp", "react-19"),
    ("jsp", "angular-22"),
    ("dotnet-10", "spring-boot-4"),
    ("helidon-mp", "spring-boot-4"),   # modern target, no deterministic plugin
    ("oracle", "postgres-18"),         # modern target, no deterministic plugin
    ("cobol-85", "rust-axum"),         # not even in the catalogue
])
def test_every_other_pair_resolves_to_the_generic_plugin(pair):
    """The engine raises on `resolve() is None`, so a dropdown entry without
    a plugin would be selectable and dead on start."""
    plugin = get_registry().resolve(*pair)
    assert plugin is not None, f"{pair} would hard-fail the job"
    assert plugin.id == "generic-ai"


def test_generic_plugin_is_not_listed_as_a_concrete_transformation():
    """`/plugins` drives the 'deterministic vs AI' hint — a wildcard entry
    there would claim every pair has a real transformer."""
    ids = {p["id"] for p in get_registry().describe_all()}
    assert "generic-ai" not in ids
    assert ids == {"helidon-mp-to-spring-boot-3", "oracle-to-postgres"}


def test_resolve_exact_reports_honestly_whether_a_pair_is_deterministic():
    reg = get_registry()
    assert reg.resolve_exact("helidon-mp", "spring-boot-3") is not None
    assert reg.resolve_exact("jsp", "react-19") is None


def test_registry_without_a_fallback_still_returns_none():
    """`resolve` must not invent a plugin when none was registered."""
    reg = PluginRegistry()
    assert reg.resolve("jsp", "react-19") is None


def _ctx(tmp_path: Path, src_stack="jsp", tgt_stack="react-19", ai=True) -> TransformContext:
    src = tmp_path / "src"
    (src / "web").mkdir(parents=True)
    (src / "web" / "list.jsp").write_text("<% out.print(1); %>", encoding="utf-8")
    (src / "web" / "app.js").write_text("$(document).ready(function(){});", encoding="utf-8")
    (src / "target").mkdir()
    (src / "target" / "ignored.java").write_text("class X {}", encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG")
    dest = tmp_path / "out" / "converted-source" / "svc"
    return TransformContext(
        job_id="j", service_id="s", source_path=src, module_path=None,
        destination_path=dest, source_stack=src_stack, target_stack=tgt_stack,
        output_root=tmp_path / "out", options={"ai_refactor": ai},
    )


def test_generic_plugin_stages_source_files_and_skips_build_output(tmp_path):
    ctx = _ctx(tmp_path)
    p = GenericAiPlugin()
    a = p.analyze(ctx)
    names = {Path(f).name for f in a.matched_files}
    assert names == {"list.jsp", "app.js"}, names   # target/ and .png excluded

    r = p.transform(ctx, a)
    assert (ctx.destination_path / "web" / "list.jsp").is_file()
    assert (ctx.destination_path / "web" / "app.js").is_file()
    # Layout preserved, build output never copied.
    assert not (ctx.destination_path / "target").exists()


def test_generic_plugin_never_reports_a_copy_as_a_successful_migration(tmp_path):
    ctx = _ctx(tmp_path)
    p = GenericAiPlugin()
    r = p.transform(ctx, p.analyze(ctx))
    assert r.files, "nothing staged"
    assert all(f.status == "needs_manual" for f in r.files)
    assert r.manual_intervention


def test_generic_plugin_warns_up_front_when_the_ai_pass_is_off(tmp_path):
    """With ai_refactor off this plugin only copies — say so before the run,
    not after a 'completed' job produced an unchanged tree."""
    ctx = _ctx(tmp_path, ai=False)
    msgs = " ".join(f["message"] for f in GenericAiPlugin().analyze(ctx).findings)
    assert "AI-assisted refactor is OFF" in msgs

    on = " ".join(f["message"] for f in GenericAiPlugin().analyze(_ctx(tmp_path / "b")).findings)
    assert "AI-assisted refactor is OFF" not in on


def test_generic_plugin_flags_a_stack_it_has_no_conventions_for(tmp_path):
    ctx = _ctx(tmp_path, src_stack="cobol-85", tgt_stack="react-19")
    msgs = " ".join(f["message"] for f in GenericAiPlugin().analyze(ctx).findings)
    assert "not in the catalogue" in msgs


def test_generic_plugin_reports_a_missing_source_without_raising(tmp_path):
    ctx = TransformContext(
        job_id="j", service_id="s", source_path=tmp_path / "nope", module_path=None,
        destination_path=tmp_path / "out", source_stack="jsp", target_stack="react-19",
        output_root=tmp_path, options={},
    )
    a = GenericAiPlugin().analyze(ctx)
    assert a.risk_level == "high"
    assert any(f["level"] == "error" for f in a.findings)


def test_generic_plugin_report_says_it_was_not_deterministic(tmp_path):
    ctx = _ctx(tmp_path)
    p = GenericAiPlugin()
    a = p.analyze(ctx)
    r = p.transform(ctx, a)
    b = p.generate_report(ctx, a, r, p.validate(ctx, r))
    assert b.metrics["deterministic"] is False
    assert "generic AI-driven plugin" in b.summary_md
    assert "needs_manual" in b.summary_md


def teardown_module(_m):
    """Other suites build their own registries; leave the singleton clean."""
    reset_registry()


# ---------------------------------------------------------------------------
# 4 — the build / DevOps / Tester agents are actually reachable
# ---------------------------------------------------------------------------
# Found by running a live JSP -> React job and reading the event log:
#
#   [warn/build]  Build agent errored: 'TransformContext' object has no
#                 attribute 'dest_root'
#   [warn/devops] DevOps agent errored: ... no attribute 'dest_root'
#   [warn/test]   Tester agent errored: ... no attribute 'dest_root'
#
# TransformContext has `destination_path`; `dest_root` was only ever a local
# name in the same function. All three call sites read it off the context, so
# all three raised AttributeError — swallowed by the surrounding
# `except Exception` into a warn. The three agents had never once executed,
# and every job still reported `completed`.
def test_build_devops_tester_receive_the_destination_path(tmp_path):
    from dcte.engine import TransformationEngine
    from dcte.models import DcteJob, ServiceConfig

    src = tmp_path / "src"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "src/main/java/com/x/A.java").write_text("class A {}", encoding="utf-8")
    out = tmp_path / "out"

    job = DcteJob(
        name="agents reachable", source_root=str(src), output_root=str(out),
        services=[ServiceConfig(
            name="svc", source_path=str(src), destination_path=str(out),
            source_stack="jsp", target_stack="react-19",
        )],
        ai_refactor=False, generate_cicd=[],
    )

    seen: dict[str, Path] = {}

    def _build(dest_root, service_id):
        seen["build"] = Path(dest_root)
        return {"skipped": True, "success": True, "attempted": False, "notes": [],
                "attempts": 0, "fixes_applied": 0, "errors": [], "tool": None,
                "final_output_tail": ""}

    def _devops(dest_root, service_id, build_result):
        seen["devops"] = Path(dest_root)
        return {"attempted": True, "fixes_applied": 0, "gaps_found": [],
                "fixes": [], "unresolved": [], "notes": []}

    def _tester(dest_root, service_id, source_root):
        seen["tester"] = Path(dest_root)
        return {"verdict": "PASS", "notes": [], "parity_endpoints": {},
                "parity_config": {}, "residual": [],
                "boot_smoke": {"runnable": False, "reason": "test", "healthy": False}}

    events: list = []
    out_job = TransformationEngine().run_job(
        job,
        event_sink=events.append,
        build_fix_fn=_build, devops_fn=_devops, tester_fn=_tester,
    )

    # Every one of the three ran, and got the real destination tree.
    assert set(seen) == {"build", "devops", "tester"}, f"never invoked: {seen}"
    expected = (out / "converted-source" / "svc").resolve()
    for agent, got in seen.items():
        assert got.resolve() == expected, f"{agent} got {got}, expected {expected}"

    # ...and none of them reported the AttributeError this test exists for.
    errs = [e.message for e in events if "dest_root" in (e.message or "")]
    assert errs == [], errs
    assert out_job.status.value in ("completed", "reporting")
