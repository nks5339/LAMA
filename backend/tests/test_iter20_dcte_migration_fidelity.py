"""iter-20 — Direct Transform migration fidelity: the output must compile.

Found by running a real job through the live API against the shipped
Helidon sample and reading the Java that came out. Three defects, all of
which produce a tree that looks migrated and will not build:

  1. `_rewrite_imports` matched only three namespaces -- `jakarta.ws.rs`,
     `jakarta.inject`/`jakarta.enterprise.context`, and
     `org.eclipse.microprofile.config`. Eleven of the thirty-two entries in
     IMPORT_REPLACEMENTS live outside those, so no regex could ever match
     them and they were dead lookup rows. The visible consequence:
     `import io.helidon.security.annotations.Authenticated;` survived into
     the output, leaving `io.helidon` residue AND no import for the
     `@PreAuthorize` the annotation had already been rewritten to.

  2. `@ConfigProperty(name = "k", defaultValue = "d")` was token-replaced to
     `@Value(name = "k", defaultValue = "d")`. Spring's `@Value` takes a
     single String; that does not compile.

  3. `@Inject @ConfigProperty` is the MicroProfile idiom for a config field.
     Rewritten pairwise it becomes `@Autowired @Value`, and Spring then
     tries to autowire a bean for an `int`, which fails at startup.

Offline: pure string transforms, no LLM, no Mongo, no subprocess.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND))

from dcte.plugins.helidon_to_spring.endpoint_transformer import (  # noqa: E402
    EndpointTransformer,
)
from dcte.plugins.helidon_to_spring.mappings import (  # noqa: E402
    IMPORT_REPLACEMENTS,
)

SAMPLE = (BACKEND / "dcte" / "samples" / "helidon_sample" / "src" / "main"
          / "java" / "com" / "example" / "pmis" / "ProjectResource.java")


def _apply(src: str) -> str:
    return EndpointTransformer().apply(src)[0]


# ---------------------------------------------------------------------------
# 1 — every mapping row must be reachable
# ---------------------------------------------------------------------------
def test_every_import_mapping_row_is_reachable():
    """A lookup row no regex can match is a silent no-op, not a mapping.

    Asserted against the regex directly rather than against the output,
    because two rows map a class to ITSELF (`jakarta.annotation.PostConstruct`
    is the same on Spring). For those, "the old import is still there" is
    equally true whether the row fired or was never reached, so output alone
    cannot tell the two apart.
    """
    from dcte.plugins.helidon_to_spring.endpoint_transformer import (
        _LEGACY_IMPORT_RE,
    )
    unreachable = [old for old, _new in IMPORT_REPLACEMENTS
                   if not _LEGACY_IMPORT_RE.search(f"import {old};")]
    assert unreachable == [], (
        f"{len(unreachable)} IMPORT_REPLACEMENTS rows never fire: {unreachable}"
    )


def test_import_rewriter_leaves_application_imports_alone():
    """The alternation is anchored to migrated namespaces on purpose: a
    blanket `import [^;]+` would also rewrite the project's own classes."""
    src = (
        "package p;\n"
        "import com.example.pmis.ProjectService;\n"
        "import java.util.List;\n"
        "import org.slf4j.Logger;\n"
        "\nclass C {}\n"
    )
    out = _apply(src)
    assert "import com.example.pmis.ProjectService;" in out
    assert "import java.util.List;" in out
    assert "import org.slf4j.Logger;" in out


def test_helidon_security_import_becomes_spring_preauthorize():
    src = (
        "package p;\n"
        "import io.helidon.security.annotations.Authenticated;\n"
        "\n"
        "class C {\n"
        "    @Authenticated\n"
        "    public void go() {}\n"
        "}\n"
    )
    out = _apply(src)
    assert "io.helidon" not in out
    assert "org.springframework.security.access.prepost.PreAuthorize" in out
    assert '@PreAuthorize("isAuthenticated()")' in out


