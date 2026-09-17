"""Direct Transform stack catalogue — iter-21.

One row per selectable technology. This is the single source of truth for
the source/target dropdowns AND for the migration brief the AI pass sends,
so a stack can never appear in the UI without the prompt knowing what it is.

Adding a technology is one entry here — no code, the same bar
`MIGRATION_PLAYBOOKS` sets for the Transformer (see CLAUDE.md).

### Versions

Every `version` below was checked against the vendor's own release/EOL data
in **September 2026**, not recalled. Pinning a version the vendor has already
retired is worse than pinning none: it tells the model to emit code that is
out of support on the day it is generated.

| Stack | Pinned | Why |
|---|---|---|
| Java | 25 LTS | Sept 2025, premier support to 2030. JDK 21 updates after Sept 2026 move to the OTN licence, so 25 is the permissive LTS. |
| Spring Boot | 4.1 | 4.1.1 GA Aug 2026, Java 17-26. **3.5 left OSS support on 30 Jun 2026**, so `spring-boot-3` is kept only for existing jobs. |
| .NET | 10 LTS | Nov 2025 → Nov 2028. .NET 8 LTS ends Nov 2026. |
| Angular | 22 | Released Jun 2026; v20 leaves LTS ~Nov 2026. |
| React | 19 | 19.3.0, Sept 2026. React runs no LTS programme — the major is the contract. |
| Node | 26 LTS | Active LTS to Oct 2027; 24 is maintenance-only. |
| PostgreSQL | 18 | Sept 2025, supported to Nov 2030. `postgres-15` kept for existing jobs. |
| Jakarta Pages | 4.0 | Jakarta EE 11. The modern name for JSP. |

`role` controls which dropdown a stack appears in — several legacy stacks are
migrate-FROM only, and nothing should offer "migrate TO Struts".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Stack:
    """One selectable technology.

    `idioms` / `manifest` / `api_docs` are consumed when this stack is the
    TARGET; `forbidden` when it is the SOURCE — they become the "no residue"
    clause of the brief, which is what stops a half-migrated file passing.

    `suffixes` is consumed in BOTH roles: the union over the selected pair is
    what the AI pass actually reads off disk. Until iter-22 the engine
    hardcoded `(".java", ".sql")`, so every pair that was not Java or SQL —
    JSP, React, Angular, .NET, jQuery — staged its files and then had nothing
    converted, while the job still reported COMPLETED.

    Source-code extensions only. Build manifests (pom.xml, package.json,
    *.csproj) are deliberately absent: they are owned by
    `dependency_migrator` and `devops_agent`, and handing the same file to
    two writers is how a repaired pom gets un-repaired.
    """
    id: str
    label: str
    family: str                 # backend | frontend | database | platform
    language: str
    version: str = ""
    role: str = "both"          # source | target | both
    aliases: tuple[str, ...] = ()
    idioms: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    # Markers this stack legitimately uses that a SOURCE stack lists as
    # forbidden. `jakarta.ws.rs.` is Helidon residue under Spring and the
    # house style under Quarkus; without this, adding a Quarkus row would
    # make every correct Quarkus file scan as half-migrated. Empty for every
    # current row — see `residue_markers`.
    permitted: tuple[str, ...] = ()
    manifest: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    api_docs: str = ""
    build_cmd: str = ""
    notes: str = ""

    def describe(self) -> dict[str, Any]:
        """The shape the dropdown consumes. Prompt-only fields stay server-side."""
        return {
            "id": self.id,
            "label": self.label,
            "family": self.family,
            "language": self.language,
            "version": self.version,
            "role": self.role,
        }


# ── Backend ──────────────────────────────────────────────────────────────
_BACKEND: tuple[Stack, ...] = (
    Stack(
        id="helidon-mp", label="Helidon MicroProfile", family="backend",
        language="Java", version="4.x", role="source",
        aliases=("helidon", "microprofile", "jaxrs"),
        forbidden=("io.helidon.", "org.eclipse.microprofile.", "jakarta.ws.rs.",
                   "@ApplicationScoped", "@ConfigProperty", "@Inject"),
        notes="MicroProfile Config/Health/Metrics/OpenAPI + JAX-RS resources.",
        suffixes=(".java",),
    ),
    Stack(
        id="helidon-se", label="Helidon SE", family="backend",
        language="Java", version="4.x", role="source",
        aliases=("helidon-reactive",),
        forbidden=("io.helidon.",),
        notes="Reactive WebServer routing built with Routing.builder().",
        suffixes=(".java",),
    ),
    Stack(
        id="spring-boot-4", label="Spring Boot 4.1 (Java 25 LTS)", family="backend",
        language="Java 25", version="4.1", role="both",
        aliases=("spring-boot", "springboot", "spring"),
        idioms=(
            "@RestController + @RequestMapping/@GetMapping (never JAX-RS @Path/@GET)",
            "constructor injection (never field @Inject/@Autowired)",
            "@ConfigurationProperties or @Value(\"${key:default}\") for configuration",
            "ResponseEntity<T> for responses that set status or headers",
            "Spring Data JPA repositories for persistence",
            "@Transactional from org.springframework.transaction.annotation",
            "jakarta.* imports throughout (never javax.* for EE APIs)",
            "Spring Security filter chain as a @Bean (never a Helidon/JAAS config)",
        ),
        forbidden=("io.helidon.", "org.eclipse.microprofile.", "jakarta.ws.rs.",
                   "@ApplicationScoped", "@ConfigProperty"),
        manifest=(
            "spring-boot-starter-parent 4.1.x as <parent> so versions are inherited",
            "spring-boot-starter-web for REST; -data-jpa for persistence; "
            "-validation for bean validation; -security where the source had auth",
            "springdoc-openapi-starter-webmvc-ui for OpenAPI/Swagger UI",
            "spring-boot-maven-plugin so the jar is executable",
            "maven.compiler.release=25",
        ),
        api_docs=("Expose OpenAPI with springdoc-openapi-starter-webmvc-ui and "
                  "annotate controllers with @Tag/@Operation. /swagger-ui.html "
                  "must resolve on a running app."),
        build_cmd="mvn -q -DskipTests compile",
        suffixes=(".java",),
    ),
    Stack(
        id="spring-boot-3", label="Spring Boot 3.x (Java 21) — legacy target",
        family="backend", language="Java 21", version="3.5", role="both",
        idioms=(
            "@RestController + @RequestMapping/@GetMapping (never JAX-RS @Path/@GET)",
            "constructor injection (never field @Inject/@Autowired)",
            "@ConfigurationProperties or @Value(\"${key:default}\") for configuration",
            "Spring Data JPA repositories for persistence",
            "jakarta.* imports throughout (Spring Boot 3 is Jakarta EE 9+)",
        ),
        forbidden=("io.helidon.", "org.eclipse.microprofile.", "jakarta.ws.rs.",
                   "@ApplicationScoped", "@ConfigProperty"),
        manifest=(
            "spring-boot-starter-parent 3.5.x as <parent>",
            "spring-boot-starter-web, -data-jpa, -validation",
            "springdoc-openapi-starter-webmvc-ui for the OpenAPI/Swagger UI",
            "spring-boot-maven-plugin",
        ),
        api_docs=("Expose OpenAPI with springdoc-openapi-starter-webmvc-ui; "
                  "/swagger-ui.html must resolve."),
        build_cmd="mvn -q -DskipTests compile",
        notes=("Spring Boot 3.5 left OSS support on 30 Jun 2026. Kept selectable "
               "so jobs created before iter-21 still resolve; new work should "
               "target spring-boot-4."),
        suffixes=(".java",),
    ),
    Stack(
        id="dotnet-10", label=".NET 10 LTS (C# / ASP.NET Core)", family="backend",
        language="C# / .NET 10", version="10.0", role="both",
        aliases=("dotnet", ".net", "aspnet", "aspnetcore", "csharp"),
        idioms=(
            "ASP.NET Core minimal APIs or [ApiController] controllers",
            "constructor injection via the built-in DI container",
            "IOptions<T> bound from appsettings.json for configuration",
            "Entity Framework Core for persistence",
            "async/await end to end; Task<IResult>/Task<ActionResult<T>> returns",
            "nullable reference types enabled",
        ),
        forbidden=("System.Web.", "HttpContext.Current", "ConfigurationManager."),
        manifest=(
            "a .csproj targeting net10.0 with <Nullable>enable</Nullable>",
            "Microsoft.AspNetCore.OpenApi + Swashbuckle.AspNetCore for OpenAPI",
            "Microsoft.EntityFrameworkCore + the matching provider package",
        ),
        api_docs=("Expose OpenAPI via Swashbuckle (AddSwaggerGen/UseSwaggerUI) "
                  "or the built-in AddOpenApi. /swagger must resolve."),
        build_cmd="dotnet build",
        suffixes=(".cs", ".cshtml", ".razor"),
    ),
    Stack(
        id="jsp", label="JSP / Jakarta Pages 4.0 (Servlet)", family="backend",
        language="Java", version="4.0", role="source",
        aliases=("jakarta-pages", "servlet", "jsp-servlet"),
        forbidden=("<%", "<jsp:", "javax.servlet.", "jakarta.servlet.http.HttpServlet"),
        notes=("Server-rendered pages with scriptlets/JSTL, usually behind "
               "servlets. Migrating to a SPA means the JSP becomes a component "
               "and the servlet becomes a REST endpoint."),
        suffixes=(".jsp", ".jspx", ".tag", ".java"),
    ),
    Stack(
        id="struts", label="Apache Struts (Action/ActionForm)", family="backend",
        language="Java", version="1.x/2.x", role="source",
        aliases=("struts-1", "struts-2"),
        forbidden=("org.apache.struts", "ActionForm", "struts-config.xml"),
        suffixes=(".java", ".jsp"),
    ),
    Stack(
        id="ejb", label="EJB 3.x (Session Beans)", family="backend",
        language="Java", version="3.x", role="source",
        aliases=("ejb-3", "jee"),
        forbidden=("@Stateless", "@Stateful", "javax.ejb.", "jakarta.ejb."),
        suffixes=(".java",),
    ),
)

# ── Frontend ─────────────────────────────────────────────────────────────
_FRONTEND: tuple[Stack, ...] = (
    Stack(
        id="react-19", label="React 19 (TypeScript, Node 26 LTS)", family="frontend",
        language="TypeScript / React 19", version="19", role="both",
        aliases=("react", "react-18"),
        idioms=(
            "function components with hooks (never class components)",
            "typed props via TypeScript interfaces",
            "data fetching in an effect or a query hook, never in render",
            "react-router for navigation where the source had page routing",
            "controlled inputs for anything the source posted as a form",
        ),
        forbidden=("<%", "<jsp:", "$(document).ready", "ReactDOM.render("),
        manifest=(
            "package.json with react@^19 and react-dom@^19",
            "typescript + @types/react + @types/react-dom",
            "a bundler (Vite is the default choice) with a build script",
        ),
        api_docs=("Document the REST endpoints the UI consumes in the README; "
                  "OpenAPI lives on the backend, not in the SPA."),
        build_cmd="npm run build",
        suffixes=(".js", ".jsx", ".ts", ".tsx", ".css", ".scss"),
    ),
    Stack(
        id="angular-22", label="Angular 22 (TypeScript, Node 26 LTS)", family="frontend",
        language="TypeScript / Angular 22", version="22", role="both",
        aliases=("angular", "angular2+"),
        idioms=(
            "standalone components (never NgModule scaffolding for new code)",
            "signals for component state; typed reactive forms for input",
            "HttpClient in an injectable service, never in the component",
            "the Angular Router for navigation where the source had page routing",
            "strict template type-checking enabled",
        ),
        forbidden=("<%", "<jsp:", "$(document).ready", "angular.module("),
        manifest=(
            "package.json with @angular/core@^22 and the matching CLI",
            "angular.json with a production build target",
            "typescript matching the Angular 22 compatibility range",
        ),
        api_docs=("Document the REST endpoints the UI consumes in the README; "
                  "OpenAPI lives on the backend, not in the SPA."),
        build_cmd="npm run build",
        suffixes=(".ts", ".html", ".scss", ".css"),
    ),
    Stack(
        id="jquery", label="jQuery (legacy DOM scripting)", family="frontend",
        language="JavaScript", version="1.x/2.x/3.x", role="source",
        forbidden=("$(document).ready", "jQuery(", "$.ajax("),
        suffixes=(".js", ".html"),
    ),
)

# ── Database ─────────────────────────────────────────────────────────────
_DATABASE: tuple[Stack, ...] = (
    Stack(
        id="oracle", label="Oracle (SQL / PL-SQL)", family="database",
        language="Oracle SQL", version="", role="source",
        aliases=("plsql", "oracle-db"),
        forbidden=("VARCHAR2", "NVL(", "SYSDATE", "DECODE(", "FROM DUAL",
                   ".NEXTVAL", "TO_DATE(", "TO_CHAR(", "MINUS ", "PRAGMA "),
        suffixes=(".sql", ".pks", ".pkb", ".prc", ".fnc"),
    ),
    Stack(
        id="postgres-18", label="PostgreSQL 18", family="database",
        language="PostgreSQL SQL / PL-pgSQL", version="18", role="both",
        aliases=("postgres", "postgresql"),
        idioms=(
            "VARCHAR/TEXT instead of VARCHAR2; NUMERIC/BIGINT instead of NUMBER",
            "TIMESTAMP/TIMESTAMPTZ instead of Oracle DATE",
            "CURRENT_TIMESTAMP instead of SYSDATE; COALESCE instead of NVL",
            "CASE WHEN instead of DECODE; drop FROM DUAL entirely",
            "GENERATED ... AS IDENTITY or nextval('seq') instead of .NEXTVAL",
            "CREATE OR REPLACE FUNCTION in PL/pgSQL instead of packages",
        ),
        manifest=("a migration tool (Flyway or Liquibase) if the source had versioned DDL",),
        build_cmd="psql -f schema.sql",
        suffixes=(".sql",),
    ),
    Stack(
        id="postgres-15", label="PostgreSQL 15 — legacy target", family="database",
        language="PostgreSQL SQL / PL-pgSQL", version="15", role="both",
        idioms=(
            "VARCHAR/TEXT instead of VARCHAR2; NUMERIC/BIGINT instead of NUMBER",
            "CURRENT_TIMESTAMP instead of SYSDATE; COALESCE instead of NVL",
            "CASE WHEN instead of DECODE; drop FROM DUAL entirely",
        ),
        notes="Kept selectable so jobs created before iter-21 still resolve.",
        suffixes=(".sql",),
    ),
)

STACKS: tuple[Stack, ...] = _BACKEND + _FRONTEND + _DATABASE
_BY_ID: dict[str, Stack] = {s.id: s for s in STACKS}
_BY_ALIAS: dict[str, Stack] = {
    alias: s for s in STACKS for alias in (s.id, *s.aliases)
}

FAMILY_ORDER: tuple[str, ...] = ("backend", "frontend", "database", "platform")


def get_stack(stack_id: str) -> Stack | None:
    """Resolve by id or alias. Case/format tolerant — the id a job was saved
    with years ago must still resolve after the catalogue is re-labelled."""
    if not stack_id:
        return None
    key = str(stack_id).strip().lower().replace("_", "-").replace(" ", "-")
    return _BY_ID.get(key) or _BY_ALIAS.get(key)


def stack_label(stack_id: str) -> str:
    """Human label for a stack id, falling back to the raw id.

    Never raises: an id from an old job that has since left the catalogue
    still has to render in the UI and read sensibly in a prompt.
    """
    s = get_stack(stack_id)
    return s.label if s else str(stack_id or "unknown")


def sources() -> list[Stack]:
    return [s for s in STACKS if s.role in ("source", "both")]


def targets() -> list[Stack]:
    return [s for s in STACKS if s.role in ("target", "both")]


# Fallback when neither side of the pair is in the catalogue. Broad enough
# that an unknown pair still gets its code looked at, narrow enough to stay
# off binaries and lockfiles. Mirrors `plugins/generic_ai._SOURCE_SUFFIXES`
# minus the config/markup formats that no transformer should rewrite blind.
_FALLBACK_SUFFIXES: frozenset[str] = frozenset({
    ".java", ".kt", ".scala", ".groovy", ".cs", ".vb",
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".py", ".rb", ".go", ".php",
    ".jsp", ".jspx", ".tag", ".ftl", ".vm",
    ".sql", ".pks", ".pkb", ".prc", ".fnc",
})

# Never handed to the AI pass even when a suffix would otherwise match: these
# are owned by `dependency_migrator` / `devops_agent`, and two writers on one
# manifest is how a repaired pom gets un-repaired.
MANIFEST_BASENAMES: frozenset[str] = frozenset({
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "package.json", "package-lock.json", "yarn.lock", "tsconfig.json",
    "angular.json", "requirements.txt", "pyproject.toml", "go.mod",
})


def ai_sweep_suffixes(source_stack: str, target_stack: str) -> frozenset[str]:
    """File extensions the AI pass should read for this pair.

    iter-22 — the engine used to hardcode `(".java", ".sql")`. The catalogue
    had grown to 14 sources × 7 targets by then, so a JSP → React job staged
    its `.jsp` files, handed the model an empty list, converted nothing, and
    still finished `COMPLETED`. Everything that is not Java or SQL was in
    that state.

    The union of both sides is deliberate. The SOURCE's extensions are the
    files that need converting; the TARGET's are the files a previous pass
    (or a deterministic plugin) may already have emitted in the new
    language, which still have to be swept for residue and finished off.
    """
    src, tgt = get_stack(source_stack), get_stack(target_stack)
    out: set[str] = set()
    for st in (src, tgt):
        if st:
            out.update(st.suffixes)
    return frozenset(out) or _FALLBACK_SUFFIXES


def residue_markers(source_stack: str, target_stack: str) -> tuple[str, ...]:
    """Markers that must NOT appear in a file reported as migrated.

    Before iter-22 the scanner used a hardcoded Helidon+Oracle constant, so
    the gate that is supposed to stop a half-migrated file passing was inert
    for every pair the catalogue added: leftover `org.apache.struts` or
    `<jsp:` scanned clean.

    `Stack.forbidden` reads "must not appear in a file of THIS stack", which
    is why the two sides are UNIONED rather than subtracted:

      * the SOURCE's list is what the migration is supposed to remove
        (`io.helidon.`, `org.apache.struts`, `<%`);
      * the TARGET's list is what must not appear in its output anyway
        (`ReactDOM.render(` in React 19, `System.Web.` in .NET).

    A JSP → React job needs both halves: the first catches a scriptlet that
    survived, the second catches a React-18 idiom the model reached for.

    `Stack.permitted` is the escape hatch for a marker that is residue for
    one target and correct for another — `jakarta.ws.rs.` is Helidon residue
    under Spring and the house style under Quarkus. Setting it is one
    catalogue entry, the same bar as everything else here.
    """
    src, tgt = get_stack(source_stack), get_stack(target_stack)
    markers: list[str] = []
    for st in (src, tgt):
        if st:
            markers.extend(st.forbidden)
    allowed = set(tgt.permitted) if tgt else set()
    seen: set[str] = set()
    out: list[str] = []
    for m in markers:
        if m in allowed or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return tuple(out)


def describe_catalogue() -> dict[str, Any]:
    """Payload for GET /api/dcte/stacks — what the two dropdowns render."""
    return {
        "families": list(FAMILY_ORDER),
        "sources": [s.describe() for s in sources()],
        "targets": [s.describe() for s in targets()],
    }
