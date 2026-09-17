"""iter-18 — DCTE (Direct Code Transformation Engine) unit tests.

Covers:
    * project detection (Helidon MP + Oracle SQL sample fixtures)
    * plugin registry (resolve by stack pair)
    * Helidon → Spring endpoint transformer (imports, annotations, mappings)
    * Helidon → Spring plugin end-to-end (writes files, generates POM,
      Application.java, SecurityConfig.java, application.properties)
    * Oracle → PG SQL translator (types, funcs, sequences, DECODE, DUAL)
    * Oracle → PG plugin end-to-end
    * Dependency migrator (POM rewrite, .bak preserved)
    * Impact analyzer (weighted formula + risk multiplier)
    * CI/CD generator writes template files
    * Engine `run_job` drives full lifecycle with in-memory sinks

Runs entirely offline — no LLM calls, no Mongo required. Uses the
shipped sample fixtures under `backend/dcte/samples/*`.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND))

from dcte import (  # noqa: E402
    TransformationEngine,
    ProjectDetector,
    DcteJob,
    ServiceConfig,
    DcteJobStatus,
    get_registry,
)
from dcte.plugin_registry import reset_registry  # noqa: E402
from dcte.dependency_migrator import DependencyMigrator  # noqa: E402
from dcte.impact_analyzer import ImpactAnalyzer  # noqa: E402
from dcte.cicd_generator import CicdGenerator  # noqa: E402
from dcte.plugins.helidon_to_spring.endpoint_transformer import EndpointTransformer  # noqa: E402
from dcte.plugins.oracle_to_postgres.sql_translator import SqlTranslator  # noqa: E402

SAMPLES = BACKEND / "dcte" / "samples"
HELIDON = SAMPLES / "helidon_sample"
ORACLE = SAMPLES / "oracle_sample"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
def test_detector_helidon_mp():
    fp = ProjectDetector().detect(str(HELIDON))
    assert fp.exists and fp.is_valid
    assert fp.detected_stack == "helidon-mp"
    assert fp.confidence >= 0.9
    assert fp.suggested_target == "spring-boot-3"


def test_detector_oracle():
    fp = ProjectDetector().detect(str(ORACLE))
    assert fp.exists and fp.is_valid
    assert fp.detected_stack == "oracle"
    assert fp.suggested_target == "postgres-15"


def test_detector_missing_path():
    fp = ProjectDetector().detect("/definitely/not/a/real/path/xyzzy")
    assert not fp.is_valid
    assert fp.detected_stack == "unknown"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_registry_resolves_helidon_and_oracle():
    reset_registry()
    reg = get_registry()
    ids = {p["id"] for p in reg.describe_all()}
    assert "helidon-mp-to-spring-boot-3" in ids
    assert "oracle-to-postgres" in ids
    assert reg.resolve("helidon-mp", "spring-boot-3") is not None
    assert reg.resolve("oracle", "postgres-15") is not None
    # iter-21 — an unregistered pair no longer resolves to None. It used to,
    # and the engine raises on None, so every stack added to the dropdown
    # without a hand-written transformer would have been selectable and dead
    # on start. `resolve` now falls back to the generic AI plugin; the
    # question this line was really asking ("is there a DETERMINISTIC plugin
    # for this pair?") is `resolve_exact`.
    assert reg.resolve_exact("cobol", "rust") is None
    assert reg.resolve("cobol", "rust").id == "generic-ai"


# ---------------------------------------------------------------------------
# Endpoint transformer (unit)
# ---------------------------------------------------------------------------
def test_endpoint_transformer_rewrites_resource_class():
    src = (HELIDON / "src/main/java/com/example/pmis/ProjectResource.java").read_text()
    new_src, meta = EndpointTransformer().apply(src)
    # Class-level mapping
    assert "@RestController" in new_src
    assert '@RequestMapping("/api/projects")' in new_src
    assert meta["class_paths"] == ["/api/projects"]
    # Method mappings
    assert "@GetMapping" in new_src
    assert "@PostMapping" in new_src
    assert '@GetMapping("{id}")' in new_src
    assert '@PostMapping("{id}/approve")' in new_src
    # DI
    assert "@Autowired" in new_src
    assert "@Inject" not in new_src
    # Param annotations
    assert '@PathVariable("id")' in new_src
    assert '@RequestParam("page")' in new_src
    # Config
    assert "@Value" in new_src
    assert "@ConfigProperty" not in new_src
    # Security
    assert '@PreAuthorize("isAuthenticated()")' in new_src
    # Imports rewritten
    assert "import org.springframework.web.bind.annotation.RestController;" in new_src
    assert "import org.springframework.web.bind.annotation.RequestMapping;" in new_src
    assert "import org.springframework.web.bind.annotation.GetMapping;" in new_src
    assert "import org.springframework.beans.factory.annotation.Autowired;" in new_src
    # JAX-RS gone
    assert "jakarta.ws.rs.GET" not in new_src
    assert "jakarta.ws.rs.Path" not in new_src


# ---------------------------------------------------------------------------
# SQL translator (unit)
# ---------------------------------------------------------------------------
def test_sql_translator_full_sample():
    src = (ORACLE / "schema.sql").read_text()
    out, meta = SqlTranslator().translate(src)
    # types
    assert "VARCHAR2" not in out
    assert "VARCHAR(32)" in out
    assert "NUMERIC(18,2)" in out
    assert "TEXT" in out            # CLOB → TEXT
    assert "TIMESTAMPTZ" in out     # TIMESTAMP WITH LOCAL TIME ZONE
    # funcs
    assert "SYSDATE" not in out
    assert "CURRENT_TIMESTAMP" in out
    assert "COALESCE(" in out       # NVL → COALESCE
    # DECODE → CASE
    assert "DECODE(" not in out
    assert "CASE " in out and "WHEN 'APPROVED'" in out
    # sequences
    assert "nextval('seq_project_no')" in out
    # DUAL
    assert " FROM DUAL" not in out.upper()
    # ROWNUM → LIMIT
    assert "LIMIT 500" in out
    assert "ROWNUM" not in out.upper()
    # counters
    assert meta["types_replaced"] > 0
    assert meta["funcs_replaced"] > 0
    assert meta["sequences_touched"] >= 1
    assert meta["decode_expanded"] >= 1


def test_sql_translator_flags_package():
    src = "CREATE OR REPLACE PACKAGE foo IS PROCEDURE bar; END;"
    out, meta = SqlTranslator().translate(src)
    assert "TODO(dcte-manual)" in out
    assert "PACKAGE" in meta["unsupported"]


# ---------------------------------------------------------------------------
# Dependency migrator
# ---------------------------------------------------------------------------
def test_dependency_migrator_writes_spring_pom(tmp_path):
    tgt = tmp_path / "pom.xml"
    changes = DependencyMigrator().migrate_pom(HELIDON / "pom.xml", tgt)
    assert tgt.exists()
    content = tgt.read_text()
    assert "spring-boot-starter-web" in content
    assert "spring-boot-starter-actuator" in content
    assert "postgresql" in content
    assert "flyway-core" in content
    assert "micrometer-registry-prometheus" in content
    assert "springdoc-openapi" in content
    assert "<java.version>21</java.version>" in content
    # iter-18.4 — original pom no longer preserved as .bak (was clutter).
    assert not (tmp_path / "pom.xml.bak").exists()
    assert changes["group_id"] == "com.example.pmis"
    assert changes["java_version"] == "21"


# ---------------------------------------------------------------------------
# Impact analyzer
# ---------------------------------------------------------------------------
def test_impact_analyzer_math():
    job = DcteJob(
        name="t", source_root="/", output_root="/",
        services=[ServiceConfig(name="s", source_path="/", destination_path="/",
                                source_stack="helidon-mp", target_stack="spring-boot-3")],
    )
    agg = {
        "files_transformed": 20, "files_needs_manual": 4, "files_failed": 1,
        "services": [{"risk_level": "medium"}],
    }
    r = ImpactAnalyzer().compute(job, agg)
    # base 20*0.25 + 4*2.5 + 1*4 = 19.0. With one "medium" service,
    # risk_total=2 which is < 4 so aggregate stays LOW → multiplier=1.0.
    assert r["effort_hours"] == pytest.approx(19.0, abs=0.1)
    assert r["risk_level"] == "low"
    assert 0 <= r["complexity_score"] <= 100
    assert r["services"] == 1


# ---------------------------------------------------------------------------
# CI/CD templates
# ---------------------------------------------------------------------------
def test_cicd_generator_writes_all_three(tmp_path):
    g = CicdGenerator()
    for prov, name in (("github", "github-actions.yml"),
                       ("azure", "azure-pipelines.yml"),
                       ("jenkins", "Jenkinsfile")):
        p = g.write(prov, tmp_path)
        assert p.exists()
        assert p.name == name
        assert p.read_text().strip() != ""


def test_cicd_generator_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        CicdGenerator().write("bamboo", tmp_path)


# ---------------------------------------------------------------------------
# Engine end-to-end (Helidon → Spring)
# ---------------------------------------------------------------------------
def test_engine_helidon_to_spring_end_to_end(tmp_path):
    dest = tmp_path / "converted-source" / "pmis"
    output_root = tmp_path
    job = DcteJob(
        name="e2e-helidon",
        source_root=str(HELIDON),
        output_root=str(output_root),
        services=[ServiceConfig(
            name="pmis",
            source_path=str(HELIDON),
            destination_path=str(dest),
            source_stack="helidon-mp",
            target_stack="spring-boot-3",
        )],
        ai_refactor=False,
        generate_cicd=["github", "jenkins"],
    )
    events = []
    records = []
    reports = []
    eng = TransformationEngine()
    final = eng.run_job(
        job,
        event_sink=lambda e: events.append(e),
        record_sink=lambda r: records.append(r),
        report_sink=lambda r: reports.append(r),
    )
    assert final.status == DcteJobStatus.COMPLETED
    # Application + SecurityConfig were synthesised
    assert (dest / "src/main/java/com/example/pmis/Application.java").exists()
    assert (dest / "src/main/java/com/example/pmis/SecurityConfig.java").exists()
    # Rewritten resource class
    rc = (dest / "src/main/java/com/example/pmis/ProjectResource.java").read_text()
    assert "@RestController" in rc
    assert "@GetMapping" in rc
    # POM
    assert (dest / "pom.xml").exists()
    # application.properties translated
    props = (dest / "src/main/resources/application.properties").read_text()
    assert "server.port=7001" in props
    # traceability + impact reports
    assert (output_root / "reports" / "traceability_matrix.md").exists()
    assert (output_root / "reports" / "impact_analysis.md").exists()
    # CI/CD templates
    assert (output_root / "cicd" / "github-actions.yml").exists()
    assert (output_root / "cicd" / "Jenkinsfile").exists()
    # events emitted
    phases = {e.phase for e in events}
    assert {"start", "analyze", "transform", "validate", "done"}.issubset(phases)
    # transform records include a REST-controller entry
    assert any(r.kind == "rest_controller" and r.status == "success" for r in records)


# ---------------------------------------------------------------------------
# Engine end-to-end (Oracle → PG)
# ---------------------------------------------------------------------------
def test_engine_oracle_to_pg_end_to_end(tmp_path):
    # iter-18.4 — engine derives dest = output_root/database-scripts/<svc.name>
    output_root = tmp_path
    job = DcteJob(
        name="e2e-oracle",
        source_root=str(ORACLE),
        output_root=str(output_root),
        services=[ServiceConfig(
            name="pmis-db",
            source_path=str(ORACLE),
            destination_path=str(tmp_path / "advisory"),  # advisory only
            source_stack="oracle",
            target_stack="postgres-15",
        )],
        ai_refactor=False,
    )
    records = []
    reports = []
    eng = TransformationEngine()
    final = eng.run_job(
        job,
        record_sink=lambda r: records.append(r),
        report_sink=lambda r: reports.append(r),
    )
    assert final.status == DcteJobStatus.COMPLETED
    dest = output_root / "database-scripts" / "pmis-db"
    out = dest / "schema.sql"
    assert out.exists(), f"expected {out} to exist"
    body = out.read_text()
    assert "VARCHAR2" not in body
    assert "COALESCE(" in body
    assert "nextval('seq_project_no')" in body
    # at least one report emitted
    assert any(r.kind.value == "database_conversion" for r in reports)


# ---------------------------------------------------------------------------
# Rollback moves output aside
# ---------------------------------------------------------------------------
def test_rollback_moves_output(tmp_path):
    (tmp_path / "converted-source").mkdir()
    (tmp_path / "converted-source" / "x.java").write_text("hi")
    job = DcteJob(
        name="r", source_root=str(tmp_path), output_root=str(tmp_path),
        services=[ServiceConfig(name="s", source_path=str(tmp_path),
                                destination_path=str(tmp_path),
                                source_stack="helidon-mp",
                                target_stack="spring-boot-3")],
    )
    TransformationEngine().rollback(job)
    assert job.status == DcteJobStatus.ROLLED_BACK
    # Old dir renamed with .rollback.<ts> suffix
    siblings = list(tmp_path.parent.glob(tmp_path.name + ".rollback.*"))
    assert siblings, "expected a rollback backup dir alongside output_root"


# ---------------------------------------------------------------------------
# iter-18.4 — destination INSIDE source must not cause nested-out recursion.
# ---------------------------------------------------------------------------
def test_no_nested_out_when_dest_inside_source(tmp_path):
    """User-reported: pointing destination inside source caused out/out/out/..."""
    import shutil
    src = tmp_path / "src-project"
    shutil.copytree(HELIDON, src)
    out = src / "out"          # destination LIVES INSIDE the source tree
    job = DcteJob(
        name="nested-guard",
        source_root=str(src),
        output_root=str(out),
        services=[ServiceConfig(
            name="svc1",
            source_path=str(src),
            destination_path=str(out),
            source_stack="helidon-mp",
            target_stack="spring-boot-3",
        )],
        ai_refactor=False,
    )
    TransformationEngine().run_job(job)
    dest = out / "converted-source" / "svc1"
    assert dest.exists()
    # No nested `out/out/`
    assert not (dest / "out").exists(), \
        f"nested out/ detected under {dest} — engine walked into its own output"
    # No .bak clutter
    assert list(dest.rglob("*.bak")) == [], "no .bak files should be produced"
    # Files land under converted-source/<svc>/, NOT at output root
    top_level = {p.name for p in out.iterdir() if p.is_dir()}
    assert "converted-source" in top_level
    assert "reports" in top_level


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ─── iter-18.11 — Build agent tests ─────────────────────────────────

def test_build_agent_skips_when_no_pom(tmp_path):
    """No build manifest at all → skipped=True, attempted=False, no crash.

    iter-22 — the note names every manifest the agent now looks for, not
    just the two Java ones, because .NET and Node targets reach here too.
    """
    from dcte.build_agent import build_and_fix
    import asyncio
    res = asyncio.run(build_and_fix(tmp_path, max_attempts=1))
    assert res.skipped is True
    assert res.attempted is False
    assert res.success is False
    assert any("no build manifest" in n for n in res.notes)
    assert any("pom.xml" in n and "package.json" in n for n in res.notes)


def test_build_agent_parses_javac_errors(tmp_path):
    """Regex must attribute errors ONLY to files under dest_root."""
    from dcte.build_agent import _parse_errors
    src = tmp_path / "src" / "main" / "java" / "com" / "x"
    src.mkdir(parents=True)
    owned = src / "Foo.java"
    owned.write_text("class Foo {}", encoding="utf-8")
    fake_out = (
        f"[ERROR] {owned}:[42,17] cannot find symbol\n"
        f"[ERROR] {owned}:[43,10] method X() undefined\n"
        f"[ERROR] /opt/maven/lib/other.jar!/Other.java:[9,1] ignored\n"
    )
    errs = _parse_errors(fake_out, tmp_path)
    files = {e["file"] for e in errs}
    # only the owned file survives the dest_root filter
    assert str(owned.resolve()) in files
    assert len(errs) == 2
    assert errs[0]["line"] == 42


def test_build_agent_detect_maven(tmp_path):
    """iter-22 — detection returns (tool, manifest_dir): the directory
    matters because the manifest is not always at the destination root."""
    from dcte.build_agent import _detect_build_tool
    assert _detect_build_tool(tmp_path) is None
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert _detect_build_tool(tmp_path) == ("maven", tmp_path)


def test_build_agent_detects_the_non_java_toolchains(tmp_path):
    """iter-22 — root-only Maven/Gradle detection meant every .NET, React
    and Angular target the iter-21 catalogue added reported "build
    validation skipped" and the job carried on as if there were nothing to
    build."""
    from dcte.build_agent import _detect_build_tool
    node = tmp_path / "web"
    node.mkdir()
    (node / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    assert _detect_build_tool(tmp_path) == ("npm", node)

    net = tmp_path / "api"
    net.mkdir()
    (net / "Api.csproj").write_text("<Project/>", encoding="utf-8")
    # Both exist now; the first match in _MANIFEST_TOOLS order wins per dir,
    # and dirs are walked root-first then sorted, so `api/` precedes `web/`.
    tool, where = _detect_build_tool(tmp_path)
    assert (tool, where) == ("dotnet", net)


def test_build_agent_finds_a_nested_manifest(tmp_path):
    """A multi-module tree keeps its own nesting under converted-source/,
    and the root-only check missed every one of those."""
    from dcte.build_agent import _detect_build_tool
    deep = tmp_path / "services" / "orders"
    deep.mkdir(parents=True)
    (deep / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert _detect_build_tool(tmp_path) == ("maven", deep)


def test_build_agent_does_not_force_a_release_below_the_target(monkeypatch):
    """iter-22 — the release level was hardcoded to 17. `spring-boot-4`
    pins Java 25, so a Java 25 codebase was compiled at language level 17,
    every modern construct became a syntax error, and the fix loop
    "repaired" correct source."""
    from dcte import build_agent as BA
    monkeypatch.setattr(BA, "_installed_jdk_major", lambda: 17)
    release, note = BA._release_for("spring-boot-4")
    assert release == ""          # never silently downgrade
    assert "Java 25" in note and "17" in note

    # A target the installed JDK can satisfy may be forced.
    monkeypatch.setattr(BA, "_installed_jdk_major", lambda: 25)
    release, note = BA._release_for("spring-boot-4")
    assert release == "25"
    assert note == ""

    # Non-Java targets never get a -Dmaven.compiler flag.
    assert BA._release_for("react-19") == ("", "")


def test_maven_runs_with_dash_u(tmp_path):
    """iter-21 contract: Maven caches a FAILED resolution for 24h, so after
    a pom repair the next build can still report the artifact missing."""
    import shutil as _sh
    from dcte.build_agent import _maven_cmd
    if not _sh.which("mvn"):
        import pytest as _pt
        _pt.skip("mvn not on PATH")
    assert "-U" in _maven_cmd(tmp_path)


def test_build_agent_registered_in_fabric():
    from fabric.model_fabric import AGENT_COMPLEXITY
    assert AGENT_COMPLEXITY.get("dcte.build_fixer") == "high"


def test_dcte_job_status_has_building():
    from dcte.models import DcteJobStatus
    assert DcteJobStatus.BUILDING.value == "building"
