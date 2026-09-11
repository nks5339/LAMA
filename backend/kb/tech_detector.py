"""Legacy tech-stack auto-detection.

Inspects the uploaded files for a project and produces a fingerprint of the
ACTUAL legacy stack — language, framework, build system, database engine.
This output then overrides the seed-supplied `project.source_tech` so every
LLM call (SRS, revalidation, architecture) sees the truth instead of the
default "PHP / CodeIgniter / MariaDB" placeholder.

The detector is intentionally evidence-based: every claim it makes carries
a list of evidence files / patterns. Where no evidence is present for a
slot, it returns "" — never a guess (matches `gov.core` truth_rule_mode).

**ZIP-aware** (iter 13.6): when an uploaded file's `filetype == "zip"`,
its parsed chunks contain `===== FILE: <inner-path> =====` markers (written
by `kb.parsers.parse_zip`). We mine those inner paths so a project that
only uploaded `legacy-code.zip` still detects Java / JSP / Struts /
Oracle from the files INSIDE the archive, instead of just seeing "zip"
and falling back to the SQL file alongside it.
"""
from __future__ import annotations
import re
from collections import Counter
from typing import Iterable


# ── File-extension → language fingerprint ─────────────────────────────────
EXT_LANGUAGE = {
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".jsp": "JSP", ".jspx": "JSP", ".jspf": "JSP", ".tag": "JSP", ".tld": "JSP",
    ".cs": "C#", ".vb": "VB.NET", ".aspx": "ASP.NET", ".cshtml": "Razor",
    ".vbhtml": "Razor",
    ".php": "PHP",
    ".py": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".rb": "Ruby", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".h": "C/C++",
    ".swift": "Swift", ".m": "Objective-C", ".pl": "Perl",
    ".html": "HTML", ".htm": "HTML", ".xhtml": "HTML",
    ".sql": "SQL", ".plsql": "PL/SQL",
    ".pls": "PL/SQL", ".pkb": "PL/SQL", ".pks": "PL/SQL",
}

# ── Filename / content markers → frameworks & build systems ────────────────
# Each marker is (predicate, label). Predicate is checked against the lowercase
# full path; `+ <substring>` markers are content fragments looked up inside
# common manifest files (pom.xml, build.gradle, web.xml, *.csproj, etc.).
FRAMEWORK_NAME_MARKERS = [
    ("pom.xml",                        "Maven"),
    ("build.gradle",                   "Gradle"),
    ("build.gradle.kts",               "Gradle (Kotlin DSL)"),
    ("settings.gradle",                "Gradle"),
    ("web.xml",                        "Java EE Servlet/JSP"),
    ("struts.xml",                     "Struts 2"),
    ("struts-config.xml",              "Struts 1"),
    ("struts-default.xml",             "Struts 2"),
    ("struts-plugin.xml",              "Struts 2"),
    ("validation.xml",                 "Struts Validator"),
    ("applicationcontext.xml",         "Spring"),
    ("spring-",                        "Spring"),
    ("hibernate.cfg.xml",              "Hibernate"),
    ("persistence.xml",                "JPA"),
    ("faces-config.xml",               "JSF"),
    ("mybatis-config.xml",             "MyBatis"),
    # iter-14.81 — Oracle Helidon detection
    ("microprofile-config.properties", "Helidon MicroProfile"),
    ("application.yaml",               "Helidon/Spring Config"),
    ("helidon",                        "Helidon"),
    (".csproj",                        ".NET (project)"),
    (".vbproj",                        ".NET / VB.NET"),
    ("packages.config",                "NuGet"),
    ("web.config",                     "ASP.NET"),
    ("global.asax",                    "ASP.NET WebForms/MVC"),
    ("composer.json",                  "Composer (PHP)"),
    ("artisan",                        "Laravel"),
    ("application/config",             "CodeIgniter"),
    ("system/codeigniter",             "CodeIgniter"),
    ("symfony",                        "Symfony"),
    ("manage.py",                      "Django"),
    ("django",                         "Django"),
    ("flask",                          "Flask"),
    ("fastapi",                        "FastAPI"),
    ("rails",                          "Rails"),
    ("package.json",                   "npm / Node"),
    ("angular.json",                   "Angular"),
    ("vite.config",                    "Vite"),
    ("next.config",                    "Next.js"),
    ("nuxt.config",                    "Nuxt"),
    ("webpack.config",                 "Webpack"),
    ("dockerfile",                     "Docker"),
    ("docker-compose",                 "Docker Compose"),
    (".jenkinsfile",                   "Jenkins"),
    ("makefile",                       "Make"),
]

