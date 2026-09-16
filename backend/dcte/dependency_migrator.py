"""pom.xml -> Spring Boot 3 / Java 21 dependency migrator.

iter-21 — this used to emit `_SPRING_POM` and nothing else. It read the
source pom ONLY for its groupId/artifactId/version and then threw the rest
away, so every third-party dependency the project actually had — iText,
BouncyCastle, jsoup, Jasper, an internal artifact — silently vanished.

That is the direct cause of the operator's build failure: 43 compile
errors, the large majority cascading from `package com.itextpdf.text does
not exist`. The generated source still imported those libraries, because
the code was migrated faithfully; only the manifest forgot them.

The template is still the SKELETON — parent, properties, Spring starters,
build plugins — because those genuinely are the same for every Spring Boot
3 target. What is new is that the source pom's own dependencies are
carried across, minus the ones the target framework replaces, and that the
generated SOURCE is then scanned so anything the code imports but the
manifest lacks is added with real coordinates (`dependency_resolver`).
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

SPRING_BOOT_VERSION = "3.3.4"
JAVA_VERSION = "21"

_SPRING_POM = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
                             http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{{group_id}}</groupId>
  <artifactId>{{artifact_id}}</artifactId>
  <version>{{version}}</version>
  <packaging>jar</packaging>

  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>{SPRING_BOOT_VERSION}</version>
    <relativePath/>
  </parent>

  <properties>
    <java.version>{JAVA_VERSION}</java.version>
    <maven.compiler.source>{JAVA_VERSION}</maven.compiler.source>
    <maven.compiler.target>{JAVA_VERSION}</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-actuator</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-security</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-data-jpa</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-validation</artifactId>
    </dependency>
    <dependency>
      <groupId>io.micrometer</groupId>
      <artifactId>micrometer-registry-prometheus</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springdoc</groupId>
      <artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>
      <version>2.6.0</version>
    </dependency>
    <dependency>
      <groupId>org.postgresql</groupId>
      <artifactId>postgresql</artifactId>
      <scope>runtime</scope>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-core</artifactId>
    </dependency>
    <dependency>
      <groupId>org.flywaydb</groupId>
      <artifactId>flyway-database-postgresql</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-maven-plugin</artifactId>
      </plugin>
    </plugins>
  </build>
</project>
"""


# Source-side dependencies the Spring Boot target REPLACES. These are the
# only ones dropped on purpose; everything else in the source pom is the
# project's own business and is carried across.
#
# Matched on groupId prefix or artifactId substring, because a Helidon pom
# spells the same concern several ways across versions.
_REPLACED_GROUP_PREFIXES: Tuple[str, ...] = (
    "io.helidon", "org.eclipse.microprofile", "org.glassfish.jersey",
    "org.jboss.weld", "io.smallrye",
)
_REPLACED_ARTIFACT_MARKERS: Tuple[str, ...] = (
    "helidon", "microprofile", "jersey", "weld", "smallrye",
    # Oracle JDBC: the target is PostgreSQL, whose driver the skeleton
    # already declares.
    "ojdbc", "orai18n", "oraclepki", "ucp",
)

_DEP_BLOCK_RE = re.compile(r"<dependency>.*?</dependency>", re.DOTALL)
_G_RE = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>")
_A_RE = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>")
_V_RE = re.compile(r"<version>\s*([^<\s]+)\s*</version>")
_SCOPE_RE = re.compile(r"<scope>\s*([^<\s]+)\s*</scope>")


def _is_replaced(group_id: str, artifact_id: str) -> bool:
    g, a = (group_id or "").lower(), (artifact_id or "").lower()
    if any(g.startswith(pref) for pref in _REPLACED_GROUP_PREFIXES):
        return True
    return any(marker in a for marker in _REPLACED_ARTIFACT_MARKERS)


