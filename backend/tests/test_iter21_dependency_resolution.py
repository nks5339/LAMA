"""iter-21 — the migrated project's manifest is derived from its imports.

The operator's build failed with 43 compile errors. Their own analysis was
right, and it named three distinct faults that all *look* identical to
javac ("package ... does not exist"):

  1. `com.itextpdf.text.*` — a real missing dependency. The source project
     declared iText; the generated pom did not, because
     `DependencyMigrator` emitted a fixed template and kept only the GAV
     from the source pom. Every third-party dependency was dropped.
  2. `jakarta.validation.constraints` — same class of fault.
  3. `jakarta.security.auth.x500.X500Principal` — NOT a dependency fault
     at all. `javax.security.auth` is JAAS, Java SE since 1.4, and never
     moved to the Jakarta namespace. A prefix-based javax->jakarta rename
     produced a package that exists in no artifact anywhere, so no
     dependency could ever fix it — which is why it survived the repair
     rounds while agents hunted for one.

(3) is the important one: it is why "add the missing dependency" as a
strategy could not converge. These tests pin the distinction.

Network is required only by the tests marked `live`; everything else runs
against the curated layer so the suite stays fast and offline-safe.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

import dependency_resolver as DR  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    DR.reset_cache()
    yield
    DR.reset_cache()


# ── Layer 1: the JDK guard ────────────────────────────────────────────

@pytest.mark.parametrize("pkg", [
    "javax.security.auth.x500", "javax.security.auth", "javax.crypto",
    "javax.net.ssl", "javax.sql", "javax.naming.directory",
    "javax.xml.parsers", "javax.xml.transform", "javax.imageio",
    "javax.management", "javax.script", "javax.swing", "javax.tools",
    "javax.smartcardio", "javax.transaction.xa",
])
def test_java_se_packages_are_jdk(pkg):
    """These stay `javax.` forever. Renaming them is the operator's bug."""
    assert DR.is_jdk_package(pkg) is True


@pytest.mark.parametrize("pkg", [
    "javax.servlet", "javax.persistence", "javax.validation", "javax.ws.rs",
    "javax.annotation", "javax.inject", "javax.ejb", "javax.json",
    "javax.jms", "javax.mail", "javax.transaction", "javax.xml.bind",
])
def test_jakarta_ee_packages_are_not_jdk(pkg):
    """These genuinely moved; renaming them is correct."""
    assert DR.is_jdk_package(pkg) is False
    assert DR.jakarta_rename_is_valid(pkg) is True


def test_the_transaction_split_is_handled_by_longest_prefix():
    """`javax.transaction` moved to Jakarta; `javax.transaction.xa` did
    not — it is JDBC's XA support and lives in the JDK. A first-match
    prefix test gets exactly one of these wrong."""
    assert DR.is_jdk_package("javax.transaction") is False
    assert DR.is_jdk_package("javax.transaction.xa") is True


# ── The reported defect, detected ─────────────────────────────────────

def test_the_x500principal_rename_is_detected():
    """The exact import from the operator's build log."""
    src = "import jakarta.security.auth.x500.X500Principal;\n"
    assert DR.invalid_jakarta_imports(src) == [
        "jakarta.security.auth.x500.X500Principal"
    ]


def test_legitimate_jakarta_imports_are_not_flagged():
    src = (
        "import jakarta.validation.constraints.NotBlank;\n"
        "import jakarta.persistence.Entity;\n"
        "import jakarta.servlet.http.HttpServletRequest;\n"
    )
    assert DR.invalid_jakarta_imports(src) == []


def test_a_mixed_file_reports_only_the_bad_one():
    src = (
        "import jakarta.validation.constraints.NotBlank;\n"
        "import jakarta.security.auth.x500.X500Principal;\n"
        "import jakarta.crypto.Cipher;\n"
    )
    bad = DR.invalid_jakarta_imports(src)
    assert "jakarta.security.auth.x500.X500Principal" in bad
    assert "jakarta.crypto.Cipher" in bad
    assert not any("validation" in b for b in bad)


def test_an_invalid_jakarta_package_never_reaches_the_resolver():
    """It must not be looked up. Maven Central answers
    `javax.security.auth.x500.X500Principal` with a Scala Native stub JAR;
    adding that to a Spring Boot pom would be worse than the original
    error."""
    assert DR.is_invalid_jakarta_package("jakarta.security.auth.x500") is True
    assert DR.is_invalid_jakarta_package("jakarta.validation") is False