def test_microprofile_health_metrics_openapi_imports_are_rewritten():
    src = (
        "package p;\n"
        "import org.eclipse.microprofile.health.HealthCheck;\n"
        "import org.eclipse.microprofile.metrics.annotation.Timed;\n"
        "import org.eclipse.microprofile.openapi.annotations.Operation;\n"
        "\nclass C {}\n"
    )
    out = _apply(src)
    assert "org.eclipse.microprofile" not in out
    assert "org.springframework.boot.actuate.health.HealthIndicator" in out
    assert "io.micrometer.core.annotation.Timed" in out
    assert "io.swagger.v3.oas.annotations.Operation" in out


# ---------------------------------------------------------------------------
# 2 — @ConfigProperty attributes must become Spring placeholder syntax
# ---------------------------------------------------------------------------
def test_config_property_with_default_becomes_value_placeholder():
    src = (
        "package p;\n"
        "import org.eclipse.microprofile.config.inject.ConfigProperty;\n"
        "\nclass C {\n"
        '    @ConfigProperty(name = "pmis.page.size", defaultValue = "25")\n'
        "    private int pageSize;\n"
        "}\n"
    )
    out = _apply(src)
    assert '@Value("${pmis.page.size:25}")' in out
    assert "defaultValue" not in out
    assert "@Value(name" not in out


def test_config_property_without_default_becomes_bare_placeholder():
    src = (
        "package p;\n"
        'class C {\n    @ConfigProperty(name = "db.url")\n'
        "    private String url;\n}\n"
    )
    out = _apply(src)
    assert '@Value("${db.url}")' in out
    assert "name =" not in out


def test_config_property_single_arg_form_is_handled():
    """`@ConfigProperty("k")` — the positional shorthand."""
    src = 'package p;\nclass C {\n    @ConfigProperty("a.b")\n    private String v;\n}\n'
    out = _apply(src)
    assert '@Value("${a.b}")' in out


# ---------------------------------------------------------------------------
# 3 — @Inject stacked on a config field must not survive as @Autowired
# ---------------------------------------------------------------------------
def test_inject_above_config_property_is_dropped():
    """Spring injects the value from @Value alone; a stacked @Autowired on an
    `int` field makes it look for an int bean and fail at startup."""
    src = (
        "package p;\n"
        "class C {\n"
        "    @Inject\n"
        '    @ConfigProperty(name = "k", defaultValue = "1")\n'
        "    private int v;\n"
        "}\n"
    )
    out = _apply(src)
    assert "@Autowired" not in out, "redundant @Autowired left on a @Value field"
    assert '@Value("${k:1}")' in out


def test_plain_inject_is_still_rewritten_to_autowired():
    """The fix above must not disarm ordinary dependency injection."""
    src = (
        "package p;\nclass C {\n"
        "    @Inject\n    private Service svc;\n}\n"
    )
    out = _apply(src)
    assert "@Autowired" in out
    assert "org.springframework.beans.factory.annotation.Autowired" in out


# ---------------------------------------------------------------------------
# End to end on the shipped sample — the file that exposed all three
# ---------------------------------------------------------------------------
def test_shipped_sample_migrates_to_compilable_spring():
    out = _apply(SAMPLE.read_text(encoding="utf-8"))

    # No legacy namespace survives anywhere in the file.
    for marker in ("io.helidon", "jakarta.ws.rs", "org.eclipse.microprofile",
                   "jakarta.enterprise", "@ApplicationScoped", "@ConfigProperty"):
        assert marker not in out, f"legacy marker survived: {marker}"

    # Every annotation used has its import.
    for ann, fqcn in [
        ("@RestController", "org.springframework.web.bind.annotation.RestController"),
        ("@RequestMapping", "org.springframework.web.bind.annotation.RequestMapping"),
        ("@GetMapping", "org.springframework.web.bind.annotation.GetMapping"),
        ("@PostMapping", "org.springframework.web.bind.annotation.PostMapping"),
        ("@PathVariable", "org.springframework.web.bind.annotation.PathVariable"),
        ("@RequestParam", "org.springframework.web.bind.annotation.RequestParam"),
        ("@Autowired", "org.springframework.beans.factory.annotation.Autowired"),
        ("@Value", "org.springframework.beans.factory.annotation.Value"),
        ("@PreAuthorize", "org.springframework.security.access.prepost.PreAuthorize"),
    ]:
        if ann in out:
            assert f"import {fqcn};" in out, f"{ann} used with no import for {fqcn}"

    # The config field is a valid Spring placeholder.
    assert '@Value("${pmis.default.page.size:25}")' in out

    # No import is emitted twice.
    imports = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("import ")]
    assert len(imports) == len(set(imports)), f"duplicate imports: {imports}"


