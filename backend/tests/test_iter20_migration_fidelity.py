"""iter-20 — the source-stack residue gate, and the bug that disabled it.

`_structural_check` has carried a "source-stack signature residue" rule
since iter-15.57, but it never ran: its only caller looked the job up by
`{"transform_id": ...}` while `transformations` is keyed on `_id`, so the
lookup returned None on every call and both stacks arrived as `{}`. A
Helidon -> Spring Boot migration therefore had nothing to check against,
and shipped `io.helidon` imports with an ACCEPT verdict attached.

These tests pin three things:
  1. the residue rule actually fires on real leaked framework tokens;
  2. it does NOT fire on tokens the chosen target legitimately uses
     (the reason arming it needed target-subtraction first);
  3. the caller queries by the field the collection is actually keyed on.

(3) is asserted against the source text rather than behaviour on purpose
— the defect was invisible at runtime precisely because a wrong-key
find_one fails silently, so the regression it guards against is a wrong
key, not a wrong result.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

TOOLS_PY = Path(__file__).resolve().parents[1] / "routes" / "tools.py"


# ──────────────────────────────────────────────────────────────────────
# Load just the pure pieces of tools.py.
#
# Importing routes.tools pulls in FastAPI, Motor and the whole db module.
# The functions under test are pure, so lifting them out by AST keeps
# this suite fast and free of a live Mongo — the same trick the rest of
# the transformer suites use via FakeCollection, one level cheaper.
# ──────────────────────────────────────────────────────────────────────
def _load_pure_symbols() -> Dict[str, Any]:
    src = TOOLS_PY.read_text()
    tree = ast.parse(src)
    want_fn = {
        "_residue_tokens_for", "_structural_check", "_strip_comments_for_scan",
        "_playbook_for", "_manifest_migration_contract", "_source_manifest_notes",
        "_source_manifest_for",
    }
    want_const = {
        "_SOURCE_STACK_SIGNATURES", "_TARGET_STACK_SIGNATURES",
        "SUPPORTED_TRANSFORMATIONS", "_PROSE_RESIDUE_MARKERS_RE",
        "_SMART_PUNCT_MAP", "_LINE_COMMENT_MARKERS", "_BLOCK_COMMENT_SPANS",
        "_EXT_COMMENT_STYLE", "MIGRATION_PLAYBOOKS", "_PLAYBOOK_ALIASES",
        "_SOURCE_MANIFEST_BASENAMES", "_SOURCE_MANIFEST_MAX_CHARS",
        "BUILD_TOOL_SUGGESTIONS",
    }
    ns: Dict[str, Any] = {
        "re": re, "Dict": Dict, "List": List, "Set": Set, "Any": Any,
        "Tuple": Tuple, "Optional": Optional,
    }
    chunks: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in want_fn:
            chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name) and tgt.id in want_const:
                chunks.append(ast.get_source_segment(src, node))
    missing = (want_fn | want_const) - set()
    exec("\n".join(chunks), ns)  # noqa: S102 — test-only, reading our own repo
    for name in want_fn | want_const:
        assert name in ns, f"{name} not found in tools.py (renamed?) — {missing}"
    return ns


_NS = _load_pure_symbols()
_residue_tokens_for = _NS["_residue_tokens_for"]
_structural_check = _NS["_structural_check"]
_strip_comments_for_scan = _NS["_strip_comments_for_scan"]
_SOURCE_STACK_SIGNATURES = _NS["_SOURCE_STACK_SIGNATURES"]
SUPPORTED_TRANSFORMATIONS = _NS["SUPPORTED_TRANSFORMATIONS"]
_playbook_for = _NS["_playbook_for"]
_manifest_migration_contract = _NS["_manifest_migration_contract"]
_source_manifest_notes = _NS["_source_manifest_notes"]
_source_manifest_for = _NS["_source_manifest_for"]
MIGRATION_PLAYBOOKS = _NS["MIGRATION_PLAYBOOKS"]
_PLAYBOOK_ALIASES = _NS["_PLAYBOOK_ALIASES"]
BUILD_TOOL_SUGGESTIONS = _NS["BUILD_TOOL_SUGGESTIONS"]


HELIDON_STACK = {"framework": "helidon-mp", "language": "java", "runtime": "java-11"}
SPRINGBOOT_TARGET = {"backend": "spring-boot-3", "database": "postgresql"}

LEAKY_CONTROLLER = """package com.acme.negotiation;