def test_the_shopping_list_excludes_unfixable_imports():
    src = ("import jakarta.security.auth.x500.X500Principal;\n"
           "import com.itextpdf.text.Font;\n")
    pkgs = DR.external_packages({"A.java": src})
    assert "com.itextpdf.text" in pkgs
    assert not any(p.startswith("jakarta.security") for p in pkgs)


# ── Import scanning ───────────────────────────────────────────────────

def test_imports_are_scanned_including_static_and_wildcard():
    src = (
        "import com.itextpdf.text.pdf.PdfContentByte;\n"
        "import static org.apache.commons.lang3.StringUtils.isBlank;\n"
        "import org.jsoup.nodes.*;\n"
    )
    found = DR.scan_java_imports(src)
    assert "com.itextpdf.text.pdf.PdfContentByte" in found
    assert "org.apache.commons.lang3.StringUtils.isBlank" in found
    assert "org.jsoup.nodes" in found


def test_the_projects_own_packages_are_not_dependencies():
    src = ("import com.shpp.gov.in.core.model.Negotiation;\n"
           "import com.itextpdf.text.Font;\n")
    pkgs = DR.external_packages({"A.java": src}, own_group="com.shpp.gov.in")
    assert pkgs == {"com.itextpdf.text"}


def test_jdk_imports_are_not_dependencies():
    src = ("import java.util.List;\n"
           "import javax.crypto.Cipher;\n"
           "import javax.security.auth.x500.X500Principal;\n")
    assert DR.external_packages({"A.java": src}) == set()


def test_non_java_files_are_ignored():
    assert DR.external_packages({"a.py": "import os"}) == set()


# ── Layer 2: curated resolution ───────────────────────────────────────

@pytest.mark.asyncio
async def test_itext_resolves_to_the_itext5_api_the_code_uses():
    """`com.itextpdf.text.*` is the iText **5** package layout. iText 7
    uses `com.itextpdf.kernel.*` and is not source-compatible, so
    resolving this to iText 7 would replace one compile error with
    dozens."""
    r = await DR.resolve_package("com.itextpdf.text.pdf", allow_network=False)
    assert r["group_id"] == "com.itextpdf"
    assert r["artifact_id"] == "itextpdf"
    assert r["source"] == "curated"


@pytest.mark.asyncio
async def test_bouncycastle_pkix_beats_the_third_party_repackage():
    """Maven Central's top hit for `org.bouncycastle.cert.jcajce` is
    `org.italiangrid:bcmail`. The curated entry avoids the question."""
    r = await DR.resolve_package("org.bouncycastle.cert.jcajce", allow_network=False)
    assert r["group_id"] == "org.bouncycastle"
    assert r["artifact_id"] == "bcpkix-jdk18on"


@pytest.mark.asyncio
async def test_a_jdk_package_resolves_to_nothing():
    assert await DR.resolve_package("javax.security.auth.x500") is None


@pytest.mark.asyncio
async def test_offline_resolution_does_not_raise():
    """Layers 1 and 2 must work with no network at all."""
    assert await DR.resolve_package("org.jsoup.nodes", allow_network=False) is not None
    assert await DR.resolve_package("com.nobody.knows.this", allow_network=False) is None


# ── Ranking (why a bare API call is not enough) ───────────────────────

def test_ranking_prefers_the_publisher_whose_group_matches_the_package():
    docs = [
        {"g": "de.rpgframework", "a": "itextpdf"},
        {"g": "com.itextpdf", "a": "itextpdf"},
    ]
    assert DR._rank_candidates("com.itextpdf.text.pdf", docs)[0] == ("com.itextpdf", "itextpdf")


def test_ranking_penalises_stubs_and_platform_ports():
    docs = [
        {"g": "org.scala-native", "a": "javax-security-stubs_native0.5_3"},
        {"g": "org.example", "a": "real-lib"},
    ]
    assert DR._rank_candidates("javax.security.auth", docs)[0][0] != "org.scala-native"


# ── pom inspection + injection ────────────────────────────────────────