# ---------------------------------------------------------------------------
# 4 — the residue scan must read code, not prose
# ---------------------------------------------------------------------------
# Found the same way: the live job reported 1 file "still carrying legacy
# markers" on a migration that was clean. The file was DCTE's OWN generated
# Application.java, whose Javadoc explains what happened to the Helidon
# entrypoint. A raw substring scan cannot tell that from real residue, so
# every fix-up round re-sent a correct file to the model to "fix" a sentence.
from dcte.ai_refactor import _scan_residue_in_file, scan_residual  # noqa: E402


def test_comment_mentioning_helidon_is_not_residue(tmp_path):
    p = tmp_path / "Application.java"
    p.write_text(
        "package p;\n"
        "/**\n"
        " * Original entrypoint (io.helidon.microprofile.server.Main) does not\n"
        " * survive the migration.\n"
        " */\n"
        "@SpringBootApplication\n"
        "public class Application {}\n",
        encoding="utf-8",
    )
    assert _scan_residue_in_file(p) == []


def test_todo_marker_quoting_a_jaxrs_annotation_is_not_residue(tmp_path):
    """The plugin deliberately leaves these; they are guidance, not debt."""
    p = tmp_path / "R.java"
    p.write_text(
        "package p;\nclass R {\n"
        "    @GetMapping\n"
        "    // TODO(dcte): move produces to @*Mapping produces=: "
        "@Produces(MediaType.APPLICATION_JSON)\n"
        "    public Object list() { return null; }\n}\n",
        encoding="utf-8",
    )
    assert _scan_residue_in_file(p) == []


def test_real_residue_in_code_is_still_caught(tmp_path):
    """The fix must not blind the scanner to the thing it exists for."""
    p = tmp_path / "R.java"
    p.write_text(
        "package p;\n"
        "import io.helidon.security.annotations.Authenticated;\n"
        "class R {\n    @Inject\n    private S s;\n}\n",
        encoding="utf-8",
    )
    hits = _scan_residue_in_file(p)
    assert "io.helidon." in hits
    assert "@Inject" in hits


def test_marker_inside_a_string_literal_is_not_a_comment(tmp_path):
    """Quote-aware: `//` inside a URL must not open a comment and swallow
    the real residue that follows it."""
    p = tmp_path / "R.java"
    p.write_text(
        'package p;\nclass R {\n'
        '    String u = "http://example.com/x";\n'
        "    @ApplicationScoped\n    class Inner {}\n}\n",
        encoding="utf-8",
    )
    assert "@ApplicationScoped" in _scan_residue_in_file(p)


def test_sql_dash_comment_is_blanked_but_string_is_not(tmp_path):
    p = tmp_path / "a.sql"
    p.write_text(
        "-- was: SYSDATE, now CURRENT_TIMESTAMP\n"
        "SELECT CURRENT_TIMESTAMP FROM t;\n",
        encoding="utf-8",
    )
    assert _scan_residue_in_file(p) == []

    q = tmp_path / "b.sql"
    q.write_text("SELECT NVL(a, b) FROM dual;\n", encoding="utf-8")
    hits = _scan_residue_in_file(q)
    assert "NVL(" in hits and "FROM dual" in hits


def test_scan_residual_reports_clean_for_a_generated_tree(tmp_path):
    (tmp_path / "Application.java").write_text(
        "package p;\n/* io.helidon.Main is gone */\nclass Application {}\n",
        encoding="utf-8")
    (tmp_path / "schema.sql").write_text(
        "-- VARCHAR2 became VARCHAR\nCREATE TABLE t (c VARCHAR(10));\n",
        encoding="utf-8")
    assert scan_residual(tmp_path) == []