def carried_over_dependencies(source_pom_xml: str) -> List[Dict[str, str]]:
    """Source dependencies that must survive the migration.

    Everything the source declared EXCEPT what the target framework
    replaces. A version that is a `${property}` reference is dropped rather
    than carried, because the property lived in the old pom's
    `<properties>` (or its Helidon parent) and would resolve to nothing
    here — an unresolvable `${...}` is a hard build failure, while omitting
    the version lets the Spring Boot BOM manage it or Maven pick the
    newest release.
    """
    out: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "",
                  source_pom_xml or "", flags=re.DOTALL)
    body = re.sub(r"<parent>.*?</parent>", "", body, flags=re.DOTALL)
    for block in _DEP_BLOCK_RE.findall(body):
        gm, am = _G_RE.search(block), _A_RE.search(block)
        if not gm or not am:
            continue
        g, a = gm.group(1).strip(), am.group(1).strip()
        if _is_replaced(g, a) or (g, a) in seen:
            continue
        seen.add((g, a))
        vm, sm = _V_RE.search(block), _SCOPE_RE.search(block)
        version = vm.group(1).strip() if vm else ""
        if version.startswith("${"):
            version = ""
        out.append({"group_id": g, "artifact_id": a, "version": version,
                    "scope": sm.group(1).strip() if sm else ""})
    return out


def _render(dep: Dict[str, str]) -> str:
    lines = ["    <dependency>",
             f"      <groupId>{dep['group_id']}</groupId>",
             f"      <artifactId>{dep['artifact_id']}</artifactId>"]
    if dep.get("version"):
        lines.append(f"      <version>{dep['version']}</version>")
    if dep.get("scope"):
        lines.append(f"      <scope>{dep['scope']}</scope>")
    lines.append("    </dependency>")
    return "\n".join(lines)


class DependencyMigrator:
    def migrate_pom(self, source_pom: Path, target_pom: Path) -> dict[str, Any]:
        original = source_pom.read_text(encoding="utf-8", errors="ignore")
        # iter-18.4 — extract project-level GAV, NOT the <parent> GAV.
        # Strip out the <parent>…</parent> block before regex extraction, else
        # Helidon poms hand us `io.helidon.applications` / `helidon-mp` which is
        # the parent's identity, not the module being migrated.
        stripped = re.sub(r"<parent>.*?</parent>", "", original, flags=re.DOTALL)
        group_id = _extract(stripped, r"<groupId>([^<]+)</groupId>", "com.example")
        artifact_id = _extract(stripped, r"<artifactId>([^<]+)</artifactId>", "app")
        version = _extract(stripped, r"<version>([^<]+)</version>", "1.0.0")

        rendered = _SPRING_POM.format(
            group_id=group_id, artifact_id=artifact_id, version=version,
        )

        # iter-21 — carry the source project's own dependencies across.
        # Without this the generated code still imports iText, BouncyCastle
        # and friends while the manifest declares none of them, which is
        # precisely the reported 43-error build.
        skeleton_artifacts = set(_A_RE.findall(rendered))
        carried = [d for d in carried_over_dependencies(original)
                   if d["artifact_id"] not in skeleton_artifacts]
        if carried:
            blocks = "\n".join(_render(d) for d in carried)
            idx = rendered.rfind("</dependencies>")
            rendered = (rendered[:idx]
                        + "    <!-- Carried over from the source project -->\n"
                        + blocks + "\n  " + rendered[idx:])

        target_pom.parent.mkdir(parents=True, exist_ok=True)
        target_pom.write_text(rendered, encoding="utf-8")
        return {
            "group_id": group_id,
            "artifact_id": artifact_id,
            "version": version,
            "spring_boot_version": SPRING_BOOT_VERSION,
            "java_version": JAVA_VERSION,
            "carried_over": [f"{d['group_id']}:{d['artifact_id']}" for d in carried],
            "carried_over_count": len(carried),
        }


