"""Target-stack suggester.

Given the KB knowledge graph (entity stats + detected legacy tech), produce a
ranked list of candidate MODERN target stacks the user can pick from before
SRS / Architecture / CodeGen generation.

Pure functions — no I/O, no Mongo, no LLM — safe to import anywhere.

Output shape (one entry per suggestion):
    {
        "id":          "stay-in-language",      # stable id
        "label":       "Laravel 11 / PHP 8.3 / PostgreSQL 16",  # canonical
        "backend":     "Laravel 11 / PHP 8.3",
        "pattern":     "Modular Monolith",      # Microservices | Modular Monolith | Serverless …
        "database":    "PostgreSQL 16",
        "frontend":    "React 19",              # optional, "" if N/A
        "rationale":   "...",
        "score":       0.92,
        "kind":        "stay-in-language" | "cross-language" | "microservices" | "serverless",
        "validated":   True,                    # always True — only emits language-pure combos
    }

Consumed by:
    - GET  /api/kb/{project_id}/target-stack/suggestions
    - POST /api/kb/{project_id}/target-stack/select   (the chosen `label` is
      written verbatim to project.target_tech, where the existing
      target_stack guardrails / SRS / Arch / CodeGen pipelines pick it up.)
"""
from __future__ import annotations

from typing import Any