import io.helidon.config.Config;
import jakarta.ws.rs.GET;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class NegotiationController {
    public String get() { return "ok"; }
}
"""

CLEAN_CONTROLLER = """package com.acme.negotiation;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class NegotiationController {
    @Value("${negotiation.mode}")
    private String mode;

    @GetMapping("/negotiation")
    public String get() { return mode; }
}
"""

LEAKY_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>negotiation-service</artifactId>
  <version>1.0.0</version>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.5</version>
  </parent>
  <dependencies>
    <dependency>
      <groupId>io.helidon.microprofile.bundles</groupId>
      <artifactId>helidon-microprofile</artifactId>
      <version>4.0.0</version>
    </dependency>
  </dependencies>
</project>
"""

CLEAN_POM = LEAKY_POM.replace(
    """    <dependency>
      <groupId>io.helidon.microprofile.bundles</groupId>
      <artifactId>helidon-microprofile</artifactId>
      <version>4.0.0</version>
    </dependency>
""",
    """    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
""",
)


# ── 1. The rule fires on real residue ─────────────────────────────────

def test_helidon_imports_in_a_springboot_file_are_a_critical_reject():
    r = _structural_check(
        LEAKY_CONTROLLER, "NegotiationController.java",
        HELIDON_STACK, SPRINGBOOT_TARGET,
    )
    assert r["ok"] is False
    assert r["severity"] == "critical"
    joined = " ".join(r["issues"])
    assert "io.helidon" in joined
    assert "remnant" in joined.lower()


def test_a_genuinely_migrated_springboot_file_passes():
    r = _structural_check(
        CLEAN_CONTROLLER, "NegotiationController.java",
        HELIDON_STACK, SPRINGBOOT_TARGET,
    )
    assert r["ok"] is True, r["issues"]


