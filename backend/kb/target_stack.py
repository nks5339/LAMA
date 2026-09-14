"""Target-stack guardrails.

Iter 13.8 — User report: SRS / Architecture were recommending nonsense
hybrids like "PHP FastAPI" (i.e. legacy language + foreign framework
crammed into the target_tech string). This module:

  1. Validates `project.target_tech` against a list of known-bad combos.
  2. Derives a sensible default modern stack when the value is missing,
     blank, or a disallowed hybrid — language-appropriate, justified.
  3. Emits a governance block ("TARGET STACK GUARDRAILS") that every
     prompt-bundling code path prepends so the LLM cannot regress.

The block is paired with a LEGACY COVERAGE CHECKLIST that forces the
analysis to cover architecture overview, codebase structure, dependency
mapping, schema relevance, and integration points before any
modernization recommendation is made.

Pure functions — no I/O, no Mongo, no LLM calls — so it can be imported
from any route without circular-deps.
"""
from __future__ import annotations

from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Disallowed combinations.
#
# Rule: a target_tech string is INVALID if it mentions a legacy
# language/runtime token AND a foreign-language framework token in the
# same string. FastAPI is Python, Spring Boot is Java, Laravel is PHP,
# Express/Nest is Node, ASP.NET / EF Core are .NET, Gin / Fiber are Go,
# Rails is Ruby — these are NOT interchangeable.
# ──────────────────────────────────────────────────────────────────────
_LEGACY_LANG_TOKENS = {
    "php":     {"php", "codeigniter", "laravel", "symfony", "cakephp", "yii"},
    "java":    {"java", "spring", "struts", "jsp", "servlet", "hibernate", "ejb"},
    "python":  {"python", "django", "flask", "fastapi", "pyramid", "tornado"},
    "dotnet":  {"c#", "csharp", ".net", "asp.net", "aspnet", "vb.net", "vbnet",
                "ef core", "efcore", "entity framework"},
    "node":    {"node", "nodejs", "node.js", "express", "nestjs", "koa", "fastify"},
    "ruby":    {"ruby", "rails", "ror", "sinatra"},
    "go":      {"golang", " go ", "gin ", "fiber", "echo"},
    "rust":    {"rust", "actix", "axum", "rocket"},
    "perl":    {"perl ", "mojolicious", "catalyst"},
    "coldfusion": {"coldfusion", "cfml"},
    "vb6":     {"vb6", "vbscript", "asp classic", "classic asp"},
}

# A "foreign framework" token belongs to a single language. If the string
# contains a legacy-lang token from family A AND a framework token from
# family B (A != B), it is a disallowed hybrid.
_FRAMEWORK_OWNER = {
    "fastapi": "python", "django": "python", "flask": "python",
    "spring": "java", "spring boot": "java", "struts": "java",
    "helidon": "java", "helidon se": "java", "helidon mp": "java",  # iter-14.81
    "quarkus": "java", "micronaut": "java",  # iter-14.81 — modern Java alternatives
    "laravel": "php", "symfony": "php", "codeigniter": "php",
    "express": "node", "nestjs": "node", "koa": "node", "fastify": "node",
    "asp.net": "dotnet", "ef core": "dotnet", "entity framework": "dotnet",
    "rails": "ruby", "sinatra": "ruby",
    "gin": "go", "fiber": "go", "echo": "go",
    "actix": "rust", "axum": "rust",
}


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def _families_in(text: str, tokens: dict[str, set[str]]) -> set[str]:
    t = _norm(text)
    fams: set[str] = set()
    for fam, toks in tokens.items():
        if any(tok.strip() in t for tok in toks):
            fams.add(fam)
    return fams


def _foreign_frameworks_in(text: str) -> dict[str, str]:
    """Return {framework_label: owning_family} for every framework token
    that appears in the string."""
    t = _norm(text)
    hits: dict[str, str] = {}
    for fw, owner in _FRAMEWORK_OWNER.items():
        if fw in t:
            hits[fw] = owner
    return hits