# Content fragments looked up inside parsed file text (case-insensitive).
CONTENT_FRAMEWORK_MARKERS = [
    ("@springbootapplication",         "Spring Boot"),
    ("@restcontroller",                "Spring MVC (REST)"),
    ("@controller",                    "Spring MVC"),
    ("@entity",                        "JPA"),
    ("javax.persistence",              "JPA"),
    ("jakarta.persistence",            "JPA (Jakarta)"),
    ("org.springframework",            "Spring"),
    ("org.hibernate",                  "Hibernate"),
    # iter-14.81 — Oracle Helidon SE/MP detection
    ("io.helidon",                     "Helidon"),
    ("io.helidon.webserver",           "Helidon SE"),
    ("io.helidon.microprofile",        "Helidon MicroProfile"),
    ("io.helidon.nima",                "Helidon Níma"),
    ("io.helidon.config",              "Helidon Config"),
    ("io.helidon.health",              "Helidon Health"),
    ("io.helidon.metrics",             "Helidon Metrics"),
    ("io.helidon.security",            "Helidon Security"),
    ("io.helidon.dbclient",            "Helidon DB Client"),
    ("io.helidon.integrations",        "Helidon Integrations"),
    ("@applicationscoped",             "CDI (Jakarta/Helidon)"),
    ("@requestscoped",                 "CDI (Jakarta/Helidon)"),
    ("@inject",                        "CDI/DI"),
    ("@path(",                         "JAX-RS"),
    ("@get(",                          "JAX-RS"),
    ("@post(",                         "JAX-RS"),
    ("@put(",                          "JAX-RS"),
    ("@delete(",                       "JAX-RS"),
    ("@produces(",                     "JAX-RS"),
    ("@consumes(",                     "JAX-RS"),
    ("javax.ws.rs",                    "JAX-RS"),
    ("jakarta.ws.rs",                  "JAX-RS (Jakarta)"),
    ("org.eclipse.microprofile",       "MicroProfile"),
    ("@restclient",                    "MicroProfile Rest Client"),
    ("@configproperty",                "MicroProfile Config"),
    ("@health",                        "MicroProfile Health"),
    ("@liveness",                      "MicroProfile Health"),
    ("@readiness",                     "MicroProfile Health"),
    ("@metered",                       "MicroProfile Metrics"),
    ("@timed",                         "MicroProfile Metrics"),
    ("@fallback",                      "MicroProfile Fault Tolerance"),
    ("@retry",                         "MicroProfile Fault Tolerance"),
    ("@circuitbreaker",                "MicroProfile Fault Tolerance"),
    ("@timeout",                       "MicroProfile Fault Tolerance"),
    # Quarkus detection
    ("io.quarkus",                     "Quarkus"),
    ("@quartzscheduled",               "Quarkus"),
    # Micronaut detection  
    ("io.micronaut",                   "Micronaut"),
    ("@controller",                    "Micronaut Controller"),
    # Continue existing markers
    ("struts2",                        "Struts 2"),
    ("org.apache.struts2",             "Struts 2"),
    ("org.apache.struts",              "Struts"),
    # Common Struts indicators that appear in plain Java action classes even
    # when the imports use an alias or are auto-organised: extending
    # ActionSupport / DispatchAction / Action, the @Action / @Result /
    # @Namespace annotations, the <action name="…"> XML element, and the
    # `extends="struts-default"` inheritance marker. Without these, repos
    # that ship .java files without struts.xml uploaded got classified as
    # plain "Java" and the SRS hallucinated "SQL/Java stack" (iter 13.5).
    ("extends actionsupport",          "Struts 2"),
    ("dispatchaction",                 "Struts 1"),
    ("extends action ",                "Struts 1"),
    ("@action(",                       "Struts 2"),
    ("@result(",                       "Struts 2"),
    ("@namespace(",                    "Struts 2"),
    ("<action name=",                  "Struts (action mapping)"),
    ("extends=\"struts-default\"",     "Struts 2"),
    ("extends='struts-default'",       "Struts 2"),
    ("struts.convention",              "Struts 2 Convention plugin"),
    ("javax.servlet",                  "Servlet API"),
    ("javax.faces",                    "JSF"),
    ("primefaces",                     "PrimeFaces"),
    ("ibatis",                         "iBatis"),
    ("mybatis",                        "MyBatis"),
    ("microsoft.entityframeworkcore",  "Entity Framework Core"),
    ("system.web.mvc",                 "ASP.NET MVC"),
    ("codeigniter",                    "CodeIgniter"),
    ("laravel",                        "Laravel"),
    ("symfony",                        "Symfony"),
    ("django.db",                      "Django ORM"),
    ("from flask",                     "Flask"),
    ("from fastapi",                   "FastAPI"),
    ("express()",                      "Express.js"),
    ("react.component",                "React"),
    ("ng-app",                         "AngularJS"),
    ("@angular/core",                  "Angular"),
    ("vue.createapp",                  "Vue"),
]