_POM = """<?xml version="1.0"?>
<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def test_declared_coordinates_are_read():
    assert ("org.springframework.boot", "spring-boot-starter-web") in DR.declared_coordinates(_POM)


def test_injection_preserves_the_rest_of_the_file():
    out = DR.inject_dependencies(_POM, [DR.render_dependency("com.itextpdf", "itextpdf", "5.5.13.4")])
    assert "spring-boot-starter-web" in out
    assert "itextpdf" in out
    assert "5.5.13.4" in out
    assert out.count("</dependencies>") == 1
    assert out.strip().endswith("</project>")


def test_a_bom_managed_dependency_gets_no_version():
    """Pinning a version the Spring BOM manages is how a project ends up
    with two Jackson versions on the classpath."""
    assert "<version>" not in DR.render_dependency("org.springframework.boot", "x")


@pytest.mark.asyncio
async def test_missing_dependencies_is_the_difference_not_the_whole_list():
    files = {
        "A.java": ("import org.springframework.web.bind.annotation.RestController;\n"
                   "import com.itextpdf.text.Font;\n"),
    }
    missing, _ = await DR.missing_dependencies_for_sources(
        files, _POM, own_group="com.acme", allow_network=False,
    )
    arts = {m["artifact_id"] for m in missing}
    # already declared -> not returned
    assert "spring-boot-starter-web" not in arts
    assert "itextpdf" in arts


@pytest.mark.asyncio
async def test_a_spring_starter_is_preferred_over_the_bare_api():
    """`jakarta.validation` under a Spring Boot parent should pull
    `spring-boot-starter-validation`, which brings a coherent set at BOM
    versions, rather than the bare API jar with no implementation."""
    files = {"A.java": "import jakarta.validation.constraints.NotBlank;\n"}
    missing, _ = await DR.missing_dependencies_for_sources(
        files, "<project><dependencies></dependencies></project>",
        allow_network=False,
    )
    assert {m["artifact_id"] for m in missing} == {"spring-boot-starter-validation"}


# ── The DCTE migrator: the source pom is no longer discarded ──────────

_HELIDON_POM = """<?xml version="1.0"?>
<project>
  <parent>
    <groupId>io.helidon.applications</groupId>
    <artifactId>helidon-mp</artifactId>
    <version>4.0.0</version>
  </parent>
  <groupId>com.shpp.gov.in</groupId>
  <artifactId>dsc-service</artifactId>
  <version>2.1.0</version>
  <dependencies>
    <dependency><groupId>io.helidon.microprofile.bundles</groupId>
      <artifactId>helidon-microprofile</artifactId></dependency>
    <dependency><groupId>com.itextpdf</groupId>
      <artifactId>itextpdf</artifactId><version>5.5.13.3</version></dependency>
    <dependency><groupId>org.bouncycastle</groupId>
      <artifactId>bcpkix-jdk18on</artifactId><version>1.78</version></dependency>
    <dependency><groupId>org.jsoup</groupId>
      <artifactId>jsoup</artifactId><version>${jsoup.version}</version></dependency>
    <dependency><groupId>com.oracle.database.jdbc</groupId>
      <artifactId>ojdbc11</artifactId></dependency>
  </dependencies>
</project>
"""


def test_third_party_dependencies_survive_the_migration():
    """The root cause of the 43 errors: these used to be discarded."""
    from dcte.dependency_migrator import carried_over_dependencies

    carried = {d["artifact_id"] for d in carried_over_dependencies(_HELIDON_POM)}
    assert "itextpdf" in carried
    assert "bcpkix-jdk18on" in carried
    assert "jsoup" in carried


def test_the_replaced_framework_is_not_carried_over():
    """Helidon is what we are migrating AWAY from, and Oracle's driver is
    replaced by PostgreSQL's — carrying either would be its own failure."""
    from dcte.dependency_migrator import carried_over_dependencies

    carried = {d["artifact_id"] for d in carried_over_dependencies(_HELIDON_POM)}
    assert "helidon-microprofile" not in carried
    assert "ojdbc11" not in carried


def test_an_unresolvable_property_version_is_dropped_not_carried():
    """`${jsoup.version}` was defined in the Helidon parent's
    <properties>. Carrying the reference into a pom that inherits from
    Spring Boot instead gives Maven an unresolvable expression, which is a
    hard build failure — strictly worse than letting the version float."""
    from dcte.dependency_migrator import carried_over_dependencies

    jsoup = next(d for d in carried_over_dependencies(_HELIDON_POM)
                 if d["artifact_id"] == "jsoup")
    assert jsoup["version"] == ""


def test_the_generated_pom_contains_the_carried_dependencies():
    from dcte.dependency_migrator import DependencyMigrator

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "pom.xml"
        src.write_text(_HELIDON_POM)
        tgt = Path(td) / "out" / "pom.xml"
        changes = DependencyMigrator().migrate_pom(src, tgt)
        body = tgt.read_text()

    assert changes["group_id"] == "com.shpp.gov.in"
    assert changes["artifact_id"] == "dsc-service"
    assert changes["carried_over_count"] == 3
    for kept in ("itextpdf", "bcpkix-jdk18on", "jsoup"):
        assert kept in body
    for dropped in ("helidon", "ojdbc", "${jsoup"):
        assert dropped not in body
    # still a valid single-root document
    assert body.count("</dependencies>") == 1
    assert body.strip().endswith("</project>")