# ──────────────────────────────────────────────────────────────────────
# Validation.
# ──────────────────────────────────────────────────────────────────────
def validate_target_stack(
    target_tech: str,
    detected_language: str = "",
) -> dict[str, Any]:
    """Decide whether `target_tech` is a valid modernization choice.

    Returns:
        {
          "valid":        bool,
          "issues":       [str, ...],          # human-readable problems
          "disallowed":   [str, ...],          # specific tokens that caused failure
          "suggested":    str,                 # canonical replacement when invalid
          "rationale":    str,                 # one-line justification
        }
    """
    raw = (target_tech or "").strip()
    issues: list[str] = []
    disallowed: list[str] = []

    if not raw:
        rec = recommend_target_stack(detected_language)
        return {
            "valid": False,
            "issues": ["target_tech is blank — defaulting to a language-appropriate stack."],
            "disallowed": [],
            "suggested": rec["stack"],
            "rationale": rec["rationale"],
        }

    # Detect hybrid: legacy-lang token + foreign-framework token from a
    # different family in the same string.
    families = _families_in(raw, _LEGACY_LANG_TOKENS)
    frameworks = _foreign_frameworks_in(raw)
    for fw, owner in frameworks.items():
        # If the string also names a different family (e.g. "php" + "fastapi"),
        # it is an invalid hybrid.
        other_fams = families - {owner}
        # Tolerate "node" + "express" etc. (same family).
        if other_fams:
            for fam in other_fams:
                # Skip when the only "other" family is just a database token
                # that incidentally collides (none in current dict, but defensive).
                issues.append(
                    f"Disallowed hybrid: '{fw}' is a {owner} framework but "
                    f"target_tech also names {fam} runtime tokens — these are "
                    "not interoperable as a single modern stack."
                )
                disallowed.append(f"{fam}+{fw}")

    if issues:
        rec = recommend_target_stack(detected_language or list(families)[0])
        return {
            "valid": False,
            "issues": issues,
            "disallowed": sorted(set(disallowed)),
            "suggested": rec["stack"],
            "rationale": rec["rationale"],
        }

    return {
        "valid": True,
        "issues": [],
        "disallowed": [],
        "suggested": raw,
        "rationale": "target_tech passes hybrid-combo validation.",
    }