# Database engines — content / filename markers (case-insensitive).
DATABASE_MARKERS = [
    ("oracle.jdbc",                    "Oracle"),
    ("jdbc:oracle:",                   "Oracle"),
    ("nvarchar2",                      "Oracle"),
    ("dbms_",                          "Oracle (PL/SQL)"),
    ("create or replace package",      "Oracle (PL/SQL)"),
    ("jdbc:postgresql:",               "PostgreSQL"),
    ("psycopg2",                       "PostgreSQL"),
    ("jdbc:mysql:",                    "MySQL"),
    ("mysqli_",                        "MySQL"),
    ("pdo_mysql",                      "MySQL"),
    ("engine=innodb",                  "MySQL/MariaDB"),
    ("mariadb",                        "MariaDB"),
    ("jdbc:sqlserver:",                "Microsoft SQL Server"),
    ("microsoft.data.sqlclient",       "Microsoft SQL Server"),
    ("system.data.sqlclient",          "Microsoft SQL Server"),
    ("nvarchar(max)",                  "Microsoft SQL Server"),
    ("jdbc:db2:",                      "IBM DB2"),
    ("jdbc:sybase:",                   "Sybase"),
    ("mongodb://",                     "MongoDB"),
    ("redis://",                       "Redis"),
    ("sqlite",                         "SQLite"),
]

# Manifest files whose content is worth scanning for framework markers.
MANIFEST_FILENAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "web.xml", "struts.xml", "struts-config.xml", "applicationcontext.xml",
    "persistence.xml", "hibernate.cfg.xml", "faces-config.xml",
    "mybatis-config.xml", "packages.config", "web.config", "global.asax",
    "composer.json", "package.json", "angular.json", "requirements.txt",
    "pipfile", "pyproject.toml", "gemfile",
}


def _detect_languages(filenames: Iterable[str]) -> list[tuple[str, int]]:
    """Tally languages by extension, return [(language, file_count), ...] sorted desc."""
    counter: Counter[str] = Counter()
    for fn in filenames:
        lf = fn.lower()
        for ext, lang in EXT_LANGUAGE.items():
            if lf.endswith(ext):
                counter[lang] += 1
                break
    return counter.most_common()


def _detect_frameworks(filenames: Iterable[str]) -> set[str]:
    """Detect frameworks / build systems from filename hints alone."""
    found: set[str] = set()
    for fn in filenames:
        lf = fn.lower().replace("\\", "/")
        for marker, label in FRAMEWORK_NAME_MARKERS:
            if marker in lf:
                found.add(label)
    return found