def test_a_pom_still_declaring_helidon_coordinates_is_rejected():
    """The reported symptom: the 'Spring Boot' pom carried Helidon deps.

    Manifests already flow through the same gate, so populating the
    signature table is all this needed — no separate manifest scanner.
    """
    r = _structural_check(LEAKY_POM, "pom.xml", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is False
    assert r["severity"] == "critical"
    assert "io.helidon" in " ".join(r["issues"])


def test_a_clean_springboot_pom_passes():
    r = _structural_check(CLEAN_POM, "pom.xml", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is True, r["issues"]


def test_all_offending_tokens_are_reported_not_just_the_first():
    """A pom with four leaked coordinates should tell the fix loop about
    four. Reporting only the first sent the loop round once per
    dependency, which is how a repair run burned its whole budget."""
    r = _structural_check(
        LEAKY_CONTROLLER, "X.java", HELIDON_STACK, SPRINGBOOT_TARGET,
    )
    issue = " ".join(r["issues"])
    assert "io.helidon" in issue and "jakarta.ws.rs" in issue


# ── 1b. Comments are not code ─────────────────────────────────────────

def test_a_migration_comment_naming_the_old_construct_is_not_residue():
    """The Coder prompt asks for `// MIGRATION:` notes "where a
    non-obvious change was made", so a correctly-migrated file routinely
    names the construct it replaced. Scanning raw text would reject
    exactly the files that documented themselves best."""
    body = """package com.acme.service;
import org.springframework.stereotype.Service;

@Service
public class NegotiationService {
    // MIGRATION: was EXECUTE IMMEDIATE in the legacy PL/SQL package, now JPA
    /* previously io.helidon.config.Config was injected here */
    public void settle(Long id) { repo.findById(id); }
}
"""
    r = _structural_check(body, "NegotiationService.java", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is True, r["issues"]


def test_a_commented_out_dependency_is_not_a_build_problem():
    body = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion>
  <!-- removed during migration:
  <dependency><groupId>io.helidon.microprofile.bundles</groupId></dependency>
  -->
  <dependencies><dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-web</artifactId>
  </dependency></dependencies>
</project>
"""
    r = _structural_check(body, "pom.xml", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is True, r["issues"]


def test_residue_inside_a_string_literal_is_still_residue():
    """Deliberate: string literals ARE scanned.

    Embedded SQL and reflective driver names are among the most important
    residue cases — `Class.forName("oracle.jdbc.OracleDriver")` and a
    `FROM DUAL` inside a query string are real migration failures that
    live nowhere else. The cost is that a documentation URL mentioning
    `oracle.jdbc` would also flag; that is rare, and worth a human look
    when it happens.
    """
    body = """package com.acme.repo;
public class LegacyRepo {
    private static final String Q = "SELECT sysdate FROM DUAL";
    static { Class.forName("oracle.jdbc.OracleDriver"); }
}
"""
    r = _structural_check(body, "LegacyRepo.java", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is False
    joined = " ".join(r["issues"])
    assert "oracle.jdbc" in joined
    assert "FROM DUAL" in joined


def test_a_url_containing_a_double_slash_does_not_truncate_the_scan():
    """Quote tracking exists so `"https://..."` is not mistaken for a
    line comment — which would blank the rest of the line and hide real
    residue after it."""
    body = """package a;
public class Cfg {
    String docs = "https://example.com/docs";
    String url = "jdbc:oracle:thin:@legacy-db:1521:ORCL";
}
"""
    r = _structural_check(body, "Cfg.java", HELIDON_STACK, SPRINGBOOT_TARGET)
    assert r["ok"] is False
    assert "jdbc:oracle:" in " ".join(r["issues"])


def test_stripping_preserves_line_count():
    """Comments are blanked, not deleted, so any downstream line/column
    arithmetic over the scanned text stays aligned with the real file."""
    body = "line1();\n// a comment\n/* block\n   spans */\nline5();\n"
    out = _strip_comments_for_scan(body, "x.java")
    assert out.count("\n") == body.count("\n")
    assert len(out) == len(body)
    assert "comment" not in out and "block" not in out
    assert "line1();" in out and "line5();" in out


def test_files_with_no_known_comment_syntax_are_scanned_verbatim():
    body = "io.helidon something\n"
    assert _strip_comments_for_scan(body, "notes.unknownext") == body


# ── 2. Target subtraction — why this could not just be switched on ────

def test_target_tokens_are_not_flagged_as_residue():
    """`jakarta.ws.rs` is residue for a Spring Boot target and CORRECT
    for a Quarkus one. Without subtracting the target's own vocabulary,
    arming this gate would have rejected legitimate output."""
    quarkus = {"backend": "quarkus"}
    toks = _residue_tokens_for(HELIDON_STACK, quarkus)
    assert "jakarta.ws.rs" not in toks
    assert "javax.ws.rs" not in toks
    # ...but a genuine Helidon remnant is still residue on Quarkus.
    assert "io.helidon" in toks


def test_springboot_target_does_flag_jakarta_ws_rs():
    toks = _residue_tokens_for(HELIDON_STACK, SPRINGBOOT_TARGET)
    assert "jakarta.ws.rs" in toks


@pytest.mark.parametrize("token", ["%TYPE", "%ROWTYPE", "END LOOP;", "to_date("])
def test_valid_plpgsql_is_never_treated_as_oracle_residue(token):
    """PostgreSQL supports these. Listing them would reject a correct
    Oracle -> PostgreSQL migration."""
    toks = _residue_tokens_for(
        {"framework": "oracle", "language": "plsql"},
        {"database": "postgresql"},
    )
    assert token not in toks


def test_oracle_only_constructs_are_residue_on_postgres():
    toks = _residue_tokens_for(
        {"framework": "oracle", "language": "plsql"},
        {"database": "postgresql"},
    )
    for t in ("VARCHAR2", "SYSDATE", "ROWNUM", "FROM DUAL", "NVL("):
        assert t in toks, t


# ── 3. Resolution from the declared pair, not just the sniffed name ───

def test_residue_resolves_from_the_declared_transformation_pair():
    """Even when `tech_detector` returns something the substring test
    misses, the operator's own target selection still arms the gate."""
    toks = _residue_tokens_for({"framework": "", "language": ""}, SPRINGBOOT_TARGET)
    assert "io.helidon" in toks


def test_every_advertised_transformation_has_signatures_for_its_sources():
    """The table was missing helidon, jaxrs, ejb, oracle and jquery —
    the source side of five of the six advertised transformations. This
    asserts the gap cannot silently reopen."""
    missing = []
    for pair_id, spec in SUPPORTED_TRANSFORMATIONS.items():
        for src_key in spec.get("source") or []:
            if str(src_key).lower() not in _SOURCE_STACK_SIGNATURES:
                missing.append(f"{pair_id} -> {src_key}")
    assert not missing, f"no residue signatures for: {missing}"


def test_no_signatures_when_nothing_identifies_the_source():
    """An unknown stack must produce an empty set, not a default one —
    flagging tokens we cannot justify would be worse than not checking."""
    assert _residue_tokens_for({}, {"backend": "some-unknown-framework"}) == []


# ── 4. The defect itself: the caller's query key ──────────────────────

def test_the_verifier_looks_the_job_up_by_the_key_the_collection_uses():
    """The original bug in one assertion.

    `transformations` is keyed on `_id`; `transform_id` is not a field on
    the document. A find_one with the wrong key returns None silently, so
    nothing at runtime revealed that the structural gate was receiving {}
    for both stacks. Asserting on the source text is the only way to
    catch a reintroduction before it reaches production again.
    """
    src = TOOLS_PY.read_text()
    # Scoped to `transformations` only. `{"transform_id": ...}` is the
    # CORRECT query for transform_files / transformer_tasks / the log
    # collections, which really do carry that field — asserting on the
    # bare string would forbid a dozen legitimate queries.
    bad = re.findall(
        r"transformations\.find_one\(\s*\{\s*[\"']transform_id[\"']",
        src,
    )
    assert not bad, (
        "transformations is keyed on _id — a find_one by transform_id "
        "matches nothing and fails silently"
    )


def test_the_fix_loops_read_the_field_the_job_actually_stores():
    """`detected_stack` is not a field on a transformations document; the
    detected stack is stored as `source_stack`."""
    src = TOOLS_PY.read_text()
    assert 'tx.get("detected_stack")' not in src, (
        "the stored field is source_stack — tx.get('detected_stack') is "
        "always {}"
    )


# ── 5. The manifest is no longer authored blind ───────────────────────

HELIDON_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <groupId>com.acme</groupId>
  <artifactId>negotiation-service</artifactId>
  <version>2.1.0</version>
  <dependencies>
    <dependency>
      <groupId>io.helidon.microprofile.bundles</groupId>
      <artifactId>helidon-microprofile</artifactId>
    </dependency>
    <dependency>
      <groupId>com.oracle.database.jdbc</groupId>
      <artifactId>ojdbc11</artifactId>
    </dependency>
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>3.14.0</version>
    </dependency>
  </dependencies>
</project>
"""


def test_the_manifest_task_carries_the_source_manifest():
    """The root cause of the invented pom.

    A synthetic manifest task had `source_path: ""`, so `_run_coder` ran
    with `source_content=""` — the target pom was authored having never
    seen the source pom. It could not carry the project's real third-party
    dependencies across (commons-lang3 here) nor knowingly drop the
    framework's.
    """
    notes = _source_manifest_notes(
        {"path": "negotiation-service/pom.xml", "content": HELIDON_POM},
        HELIDON_STACK, SPRINGBOOT_TARGET,
    )
    assert "BEGIN SOURCE MANIFEST" in notes
    assert "commons-lang3" in notes, "the source manifest body was not included"
    assert "io.helidon" in notes


def test_no_source_manifest_means_no_fabricated_block():
    """A project with no build file must not be handed an empty section
    that reads as 'there were no dependencies'."""
    assert _source_manifest_notes(None, HELIDON_STACK, SPRINGBOOT_TARGET) == ""
    assert _source_manifest_notes(
        {"path": "pom.xml", "content": "   "}, HELIDON_STACK, SPRINGBOOT_TARGET,
    ) == ""


def test_the_contract_says_keep_third_party_and_remove_framework():
    """Half a dependency list must survive verbatim and half must
    disappear — nothing in the generic per-layer guidance said so."""
    c = _manifest_migration_contract(HELIDON_STACK, SPRINGBOOT_TARGET)
    assert "KEEP" in c and "REMOVE" in c
    assert "comment" in c.lower(), "must forbid commenting deps out rather than deleting"


def test_forbidden_list_holds_coordinates_not_sql_or_annotations():
    """A dependency list cannot contain `NVL(` or `@Produces(`. Listing
    them spends tokens forbidding something impossible and buries the two
    entries that matter."""
    c = _manifest_migration_contract(HELIDON_STACK, SPRINGBOOT_TARGET)
    forbidden = c.split("FORBIDDEN")[-1]
    assert "io.helidon" in forbidden
    assert "oracle.jdbc" in forbidden
    for noise in ("NVL(", "@Produces(", "FROM DUAL", "SYSDATE", "VARCHAR2"):
        assert noise not in forbidden, f"{noise} is not a dependency coordinate"


def test_source_manifest_is_matched_to_its_module_first():
    manifests = {
        "": {"path": "pom.xml", "content": "root"},
        "negotiation-service": {"path": "negotiation-service/pom.xml", "content": "mod"},
    }
    assert _source_manifest_for(manifests, "negotiation-service")["content"] == "mod"


def test_the_root_manifest_is_the_fallback_for_an_unknown_module():
    """A parent pom governs its children's versions, so it is the right
    second choice — better than generating with nothing."""
    manifests = {"": {"path": "pom.xml", "content": "root"}}
    assert _source_manifest_for(manifests, "some-module")["content"] == "root"


def test_any_manifest_beats_none():
    manifests = {"weird/place": {"path": "weird/place/pom.xml", "content": "x"}}
    assert _source_manifest_for(manifests, "other")["content"] == "x"


def test_no_manifests_at_all_returns_none():
    assert _source_manifest_for({}, "mod") is None


# ── 6. Per-language playbooks ─────────────────────────────────────────

def test_the_springboot_playbook_names_the_idioms_that_actually_drift():
    pb = _playbook_for({"backend": "spring-boot-3"})
    assert "@RestController" in pb
    assert "JAX-RS" in pb, "must say explicitly which idiom it replaces"
    assert "constructor injection" in pb


def test_the_playbook_covers_every_component_of_the_target():
    """A Helidon+Oracle -> SpringBoot+Postgres job needs BOTH halves."""
    pb = _playbook_for({"backend": "spring-boot-3", "database": "postgresql"})
    assert "spring-boot-3" in pb
    assert "postgresql" in pb
    assert "VARCHAR2" in pb


def test_swagger_is_required_for_java_and_python_backends():
    """The operator's own brief asks for it by name; it is not something
    the model volunteers."""
    for target, marker in (
        ({"backend": "spring-boot-3"}, "springdoc"),
        ({"backend": "fastapi"}, "/docs"),
        ({"backend": "dotnet"}, "Swashbuckle"),
        ({"backend": "express"}, "swagger-ui-express"),
    ):
        assert marker in _playbook_for(target), target


def test_a_versioned_or_sibling_target_resolves_through_an_alias():
    assert "spring-boot-3" in _playbook_for({"backend": "spring-boot-2"})
    assert "postgresql" in _playbook_for({"database": "mysql"})


def test_an_unknown_target_degrades_to_no_playbook():
    """Better to fall back to the stack-agnostic prompt than to invent
    guidance for a stack we have not described."""
    assert _playbook_for({"backend": "cobol-cics"}) == ""
    assert _playbook_for({}) == ""
    assert _playbook_for("") == ""


def test_a_v1_string_target_resolves_too():
    """v1 jobs store a single collapsed string, not a dict."""
    assert "@RestController" in _playbook_for("spring-boot-3")


def test_the_playbook_is_not_duplicated_when_components_agree():
    """runtime and backend both naming a Spring target must not print the
    block twice — it is injected into every Coder call."""
    pb = _playbook_for({"backend": "spring-boot-3", "runtime": "spring-boot-2"})
    assert pb.count("TARGET `spring-boot-3`") == 1


def test_the_playbook_says_the_target_wins_on_conflict():
    pb = _playbook_for({"backend": "spring-boot-3"})
    assert "playbook wins" in pb


def test_every_playbook_entry_is_a_real_build_target():
    """A playbook keyed on a stack id nothing can select would never
    render — dead configuration that reads as coverage."""
    known = set(BUILD_TOOL_SUGGESTIONS) | {
        str(v.get("target")) for v in SUPPORTED_TRANSFORMATIONS.values()
    }
    for key in MIGRATION_PLAYBOOKS:
        assert key in known, f"{key} is not a selectable target stack"


def test_every_alias_points_at_a_real_playbook():
    for alias, target in _PLAYBOOK_ALIASES.items():
        assert target in MIGRATION_PLAYBOOKS, f"{alias} -> {target} is dangling"


@pytest.mark.parametrize("target", list(SUPPORTED_TRANSFORMATIONS.values()))
def test_every_advertised_transformation_target_has_a_playbook(target):
    """The six advertised migrations are exactly the ones the product
    promises; each needs its target described."""
    tid = str(target.get("target"))
    assert tid in MIGRATION_PLAYBOOKS or tid in _PLAYBOOK_ALIASES, tid