# ──────────────────────────────────────────────────────────────────────
# Recommendation table — single source of truth for "what to migrate to".
# Every entry is a battle-tested, language-appropriate modern stack.
# ──────────────────────────────────────────────────────────────────────
_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "php":     {"stack": "Laravel 11 / PHP 8.3 / PostgreSQL 16 (or rewrite path: FastAPI / Python 3.12 / PostgreSQL 16)",
                "rationale": "Stay-in-language path keeps team velocity; cross-language path only if business explicitly chooses Python."},
    "java":    {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19",
                "rationale": "Spring Boot 3 is the modern Java baseline; preserves JVM team skills + ecosystem."},
    # iter-14.81 — Helidon-specific migration paths
    "helidon": {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19",
                "rationale": "Helidon MP uses JAX-RS/CDI; Spring Boot 3 offers equivalent annotations with larger ecosystem. Consider Quarkus for cloud-native GraalVM targets."},
    "helidon se": {"stack": "Spring Boot 3.3 / Java 21 / WebFlux (reactive) / PostgreSQL 16",
                   "rationale": "Helidon SE is reactive; Spring WebFlux provides equivalent reactive APIs with wider adoption."},
    "helidon mp": {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19",
                   "rationale": "Helidon MP's MicroProfile annotations map directly to Spring equivalents (@Path→@RequestMapping, @Inject→@Autowired)."},
    "quarkus": {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19 (or stay with Quarkus 3.x)",
                "rationale": "Quarkus is already modern; migrate only if Spring ecosystem is required. Native compilation works on both."},
    "micronaut": {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19 (or stay with Micronaut 4.x)",
                  "rationale": "Micronaut is already modern; migrate only if Spring ecosystem is required."},
    "python":  {"stack": "FastAPI / Python 3.12 / PostgreSQL 16 + React 19",
                "rationale": "FastAPI is the canonical modern Python web stack with first-class async + OpenAPI."},
    "dotnet":  {"stack": "ASP.NET Core 8 / C# 12 / EF Core 8 / PostgreSQL 16 + Blazor or React 19",
                "rationale": ".NET 8 LTS is the supported modern path; preserves C# team skills."},
    "node":    {"stack": "NestJS / Node 22 LTS / PostgreSQL 16 + React 19",
                "rationale": "NestJS adds DI, modules, and OpenAPI generation that legacy Express lacks."},
    "ruby":    {"stack": "Rails 7.2 / Ruby 3.3 / PostgreSQL 16 + Hotwire (or React 19)",
                "rationale": "Rails 7.2 is the supported modern path; Hotwire reduces SPA complexity."},
    "go":      {"stack": "Go 1.23 + chi or Gin / PostgreSQL 16 + React 19",
                "rationale": "Go for high-throughput rewrites; chi/Gin remain idiomatic."},
    "rust":    {"stack": "Rust + Axum / PostgreSQL 16 + React 19",
                "rationale": "Axum is the modern tokio-based baseline."},
    "perl":    {"stack": "FastAPI / Python 3.12 / PostgreSQL 16 + React 19",
                "rationale": "Perl talent is scarce; Python is the closest scripting-idiom rewrite target."},
    "coldfusion": {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19",
                   "rationale": "Coldfusion runs on the JVM; Spring Boot preserves the runtime ecosystem."},
    "vb6":     {"stack": "ASP.NET Core 8 / C# 12 / EF Core 8 / PostgreSQL 16 + Blazor",
                "rationale": "Closest .NET-family rewrite path; preserves Windows-centric ops."},
    "jsp":     {"stack": "Spring Boot 3.3 / Java 21 / PostgreSQL 16 + React 19",
                "rationale": "JSP is Java/Servlet-era; Spring Boot is the supported modern Java baseline."},
}

# Aliases — many detector outputs land here.
_LANG_ALIAS = {
    "javascript": "node", "js": "node", "typescript": "node", "ts": "node",
    "csharp": "dotnet", "c#": "dotnet", ".net": "dotnet", "vb.net": "dotnet",
    "vbnet": "dotnet", "vb": "dotnet",
    "golang": "go",
    "jsp servlet": "jsp", "servlet": "jsp",
    "asp classic": "vb6", "classic asp": "vb6",
    "cfml": "coldfusion",
    # iter-14.81 — Helidon aliases
    "helidon microprofile": "helidon mp",
    "helidon nima": "helidon se",
    "oracle helidon": "helidon",
}


def recommend_target_stack(detected_language: str) -> dict[str, str]:
    """Return {'stack': ..., 'rationale': ...} for the detected legacy lang."""
    key = _norm(detected_language)
    key = _LANG_ALIAS.get(key, key)
    if key in _RECOMMENDATIONS:
        return _RECOMMENDATIONS[key]
    # Unknown legacy → bias to FastAPI/Python (most common rewrite default).
    return {
        "stack": "FastAPI / Python 3.12 / PostgreSQL 16 + React 19",
        "rationale": ("Legacy language could not be identified with confidence; "
                      "defaulting to the most common modern rewrite target. Confirm with the user."),
    }