def _scan_content(
    filenames_and_text: Iterable[tuple[str, str]],
    markers: list[tuple[str, str]],
    char_cap: int = 60000,
) -> set[str]:
    """Substring-scan a small slice of each file for framework / DB markers.

    `char_cap` keeps the scan cheap even for big files — markers we care
    about almost always appear in import statements / first declarations.
    """
    found: set[str] = set()
    pending_labels = {label for _, label in markers}
    for fn, text in filenames_and_text:
        if not pending_labels:
            break
        if not text:
            continue
        sample = text[:char_cap].lower()
        for needle, label in markers:
            if label in found:
                continue
            if needle in sample:
                found.add(label)
                pending_labels.discard(label)
    return found


def detect_tech_stack(
    files: list[dict],
    chunks_by_file: dict[str, list[str]] | None = None,
) -> dict:
    """Produce a structured tech-stack fingerprint from uploaded `kb_files`.

    Args:
      files:           kb_files documents (each has `filename`, `filetype`, `id`).
      chunks_by_file:  optional file_id → list of chunk-content strings. When
                       supplied, content markers (Spring Boot, Oracle JDBC,
                       etc.) are detected with much higher fidelity. Without
                       it, only filename-based heuristics fire.

    Returns:
      {
        "language": "Java",              # primary language by file count
        "languages": [("Java", 412), ("JSP", 88), ...],  # ranked
        "frameworks": ["Spring", "Hibernate"],
        "database": "Oracle",
        "databases": ["Oracle"],
        "build": "Maven",
        "summary": "Java / Spring + Hibernate / Oracle (Maven build)",
        "evidence": {
            "language":   ["AccountService.java", "..."],
            "frameworks": {"Spring": ["pom.xml", "applicationcontext.xml"], ...},
            "database":   {"Oracle": ["DataSource.java", "schema.sql"]},
        },
      }

    Empty fields mean NO evidence was found — do not guess.
    """
    filenames = [f.get("filename", "") for f in files]

    # ── ZIP-aware filename expansion (iter 13.6) ────────────────────────
    # When the user uploaded their legacy app as a single .zip, the OUTER
    # filename list contains just "legacy-code.zip" and the OUTER filetype
    # is "zip". Language detection would then see zero .java / .jsp / .php
    # files and fall back to whichever loose file sits alongside the zip
    # (commonly a SQL DDL → "source_tech: SQL"). Mine the inner paths from
    # the chunk text of zip files — kb.parsers.parse_zip writes them as
    # `===== FILE: src/com/acme/foo.java =====` block headers — and merge
    # them into both the filename tally AND the framework name-marker scan.
    inner_filenames: list[str] = []
    if chunks_by_file:
        zip_inner_marker = re.compile(r"=====\s*FILE:\s*([^=\n]+?)\s*=====")
        for f in files:
            if (f.get("filetype") or "").lower() != "zip":
                continue
            fid = f.get("id")
            if not fid:
                continue
            for chunk in chunks_by_file.get(fid, [])[:50]:  # cap to first 50 chunks per zip
                for m in zip_inner_marker.finditer(chunk or ""):
                    inner = m.group(1).strip()
                    if inner and not inner.endswith("/"):
                        inner_filenames.append(inner)
    if inner_filenames:
        # De-dupe while keeping insertion order so evidence lists stay stable.
        seen: set[str] = set()
        merged = list(filenames)
        for n in inner_filenames:
            if n not in seen:
                seen.add(n)
                merged.append(n)
        filenames = merged

    # ---- Language ranking ---------------------------------------------------
    lang_counts = _detect_languages(filenames)
    primary_lang = lang_counts[0][0] if lang_counts else ""

    # ---- Framework / build system from filenames ---------------------------
    fw_from_names = _detect_frameworks(filenames)

    # ---- Content-driven markers (much higher precision) --------------------
    fw_from_content: set[str] = set()
    db_from_content: set[str] = set()
    if chunks_by_file:
        # Iterate files lazily; prioritise manifests + code files so the scan
        # short-circuits as soon as every marker is identified.
        def _iter() -> Iterable[tuple[str, str]]:
            ranked = sorted(
                files,
                key=lambda f: (
                    0 if f.get("filename", "").lower().rsplit("/", 1)[-1] in MANIFEST_FILENAMES else 1,
                    -(f.get("size") or 0),
                ),
            )
            for f in ranked:
                fid = f.get("id")
                if not fid:
                    continue
                pieces = chunks_by_file.get(fid) or []
                if not pieces:
                    continue
                # ZIP-uploaded projects need a wider content window because
                # Struts / Spring / EF Core code may sit deep inside the
                # archive's chunk stream (chunks 30..200), not the first 6.
                # iter 13.6 fix for the "source_tech: SQL" miss on a
                # legacy-code.zip + DDL upload.
                per_file_cap = 80 if (f.get("filetype") or "").lower() == "zip" else 6
                yield f.get("filename", ""), "\n".join(pieces[:per_file_cap])

        fw_from_content = _scan_content(_iter(), CONTENT_FRAMEWORK_MARKERS)
        db_from_content = _scan_content(_iter(), DATABASE_MARKERS)

    frameworks = sorted(fw_from_names | fw_from_content)
    databases  = sorted(db_from_content)

    # ---- Build system distilled from frameworks list -----------------------
    build = ""
    for cand in ("Maven", "Gradle", "Gradle (Kotlin DSL)", "Composer (PHP)",
                 "npm / Node", "NuGet"):
        if cand in frameworks:
            build = cand
            break

    # ---- Summary string suitable for project.source_tech ------------------
    summary_parts: list[str] = []
    if primary_lang:
        # Java/JSP combined → "Java / JSP"
        if primary_lang == "Java" and any(l == "JSP" for l, _ in lang_counts):
            summary_parts.append("Java / JSP")
        else:
            summary_parts.append(primary_lang)

    # Pick the most "intent-bearing" frameworks first. The Struts family is
    # ahead of Spring because a project that has BOTH usually means "legacy
    # Struts code with a Spring container glue" — Struts is the layer the
    # SRS needs to describe first. (iter 13.5)
    fw_priority = [
        "Struts 2", "Struts 1", "Struts", "Struts 2 Convention plugin",
        "Struts Validator", "Struts (action mapping)",
        "Spring Boot", "Spring MVC (REST)", "Spring MVC", "Spring",
        "JSF", "Hibernate", "JPA", "MyBatis", "iBatis",
        "ASP.NET MVC", "ASP.NET", "Entity Framework Core",
        "CodeIgniter", "Laravel", "Symfony",
        "Django", "Flask", "FastAPI",
        "Rails", "Express.js", "Angular", "React", "Vue",
    ]
    chosen_fw = [f for f in fw_priority if f in frameworks][:3]
    if chosen_fw:
        summary_parts.append(" + ".join(chosen_fw))

    if databases:
        summary_parts.append(databases[0])

    if build and build not in summary_parts:
        summary_parts.append(f"{build} build")

    summary = " / ".join(summary_parts) if summary_parts else "Unknown legacy stack"

    # ---- Evidence map for audit-trail / SRS citations ----------------------
    evidence: dict = {
        "language": [
            fn for fn in filenames
            if primary_lang
            and any(fn.lower().endswith(ext) for ext, lang in EXT_LANGUAGE.items() if lang == primary_lang)
        ][:8],
        "frameworks": {
            label: [fn for fn in filenames if marker in fn.lower().replace("\\", "/")][:5]
            for marker, label in FRAMEWORK_NAME_MARKERS
            if label in frameworks
        },
        "database": {db: [] for db in databases},
    }

    return {
        "language": primary_lang,
        "languages": lang_counts,
        "frameworks": frameworks,
        "database": databases[0] if databases else "",
        "databases": databases,
        "build": build,
        "summary": summary,
        "evidence": evidence,
    }

