"""iter-21 — Resolve a migrated project's IMPORTS into build dependencies.

The operator's report, verbatim: *"the devops agent is unable to build the
package converted, make it import the packages easily like every IDE like
vscode or claude does."*

That is the right frame. When a human opens a migrated file in an IDE and
sees `import com.itextpdf.text.pdf.PdfContentByte;` underlined, the IDE
offers to add the artifact that provides it. Nothing in this codebase did
that: the target manifest was produced either from a fixed template
(`dcte/dependency_migrator.py`) or by a model writing a dependency list
from memory. Both drop every third-party dependency the source project had
that the template's author did not think of, which produces exactly the
failure reported — 43 compile errors, almost all of them cascading from a
handful of missing artifacts.

This module closes that by doing what the IDE does, in three layers, in
order of confidence:

  1. **JDK check.** Some packages need no dependency at all because they
     ship with Java. Getting this wrong is worse than missing a dependency
     (see `JDK_PACKAGE_ROOTS`).
  2. **Curated table.** The ~60 libraries that actually show up in
     enterprise Java migrations, mapped to their canonical coordinates. No
     network, deterministic, and correct for the common case.
  3. **Live Maven Central lookup.** For anything else, Maven Central's
     `fc:` (full-class) search answers "which artifact contains this
     class?" — the same question the IDE asks. Best-effort: offline, the
     first two layers still work.

Layer 3 is where judgement is needed, and why a bare API call is not
enough. Two real examples from the operator's own build:

  * `javax.security.auth.x500.X500Principal` — a JDK class. Maven Central's
    top hit is `org.scala-native:javax-security-stubs`. Adding that would
    be a disaster. Layer 1 stops it before the network is touched.
  * `org.bouncycastle.cert.jcajce.JcaCertStore` — the top hit is
    `org.italiangrid:bcmail`, a third-party repackage. The right answer is
    `org.bouncycastle:bcpkix-jdk18on`. `_rank_candidates` prefers a groupId
    that shares a prefix with the package being resolved.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("lama.deps")

# ──────────────────────────────────────────────────────────────────────
# Layer 1 — packages that ship WITH the JDK.
# ──────────────────────────────────────────────────────────────────────
#
# Two jobs, both load-bearing:
#
#   (a) They must never be resolved to an artifact. Maven Central will
#       happily return a stub or an Android repackage for a JDK class.
#   (b) They must never be renamed `javax.*` -> `jakarta.*`.
#
# (b) is the bug the operator found. A Helidon -> Spring Boot migration
# involves a real javax -> jakarta rename for the Jakarta EE packages, and
# whatever performed it applied the rename by prefix, producing
# `jakarta.security.auth.x500.X500Principal`. No such package exists or
# ever will: `javax.security.auth` is JAAS, part of Java SE since 1.4, and
# it did not move to the Jakarta namespace because it was never Java EE.
# The compiler's "package jakarta.security.auth.x500 does not exist" reads
# like a missing dependency, which is why it survived several fix rounds —
# no dependency can fix it.
JDK_PACKAGE_ROOTS: Tuple[str, ...] = (
    # java.* is obviously JDK; listed for completeness of the check.
    "java.",
    # javax.* roots that are Java SE, NOT Jakarta EE. These are the ones a
    # prefix-based javax->jakarta rename destroys.
    "javax.accessibility.",
    "javax.crypto.",
    "javax.imageio.",
    "javax.lang.model.",
    "javax.management.",
    "javax.naming.",
    "javax.net.",
    "javax.print.",
    "javax.script.",
    "javax.security.auth.",
    "javax.security.cert.",
    "javax.security.sasl.",
    "javax.smartcardio.",
    "javax.sound.",
    "javax.sql.",
    "javax.swing.",
    "javax.tools.",
    "javax.transaction.xa.",
    "javax.xml.catalog.",
    "javax.xml.crypto.",
    "javax.xml.datatype.",
    "javax.xml.namespace.",
    "javax.xml.parsers.",
    "javax.xml.stream.",
    "javax.xml.transform.",
    "javax.xml.validation.",
    "javax.xml.xpath.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.w3c.dom",
    "org.xml.sax",
    "org.ietf.jgss",
)

# javax roots that DID move to the jakarta namespace in Jakarta EE 9. A
# rename here is correct; anywhere else it is the bug above.
JAKARTA_MIGRATED_ROOTS: Tuple[str, ...] = (
    "javax.activation.", "javax.annotation.", "javax.batch.", "javax.decorator.",
    "javax.ejb.", "javax.el.", "javax.enterprise.", "javax.faces.",
    "javax.inject.", "javax.interceptor.", "javax.jms.", "javax.json.",
    "javax.jws.", "javax.mail.", "javax.persistence.", "javax.resource.",
    "javax.security.enterprise.", "javax.security.jacc.", "javax.servlet.",
    "javax.transaction.", "javax.validation.", "javax.websocket.",
    "javax.ws.rs.", "javax.xml.bind.", "javax.xml.soap.", "javax.xml.ws.",
)


def is_jdk_package(pkg: str) -> bool:
    """True when `pkg` ships with the JDK and needs no dependency.

    `javax.transaction.xa` is JDK while `javax.transaction` is Jakarta, so
    the longest matching prefix wins rather than the first.
    """
    p = (pkg or "").strip()
    if not p:
        return False
    jdk_hit = max((len(r) for r in JDK_PACKAGE_ROOTS if p.startswith(r) or p + "." == r), default=0)
    jak_hit = max((len(r) for r in JAKARTA_MIGRATED_ROOTS if p.startswith(r) or p + "." == r), default=0)
    return jdk_hit > jak_hit


def is_invalid_jakarta_package(pkg: str) -> bool:
    """True for a `jakarta.*` package produced by renaming a JDK package.

    `jakarta.security.auth.x500` is the operator's case. It must never be
    resolved to an artifact: no artifact provides it, and Maven Central
    will cheerfully offer a stub JAR to anyone who asks. The fix is to undo
    the rename, which `invalid_jakarta_imports` reports.
    """
    p = (pkg or "").strip()
    if not p.startswith("jakarta."):
        return False
    return is_jdk_package("javax." + p[len("jakarta."):])


def jakarta_rename_is_valid(pkg: str) -> bool:
    """True when this `javax.*` package legitimately becomes `jakarta.*`."""
    p = (pkg or "").strip()
    if not p.startswith("javax."):
        return False
    return not is_jdk_package(p)


def invalid_jakarta_imports(text: str) -> List[str]:
    """`jakarta.*` imports whose `javax.*` original is a JDK package.

    These are the residue of a prefix-based rename and are unfixable by any
    dependency — the package does not exist in any artifact anywhere. This
    is the deterministic detector for the operator's `X500Principal` case.
    """
    bad: List[str] = []
    for m in re.finditer(r"^\s*import\s+(?:static\s+)?(jakarta\.[\w.]+)", text or "", re.MULTILINE):
        fq = m.group(1)
        as_javax = "javax." + fq[len("jakarta."):]
        # Compare at package level (drop the class name).
        pkg = as_javax.rsplit(".", 1)[0]
        if is_jdk_package(pkg) or is_jdk_package(as_javax):
            bad.append(fq)
    return sorted(set(bad))


# ──────────────────────────────────────────────────────────────────────
# Layer 2 — curated coordinates for the libraries that actually appear.
# ──────────────────────────────────────────────────────────────────────
#
# Longest-prefix match, so `com.fasterxml.jackson.dataformat` beats
# `com.fasterxml.jackson`. Versions are deliberately ABSENT: anything under
# the Spring Boot parent should inherit its managed version, and for the
# rest `latest_stable_version` asks Maven Central rather than pinning a
# number here that goes stale.
#
# `spring_managed=True` means the Spring Boot BOM manages the version, so
# the emitted <dependency> must NOT carry one — pinning it there is how a
# project ends up with two Jackson versions on the classpath.
CURATED: Dict[str, Tuple[str, str, bool]] = {
    # package root: (groupId, artifactId, spring_managed)
    "com.itextpdf":                 ("com.itextpdf", "itextpdf", False),
    "com.lowagie.text":             ("com.lowagie", "itext", False),
    "org.apache.pdfbox":            ("org.apache.pdfbox", "pdfbox", False),
    "org.bouncycastle.cert":        ("org.bouncycastle", "bcpkix-jdk18on", False),
    "org.bouncycastle.pkcs":        ("org.bouncycastle", "bcpkix-jdk18on", False),
    "org.bouncycastle.operator":    ("org.bouncycastle", "bcpkix-jdk18on", False),
    "org.bouncycastle.cms":         ("org.bouncycastle", "bcpkix-jdk18on", False),
    "org.bouncycastle.tsp":         ("org.bouncycastle", "bcpkix-jdk18on", False),
    "org.bouncycastle":             ("org.bouncycastle", "bcprov-jdk18on", False),
    "org.jsoup":                    ("org.jsoup", "jsoup", False),
    "org.apache.commons.lang3":     ("org.apache.commons", "commons-lang3", False),
    "org.apache.commons.text":      ("org.apache.commons", "commons-text", False),
    "org.apache.commons.codec":     ("commons-codec", "commons-codec", False),
    "org.apache.commons.io":        ("commons-io", "commons-io", False),
    "org.apache.commons.csv":       ("org.apache.commons", "commons-csv", False),
    "org.apache.commons.collections4": ("org.apache.commons", "commons-collections4", False),
    "org.apache.poi":               ("org.apache.poi", "poi-ooxml", False),
    "org.apache.http":              ("org.apache.httpcomponents.client5", "httpclient5", False),
    "com.google.gson":              ("com.google.code.gson", "gson", True),
    "com.google.common":            ("com.google.guava", "guava", False),
    "com.fasterxml.jackson.dataformat": ("com.fasterxml.jackson.dataformat", "jackson-dataformat-xml", True),
    "com.fasterxml.jackson.datatype":   ("com.fasterxml.jackson.datatype", "jackson-datatype-jsr310", True),
    "com.fasterxml.jackson":        ("com.fasterxml.jackson.core", "jackson-databind", True),
    "redis.clients":                ("redis.clients", "jedis", True),
    "org.modelmapper":              ("org.modelmapper", "modelmapper", False),
    "org.mapstruct":                ("org.mapstruct", "mapstruct", False),
    "lombok":                       ("org.projectlombok", "lombok", True),
    "io.jsonwebtoken":              ("io.jsonwebtoken", "jjwt-api", False),
    "com.auth0.jwt":                ("com.auth0", "java-jwt", False),
    "org.quartz":                   ("org.quartz-scheduler", "quartz", False),
    "org.apache.kafka":             ("org.apache.kafka", "kafka-clients", True),
    "com.zaxxer.hikari":            ("com.zaxxer", "HikariCP", True),
    "org.slf4j":                    ("org.slf4j", "slf4j-api", True),
    "ch.qos.logback":               ("ch.qos.logback", "logback-classic", True),
    "org.apache.logging.log4j":     ("org.apache.logging.log4j", "log4j-api", True),
    "org.json":                     ("org.json", "json", False),
    "com.opencsv":                  ("com.opencsv", "opencsv", False),
    "net.sf.jasperreports":         ("net.sf.jasperreports", "jasperreports", False),
    "org.thymeleaf":                ("org.thymeleaf", "thymeleaf", True),
    "freemarker":                   ("org.freemarker", "freemarker", True),
    "org.postgresql":               ("org.postgresql", "postgresql", True),
    "oracle.jdbc":                  ("com.oracle.database.jdbc", "ojdbc11", False),
    "com.mysql":                    ("com.mysql", "mysql-connector-j", True),
    "org.hibernate":                ("org.hibernate.orm", "hibernate-core", True),
    "org.flywaydb":                 ("org.flywaydb", "flyway-core", True),
    "org.mockito":                  ("org.mockito", "mockito-core", True),
    "org.junit.jupiter":            ("org.junit.jupiter", "junit-jupiter", True),
    "org.assertj":                  ("org.assertj", "assertj-core", True),
    "io.micrometer":                ("io.micrometer", "micrometer-core", True),
    # Jakarta EE APIs. Under a Spring Boot 3 parent most of these arrive
    # through a starter, but a project can legitimately use the API alone.
    "jakarta.validation":           ("jakarta.validation", "jakarta.validation-api", True),
    "jakarta.persistence":          ("jakarta.persistence", "jakarta.persistence-api", True),
    "jakarta.servlet":              ("jakarta.servlet", "jakarta.servlet-api", True),
    "jakarta.annotation":           ("jakarta.annotation", "jakarta.annotation-api", True),
    "jakarta.json":                 ("jakarta.json", "jakarta.json-api", False),
    "jakarta.xml.bind":             ("jakarta.xml.bind", "jakarta.xml.bind-api", True),
    "jakarta.mail":                 ("jakarta.mail", "jakarta.mail-api", True),
    "jakarta.transaction":          ("jakarta.transaction", "jakarta.transaction-api", True),
    "jakarta.inject":               ("jakarta.inject", "jakarta.inject-api", True),
    "io.swagger.v3.oas.annotations": ("org.springdoc", "springdoc-openapi-starter-webmvc-ui", False),
}

# Package roots a Spring Boot starter already brings in. Naming the STARTER
# rather than the individual artifact is what a competent engineer would do
# — it pulls the whole coherent set at versions the BOM manages.
SPRING_STARTER_FOR: Dict[str, str] = {
    "org.springframework.web":          "spring-boot-starter-web",
    "org.springframework.http":         "spring-boot-starter-web",
    "org.springframework.data.jpa":     "spring-boot-starter-data-jpa",
    "org.springframework.data.redis":   "spring-boot-starter-data-redis",
    "org.springframework.data.mongodb": "spring-boot-starter-data-mongodb",
    "org.springframework.security":     "spring-boot-starter-security",
    "org.springframework.validation":   "spring-boot-starter-validation",
    "org.springframework.mail":         "spring-boot-starter-mail",
    "org.springframework.batch":        "spring-boot-starter-batch",
    "org.springframework.amqp":         "spring-boot-starter-amqp",
    "org.springframework.kafka":        "spring-kafka",
    "org.springframework.cache":        "spring-boot-starter-cache",
    "org.springframework.boot.actuate": "spring-boot-starter-actuator",
    "org.springframework.test":         "spring-boot-starter-test",
    # The Spring Boot roots themselves. Without these, `import
    # org.springframework.boot.SpringApplication` falls through to Maven
    # Central and comes back as `spring-boot-autoconfigure:4.1.1` — pinned,
    # and against a 3.3.4 parent. Two Spring versions on one classpath is a
    # harder failure to diagnose than the missing import was.
    "org.springframework.boot":         "spring-boot-starter",
    "org.springframework.context":      "spring-boot-starter",
    "org.springframework.beans":        "spring-boot-starter",
    "org.springframework.core":         "spring-boot-starter",
    "org.springframework.stereotype":   "spring-boot-starter",
    "org.springframework.scheduling":   "spring-boot-starter",
    "org.springframework.transaction":  "spring-boot-starter-data-jpa",
    "org.springframework.jdbc":         "spring-boot-starter-jdbc",
    "org.springframework.data":         "spring-boot-starter-data-jpa",
    "org.springframework":              "spring-boot-starter",
    "jakarta.validation":               "spring-boot-starter-validation",
    "jakarta.persistence":              "spring-boot-starter-data-jpa",
    "jakarta.servlet":                  "spring-boot-starter-web",
}


def _longest_prefix(table: Dict, pkg: str):
    best_key = ""
    for key in table:
        if (pkg == key or pkg.startswith(key + ".")) and len(key) > len(best_key):
            best_key = key
    return table[best_key] if best_key else None


# ──────────────────────────────────────────────────────────────────────
# Import scanning
# ──────────────────────────────────────────────────────────────────────

# `[\w.*]` not `[\w.]`: a wildcard import (`import org.jsoup.nodes.*;`) is
# ordinary in legacy Java, and a pattern that cannot match one silently
# drops the dependency for every file that uses the style.
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)


def scan_java_imports(text: str) -> Set[str]:
    """Every fully-qualified name imported by this Java source."""
    out: Set[str] = set()
    for m in _IMPORT_RE.finditer(text or ""):
        fq = m.group(1)
        if fq.endswith(".*"):
            fq = fq[:-2]
        out.add(fq)
    return out


def external_packages(files: Dict[str, str], own_group: str = "") -> Set[str]:
    """Third-party PACKAGES imported across a generated source tree.

    Excludes the JDK and the project's own group, which is what makes the
    result a shopping list of dependencies rather than a list of imports.
    """
    pkgs: Set[str] = set()
    own = (own_group or "").strip()
    for path, body in (files or {}).items():
        if not path.endswith(".java"):
            continue
        for fq in scan_java_imports(body):
            pkg = fq.rsplit(".", 1)[0] if fq[:1].islower() or "." in fq else fq
            # A trailing segment starting uppercase is the class name.
            parts = fq.split(".")
            for i, seg in enumerate(parts):
                if seg[:1].isupper():
                    pkg = ".".join(parts[:i]) or fq
                    break
            if not pkg or is_jdk_package(pkg) or is_invalid_jakarta_package(pkg):
                continue
            if own and (pkg == own or pkg.startswith(own + ".")):
                continue
            pkgs.add(pkg)
    return pkgs


# ──────────────────────────────────────────────────────────────────────
# Layer 3 — live Maven Central lookup
# ──────────────────────────────────────────────────────────────────────

_SEARCH_URL = "https://search.maven.org/solrsearch/select"
_METADATA_BASE = "https://repo.maven.apache.org/maven2"
_PRERELEASE_RE = re.compile(
    r"(alpha|beta|[-.]m\d|[-.]rc\d|snapshot|preview|dev|cr\d|ea\d?)", re.IGNORECASE
)

# In-process memo. Resolution is deterministic per package and a wave can
# ask for the same one dozens of times; without this a 200-file project
# makes hundreds of identical HTTP calls.
_RESOLVE_CACHE: Dict[str, Optional[Tuple[str, str]]] = {}


def _rank_candidates(pkg: str, docs: List[dict]) -> List[Tuple[str, str]]:
    """Order Maven Central hits by how likely they are to be canonical.

    Necessary, not cosmetic. For `org.bouncycastle.cert.jcajce.JcaCertStore`
    the raw top hit is `org.italiangrid:bcmail` — a third-party repackage —
    and for iText it is `de.rpgframework:itextpdf`. The publisher whose
    groupId shares a prefix with the package is almost always the real one.
    """
    def score(doc: dict) -> tuple:
        g = str(doc.get("g") or "")
        a = str(doc.get("a") or "")
        shared = 0
        gp, pp = g.split("."), pkg.split(".")
        for x, y in zip(gp, pp):
            if x != y:
                break
            shared += 1
        exact_prefix = 1 if (pkg == g or pkg.startswith(g + ".")) else 0
        # Penalise obvious repackages / platform ports.
        penalty = 0
        for bad in ("android", "shaded", "stubs", "scala-native", "-all", "uber"):
            if bad in a.lower() or bad in g.lower():
                penalty = 1
                break
        return (-exact_prefix, -shared, penalty, len(g))

    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    for d in sorted(docs, key=score):
        ga = (str(d.get("g") or ""), str(d.get("a") or ""))
        if ga[0] and ga[1] and ga not in seen:
            seen.add(ga)
            out.append(ga)
    return out


def _group_prefixes(pkg: str) -> List[str]:
    """Plausible groupIds for a package, longest first.

    `org.apache.commons.text` -> ["org.apache.commons.text",
    "org.apache.commons", "org.apache"]. Publishers almost always use a
    groupId that is a prefix of their package namespace, which is the
    single strongest signal available for this question.
    """
    parts = pkg.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 1, -1)]


async def _query(client, q: str, rows: int = 20) -> List[dict]:
    """One search. Returns [] for "this query produced nothing", whatever
    the reason — a non-200, a timeout, a malformed body.

    iter-22 — the transport error used to propagate to
    `search_artifact_for_class`'s single outer `try`, which abandoned the
    whole resolution. It is not a whole-resolution failure: the prefix
    ladder exists precisely so that one query missing is survivable, and
    the narrowest prefix is both the most expensive query server-side and
    the most likely to time out. Reproduced live: `g:"org.apache.commons
    .text" AND fc:"…WordUtils"` read-timed out while `g:"org.apache
    .commons"` answered 10 docs in under a second — the answer was one
    cheap query away and we were returning None.
    """
    try:
        resp = await client.get(_SEARCH_URL, params={"q": q, "rows": rows, "wt": "json"})
        if resp.status_code != 200:
            return []
        return (resp.json().get("response") or {}).get("docs") or []
    except Exception as e:  # noqa: BLE001
        logger.debug("maven query failed (%s): %s", q, e)
        return []


async def search_artifact_for_class(fqcn: str, timeout: float = 8.0) -> Optional[Tuple[str, str]]:
    """Ask Maven Central which artifact contains `fqcn`.

    The same question an IDE's "add import" answers — but a bare
    `fc:"..."` query is not enough to answer it, which is worth stating
    because it is not obvious. For `org.apache.commons.text.WordUtils`
    Maven Central reports **23,881** matches and returns them in no useful
    order: the canonical `org.apache.commons:commons-text` is not in the
    first twenty, which are shaded repackages
    (`org.wso2.orbit.commons-text`, `com.guicedee.services`, …).

    So we ask a narrower question first: constrain the search to a
    groupId that prefixes the package, longest prefix first. For the same
    class that returns 16 matches, all of them `org.apache.commons:
    commons-text`. Only if every prefix misses do we fall back to the
    broad query, where `_rank_candidates` does what it can.

    Best-effort throughout: returns None on any failure so the caller
    falls back to the curated table or reports the gap honestly rather
    than inventing a coordinate.

    iter-22 — "every prefix misses" now genuinely means every prefix was
    TRIED. A single slow query used to abort the ladder and the broad
    fallback with it (see `_query`), so a resolvable package came back
    unresolved and the operator got a missing dependency blamed on the
    migration. Only the client setup can still short-circuit, because
    without a client there is no question to ask.
    """
    pkg = fqcn.rsplit(".", 1)[0]
    try:
        import httpx
        from llm import _http_verify
        client_cm = httpx.AsyncClient(timeout=timeout, verify=_http_verify())
    except Exception as e:  # noqa: BLE001
        logger.warning("maven search unavailable for %s: %s", fqcn, e)
        return None
    try:
        async with client_cm as client:
            for prefix in _group_prefixes(pkg):
                docs = await _query(client, f'g:"{prefix}" AND fc:"{fqcn}"', rows=10)
                ranked = _rank_candidates(pkg, docs)
                if ranked:
                    return ranked[0]
            docs = await _query(client, f'fc:"{fqcn}"', rows=20)
    except Exception as e:  # noqa: BLE001
        logger.warning("maven search failed for %s: %s", fqcn, e)
        return None
    ranked = _rank_candidates(pkg, docs)
    if not ranked:
        logger.info("maven search found no artifact for %s (all prefixes tried)", fqcn)
    return ranked[0] if ranked else None


async def latest_stable_version(group_id: str, artifact_id: str,
                                timeout: float = 8.0) -> str:
    """Newest non-prerelease version from the artifact's maven-metadata.xml.

    The operator asked for "the latest version" to be used. Prereleases are
    excluded deliberately: an `-M1` or `-alpha` resolves fine and then fails
    at runtime in ways that look like migration defects.
    """
    try:
        import httpx
        import xml.etree.ElementTree as ET
        from llm import _http_verify
        url = f"{_METADATA_BASE}/{group_id.replace('.', '/')}/{artifact_id}/maven-metadata.xml"
        async with httpx.AsyncClient(timeout=timeout, verify=_http_verify()) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            root = ET.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("maven metadata failed for %s:%s: %s", group_id, artifact_id, e)
        return ""
    versions = [v.text for v in root.iter("version") if v.text]
    stable = [v for v in versions if not _PRERELEASE_RE.search(v)]
    pool = stable or versions
    if not pool:
        return ""

    def key(v: str):
        out = []
        for part in re.split(r"[.\-_]", v):
            out.append((0, int(part)) if part.isdigit() else (1, part))
        return tuple(out)

    return sorted(pool, key=key)[-1]


async def resolve_package(pkg: str, allow_network: bool = True) -> Optional[Dict[str, str]]:
    """The artifact that provides `pkg`, or None when none is needed/known.

    Returns `{"group_id", "artifact_id", "spring_managed", "source"}`.
    `source` records WHICH layer answered, so a caller (and an operator
    reading a log) can tell a curated certainty from a network guess.
    """
    pkg = (pkg or "").strip()
    if not pkg or is_jdk_package(pkg) or is_invalid_jakarta_package(pkg):
        return None
    if pkg in _RESOLVE_CACHE:
        hit = _RESOLVE_CACHE[pkg]
        if hit is None:
            return None
        return {"group_id": hit[0], "artifact_id": hit[1],
                "spring_managed": "", "source": "cache"}

    curated = _longest_prefix(CURATED, pkg)
    if curated:
        g, a, managed = curated
        _RESOLVE_CACHE[pkg] = (g, a)
        return {"group_id": g, "artifact_id": a,
                "spring_managed": "1" if managed else "", "source": "curated"}

    if not allow_network:
        return None
    found = await search_artifact_for_class(f"{pkg}.package-info")
    if not found:
        # package-info rarely exists; retry against the package itself,
        # which matches Maven Central's class index for most libraries.
        found = await search_artifact_for_class(pkg)
    _RESOLVE_CACHE[pkg] = found
    if not found:
        return None
    return {"group_id": found[0], "artifact_id": found[1],
            "spring_managed": "", "source": "maven-central"}


def reset_cache() -> None:
    """Test hook."""
    _RESOLVE_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# pom.xml inspection
# ──────────────────────────────────────────────────────────────────────

_POM_DEP_RE = re.compile(
    r"<dependency>\s*(?:<!--.*?-->\s*)*<groupId>\s*([^<\s]+)\s*</groupId>\s*"
    r"<artifactId>\s*([^<\s]+)\s*</artifactId>",
    re.DOTALL,
)


def declared_coordinates(pom_xml: str) -> Set[Tuple[str, str]]:
    """`(groupId, artifactId)` pairs the pom already declares."""
    return {(m.group(1), m.group(2)) for m in _POM_DEP_RE.finditer(pom_xml or "")}


def declared_artifacts(pom_xml: str) -> Set[str]:
    return {a for _, a in declared_coordinates(pom_xml)}


def render_dependency(group_id: str, artifact_id: str, version: str = "",
                      indent: str = "    ") -> str:
    """One `<dependency>` block. Version omitted when a BOM manages it."""
    lines = [f"{indent}<dependency>",
             f"{indent}  <groupId>{group_id}</groupId>",
             f"{indent}  <artifactId>{artifact_id}</artifactId>"]
    if version:
        lines.append(f"{indent}  <version>{version}</version>")
    lines.append(f"{indent}</dependency>")
    return "\n".join(lines)


def inject_dependencies(pom_xml: str, blocks: List[str]) -> str:
    """Insert `<dependency>` blocks before the closing `</dependencies>`.

    Text insertion rather than an XML round-trip on purpose: an ElementTree
    rewrite reorders attributes, drops comments and reformats the whole
    file, which turns a two-line dependency addition into a diff nobody can
    review — and reviewability is the point when a model wrote the file.
    """
    if not blocks:
        return pom_xml
    idx = pom_xml.rfind("</dependencies>")
    if idx == -1:
        return pom_xml
    return pom_xml[:idx] + "\n".join(blocks) + "\n  " + pom_xml[idx:]


async def missing_dependencies_for_sources(
    files: Dict[str, str],
    pom_xml: str,
    own_group: str = "",
    under_spring_parent: bool = True,
    allow_network: bool = True,
    forbidden_roots: Optional[Tuple[str, ...]] = None,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """`(dependencies_to_add, source_stack_imports_left_behind)`.

    The whole point of the module, in one call: scan what the code imports,
    subtract what the manifest declares, and return the difference already
    resolved to coordinates.

    `forbidden_roots` is the safety catch, and it is not optional in
    practice. Without it this function will happily resolve a leftover
    `jakarta.ws.rs` or `io.helidon` import to a real artifact and add it —
    turning a RED build into a GREEN one that still runs the framework the
    migration was supposed to remove. That is a worse outcome than the
    failure, because the failure is visible. Packages under a forbidden
    root are never resolved; they are returned separately so the caller can
    report the migration as incomplete.
    """
    declared = declared_artifacts(pom_xml)
    needed: Dict[Tuple[str, str], Dict[str, str]] = {}
    residue: List[str] = []
    forbidden = tuple(forbidden_roots or ())

    for pkg in sorted(external_packages(files, own_group=own_group)):
        if forbidden and any(pkg == r or pkg.startswith(r + ".") or pkg.startswith(r)
                             for r in forbidden):
            residue.append(pkg)
            continue
        # A Spring starter is the better answer where one covers the package.
        starter = _longest_prefix(SPRING_STARTER_FOR, pkg) if under_spring_parent else None
        if starter:
            if starter not in declared:
                needed[("org.springframework.boot", starter)] = {
                    "group_id": "org.springframework.boot",
                    "artifact_id": starter,
                    "version": "",
                    "for_package": pkg,
                    "source": "spring-starter",
                }
            continue

        res = await resolve_package(pkg, allow_network=allow_network)
        if not res:
            continue
        g, a = res["group_id"], res["artifact_id"]
        if a in declared or (g, a) in needed:
            continue
        version = ""
        bom_managed = bool(res.get("spring_managed")) or (
            under_spring_parent and g.startswith("org.springframework")
        )
        if not (bom_managed and under_spring_parent):
            version = await latest_stable_version(g, a) if allow_network else ""
        needed[(g, a)] = {
            "group_id": g, "artifact_id": a, "version": version,
            "for_package": pkg, "source": res.get("source", ""),
        }

    return list(needed.values()), sorted(set(residue))