# ──────────────────────────────────────────────────────────────────────
# Governance block builder — consumed by srs._load_governance_bundle().
# ──────────────────────────────────────────────────────────────────────
def build_target_stack_block(
    project: dict[str, Any],
) -> str:
    """Produce the TARGET STACK GUARDRAILS + LEGACY COVERAGE CHECKLIST
    block. PREPENDED into the governance bundle so it dominates the
    model's attention.
    """
    detected = (project or {}).get("detected_tech") or {}
    detected_lang = detected.get("language", "") or ""
    raw_target = (project or {}).get("target_tech", "") or ""
    verdict = validate_target_stack(raw_target, detected_lang)

    if verdict["valid"]:
        target_line = raw_target
        validation_note = (
            f"target_tech '{raw_target}' passes hybrid-combo validation."
        )
    else:
        target_line = verdict["suggested"]
        validation_note = (
            f"target_tech '{raw_target}' REJECTED — {'; '.join(verdict['issues'])}\n"
            f"Substituted with VALIDATED suggestion: {verdict['suggested']}\n"
            f"Rationale: {verdict['rationale']}"
        )

    block = (
        "═══════════════════════════════════════════════════════════════════\n"
        "TARGET STACK GUARDRAILS — HARD CONTRACT.\n"
        "Modern stacks must be language-appropriate. The legacy language and\n"
        "the chosen framework must come from the SAME family.\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"DETECTED LEGACY LANGUAGE: {detected_lang or '(unknown)'}\n"
        f"TARGET STACK (validated):  {target_line}\n"
        f"VALIDATION:                {validation_note}\n"
        "\n"
        "DISALLOWED COMBINATIONS (NEVER recommend these in any section):\n"
        "  ❌ 'PHP FastAPI'           — FastAPI is Python, not PHP.\n"
        "  ❌ 'Java FastAPI'          — Use Spring Boot for Java.\n"
        "  ❌ 'PHP Spring Boot'       — Spring is Java, not PHP.\n"
        "  ❌ '.NET Django'           — Django is Python, not .NET.\n"
        "  ❌ 'Node Spring'           — Spring is Java, use NestJS for Node.\n"
        "  ❌ Any '<legacy-lang> + <foreign-framework>' hybrid.\n"
        "\n"
        "ALLOWED MODERN MAPPINGS (use these unless the user explicitly\n"
        "asks to cross-port):\n"
        "  • PHP        → Laravel 11 / PHP 8.3   (or Python/FastAPI on explicit request)\n"
        "  • Java/JSP   → Spring Boot 3.3 / Java 21\n"
        "  • Python     → FastAPI / Python 3.12\n"
        "  • .NET/C#    → ASP.NET Core 8 / EF Core 8\n"
        "  • Node       → NestJS / Node 22 LTS\n"
        "  • Ruby       → Rails 7.2\n"
        "  • Go         → Go 1.23 + chi/Gin\n"
        "  • Coldfusion → Spring Boot 3.3 (JVM ecosystem)\n"
        "  • Perl/VB6   → Python/FastAPI or .NET 8 respectively\n"
        "\n"
        "EVERY recommendation in this section MUST justify the target stack\n"
        "against the DETECTED LEGACY LANGUAGE above. Generic phrasing such as\n"
        "'modernize to a cloud-native stack' WITHOUT naming the validated\n"
        "framework + runtime is a VIOLATION and the section will be regenerated.\n"
        "═══════════════════════════════════════════════════════════════════\n"
        "\n"
        "═══════════════════════════════════════════════════════════════════\n"
        "LEGACY COVERAGE CHECKLIST — MANDATORY before any modernization\n"
        "recommendation. Each item must be addressed explicitly in prose,\n"
        "grounded in the KB evidence (TOON + RAG chunks).\n"
        "═══════════════════════════════════════════════════════════════════\n"
        "  [1] EXISTING ARCHITECTURE OVERVIEW — name the legacy framework,\n"
        "      runtime, presentation layer, persistence layer, and how they\n"
        "      interact (controllers/views/models, request lifecycle).\n"
        "  [2] CODEBASE STRUCTURE ANALYSIS  — list the actual top-level\n"
        "      modules/packages and their responsibilities (use real names\n"
        "      from the KB CLASSES / MODULES blocks).\n"
        "  [3] DEPENDENCY MAPPING            — internal module-to-module\n"
        "      coupling AND external libraries / services / message queues.\n"
        "  [4] DATABASE SCHEMA RELEVANCE     — which tables back which\n"
        "      modules; FK relationships; any cross-module reads/writes.\n"
        "  [5] INTEGRATION POINTS            — every inbound/outbound\n"
        "      interface (HTTP routes, batch jobs, file drops, scheduled\n"
        "      tasks, third-party APIs, SMTP/SMS, file system writes).\n"
        "\n"
        "Recommendations that skip ANY of [1]–[5] are SUPERFICIAL and will\n"
        "be regenerated. Depth requirement = DETAILED, evidence-bound.\n"
        "═══════════════════════════════════════════════════════════════════"
    )
    return block