def _extract(text: str, pattern: str, default: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else default


# ──────────────────────────────────────────────────────────────────────
# iter-21 — the post-generation sweep.
#
# Carrying the source pom across fixes the common case, but two gaps
# remain and both were live in the operator's build:
#
#   * a dependency the source got TRANSITIVELY (through a Helidon bundle
#     that we correctly dropped) but that the code imports directly;
#   * a dependency the AI pass introduced by writing an import the source
#     never had.
#
# Neither is visible from the source pom. Both are visible from the
# generated code, which is exactly what an IDE looks at. So after every
# file is written we scan the imports, subtract what the manifest already
# declares, and add the difference with coordinates resolved by
# `dependency_resolver` (curated first, Maven Central second).
# ──────────────────────────────────────────────────────────────────────


# Package roots belonging to the stack being migrated away from. An import
# under one of these is evidence the code migration is incomplete, not a
# missing dependency — see `missing_dependencies_for_sources`.
SOURCE_STACK_ROOTS: Tuple[str, ...] = (
    "io.helidon", "org.eclipse.microprofile", "jakarta.ws.rs", "javax.ws.rs",
    "jakarta.enterprise", "javax.enterprise", "org.glassfish.jersey",
    "jakarta.json", "javax.json",
)


async def complete_pom_from_sources(
    dest: Path, own_group: str = "", allow_network: bool = True,
) -> Dict[str, Any]:
    """Add any dependency the generated code needs and the pom lacks.

    Returns a summary rather than raising: a migration that cannot reach
    Maven Central should still finish and report what it could not resolve,
    because an unresolved name is information the operator can act on and a
    crashed run is not.
    """
    from dependency_resolver import (
        invalid_jakarta_imports,
        missing_dependencies_for_sources,
        render_dependency,
        inject_dependencies,
    )

    pom_path = dest / "pom.xml"
    if not pom_path.exists():
        return {"added": [], "bad_jakarta": [], "skipped": "no pom.xml"}

    sources: Dict[str, str] = {}
    bad_jakarta: List[str] = []
    for jf in dest.rglob("*.java"):
        try:
            body = jf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        sources[str(jf)] = body
        for bad in invalid_jakarta_imports(body):
            bad_jakarta.append(f"{jf.relative_to(dest)}: {bad}")

    pom_xml = pom_path.read_text(encoding="utf-8", errors="ignore")
    missing, residue = await missing_dependencies_for_sources(
        sources, pom_xml, own_group=own_group,
        under_spring_parent=True, allow_network=allow_network,
        # Never satisfy an import belonging to the framework we are
        # migrating AWAY from. Resolving `io.helidon` or `jakarta.ws.rs`
        # would make the build go green while the service still runs on
        # the old stack — a worse result than the red build, because the
        # red build is honest.
        forbidden_roots=SOURCE_STACK_ROOTS,
    )
    if missing:
        blocks = [render_dependency(d["group_id"], d["artifact_id"], d.get("version", ""))
                  for d in missing]
        pom_path.write_text(inject_dependencies(pom_xml, blocks), encoding="utf-8")

    return {
        "added": [f"{d['group_id']}:{d['artifact_id']}"
                  + (f":{d['version']}" if d.get("version") else "")
                  for d in missing],
        "added_for": {d["for_package"]: f"{d['group_id']}:{d['artifact_id']}" for d in missing},
        "bad_jakarta": sorted(set(bad_jakarta)),
        "unmigrated_imports": residue,
        "scanned_files": len(sources),
    }


def complete_pom_from_sources_sync(
    dest: Path, own_group: str = "", allow_network: bool = True,
) -> Dict[str, Any]:
    """Sync entry point for `complete_pom_from_sources`.

    The DCTE engine is synchronous end to end while the resolver is async
    (it makes HTTP calls). Rather than duplicate the resolver in a blocking
    form, this bridges: `asyncio.run` when there is no loop, and a worker
    thread with its own loop when there is — because calling `asyncio.run`
    inside a running loop raises, and this runs under a FastAPI background
    task often enough for that to matter.
    """
    import asyncio

    async def _go():
        return await complete_pom_from_sources(dest, own_group, allow_network)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_go())).result()