# ──────────────────────────────────────────────────────────────────────
# Per-language candidate stacks.
#
# Each language family contributes up to 4 candidates so we always have
# enough material to surface the top-3 the user asked for. Order inside
# each list is the *base* preference order (stay-in-language first, then
# the most common cross-port targets). The final ranking is then nudged
# by KB-graph signals (size → microservices vs monolith, presence of a
# legacy DB engine → keep it, …) in `suggest_target_stacks` below.
# ──────────────────────────────────────────────────────────────────────
_CANDIDATES: dict[str, list[dict[str, str]]] = {
    "php": [
        {"id": "php-laravel",   "kind": "stay-in-language",
         "backend": "Laravel 11 / PHP 8.3", "pattern": "Modular Monolith",
         "frontend": "Blade + Livewire (or React 19)"},
        {"id": "php-fastapi",   "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "php-springboot","kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "php-nestjs",    "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
    ],
    "java": [
        {"id": "java-springboot","kind": "stay-in-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "java-quarkus",   "kind": "stay-in-language",
         "backend": "Quarkus 3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "java-fastapi",   "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "java-nestjs",    "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
    ],
    "jsp": [
        {"id": "jsp-springboot","kind": "stay-in-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "jsp-quarkus",   "kind": "stay-in-language",
         "backend": "Quarkus 3 / Java 21", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "jsp-fastapi",   "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "jsp-nestjs",    "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "jsp-aspnet",    "kind": "cross-language",
         "backend": "ASP.NET Core 8 / C# 12 / EF Core 8", "pattern": "Microservices",
         "frontend": "Blazor (or React 19)"},
    ],
    "python": [
        {"id": "py-fastapi",    "kind": "stay-in-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "py-django",     "kind": "stay-in-language",
         "backend": "Django 5 / Python 3.12", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "py-flask",      "kind": "stay-in-language",
         "backend": "Flask 3 / Python 3.12", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "py-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "py-nestjs",     "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "py-aspnet",     "kind": "cross-language",
         "backend": "ASP.NET Core 8 / C# 12 / EF Core 8", "pattern": "Microservices",
         "frontend": "Blazor (or React 19)"},
    ],
    "dotnet": [
        {"id": "dn-aspnet",     "kind": "stay-in-language",
         "backend": "ASP.NET Core 8 / C# 12 / EF Core 8", "pattern": "Microservices",
         "frontend": "Blazor (or React 19)"},
        {"id": "dn-aspnet-mono","kind": "stay-in-language",
         "backend": "ASP.NET Core 8 / C# 12 / EF Core 8", "pattern": "Modular Monolith",
         "frontend": "Blazor"},
        {"id": "dn-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "dn-fastapi",    "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
    ],
    "node": [
        {"id": "node-nestjs",   "kind": "stay-in-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "node-fastify",  "kind": "stay-in-language",
         "backend": "Fastify / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "node-fastapi",  "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "node-springboot","kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
    ],
    "ruby": [
        {"id": "rb-rails",      "kind": "stay-in-language",
         "backend": "Rails 7.2 / Ruby 3.3", "pattern": "Modular Monolith",
         "frontend": "Hotwire (or React 19)"},
        {"id": "rb-fastapi",    "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "rb-nestjs",     "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "rb-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
    ],
    "go": [
        {"id": "go-chi",        "kind": "stay-in-language",
         "backend": "Go 1.23 + chi", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "go-gin",        "kind": "stay-in-language",
         "backend": "Go 1.23 + Gin", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "go-fastapi",    "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "go-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "go-nestjs",     "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
    ],
    "perl": [
        {"id": "pl-fastapi",    "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "pl-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "pl-nestjs",     "kind": "cross-language",
         "backend": "NestJS / Node 22 LTS", "pattern": "Modular Monolith",
         "frontend": "React 19"},
    ],
    "coldfusion": [
        {"id": "cf-springboot", "kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Microservices",
         "frontend": "React 19"},
        {"id": "cf-quarkus",    "kind": "cross-language",
         "backend": "Quarkus 3 / Java 21", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "cf-fastapi",    "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Microservices",
         "frontend": "React 19"},
    ],
    "vb6": [
        {"id": "vb6-aspnet",    "kind": "cross-language",
         "backend": "ASP.NET Core 8 / C# 12 / EF Core 8", "pattern": "Modular Monolith",
         "frontend": "Blazor"},
        {"id": "vb6-springboot","kind": "cross-language",
         "backend": "Spring Boot 3.3 / Java 21", "pattern": "Modular Monolith",
         "frontend": "React 19"},
        {"id": "vb6-fastapi",   "kind": "cross-language",
         "backend": "FastAPI / Python 3.12", "pattern": "Modular Monolith",
         "frontend": "React 19"},
    ],
}

# Lang aliases that the detector may emit.
_LANG_ALIAS = {
    "javascript": "node", "js": "node", "typescript": "node", "ts": "node",
    "csharp": "dotnet", "c#": "dotnet", ".net": "dotnet", "vb.net": "dotnet",
    "vbnet": "dotnet", "vb": "dotnet",
    "golang": "go",
    "servlet": "jsp",
    "asp classic": "vb6", "classic asp": "vb6",
    "cfml": "coldfusion",
}

# Default fallback when we cannot identify legacy language.
_FALLBACK_LANG = "python"


def _norm_lang(s: str) -> str:
    k = (s or "").lower().strip()
    return _LANG_ALIAS.get(k, k)


# ──────────────────────────────────────────────────────────────────────
# Database target picker.
#
# We emit TWO database variants per candidate so the user gets a real
# choice in the UI — matching the user's request ("…/<legacy db>"):
#   - RETAIN  the detected legacy DB engine (no migration cost)
#   - MIGRATE to PostgreSQL 16 (the canonical modern OSS target)
#
# Document stores (Mongo / Dynamo) are emitted only as "retain" — moving
# them to a relational DB is a separate redesign exercise.
# ──────────────────────────────────────────────────────────────────────
_DB_LABEL_MAP = {
    "mariadb":              "MariaDB 11 (retain)",
    "mysql":                "MySQL 8 (retain)",
    "mysql/mariadb":        "MySQL 8 / MariaDB 11 (retain)",
    "oracle":               "Oracle 23ai (retain)",
    "oracle (pl/sql)":      "Oracle 23ai (retain)",
    "microsoft sql server": "Azure SQL / SQL Server 2022 (retain)",
    "ibm db2":              "Db2 12 (retain)",
    "sybase":               "SAP ASE 16 (retain)",
    "postgresql":           "PostgreSQL 16 (retain)",
    "mongodb":              "MongoDB 7 (retain)",
    "redis":                "Redis 7 (retain)",
    "sqlite":               "PostgreSQL 16",
}


def _db_variants(detected_db: str) -> list[str]:
    """Return ordered DB label variants — first one is the preferred default."""
    db = (detected_db or "").strip().lower()
    retained = _DB_LABEL_MAP.get(db)

    if not retained:
        # Unknown legacy DB → only the modern target.
        return ["PostgreSQL 16"]

    if "mongo" in db or "redis" in db:
        # NoSQL — don't suggest a relational migration target.
        return [retained]

    if "postgres" in db:
        # Already PG → keep it; no migration variant needed.
        return [retained]

    # Default: stay-engine first (user asked for "<legacy db>" in examples),
    # PG-migration second.
    return [retained, "PostgreSQL 16"]


# ──────────────────────────────────────────────────────────────────────
# Pattern preference from KB size.
# ──────────────────────────────────────────────────────────────────────
def _prefers_microservices(kb_stats: dict | None) -> bool:
    s = kb_stats or {}
    tables = int(s.get("tables") or 0)
    classes = int(s.get("classes") or 0)
    routes = int(s.get("routes") or 0)
    # Heuristic: any one of these crossing the threshold = big enough that
    # decomposition into services is justified. Below the threshold a modular
    # monolith is the more honest recommendation.
    return tables >= 20 or classes >= 100 or routes >= 60


# ──────────────────────────────────────────────────────────────────────
# Rationale text — keeps the prompt-facing rationale evidence-bound.
# ──────────────────────────────────────────────────────────────────────
def _rationale(
    cand: dict[str, str],
    detected_lang: str,
    db_label: str,
    pattern: str,
    kb_stats: dict | None,
) -> str:
    t = int((kb_stats or {}).get("tables") or 0)
    c = int((kb_stats or {}).get("classes") or 0)
    size = (
        f"{c} classes / {t} tables"
        if (c or t) else "small KB"
    )
    if cand["kind"] == "stay-in-language":
        return (
            f"Stay-in-language path — preserves {detected_lang.title() or 'team'} "
            f"skills + ecosystem. KB size = {size} → {pattern.lower()} fits. "
            f"DB → {db_label}."
        )
    if cand["kind"] == "cross-language":
        return (
            f"Cross-language rewrite to {cand['backend'].split('/')[0].strip()} — "
            f"chosen for KB size = {size}, {pattern.lower()} target, and modern "
            f"cloud-native maturity. DB → {db_label}."
        )
    return f"{cand['backend']} / {pattern} / {db_label}."


# Backend → language family mapping for diversity dedup. The user's
# requirement (iter-13.36): the top-3 must be THREE DIFFERENT LANGUAGES
# — i.e. Python-FastAPI + Python-Django + Java-SpringBoot is NOT
# acceptable (two Python entries). One Python OR one Java OR one Node
# may appear, but never two from the same language family.
_BACKEND_LANG_KEYWORDS = (
    ("python",  ("fastapi", "django", "flask", "python")),
    ("java",    ("spring boot", "spring", "quarkus", "micronaut", "java")),
    ("node",    ("nestjs", "fastify", "express", "node")),
    ("dotnet",  ("asp.net", "aspnet", ".net", "c#", "blazor")),
    ("php",     ("laravel", "symfony", "php")),
    ("go",      ("go ", "gin", "chi", "echo", "fiber")),
    ("ruby",    ("rails", "ruby", "sinatra")),
    ("rust",    ("actix", "rocket", "axum", "rust")),
)


def _backend_language_family(backend_label: str) -> str:
    """Map a backend label like 'FastAPI / Python 3.12' to its language family.

    Returns the family key (`python`, `java`, `node`, `dotnet`, `php`, …)
    or the lowercased backend itself if no family matched.
    """
    s = (backend_label or "").lower()
    for family, kws in _BACKEND_LANG_KEYWORDS:
        if any(kw in s for kw in kws):
            return family
    return s.strip() or "unknown"


# ──────────────────────────────────────────────────────────────────────
# Public API.
# ──────────────────────────────────────────────────────────────────────
def suggest_target_stacks(
    detected_tech: dict | None,
    kb_stats: dict | None = None,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Return up to `top_n` candidate target stacks, highest score first.

    Output ordering rule of thumb:
      1. stay-in-language with retained legacy DB        (lowest migration risk)
      2. stay-in-language with PostgreSQL                (modernization target)
      3. cross-language rewrite with retained legacy DB  (incremental cutover)
      4. cross-language rewrite with PostgreSQL          (greenfield rewrite)

    Pattern (Microservices vs Modular Monolith) is nudged by KB size.
    """
    detected_tech = detected_tech or {}
    detected_lang = _norm_lang(detected_tech.get("language", "") or "")
    detected_db = detected_tech.get("database", "") or ""

    candidates = _CANDIDATES.get(detected_lang) or _CANDIDATES.get(_FALLBACK_LANG, [])
    db_variants = _db_variants(detected_db)
    micro_preferred = _prefers_microservices(kb_stats)

    enriched: list[dict[str, Any]] = []
    for i, cand in enumerate(candidates):
        base_pattern = cand["pattern"]
        # If KB is big, also offer a Microservices variant of any
        # Modular-Monolith candidate so the user gets both shapes.
        patterns = [base_pattern]
        if base_pattern == "Modular Monolith" and micro_preferred:
            patterns.append("Microservices")

        for p_idx, variant_pattern in enumerate(patterns):
            for d_idx, db_label in enumerate(db_variants):
                backend = cand["backend"]
                label = " / ".join(
                    [backend, variant_pattern, db_label]
                    + ([cand["frontend"]] if cand.get("frontend") else [])
                )

                base_score = {
                    "stay-in-language": 0.95,
                    "cross-language":   0.80,
                }.get(cand["kind"], 0.5)

                # Order penalties — earlier candidate / first-pattern /
                # first-DB-variant always score higher.
                order_pen = i * 0.05 + p_idx * 0.03 + d_idx * 0.02
                # Boost microservices when KB is big.
                if micro_preferred and variant_pattern == "Microservices":
                    base_score += 0.02
                # Tiny boost for the "retain legacy DB" variant — it's the
                # safer/cheaper option, and the user explicitly asked for
                # "<legacy db>" in the suggestion examples.
                if d_idx == 0 and len(db_variants) > 1:
                    base_score += 0.01

                enriched.append({
                    "id":        f"{cand['id']}::{variant_pattern.replace(' ', '-').lower()}::{d_idx}",
                    "label":     label,
                    "backend":   backend,
                    "pattern":   variant_pattern,
                    "database":  db_label,
                    "frontend":  cand.get("frontend", ""),
                    "rationale": _rationale(cand, detected_lang, db_label, variant_pattern, kb_stats),
                    "score":     round(max(0.0, base_score - order_pen), 3),
                    "kind":      cand["kind"],
                    "validated": True,
                })

    # Diversity-aware top-N selection:
    #   1. De-dupe by label.
    #   2. Enforce ONE entry per LANGUAGE FAMILY — the user's request
    #      (iter-13.36): "same python or java should not come multiple
    #      time". So the top-3 must each be a DIFFERENT language: e.g.
    #      Python (FastAPI) + Java (Spring Boot) + Node (NestJS), not
    #      Python (FastAPI) + Python (Django) + Java (Spring Boot).
    #      Previously this dedup was on the backend string which let two
    #      Python frameworks slip through.
    #   3. If after language-dedup we still need slots, relax to a
    #      backend-level dedup (different framework within same family);
    #      then finally relax fully so we always return target_n entries.
    target_n = max(top_n, 3)
    sorted_e = sorted(enriched, key=lambda x: -x["score"])

    seen_labels: set[str] = set()
    seen_languages: set[str] = set()
    seen_backends: set[str] = set()
    unique: list[dict[str, Any]] = []

    # Pass 1 — strict LANGUAGE-FAMILY diversity (one per Python/Java/Node/…).
    for e in sorted_e:
        if e["label"] in seen_labels:
            continue
        lang_family = _backend_language_family(e["backend"])
        if lang_family in seen_languages:
            continue
        seen_labels.add(e["label"])
        seen_languages.add(lang_family)
        seen_backends.add(e["backend"])
        # Stamp the family on the entry so the UI/audit log can show it.
        e["language_family"] = lang_family
        unique.append(e)
        if len(unique) >= target_n:
            break

    # Pass 2 — backend-level diversity (different framework, possibly same lang).
    if len(unique) < target_n:
        for e in sorted_e:
            if e["label"] in seen_labels:
                continue
            if e["backend"] in seen_backends:
                continue
            seen_labels.add(e["label"])
            seen_backends.add(e["backend"])
            e["language_family"] = _backend_language_family(e["backend"])
            unique.append(e)
            if len(unique) >= target_n:
                break

    # Pass 3 — final fill (no diversity constraint) so we always
    # return target_n entries even when the candidate pool is small.
    if len(unique) < target_n:
        for e in sorted_e:
            if e["label"] in seen_labels:
                continue
            seen_labels.add(e["label"])
            e["language_family"] = _backend_language_family(e["backend"])
            unique.append(e)
            if len(unique) >= target_n:
                break

    return unique[:top_n]