# ── Build discipline ──────────────────────────────────────────────────

def test_maven_refreshes_dependencies():
    """`-U` on the operator's instruction. Maven caches a FAILED
    resolution for 24h, so without it a repaired pom can still report the
    artifact as missing on the very next build — and the fix loop then
    spends rounds re-fixing a pom that was already correct."""
    import routes.tools as T

    argv = T.NATIVE_BUILD_COMMANDS["maven"]["argv"]
    assert "-U" in argv
    assert argv[:2] == ["mvn", "-B"]


def test_the_structural_check_rejects_the_bad_rename():
    """It must be a CRITICAL reject: no dependency can fix it, so shipping
    it guarantees a failed build."""
    import routes.tools as T

    body = ("package com.acme;\n"
            "import jakarta.security.auth.x500.X500Principal;\n"
            "public class A { }\n")
    r = T._structural_check(body, "A.java", {"framework": "helidon"},
                            {"backend": "spring-boot-3"})
    assert r["ok"] is False
    assert r["severity"] == "critical"
    assert any("Invalid jakarta rename" in i for i in r["issues"])


def test_a_correct_jakarta_import_still_passes():
    import routes.tools as T

    body = ("package com.acme;\n"
            "import jakarta.validation.constraints.NotBlank;\n"
            "import org.springframework.web.bind.annotation.RestController;\n"
            "@RestController\npublic class A { }\n")
    r = T._structural_check(body, "A.java", {"framework": "helidon"},
                            {"backend": "spring-boot-3"})
    assert r["ok"] is True, r["issues"]


def test_the_devops_prompt_states_the_dependency_first_rule():
    """Unresolved dependencies must not be diagnosed as source defects —
    the operator's explicit instruction, and the reason a 43-error build
    was read as 43 problems instead of about four."""
    import re

    import seed

    raw = next(p["template"] for p in seed.GLOBAL_PROMPTS
               if p["key"] == "tools.transformer.devops_expert")
    # Collapse whitespace: the prompt is hard-wrapped for readability, so a
    # phrase can straddle a newline ("source-code\n  defect"). Asserting on
    # the raw text makes the test fail on a rewrap rather than on a
    # meaning change.
    tpl = re.sub(r"\s+", " ", raw).lower()
    assert "is not a source-code defect" in tpl
    assert "cannot find symbol" in tpl
    assert "javax.security.auth" in tpl
    assert "dependency_resolution_first" in raw


def test_the_coder_prompt_lists_the_jdk_packages():
    import seed

    tpl = next(p["template"] for p in seed.GLOBAL_PROMPTS
               if p["key"] == "tools.transformer.coder")
    assert "javax.security.auth" in tpl
    assert "jakarta.security.auth.x500.X500Principal" in tpl
    # and the API-family rule that stops iText 5 becoming iText 7
    assert "com.itextpdf.kernel" in tpl


# ── Live (network) ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_maven_central_resolves_an_uncurated_package():
    """The IDE behaviour, end to end, for something not in the table.

    Skipped rather than failed when Maven Central is unreachable: this
    suite must stay green on a machine with no network.
    """
    got = await DR.search_artifact_for_class("org.apache.commons.text.WordUtils")
    if got is None:
        pytest.skip("Maven Central unreachable")
    assert got[0].startswith("org.apache.commons")


@pytest.mark.asyncio
async def test_live_version_lookup_skips_prereleases():
    v = await DR.latest_stable_version("org.jsoup", "jsoup")
    if not v:
        pytest.skip("Maven Central unreachable")
    assert not DR._PRERELEASE_RE.search(v), v


def test_a_source_stack_import_is_never_satisfied_with_a_dependency():
    """The trap this avoids: `jakarta.ws.rs` resolves to a real artifact.

    Adding it would turn a red build green while the service still runs
    JAX-RS — the migration silently not done, and now invisible. The
    import is reported as unmigrated instead.
    """
    import asyncio

    files = {"A.java": ("import jakarta.ws.rs.GET;\n"
                        "import io.helidon.config.Config;\n"
                        "import com.itextpdf.text.Font;\n")}
    missing, residue = asyncio.run(DR.missing_dependencies_for_sources(
        files, "<project><dependencies></dependencies></project>",
        allow_network=False,
        forbidden_roots=("io.helidon", "jakarta.ws.rs"),
    ))
    arts = {m["artifact_id"] for m in missing}
    assert "itextpdf" in arts, "a legitimate dependency must still be added"
    assert not any("ws.rs" in a or "helidon" in a for a in arts)
    assert "jakarta.ws.rs" in residue
    assert "io.helidon.config" in residue