# ──────────────────────────────────────────────────────────────────────
# Helper for the architecture.recommend route — pick a backend_lang
# from detected stack that matches the allowed set {nodejs|python|java|go}.
# ──────────────────────────────────────────────────────────────────────
def derive_backend_lang(detected_language: str, target_tech: str = "") -> str:
    """Map (detected legacy language, user-selected target_tech) → backend_lang.

    Iter-13.20 — TARGET STACK SELECTION ALWAYS WINS.
    If the user has chosen a target stack (``project.target_tech``), that
    choice unconditionally drives ``backend_lang``. The detected-legacy
    fallback is consulted ONLY when target_tech is blank/unparseable, and
    it now preserves the legacy family (no more silent ``php → python`` or
    ``dotnet → java`` rewrites that hijacked the user's intent).

    Allowed return values (must match the `arch.recommend` prompt's
    accepted set):
        nodejs | python | java | go | dotnet | php | ruby | rust
    """
    t = _norm(target_tech)

    # ---- 1) User-selected target tech is authoritative ------------------
    # Recognize every family by either a runtime token or a framework token.
    # Order matters: check longer / more specific tokens first to avoid
    # ".net" matching "node.js" or "java" matching "javascript".
    if "javascript" in t or "typescript" in t or "nestjs" in t or "express" in t \
            or "node.js" in t or "node " in t or "nodejs" in t or t.endswith("node"):
        return "nodejs"
    if "fastapi" in t or "django" in t or "flask" in t or "python" in t:
        return "python"
    if "spring" in t or "java 21" in t or "java 17" in t or "java 8" in t \
            or "java 11" in t or " java " in f" {t} " or "jsp" in t \
            or "struts" in t or "hibernate" in t:
        return "java"
    if "gin" in t or "fiber" in t or "echo " in t or " go " in f" {t} " \
            or "golang" in t:
        return "go"
    if ".net" in t or "dotnet" in t or "asp.net" in t or "aspnet" in t \
            or "c#" in t or "csharp" in t or "ef core" in t \
            or "entity framework" in t or "blazor" in t or "vb.net" in t:
        return "dotnet"
    if "laravel" in t or "symfony" in t or "codeigniter" in t \
            or "cakephp" in t or "yii" in t or "php" in t:
        return "php"
    if "rails" in t or "sinatra" in t or "ruby" in t:
        return "ruby"
    if "actix" in t or "axum" in t or "rocket" in t or "rust" in t:
        return "rust"

    # ---- 2) Fallback — preserve the legacy family ----------------------
    # NO MORE silent cross-family rewrites. If the legacy is PHP and the
    # user hasn't picked anything yet, default to PHP/Laravel — not Python.
    key = _norm(detected_language)
    key = _LANG_ALIAS.get(key, key)
    return {
        "python":     "python",
        "java":       "java",
        "jsp":        "java",
        "node":       "nodejs",
        "go":         "go",
        "php":        "php",
        "dotnet":     "dotnet",
        "ruby":       "ruby",
        "rust":       "rust",
        "perl":       "python",       # no idiomatic modern Perl rewrite path
        "coldfusion": "java",         # JVM-family
        "vb6":        "dotnet",       # .NET-family
    }.get(key, "python")              # safer default than nodejs for unknown legacy

