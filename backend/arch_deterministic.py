"""Deterministic generators for Stage-3 Architecture artifacts.

iter-14.20 — HLD, LLD and API-Contract generation is now pure Python
driven off the frozen upstream inputs. NO LLM calls.

Inputs consumed
---------------
- ``proj``            : the project doc (source_tech / target_tech / detected_tech / name)
- ``sm_data``         : parsed service_map JSON (recommended_pattern, services, frontend_service,
                        event_bus, _surface)
- ``services``        : list of arch_services docs (routes_detail, tables, module_names, roles,
                        api_endpoints, dependencies, api_count, description, responsibility, name,
                        display_name, backend_lang, kind)
- ``srs_sections``    : dict of SRS section-keys → markdown text (specific_requirements,
                        non_functional_requirements, detailed_use_cases, actors_use_case_inventory,
                        overall_description, …)
- ``oltp_ddl``        : raw CREATE TABLE SQL (target OLTP)

Outputs
-------
- ``render_hld(...)``           → single markdown blob with ``## <label>`` per section
                                  (17 sections matching ``HLD_SECTIONS`` in ``routes/architecture.py``)
- ``render_lld(...)``           → single markdown blob with ``# Service: <display_name> (`<name>`)``
                                  per business service (regex-consumed by CodeGen)
- ``render_api_contracts(...)`` → ``(body: str, skipped: list[str])`` — one OpenAPI 3.1 YAML block
                                  per service in ``body``. Empty services are reported in ``skipped``.

Contract with downstream
------------------------
* LLD headers ``# Service: <display_name> (`<name>`)`` are asserted by
  ``routes/codegen.py::_backend_files_plan`` (regex slice) — do not change.
* ``arch_documents.api_contracts.content`` must contain the literal token
  ``openapi:`` somewhere (matches the safety check in the old LLM job).
* ``arch_documents.hld.content`` must contain a ``## <label>`` header for
  every section in ``HLD_SECTIONS`` (frontend renders these as top-level).
"""
from __future__ import annotations

import re
import textwrap
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Section catalogue — kept in sync with routes/architecture.py::HLD_SECTIONS.
# We only need the (key, label) tuples for header emission.
# ---------------------------------------------------------------------------
HLD_SECTION_ORDER: List[Tuple[str, str]] = [
    ("executive_summary",     "Executive Summary"),
    ("system_context",        "System Context Diagram"),
    ("service_decomposition", "Service Decomposition & Bounded Contexts"),
    ("api_gateway",           "API Gateway & Edge Routing"),
    ("data_flow",             "Critical Data Flow Diagrams"),
    ("database_arch",         "Database Architecture"),
    ("caching_strategy",      "Caching Strategy"),
    ("async_messaging",       "Asynchronous Messaging & Event Architecture"),
    ("auth",                  "Authentication & Authorization"),
    ("security_architecture", "Security Architecture (defence in depth)"),
    ("observability",         "Observability Stack (Logs / Metrics / Traces)"),
    ("nfr",                   "Non-Functional Requirements Realisation"),
    ("deployment",            "Deployment Architecture"),
    ("ci_cd",                 "CI/CD Pipeline"),
    ("dr_bcp",                "Disaster Recovery & Business Continuity"),
    ("migration_strategy",    "Migration Strategy (legacy → target)"),
    ("tech_decisions",        "Technology Decisions & Trade-offs"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _target_summary(proj: Dict[str, Any]) -> str:
    return (proj.get("target_tech") or "target stack").strip()


def _source_summary(proj: Dict[str, Any]) -> str:
    return (proj.get("source_tech") or "legacy stack").strip()


def _detected_summary(proj: Dict[str, Any]) -> str:
    dt = proj.get("detected_tech") or {}
    if dt.get("summary"):
        return dt["summary"]
    parts = [dt.get("language", ""), ", ".join(dt.get("frameworks") or []), dt.get("database", "")]
    return " / ".join(p for p in parts if p) or "(no detection)"


def _target_flavour(proj: Dict[str, Any]) -> Dict[str, str]:
    """Very small pattern-match on the target-tech string → canonical picks
    for gateway / message-bus / cache / IdP / DB / observability products.
    Kept intentionally short: we prefer sensible defaults over a full DSL.
    """
    t = (proj.get("target_tech") or "").lower()
    is_java   = ("java" in t) or ("spring" in t) or ("kotlin" in t)
    is_dotnet = (".net" in t) or ("dotnet" in t) or ("c#" in t)
    is_python = ("python" in t) or ("fastapi" in t) or ("django" in t) or ("flask" in t)
    is_node   = ("node" in t) or ("nest" in t) or ("express" in t)

    if "kafka" in t:
        bus = "Apache Kafka (with Schema Registry)"
    elif "rabbit" in t:
        bus = "RabbitMQ (quorum queues)"
    else:
        bus = "Apache Kafka (with Schema Registry)"

    if "postgres" in t:
        db = "PostgreSQL 16"
    elif "mysql" in t or "maria" in t:
        db = "PostgreSQL 16 (migrated from MySQL/MariaDB)"
    elif "oracle" in t:
        db = "PostgreSQL 16 (migrated from Oracle)"
    else:
        db = "PostgreSQL 16"

    return {
        "language":    "Java 21 / Spring Boot 3" if is_java
                       else ".NET 8 / ASP.NET Core" if is_dotnet
                       else "Python 3.12 / FastAPI" if is_python
                       else "Node.js 20 / NestJS" if is_node
                       else "Python 3.12 / FastAPI",
        "gateway":     "Kong Gateway (OSS) fronting a Kubernetes ingress",
        "bus":         bus,
        "cache":       "Redis 7 (cluster mode) + CDN at the edge",
        "idp":         "Keycloak 24 (OIDC / OAuth2)",
        "db":          db,
        "logs":        "Loki + Promtail",
        "metrics":     "Prometheus + Grafana",
        "traces":      "OpenTelemetry Collector → Tempo",
        "vault":       "HashiCorp Vault",
        "waf":         "AWS WAF (or Cloudflare WAF for on-prem edges)",
        "container":   "Docker + Kubernetes 1.30",
        "ci":          "GitHub Actions",
    }


def _biz_services(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only business services (drop utilities + frontend)."""
    return [
        s for s in services
        if s.get("kind") != "utility" and not s.get("frontend")
    ]


def _routes_of(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise routes_detail; fall back to api_endpoints strings.

    NOTE: Only returns EXPLICIT routes (from recommend). For synthesis when
    those are empty, use ``_effective_routes_of()`` which adds CRUD scaffolding
    from owned tables.
    """
    detail = svc.get("routes_detail") or []
    if detail:
        return detail
    out: List[Dict[str, Any]] = []
    for ep in svc.get("api_endpoints") or []:
        # api_endpoints entries look like "GET /users/{id}" or "/users/list"
        parts = str(ep).strip().split(None, 1)
        if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            out.append({"verb": parts[0].upper(), "path": parts[1]})
        else:
            out.append({"verb": "GET", "path": str(ep)})
    return out


def _synthesize_crud_routes(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """iter-14.20.1 — Fallback route synthesis for services that have
    owned tables but no ``routes_detail`` (recommend either didn't attach
    routes or the legacy KB didn't surface any).

    For each owned table we emit the canonical 5-verb CRUD surface:
    GET /{plural}, GET /{plural}/{id}, POST /{plural}, PUT /{plural}/{id},
    DELETE /{plural}/{id}. This gives the deterministic OpenAPI + LLD
    something concrete to render even when arch.recommend produced an
    empty ``routes_detail`` block.
    """
    tables = svc.get("tables") or []
    if not tables:
        return []
    routes: List[Dict[str, Any]] = []
    roles = svc.get("roles") or []
    for t in tables:
        plural = _pluralise(t.lower())
        cls = _pascal(t) + "Controller"
        routes += [
            {"verb": "GET",    "path": f"/{plural}",       "class": cls, "roles": roles, "synth": True},
            {"verb": "GET",    "path": f"/{plural}/{{id}}", "class": cls, "roles": roles, "synth": True},
            {"verb": "POST",   "path": f"/{plural}",       "class": cls, "roles": roles, "synth": True},
            {"verb": "PUT",    "path": f"/{plural}/{{id}}", "class": cls, "roles": roles, "synth": True},
            {"verb": "DELETE", "path": f"/{plural}/{{id}}", "class": cls, "roles": roles, "synth": True},
        ]
    return routes


def _effective_routes_of(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return real routes if present, else synthesised CRUD from owned tables.

    Used by every renderer that would otherwise emit an empty spec.
    """
    real = _routes_of(svc)
    if real:
        return real
    return _synthesize_crud_routes(svc)


def _pluralise(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "items"
    if name.endswith("s"):
        return name
    if name.endswith("y") and len(name) > 1 and name[-2] not in "aeiou":
        return name[:-1] + "ies"
    return name + "s"


def _pascal(name: str) -> str:
    """PascalCase; treats SCREAMING_SNAKE segments as acronym-tail
    (iter-14.46 — see routes/codegen.py::_pascal for rationale)."""
    if not name:
        return ""
    parts = re.split(r"[_\-\s/]+", str(name))
    out = []
    for p in parts:
        if not p:
            continue
        if len(p) > 1 and p.isupper():
            out.append(p[0] + p[1:].lower())
        else:
            out.append(p[0].upper() + p[1:])
    return "".join(out)


def _slugify(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return n or "service"


# --- DDL parser (self-contained; parity with routes/codegen.py::_parse_ddl_columns)
def _parse_ddl_columns(ddl: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not ddl:
        return out
    txt = re.sub(r"/\*.*?\*/", "", ddl, flags=re.DOTALL)
    txt = re.sub(r"--[^\n]*", "", txt)
    for m in re.finditer(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`]?([\w\.]+)[\"`]?\s*\(([\s\S]+?)\)\s*(?:;|$)",
        txt, flags=re.IGNORECASE,
    ):
        raw_name = m.group(1)
        table = raw_name.split(".")[-1].lower()
        body = m.group(2)
        depth = 0
        chunks: List[str] = []
        buf: List[str] = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                chunks.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            chunks.append("".join(buf).strip())
        cols: List[Dict[str, Any]] = []
        for c in chunks:
            cstrip = c.strip().rstrip(",")
            if not cstrip:
                continue
            head = cstrip.lower()
            if head.startswith((
                "primary key", "foreign key", "unique ", "unique(",
                "constraint ", "check ", "key ", "index ",
            )):
                continue
            mm = re.match(
                r"[\"`]?(\w+)[\"`]?\s+([A-Za-z][\w]*(?:\s*\([\s\d,]+\))?)",
                cstrip,
            )
            if not mm:
                continue
            cols.append({
                "name":     mm.group(1),
                "sql_type": mm.group(2),
                "nullable": "not null" not in head,
                "pk":       "primary key" in head,
            })
        if cols:
            if not any(c["pk"] for c in cols):
                for c in cols:
                    if c["name"].lower() in ("id", f"{table}_id"):
                        c["pk"] = True
                        break
            out[table] = cols
    return out


# --- Map SQL type → OpenAPI schema type ---------------------------------------
def _sql_to_openapi(sql_type: str) -> Dict[str, Any]:
    t = (sql_type or "").lower().split("(")[0].strip()
    if t in ("int", "integer", "smallint", "tinyint", "mediumint", "int2", "int4", "serial", "int8", "bigint", "bigserial"):
        return {"type": "integer", "format": "int64" if t in ("bigint", "int8", "bigserial") else "int32"}
    if t in ("numeric", "decimal", "real", "double", "float", "float4", "float8", "money"):
        return {"type": "number", "format": "double"}
    if t in ("bool", "boolean"):
        return {"type": "boolean"}
    if t in ("date",):
        return {"type": "string", "format": "date"}
    if t in ("timestamp", "timestamptz", "datetime", "time", "timetz"):
        return {"type": "string", "format": "date-time"}
    if t in ("uuid",):
        return {"type": "string", "format": "uuid"}
    if t in ("json", "jsonb"):
        return {"type": "object", "additionalProperties": True}
    if t in ("bytea", "blob", "binary", "varbinary"):
        return {"type": "string", "format": "byte"}
    return {"type": "string"}


# --- Extract path params from a legacy route path -----------------------------
_PATH_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}|:([a-zA-Z_][a-zA-Z0-9_]*)")


def _normalise_path(path: str) -> Tuple[str, List[str]]:
    """Return (openapi_path, [param_names]).

    Legacy paths may use ``:id`` (Rails/CI4/Node) or ``{id}`` (already OpenAPI).
    We rewrite ``:id`` → ``{id}`` and collect the ordered list of parameters.
    """
    if not path:
        return "/", []
    params: List[str] = []

    def _sub(m):
        name = m.group(1) or m.group(2)
        params.append(name)
        return "{" + name + "}"

    p = _PATH_PARAM_RE.sub(_sub, path)
    if not p.startswith("/"):
        p = "/" + p
    return p, params


# ─── iter-14.20.3 ── REST modernizer ─────────────────────────────────────────
# Rule-based (no LLM) legacy → REST/OpenAPI-3.x path rewriter. Produces the
# modernized (verb, path) plus provenance for the OpenAPI x-legacy extension.
#
# Handles the CI4 / Rails / classic-PHP conventions that dominate our pilot
# corpora, e.g.:
#   POST /index.php/policy/save                → POST   /policies
#   POST /policy/save/:id                       → PUT    /policies/{id}
#   POST /policy/update/:id                     → PUT    /policies/{id}
#   POST /policy/edit/:id                       → PUT    /policies/{id}
#   GET  /policy/delete/:id                     → DELETE /policies/{id}
#   GET  /policy/list                           → GET    /policies
#   GET  /policy/index                          → GET    /policies
#   GET  /policy/view/:id                       → GET    /policies/{id}
#   GET  /policy/detail/:id                     → GET    /policies/{id}
#   GET  /policy/:id                            → GET    /policies/{id}
#   POST /policy/getList                        → GET    /policies:search
#   GET  /admin/reports/monthly                 → GET    /reports/monthly
#
# Strategy:
#   1. Strip legacy framework prefixes (`/index.php`, `/api`, `/api/v1`,
#      `/rest`, `/services`, `/ajax`, `/admin` etc.).
#   2. Pull path params ({id}/:id) aside so they don't participate in
#      resource-name detection.
#   3. Split remaining path into segments; classify each segment as
#      RESOURCE / ACTION / PARAM / LITERAL.
#   4. Map ACTION → REST verb via `_ACTION_VERB_MAP`; pluralise the resource;
#      re-attach params and any surviving literal tail.
#   5. Fall through unchanged if the path is already REST-shaped (no action
#      keyword, resource already plural, verb sane).
_LEGACY_PREFIXES = (
    "index.php", "api", "v1", "v2", "v3", "rest", "services",
    "ajax", "json", "xml", "public", "site", "admin", "backend",
    "frontend", "web", "mvc", "app",
)

_ACTION_VERB_MAP: Dict[str, Tuple[str, str]] = {
    # action-word: (rest_verb, requires_id: "id" | "noid" | "either")
    "create":   ("POST",   "noid"),
    "new":      ("POST",   "noid"),
    "add":      ("POST",   "noid"),
    "insert":   ("POST",   "noid"),
    "store":    ("POST",   "noid"),
    "register": ("POST",   "noid"),
    "submit":   ("POST",   "noid"),
    "save":     ("POST",   "either"),   # noid → POST; with id → PUT
    "update":   ("PUT",    "id"),
    "edit":     ("PUT",    "id"),
    "modify":   ("PUT",    "id"),
    "patch":    ("PATCH",  "id"),
    "change":   ("PUT",    "id"),
    "set":      ("PUT",    "either"),
    "delete":   ("DELETE", "id"),
    "remove":   ("DELETE", "id"),
    "destroy":  ("DELETE", "id"),
    "cancel":   ("DELETE", "id"),
    "trash":    ("DELETE", "id"),
    "list":     ("GET",    "noid"),
    "all":      ("GET",    "noid"),
    "index":    ("GET",    "noid"),
    "browse":   ("GET",    "noid"),
    "getall":   ("GET",    "noid"),
    "getlist":  ("GET",    "noid"),
    "get_list": ("GET",    "noid"),
    "search":   ("GET",    "noid"),
    "find":     ("GET",    "noid"),
    "filter":   ("GET",    "noid"),
    "view":     ("GET",    "either"),   # noid → list; id → item
    "show":     ("GET",    "either"),
    "detail":   ("GET",    "id"),
    "details":  ("GET",    "id"),
    "get":      ("GET",    "either"),
    "read":     ("GET",    "id"),
    "fetch":    ("GET",    "either"),
    "load":     ("GET",    "either"),
    "info":     ("GET",    "id"),
}


def _split_camel_snake(s: str) -> str:
    """`getUserList` → `get user list`; `get_user_list` → `get user list`."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    s = re.sub(r"[_\-]+", " ", s)
    return s.lower().strip()


def _looks_like_action(seg: str) -> Tuple[bool, str]:
    """Detect a CI4-style action segment. Returns (is_action, canonical_key)."""
    tokens = _split_camel_snake(seg).split()
    if not tokens:
        return False, ""
    # Multi-word compound e.g. "getUserList" -> tokens ["get","user","list"]
    # Priority: full compound key, then first token if it's a verb.
    compound = "".join(tokens)
    if compound in _ACTION_VERB_MAP:
        return True, compound
    joined = "_".join(tokens)
    if joined in _ACTION_VERB_MAP:
        return True, joined
    first = tokens[0]
    if first in _ACTION_VERB_MAP:
        # e.g. "getList" → treat as list; "getUserById" → treat as get
        if len(tokens) >= 2 and tokens[-1] in ("list", "all"):
            return True, "list"
        return True, first
    return False, ""


def _pluralize_resource(seg: str) -> str:
    """Convert a resource segment to plural lower-snake form."""
    tokens = _split_camel_snake(seg).replace(" ", "_").split("_")
    tokens = [t for t in tokens if t]
    if not tokens:
        return "items"
    # Pluralise only the last noun; keep qualifiers as-is
    head, tail = tokens[:-1], tokens[-1]
    return "_".join(head + [_pluralise(tail)])


def modernize_route(
    verb: str, path: str
) -> Tuple[str, str, Dict[str, Any]]:
    """Rewrite a legacy (verb, path) into a REST-shaped (verb, path).

    Returns (new_verb, new_path, meta) where ``meta`` records the original
    values, the action word detected (if any), the resource stem, and the
    ``changed`` boolean so audit trails can flag every modernised endpoint.
    """
    orig_verb = (verb or "GET").upper()
    orig_path = path or "/"
    p_norm, _ = _normalise_path(orig_path)

    segments = [s for s in p_norm.split("/") if s]
    # 1) strip legacy prefixes
    while segments and segments[0].lower() in _LEGACY_PREFIXES:
        segments.pop(0)
    # Also strip `index.php` anywhere at the head after a v-prefix
    while segments and re.fullmatch(r"index\.\w+", segments[0].lower()):
        segments.pop(0)

    if not segments:
        return orig_verb, "/", {
            "original_verb": orig_verb, "original_path": orig_path,
            "changed": False, "reason": "empty_after_strip",
        }

    # 2) collect params (`{id}`) and locate action tokens
    def _is_param(s: str) -> bool:
        return s.startswith("{") and s.endswith("}")

    resource = None
    action_key = None
    trailing_params: List[str] = []
    literal_tail: List[str] = []

    # Common shape: <resource>/<action>[/<param>][/<literal>...]
    if segments:
        resource = segments[0]
    for seg in segments[1:]:
        if _is_param(seg):
            trailing_params.append(seg)
            continue
        if action_key is None:
            is_act, key = _looks_like_action(seg)
            if is_act:
                action_key = key
                continue
        literal_tail.append(seg)

    # If the resource itself looks like an action (e.g. `/save`) — rare, skip
    # modernization to avoid nonsense.
    resource_is_action, _ = _looks_like_action(resource or "")
    if resource_is_action and not literal_tail and not trailing_params:
        return orig_verb, "/" + "/".join(segments), {
            "original_verb": orig_verb, "original_path": orig_path,
            "changed": False, "reason": "resource_is_action",
        }

    # 3) infer REST verb from action + presence of an id
    has_id = any(re.fullmatch(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", tp) for tp in trailing_params)
    new_verb = orig_verb
    if action_key:
        verb_map, needs = _ACTION_VERB_MAP[action_key]
        if needs == "id" and has_id:
            new_verb = verb_map
        elif needs == "noid" and not has_id:
            new_verb = verb_map
        elif needs == "either":
            # "save" without id → POST create; with id → PUT update
            if action_key == "save" and has_id:
                new_verb = "PUT"
            elif action_key == "save" and not has_id:
                new_verb = "POST"
            else:
                new_verb = verb_map
        else:
            # Signals mismatch (e.g. delete without id) — keep action's verb
            # but flag it in meta so reviewers can inspect.
            new_verb = verb_map

    # 4) rebuild path — pluralised resource + id + literal tail
    plural = _pluralize_resource(resource or "items")
    parts = ["", plural]
    for tp in trailing_params:
        parts.append(tp)
    for lt in literal_tail:
        parts.append(lt)
    new_path = "/".join(parts) or "/"
    if not new_path.startswith("/"):
        new_path = "/" + new_path

    changed = (new_verb != orig_verb) or (new_path != p_norm)
    return new_verb, new_path, {
        "original_verb": orig_verb,
        "original_path": orig_path,
        "changed": changed,
        "action": action_key,
        "resource": plural,
        "has_id": has_id,
    }


def _nfr_ids(text: str) -> List[str]:
    if not text:
        return []
    ids = re.findall(r"NFR[-_][A-Z]+[-_]\d+", text)
    # De-dup preserving order
    seen: set = set()
    out: List[str] = []
    for x in ids:
        k = x.replace("_", "-").upper()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _uc_ids(text: str) -> List[str]:
    if not text:
        return []
    ids = re.findall(r"UC[-_]\d+", text)
    seen: set = set()
    out: List[str] = []
    for x in ids:
        k = x.replace("_", "-").upper()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _all_roles(services: List[Dict[str, Any]]) -> List[str]:
    roles: List[str] = []
    seen: set = set()
    for s in services:
        for r in (s.get("roles") or []):
            if r and r not in seen:
                seen.add(r)
                roles.append(r)
    return roles


# ---------------------------------------------------------------------------
# HLD generator
# ---------------------------------------------------------------------------
def render_hld(
    proj: Dict[str, Any],
    sm_data: Dict[str, Any],
    services: List[Dict[str, Any]],
    srs_sections: Dict[str, str],
    oltp_ddl: str,
) -> str:
    """Produce the full HLD markdown blob deterministically."""
    biz = _biz_services(services)
    flav = _target_flavour(proj)
    pattern = sm_data.get("recommended_pattern") or "Microservices (bounded-context per service)"
    event_bus = bool(sm_data.get("event_bus"))
    nfr_ids = _nfr_ids(srs_sections.get("non_functional_requirements", "") or "")
    uc_ids  = _uc_ids(srs_sections.get("detailed_use_cases", "") or srs_sections.get("actors_use_case_inventory", "") or "")
    roles   = _all_roles(biz)
    # ddl_by_table is not used directly in HLD prose (LLD / API contracts use it),
    # but parsing here validates that the OLTP DDL is well-formed early.
    _ = _parse_ddl_columns(oltp_ddl)

    sections: Dict[str, str] = {}

    # 1) Executive Summary --------------------------------------------------
    exec_summary = textwrap.dedent(f"""
    **Project:** {proj.get('name', 'Migration Project')}
    **Legacy stack:** {_source_summary(proj)}
    **Target stack:** {_target_summary(proj)}
    **Detected legacy fingerprint:** {_detected_summary(proj)}
    **Recommended pattern:** {pattern}
    **Business services:** {len(biz)}   |   **Total endpoints:** {sum(len(_effective_routes_of(s)) for s in biz)}   |   **Owned tables:** {sum(len(s.get('tables') or []) for s in biz)}

    This document is the deterministic High-Level Design generated from the frozen
    Service Map, the frozen DataModel, and the frozen SRS. It records the target
    architecture the legacy application will be migrated to, along with the runtime,
    security, observability, deployment and CI/CD stack that will host it. Every
    decision below is grounded in an artifact the user has explicitly frozen —
    no LLM inference is involved.
    """).strip()
    sections["executive_summary"] = exec_summary

    # 2) System Context Diagram --------------------------------------------
    ctx_actors = roles or ["EndUser", "AdminUser"]
    ctx_lines = ["```mermaid", "C4Context", f'    title System Context — {proj.get("name", "Target System")}']
    for a in ctx_actors[:8]:
        alias = _pascal(a) or "Actor"
        ctx_lines.append(f'    Person({alias}, "{a}")')
    ctx_lines.append(f'    System(TargetSystem, "{proj.get("name", "Target System")}", "{_target_summary(proj)}")')
    ctx_lines += [
        '    System_Ext(IdP, "Identity Provider", "Keycloak / OIDC")',
        '    System_Ext(Mail, "Email / Notification Provider", "SMTP / SES")',
        '    System_Ext(Object, "Object Storage", "S3-compatible")',
    ]
    for a in ctx_actors[:8]:
        alias = _pascal(a) or "Actor"
        ctx_lines.append(f'    Rel({alias}, TargetSystem, "Uses", "HTTPS")')
    ctx_lines += [
        '    Rel(TargetSystem, IdP, "Authenticates via", "OIDC")',
        '    Rel(TargetSystem, Mail, "Sends", "SMTP/API")',
        '    Rel(TargetSystem, Object, "Reads/Writes", "S3 API")',
        "```",
        "",
        "**External systems**",
        "- **Identity Provider (Keycloak)** — OAuth2/OIDC issuer for end-user and service-to-service auth.",
        "- **Email / Notification Provider** — outbound transactional email and (optionally) SMS.",
        "- **Object Storage (S3-compatible)** — durable storage for user uploads and generated reports.",
    ]
    sections["system_context"] = "\n".join(ctx_lines)

    # 3) Service Decomposition ---------------------------------------------
    svc_blocks: List[str] = []
    for s in biz:
        routes = _effective_routes_of(s)
        tables = s.get("tables") or []
        mods = s.get("module_names") or s.get("modules") or []
        svc_roles = s.get("roles") or []
        desc = s.get("description") or s.get("responsibility") or f"Owns the {s.get('name', 'service')} bounded context."
        top_uc = uc_ids[:3] if uc_ids else []
        svc_blocks.append(textwrap.dedent(f"""
        ### {s.get('display_name') or s.get('name')} (`{s.get('name')}`)

        - **Bounded context:** {desc}
        - **Owned tables ({len(tables)}):** {', '.join(tables) if tables else '_none_'}
        - **Endpoints ({len(routes)}):** {', '.join(sorted({r.get('verb', 'GET') for r in routes})) or 'GET'} over `/{_slugify(s.get('name', 'svc'))}/...`
        - **Owning legacy modules:** {', '.join(mods[:12]) if mods else '_n/a_'}
        - **Roles:** {', '.join(svc_roles) if svc_roles else '_none_'}
        - **Runtime allocation:** {flav['language']} pod, 2 replicas dev / 3 replicas prod, own Deployment + Service on Kubernetes.
        - **Published/consumed events:** {"outbox → " + flav['bus'] if event_bus else "n/a (synchronous only for iter-1)"}
        - **UC coverage:** {', '.join(top_uc) if top_uc else '_see SRS Detailed Use Cases_'}
        """).rstrip())
    sections["service_decomposition"] = "\n".join(svc_blocks) if svc_blocks else "_No business services enumerated._"

    # 4) API Gateway --------------------------------------------------------
    gw_rows = [f"| `/{_slugify(s.get('name', ''))}/*` | {s.get('display_name') or s.get('name')} | JWT bearer (Keycloak) | 100 rps / user | `X-Request-Id` propagated |"
               for s in biz]
    gw = textwrap.dedent(f"""
    **Product:** {flav['gateway']} (Kubernetes ingress terminator). All external HTTPS
    traffic terminates at the gateway; internal service-to-service calls use mTLS via
    the service mesh.

    **Routing table**

    | Path prefix | Upstream service | Auth | Rate limit | Tracing |
    |---|---|---|---|---|
    {chr(10).join(gw_rows) if gw_rows else '| — | — | — | — | — |'}

    **Auth pass-through:** the gateway validates the JWT signature + `exp` / `aud`
    claims; user identity is forwarded to services via `X-User-Id` and `X-Roles`
    headers. Services never re-validate the signature (trust boundary is the mesh).

    **API versioning:** URI-based (`/v1/…`, `/v2/…`). Breaking changes require a
    new major version and a 90-day deprecation window on the old one.

    **Throttling tiers:** anonymous 20 rps, authenticated end-user 100 rps,
    service-account 500 rps, admin unlimited.
    """).strip()
    sections["api_gateway"] = gw

    # 5) Critical Data Flow Diagrams ---------------------------------------
    # Pick top-3 services by endpoint count.
    top = sorted(biz, key=lambda s: len(_effective_routes_of(s)), reverse=True)[:3]
    flow_blocks: List[str] = []
    for idx, s in enumerate(top, 1):
        routes = _effective_routes_of(s)
        write_routes = [r for r in routes if r.get("verb", "GET").upper() in {"POST", "PUT", "PATCH", "DELETE"}]
        example = write_routes[0] if write_routes else (routes[0] if routes else {"verb": "GET", "path": "/"})
        verb = (example.get("verb") or "GET").upper()
        path, _ = _normalise_path(example.get("path", "/"))
        uc = (uc_ids[idx - 1] if idx - 1 < len(uc_ids) else "UC-000")
        flow_blocks.append(textwrap.dedent(f"""
        #### Workflow {idx}: {s.get('display_name') or s.get('name')} — {verb} {path}  ({uc})

        ```mermaid
        sequenceDiagram
            autonumber
            participant Client
            participant Gateway as API Gateway
            participant Svc as {s.get('display_name') or s.get('name')}
            participant DB as PostgreSQL ({s.get('name')} schema)
            participant Bus as {flav['bus'].split(' ')[0]}

            Client->>Gateway: {verb} {path}  (JWT)
            Gateway->>Gateway: Validate JWT, check rate limit
            Gateway->>Svc: {verb} {path}  (+X-User-Id, X-Roles)
            Svc->>DB: BEGIN; write row; commit outbox row
            DB-->>Svc: OK
            Svc-->>Gateway: 2xx JSON
            Gateway-->>Client: 2xx JSON
            Note over Svc,Bus: Outbox relay publishes {s.get('name')}.event asynchronously
            Svc-)Bus: publish {s.get('name')}.changed
        ```

        **Sync legs:** Client → Gateway → Service → DB.
        **Async legs:** outbox relay → {flav['bus']}.
        **Owning writer:** `{s.get('name')}`.
        **Compensation:** on downstream consumer failure, DLQ retry with exponential
        back-off (max 5 attempts); poison messages go to `{s.get('name')}.dlq` for
        manual replay.
        """).rstrip())
    sections["data_flow"] = "\n".join(flow_blocks) if flow_blocks else "_No services with routes; regenerate the Service Map._"

    # 6) Database Architecture ---------------------------------------------
    db_rows = [f"| {s.get('display_name') or s.get('name')} | `{s.get('name')}` schema | {', '.join(s.get('tables') or []) or '_none_'} |"
               for s in biz]
    db_arch = textwrap.dedent(f"""
    **Strategy:** _Database-per-service_ on a shared {flav['db']} cluster — each
    service owns a private schema and no other service reads/writes it directly.
    Cross-service data movement goes through published events or explicit REST
    calls to the owner.

    | Service | Schema | Owned tables |
    |---|---|---|
    {chr(10).join(db_rows) if db_rows else '| — | — | — |'}

    **Distributed transactions:** avoided. Multi-service writes use the
    _Transactional Outbox_ pattern — the service writes the row and an outbox
    row in the same local transaction, and a relay publishes to
    {flav['bus']} at-least-once.

    **Read-replica strategy:** one hot-standby replica per environment; reporting
    queries are routed to the replica via a read-only connection pool.

    **PII isolation:** PII columns are stored in dedicated tables in the owning
    service; column-level encryption keys are managed in {flav['vault']}.

    **Backup / restore:** WAL streaming to object storage; nightly base backup;
    RPO ≤ 15 min, RTO ≤ 60 min (see NFR section).
    """).strip()
    sections["database_arch"] = db_arch

    # 7) Caching Strategy ---------------------------------------------------
    sections["caching_strategy"] = textwrap.dedent(f"""
    **Layers**

    | Layer | Product | Purpose | Invalidation |
    |---|---|---|---|
    | Edge (CDN) | CloudFront / Cloudflare | Static assets, image transforms | Path-based purge on deploy |
    | Application read-through | {flav['cache']} | Hot entity reads, session snapshots | TTL 60s + event-driven bust on write |
    | DB result cache | `pg_prewarm` warm buffers | Cold query warm-up | Refresh on failover |

    **Keys / TTL per data class**

    - `user:{{id}}` — TTL 300s, invalidated on user-update event.
    - `catalog:{{id}}` — TTL 900s.
    - `session:{{jwt-jti}}` — TTL 3600s (matches JWT lifetime).

    **NFR drivers:** { ', '.join([n for n in nfr_ids if 'PERF' in n or 'AVAIL' in n][:5]) or 'NFR-PERF-001, NFR-PERF-002' }.
    """).strip()

    # 8) Async Messaging ---------------------------------------------------
    if event_bus:
        msg_intro = f"**Bus:** {flav['bus']}.  Every write-owning service publishes domain events via the transactional outbox."
    else:
        msg_intro = ("**Bus:** none in iter-1 (all workflows are synchronous). "
                     "The bus stack is provisioned so async workflows can be enabled per-service in iter-2 without a re-architecture.")
    sections["async_messaging"] = textwrap.dedent(f"""
    {msg_intro}

    **Event taxonomy:** `<service>.<aggregate>.<verb>` (e.g. `orders.order.created`).
    Envelope: `{{event_id, occurred_at, aggregate_id, actor_id, correlation_id, payload}}`.

    **Schema registry:** Confluent-style (or AWS Glue Schema Registry).
    JSON-Schema for iter-1; upgrade to Avro if throughput > 10k msg/s.

    **Consumer groups:** one group per consuming service; concurrency = 4 partitions/service.

    **Dead-letter:** `<topic>.dlq` with 5-attempt exponential back-off before landing;
    a small "DLQ dashboard" tail-follows every `.dlq` topic and pages on-call.

    **Delivery semantics:** at-least-once (default). Consumers are idempotent by
    `event_id`. Exactly-once is reserved for billing/finance topics.

    **Outbox pattern:** every write transaction inserts into `<service>.outbox`.
    A relay job (per service) polls `outbox WHERE published_at IS NULL`, publishes
    to the bus and marks the row published.
    """).strip()

    # 9) Auth ---------------------------------------------------------------
    role_matrix_rows = [
        f"| {r} | " + " | ".join("✓" if r in (s.get("roles") or []) else "" for s in biz) + " |"
        for r in (roles or ["EndUser", "AdminUser"])[:20]
    ]
    role_header = "| Role \\ Service | " + " | ".join(s.get("name") or "?" for s in biz) + " |"
    role_sep = "|---|" + "---|" * len(biz)
    sections["auth"] = textwrap.dedent(f"""
    **Identity Provider:** {flav['idp']} — issues short-lived JWTs (15 min) and
    long-lived refresh tokens (7 days). Login and refresh flows are OAuth2
    Authorization-Code + PKCE (SPA) and Client-Credentials (service-to-service).

    **Token rotation:** refresh tokens are single-use and rotated on every refresh.
    Compromised refresh tokens are detected via reuse-detection and invalidate
    the whole session family.

    **Authorization model:** RBAC layered with attribute checks (ownership +
    tenancy). Roles are asserted in the JWT `roles` claim and forwarded by the
    gateway as `X-Roles`.

    **Service-to-service:** mTLS via the service mesh **plus** short-lived
    client-credentials JWTs scoped to the caller's identity.

    **SPA session:** access token in memory, refresh token in HttpOnly + Secure
    + SameSite=Lax cookie scoped to the auth service origin.

    **Role → service matrix**

    {role_header}
    {role_sep}
    {chr(10).join(role_matrix_rows) if role_matrix_rows else '| _no roles_ |'}
    """).strip()

    # 10) Security Architecture --------------------------------------------
    sections["security_architecture"] = textwrap.dedent(f"""
    **Network segmentation**
    - Public subnet: WAF + ingress LB only.
    - App subnet: services + service mesh; no direct internet egress.
    - Data subnet: {flav['db']} + Redis + object-store endpoints only; NACL locked to app subnet.

    **WAF:** {flav['waf']} with OWASP-CRS 3.3 baseline rules; custom rules for
    JSON body-size limits and rate spikes.

    **Secrets management:** {flav['vault']} — no secrets in env vars at rest.
    Services request short-lived secrets at boot via Vault Agent sidecar.

    **Encryption**
    - At rest: {flav['db']} AES-256 (KMS-managed keys, rotated 90 days).
    - In transit: TLS 1.3 everywhere (edge and mesh).
    - Application-level: column encryption for PII (envelope + Vault-managed DEKs).

    **Audit logging:** every state-changing request emits an `audit` event with
    `actor_id / action / resource_id / at / from_ip`. Sink is the observability
    stack; retention 400 days (compliance).

    **STRIDE summary (per service)**

    | Threat | Mitigation |
    |---|---|
    | Spoofing         | JWT + mTLS mesh, no service accepts anonymous internal calls |
    | Tampering        | HTTPS, request signing at gateway, DB row-level checksums for critical tables |
    | Repudiation      | Immutable audit log, event-store for domain events |
    | Info disclosure  | Column-level PII encryption, field-level RBAC on responses |
    | DoS              | WAF rate limits, gateway throttles, per-service HPA |
    | Elevation of priv| RBAC + ownership checks; deploy-time SAST + dependency scan |

    **Compliance:** { ', '.join([n for n in nfr_ids if 'COMPLIANCE' in n][:5]) or '(no NFR-COMPLIANCE-* IDs found in SRS)' }.
    """).strip()

    # 11) Observability ----------------------------------------------------
    sections["observability"] = textwrap.dedent(f"""
    **Logs:** structured JSON, one line per request, shipped by {flav['logs']}.
    Standard fields: `ts, level, service, trace_id, span_id, user_id, request_id, msg`.

    **Metrics:** {flav['metrics']}. Every service exposes `/metrics` (Prometheus
    format). Baseline metrics: `http_requests_total`, `http_request_duration_seconds`,
    `db_pool_in_use`, `outbox_lag_seconds`, `dlq_depth`.

    **Traces:** {flav['traces']}. All inbound requests are sampled at 10 %
    (100 % for `admin.*` routes); trace headers are propagated end-to-end.

    **SLO / SLI**

    | SLO | Target | Window |
    |---|---|---|
    | Availability | 99.9 % | 30 d rolling |
    | p95 API latency | ≤ 500 ms | 7 d rolling |
    | Error budget burn | < 2 %/hour | live |

    Grounded in NFR-IDs: { ', '.join([n for n in nfr_ids if 'PERF' in n or 'AVAIL' in n][:8]) or 'NFR-PERF-*, NFR-AVAIL-*' }.

    **Alerts:** paging for SLO breach + fast-burn error budget; ticketing for
    slow-burn budget, DLQ growth, and outbox-lag warnings.
    """).strip()

    # 12) NFR realisation --------------------------------------------------
    if nfr_ids:
        nfr_rows = []
        for nid in nfr_ids[:60]:
            if "PERF" in nid:
                dec, comp, ev = "Horizontal auto-scaling + Redis cache", "Kubernetes HPA + Redis", "p95 latency dashboard"
            elif "AVAIL" in nid:
                dec, comp, ev = "Multi-AZ + hot-standby replicas", "K8s + Postgres HA", "Availability SLO dashboard"
            elif "SEC" in nid or "COMPLIANCE" in nid:
                dec, comp, ev = "Vault + column encryption + WAF", "Vault, WAF, mesh mTLS", "SAST report + WAF alerts"
            elif "SCAL" in nid:
                dec, comp, ev = "Stateless services + partitioned bus topics", "K8s + Kafka", "Throughput dashboard"
            elif "MAINT" in nid or "OPER" in nid:
                dec, comp, ev = "Structured logs + traces + runbooks", "Loki + Tempo + Wiki", "MTTR trend"
            else:
                dec, comp, ev = "Documented in HLD + service-specific LLD", "See LLD", "Manual review"
            nfr_rows.append(f"| {nid} | (see SRS §NFR) | {dec} | {comp} | {ev} |")
    else:
        nfr_rows = ["| _no NFR-IDs found in SRS_ | — | — | — | — |"]
    sections["nfr"] = textwrap.dedent(f"""
    **NFR-ID → Architecture decision map**

    | NFR-ID | Requirement (summary) | Architecture decision | Component | Evidence / measurement |
    |---|---|---|---|---|
    {chr(10).join(nfr_rows)}

    See SRS §Non-Functional Requirements for the full text of each NFR.
    """).strip()

    # 13) Deployment -------------------------------------------------------
    dep_nodes = "\n".join(
        f'    S{i}["{s.get("display_name") or s.get("name")}"]' for i, s in enumerate(biz)
    )
    dep_edges = "\n".join(
        f"    GW --> S{i}\n    S{i} --> DB\n    S{i} --> CACHE" for i, s in enumerate(biz)
    )
    sections["deployment"] = textwrap.dedent(f"""
    ```mermaid
    graph LR
        Internet((Internet))
        WAF[WAF]
        Ingress[K8s Ingress]
        GW[{flav['gateway'].split(' ')[0]} Gateway]
    {dep_nodes}
        DB[({flav['db']})]
        CACHE[({flav['cache'].split(' ')[0]})]
        BUS[({flav['bus'].split(' ')[0]})]
        OBS[[Observability<br/>{flav['metrics']} · {flav['logs']} · {flav['traces']}]]

        Internet --> WAF --> Ingress --> GW
    {dep_edges}
        S0 --> BUS
        OBS -. scrape .-> GW
    ```

    **Runtime versions (verbatim):** {flav['language']}, {flav['db']}, {flav['cache']},
    {flav['bus']}, {flav['idp']}, {flav['container']}.

    **Environments:** `dev` → `stage` → `prod`. Promotion gates: (1) all unit +
    contract tests green, (2) SAST + dependency scan clean, (3) manual approval
    from service owner.
    """).strip()

    # 14) CI/CD ------------------------------------------------------------
    sections["ci_cd"] = textwrap.dedent(f"""
    **Tool:** {flav['ci']}.

    **Stages (per push)**
    1. `checkout` + `setup-{flav['language'].split()[0].lower()}`
    2. `build` — compile / package.
    3. `test-unit` — with coverage gate (≥ 80 %).
    4. `lint` — language-native linter (e.g. ruff / eslint / detekt).
    5. `sast` — Semgrep + language-specific SAST.
    6. `container-build` — multi-stage Docker; slim runtime base.
    7. `container-sign` — cosign, key from {flav['vault']}.
    8. `test-integration` — spins up dependencies with docker-compose or KinD.
    9. `deploy-dev` — auto on `main` merge.
    10. `deploy-stage` — auto on tag `v*`.
    11. `deploy-prod` — manual approval + canary (10 % → 50 % → 100 %).

    **Branching:** trunk-based; short-lived feature branches merged via PR.

    **Artifact registry:** OCI registry (GitHub Container Registry / ECR).

    **Rollout per service:** blue-green for stateless read services, canary for
    write-owning services (auto-rollback on error-rate SLO burn > 2 %/min).
    """).strip()

    # 15) DR / BCP ---------------------------------------------------------
    sections["dr_bcp"] = textwrap.dedent("""
    **RPO / RTO targets**

    | Service | RPO | RTO |
    |---|---|---|
    """).strip() + "\n" + "\n".join(
        f"| {s.get('display_name') or s.get('name')} | 15 min | 60 min |" for s in biz
    ) + textwrap.dedent(f"""

    **Cite:** { ', '.join([n for n in nfr_ids if 'AVAIL' in n or 'COMPLIANCE' in n][:6]) or 'NFR-AVAIL-* / NFR-COMPLIANCE-* (see SRS)' }.

    **Posture:** multi-AZ within the primary region; async replica in a
    secondary region for disaster fail-over (manual promotion).

    **Backup cadence:**
    - Postgres: WAL streaming (RPO 15 min) + nightly base backup (14-day retention).
    - Object storage: cross-region replication, 30-day versioning.
    - Kafka topics: 7-day retention (compacted topics: infinite).

    **Failover runbook:** documented in the ops wiki; drilled quarterly.

    **DR drill cadence:** full-region drill every 6 months; per-service game-day
    every quarter.
    """).rstrip()

    # 16) Migration Strategy -----------------------------------------------
    cutover_rows = "\n".join(
        f"| {i+1} | {s.get('display_name') or s.get('name')} | Dual-write for 2 sprints, then switch reads, then switch writes | Legacy remains hot for 1 sprint post-cutover |"
        for i, s in enumerate(biz)
    ) or "| — | — | — | — |"
    sections["migration_strategy"] = textwrap.dedent(f"""
    **Pattern:** _Strangler-fig_. The legacy monolith remains the system-of-record
    until each bounded context has been migrated, cutover, and observed clean
    for one sprint. No big-bang cutover.

    **Traffic shifting mechanism:** feature-flag at {flav['gateway'].split(' ')[0]}
    routing; `x-migration-cohort` header picks legacy vs new backend per user
    cohort. Percentage ramps: 5 % → 25 % → 50 % → 100 %.

    **Dual-write window:** each service writes to both legacy and new datastores
    for 2 sprints; a reconciliation job compares the two nightly and pages on
    drift > 0.01 %.

    **Data backfill:** initial one-shot dump via `pg_dump` + transforming
    ingestion job; on-going delta via CDC (Debezium) → new datastore.

    **Cutover criteria per service:**
    - Error-rate SLO healthy for 7 days on the new stack at 100 %.
    - Reconciliation drift = 0 for 7 consecutive days.
    - Runbook + on-call rotation in place.

    **Rollback plan:** revert the feature-flag to 0 % — legacy path is still hot
    for one sprint after cutover; drain the outbox, reverse CDC, resume writes on legacy.

    **Cutover schedule (proposed)**

    | # | Service | Cutover approach | Rollback window |
    |---|---|---|---|
    {cutover_rows}

    **Legacy retirement:** the following legacy components are decommissioned
    at the end of migration: {_source_summary(proj)}.
    """).strip()

    # 17) Tech Decisions (ADR) --------------------------------------------
    sections["tech_decisions"] = textwrap.dedent(f"""
    **ADR-001 — Runtime language / framework**
    - **Context:** Migrate from {_source_summary(proj)} to a cloud-native stack.
    - **Decision:** {flav['language']}.
    - **Consequences:** Native async I/O, first-class OpenAPI tooling, familiar for the target team.
    - **Alternatives considered:** other language runtimes rejected because the user
      selected `{_target_summary(proj)}` as the target stack.

    **ADR-002 — Messaging**
    - **Context:** Domain events + eventual consistency between services.
    - **Decision:** {flav['bus']}.
    - **Consequences:** Durable log, replayable topics, schema-registry compatible.
    - **Alternatives considered:** AWS SNS/SQS (rejected — no replay), RabbitMQ (kept as fallback for low-volume queues).

    **ADR-003 — Datastore**
    - **Context:** OLTP workloads with strong consistency + row-level security.
    - **Decision:** {flav['db']}, database-per-service.
    - **Consequences:** Isolation, per-service schema evolution.
    - **Alternatives considered:** MongoDB (rejected — relational integrity), single shared DB (rejected — coupling).

    **ADR-004 — Cache**
    - **Context:** Read-heavy endpoints + session state.
    - **Decision:** {flav['cache']}.
    - **Consequences:** Well-understood ops story, cluster mode for HA.
    - **Alternatives considered:** Memcached (rejected — no persistence), local in-memory (rejected — no cross-pod sharing).

    **ADR-005 — Identity Provider**
    - **Context:** OIDC-compliant issuer required by every service.
    - **Decision:** {flav['idp']}.
    - **Consequences:** Self-hostable, supports federation with corporate SSO.
    - **Alternatives considered:** Auth0 / Okta (rejected — cost + data residency).
    """).strip()

    # -------- Assemble ----------------------------------------------------
    parts: List[str] = []
    for key, label in HLD_SECTION_ORDER:
        parts.append(f"## {label}\n\n{sections.get(key, '_deterministic body missing_')}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLD generator
# ---------------------------------------------------------------------------
def render_lld(
    proj: Dict[str, Any],
    services: List[Dict[str, Any]],
    srs_sections: Dict[str, str],
    oltp_ddl: str,
) -> str:
    """Emit one ``# Service: <display_name> (`<name>`)`` block per business service.

    Each block contains a Mermaid class diagram, module tree, DTO tables,
    endpoint list, error taxonomy, dependencies, and an OpenAPI 3.1 YAML
    excerpt for the service (fenced with ```yaml``` so the existing extractor
    in ``_run_lld_job`` still finds it).
    """
    biz = _biz_services(services)
    flav = _target_flavour(proj)
    ddl_by_table = _parse_ddl_columns(oltp_ddl)
    per_service_blocks: List[str] = []

    for svc in biz:
        name = svc.get("name") or "service"
        disp = svc.get("display_name") or name
        routes = _effective_routes_of(svc)
        tables = svc.get("tables") or []
        roles  = svc.get("roles") or []
        deps   = svc.get("dependencies") or []
        mods   = svc.get("module_names") or svc.get("modules") or []
        desc   = svc.get("description") or svc.get("responsibility") or ""

        # Group routes by resource (first path segment after `/api/`)
        resource_map: Dict[str, List[Dict[str, Any]]] = {}
        for r in routes:
            p, _ = _normalise_path(r.get("path", "/"))
            segs = [s for s in p.split("/") if s and not s.startswith("{")]
            res = segs[0] if segs else "root"
            resource_map.setdefault(res, []).append({**r, "path": p})
        resources = list(resource_map.keys()) or ["root"]

        # Class diagram --------------------------------------------------
        cls_lines = ["```mermaid", "classDiagram"]
        for res in resources:
            r_cls = _pascal(res)
            cls_lines += [
                f"    class {r_cls}Controller {{",
                f"        +list{_pascal(_pluralise(res))}() : List[{r_cls}Dto]",
                f"        +get{r_cls}(id) : {r_cls}Dto",
                f"        +create{r_cls}(payload) : {r_cls}Dto",
                f"        +update{r_cls}(id, payload) : {r_cls}Dto",
                f"        +delete{r_cls}(id) : void",
                "    }",
                f"    class {r_cls}Service {{",
                f"        -repo : {r_cls}Repository",
                f"        +list(filter) : List[{r_cls}]",
                "        +apply_business_rules(entity)",
                "    }",
                f"    class {r_cls}Repository {{",
                f"        +find_all(filter) : List[{r_cls}]",
                f"        +find_by_id(id) : {r_cls}",
                f"        +save({r_cls}) : {r_cls}",
                "        +delete(id)",
                "    }",
                f"    class {r_cls}Dto",
                f"    class {r_cls}",
                f"    {r_cls}Controller --> {r_cls}Service",
                f"    {r_cls}Service --> {r_cls}Repository",
                f"    {r_cls}Repository --> {r_cls}",
                f"    {r_cls}Controller ..> {r_cls}Dto",
            ]
        cls_lines.append("```")

        # Module tree ----------------------------------------------------
        tree_lines = [
            "```",
            f"{name}/",
            "├── src/",
            "│   ├── api/            # controllers (thin HTTP layer)",
        ]
        for res in resources:
            tree_lines.append(f"│   │   └── {res}_controller.py")
        tree_lines.append("│   ├── services/      # business logic")
        for res in resources:
            tree_lines.append(f"│   │   └── {res}_service.py")
        tree_lines.append("│   ├── repositories/  # DB access")
        for res in resources:
            tree_lines.append(f"│   │   └── {res}_repository.py")
        tree_lines.append("│   ├── models/        # ORM entities")
        for t in tables:
            tree_lines.append(f"│   │   └── {t}.py")
        tree_lines.append("│   ├── schemas/       # Pydantic DTOs")
        for res in resources:
            tree_lines.append(f"│   │   └── {res}_dto.py")
        tree_lines += [
            "│   ├── config.py",
            "│   ├── db.py",
            "│   ├── auth.py",
            "│   └── main.py",
            "├── tests/",
            "├── Dockerfile",
            "├── pyproject.toml",
            "└── README.md",
            "```",
        ]

        # DTO tables (from DDL) -----------------------------------------
        dto_blocks: List[str] = []
        for t in tables:
            cols = ddl_by_table.get(t.lower()) or []
            if not cols:
                dto_blocks.append(f"**{t}** — no DDL rows parsed; refer to OLTP DDL for authoritative schema.")
                continue
            rows = "\n".join(
                f"| `{c['name']}` | `{c['sql_type']}` | {'yes' if c['nullable'] else 'no'} | {'✓' if c['pk'] else ''} |"
                for c in cols
            )
            dto_blocks.append(textwrap.dedent(f"""
            **`{t}`**

            | Column | SQL type | Nullable | PK |
            |---|---|---|---|
            {rows}
            """).strip())

        # Endpoint table ------------------------------------------------
        ep_rows: List[str] = []
        for r in routes[:200]:
            path, _ = _normalise_path(r.get("path", "/"))
            verb = (r.get("verb") or "GET").upper()
            cls  = r.get("class") or ""
            rls  = ", ".join(r.get("roles") or []) or "_public_"
            ep_rows.append(f"| `{verb}` | `{path}` | `{cls}` | {rls} |")
        endpoint_table = ("\n".join(ep_rows)) if ep_rows else "| _no routes attached_ | — | — | — |"

        # OpenAPI snippet -----------------------------------------------
        oapi = _render_openapi_for_service(svc, ddl_by_table, flav)

        # Assemble the service block ------------------------------------
        block = textwrap.dedent(f"""
        # Service: {disp} (`{name}`)

        > {desc or 'Bounded context deterministically derived from the frozen Service Map.'}

        - **Owned tables:** {', '.join(tables) or '_none_'}
        - **Endpoints:** {len(routes)}
        - **Roles:** {', '.join(roles) or '_none_'}
        - **Owning legacy modules:** {', '.join(mods[:20]) or '_n/a_'}
        - **Dependencies:** {', '.join(deps) or '_none_'}
        - **Runtime:** {flav['language']}

        ## 1. Class Diagram

        """).lstrip() + "\n".join(cls_lines) + textwrap.dedent("""

        ## 2. Module Structure

        """) + "\n".join(tree_lines) + textwrap.dedent("""

        ## 3. Data Transfer Objects

        """) + ("\n\n".join(dto_blocks) if dto_blocks else "_No owned tables — DTOs derived from upstream services._") + textwrap.dedent(f"""

        ## 4. Endpoints

        | Verb | Path | Legacy class | Required roles |
        |---|---|---|---|
        {endpoint_table}

        ## 5. Error Taxonomy

        | HTTP | Domain code | When |
        |---|---|---|
        | 400  | `VALIDATION_FAILED` | Payload fails Pydantic schema |
        | 401  | `UNAUTHENTICATED`   | Missing / invalid JWT |
        | 403  | `FORBIDDEN`         | Role check fails |
        | 404  | `NOT_FOUND`         | Resource id not found |
        | 409  | `CONFLICT`          | Optimistic-lock conflict |
        | 422  | `BUSINESS_RULE`     | Domain rule rejected the operation |
        | 500  | `INTERNAL`          | Unhandled exception |

        ## 6. OpenAPI (excerpt)

        ```yaml
        {textwrap.indent(oapi, "        ").lstrip()}
        ```
        """).rstrip()

        per_service_blocks.append(block)

    return "\n\n---\n\n".join(per_service_blocks) if per_service_blocks else "_No business services to render LLD for._"


# ---------------------------------------------------------------------------
# API Contracts generator (per-service OpenAPI 3.1)
# ---------------------------------------------------------------------------
def render_api_contracts(
    proj: Dict[str, Any],
    services: List[Dict[str, Any]],
    oltp_ddl: str,
) -> Tuple[str, List[str]]:
    """Return ``(body, skipped_service_names)`` where ``body`` is the concatenated
    OpenAPI 3.1 YAML for every service that has ≥1 effective route.

    iter-14.20.1 — A service is "renderable" when it has either explicit
    ``routes_detail`` OR owned tables (we synthesize CRUD for the latter).
    Only truly empty services (no routes AND no tables) are skipped.
    """
    biz_all = _biz_services(services)
    ddl_by_table = _parse_ddl_columns(oltp_ddl)
    flav = _target_flavour(proj)

    def _renderable(s: Dict[str, Any]) -> bool:
        return bool(_routes_of(s)) or bool(s.get("tables"))

    with_routes = [s for s in biz_all if _renderable(s)]
    skipped = [
        (s.get("display_name") or s.get("name") or "?")
        for s in biz_all if not _renderable(s)
    ]

    per_service: List[str] = []
    for svc in with_routes:
        name = svc.get("name") or "service"
        disp = svc.get("display_name") or name
        effective = _effective_routes_of(svc)
        api_count = len(effective)
        synthesised = not _routes_of(svc) and bool(effective)
        header = (
            f"# === Service: {disp} ({name}) — {api_count} endpoint(s) ==="
            + ("  (synthesised from owned tables — recommend attached 0 routes)" if synthesised else "")
        )
        oapi = _render_openapi_for_service(svc, ddl_by_table, flav)
        per_service.append(f"{header}\n{oapi}")

    return "\n\n---\n\n".join(per_service), skipped


# ---------------------------------------------------------------------------
# Per-service OpenAPI YAML renderer (shared by LLD + api_contracts)
# ---------------------------------------------------------------------------
def _yaml_str(s: str) -> str:
    """Serialise a string for inline YAML (very small subset — enough for our
    generated identifiers and descriptions).
    """
    if s is None:
        return '""'
    s = str(s)
    if re.match(r"^[A-Za-z0-9_\-./]+$", s) and s not in {"true", "false", "null", "yes", "no"}:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_openapi_for_service(
    svc: Dict[str, Any],
    ddl_by_table: Dict[str, List[Dict[str, Any]]],
    flav: Dict[str, str],
) -> str:
    name = svc.get("name") or "service"
    disp = svc.get("display_name") or name
    tables = svc.get("tables") or []
    routes = _effective_routes_of(svc)

    # iter-14.58 — Deduplicate tables by PascalCase schema-key before
    # emitting components/schemas. Legacy Oracle schemas often carry the
    # same table under multiple case variants (e.g. `EHFM_USR_hospital_MPG`
    # + `EHFM_USR_HOSPITAL_MPG` + `ehfm_usr_hospital_mpg` all normalise
    # to `EhfmUsrHospitalMpg` via `_pascal()`), which produced repeated
    # `EhfmUsrHospitalMpg:` mapping keys under `components.schemas` and
    # broke the FE YAML parser with "duplicated mapping key". Preserve the
    # first-seen original casing so the "table {t}" description remains
    # legible.
    _seen_schemas: set[str] = set()
    _dedup_tables: List[str] = []
    for _t in tables:
        _key = _pascal(_t)
        if _key in _seen_schemas:
            continue
        _seen_schemas.add(_key)
        _dedup_tables.append(_t)
    tables = _dedup_tables

    # Build components/schemas from owned tables ----------------------------
    schema_lines: List[str] = []
    for t in tables:
        cols = ddl_by_table.get(t.lower()) or []
        cls_name = _pascal(t)
        if not cols:
            schema_lines += [
                f"    {cls_name}:",
                "      type: object",
                "      additionalProperties: true",
                f'      description: "Schema for table {t} — DDL not parsed; verify against OLTP artifact."',
            ]
            continue
        schema_lines += [f"    {cls_name}:", "      type: object"]
        required = [c["name"] for c in cols if not c["nullable"] and not c["pk"]]
        if required:
            schema_lines.append("      required:")
            for r in required:
                schema_lines.append(f"        - {r}")
        schema_lines.append("      properties:")
        for c in cols:
            oa = _sql_to_openapi(c["sql_type"])
            schema_lines.append(f"        {c['name']}:")
            for k, v in oa.items():
                if isinstance(v, bool):
                    schema_lines.append(f"          {k}: {'true' if v else 'false'}")
                else:
                    schema_lines.append(f"          {k}: {_yaml_str(v)}")
            if c["nullable"] and not c["pk"]:
                schema_lines.append("          nullable: true")
        # Generic list wrapper
        schema_lines += [
            f"    {cls_name}List:",
            "      type: object",
            "      properties:",
            "        items:",
            "          type: array",
            "          items:",
            f"            $ref: '#/components/schemas/{cls_name}'",
            "        total:",
            "          type: integer",
        ]

    # Generic error schema always present
    schema_lines += [
        "    Error:",
        "      type: object",
        "      required: [code, message]",
        "      properties:",
        "        code: { type: string }",
        "        message: { type: string }",
        "        details: { type: object, additionalProperties: true }",
    ]

    # Build paths ----------------------------------------------------------
    # iter-14.20.3 — Modernize every legacy (verb, path) to REST/OpenAPI 3.x
    # shape via `modernize_route`. Multiple legacy routes may collapse to the
    # same modern (verb, path); their originals are preserved in `x-legacy`
    # so the migration remains fully auditable.
    path_map: Dict[str, Dict[str, Any]] = {}
    modernized_count = 0
    for r in routes:
        new_verb, new_path, mod_meta = modernize_route(
            r.get("verb") or "GET", r.get("path") or "/",
        )
        if mod_meta.get("changed"):
            modernized_count += 1
        p, params = _normalise_path(new_path)
        verb = new_verb.lower()
        if verb not in {"get", "post", "put", "patch", "delete", "head", "options"}:
            verb = "get"
        path_map.setdefault(p, {"params": params, "ops": {}})
        legacy_entry = {
            "verb": mod_meta.get("original_verb"),
            "path": mod_meta.get("original_path"),
            "action": mod_meta.get("action"),
            "class": r.get("class") or "",
        }
        # If this modern (verb, path) is being emitted for the first time,
        # create the op. If we're colliding with an earlier legacy route,
        # just append to its x-legacy list — do NOT overwrite the op.
        existing = path_map[p]["ops"].get(verb)
        if existing:
            existing.setdefault("legacy_routes", []).append(legacy_entry)
            # Union the role sets so downstream authz stays permissive-safe
            if r.get("roles"):
                merged = set(existing.get("roles") or [])
                merged.update(r["roles"])
                existing["roles"] = sorted(merged)
            continue

        op_id = f"{verb}_{_slugify(p).replace('-', '_')}"
        body_table = None
        segs = [seg for seg in p.split("/") if seg and not seg.startswith("{")]
        if segs:
            for t in tables:
                if t.lower() == segs[0].lower() or t.lower() == segs[-1].lower() or _pluralise(t.lower()) == segs[0].lower():
                    body_table = t
                    break
        if not body_table and tables:
            body_table = tables[0]
        schema_ref = f"#/components/schemas/{_pascal(body_table)}" if body_table else None
        list_ref = f"#/components/schemas/{_pascal(body_table)}List" if body_table else None
        path_map[p]["ops"][verb] = {
            "op_id":         op_id,
            "summary":       f"{verb.upper()} {p}",
            "body_ref":      schema_ref,
            "list_ref":      list_ref,
            "roles":         r.get("roles") or [],
            "legacy_cls":    r.get("class") or "",
            "legacy_routes": [legacy_entry],
        }

    # Emit YAML -----------------------------------------------------------
    lines: List[str] = [
        "openapi: 3.1.0",
        "info:",
        f"  title: {_yaml_str(disp + ' API')}",
        "  version: 1.0.0",
        f"  description: {_yaml_str('Deterministic OpenAPI 3.x contract — RESTified from legacy routes via rule-based modernizer. No LLM inference. Legacy provenance under x-legacy.')}",
        f"  x-modernized-endpoints: {modernized_count}",
        "servers:",
        f"  - url: /{_slugify(name)}",
        "    description: Cluster-internal upstream (behind gateway)",
        "security:",
        "  - bearerAuth: []",
        "tags:",
        f"  - name: {name}",
        f"    description: {_yaml_str(svc.get('description') or svc.get('responsibility') or (disp + ' bounded context'))}",
        "paths:",
    ]

    if not path_map:
        lines += [
            "  /health:",
            "    get:",
            f"      operationId: {name}_health",
            "      summary: Health probe",
            "      responses:",
            "        '200':",
            "          description: OK",
        ]
    else:
        for p, entry in path_map.items():
            lines.append(f"  {p}:")
            if entry["params"]:
                lines.append("    parameters:")
                for pname in entry["params"]:
                    lines += [
                        f"      - name: {pname}",
                        "        in: path",
                        "        required: true",
                        "        schema: { type: string }",
                    ]
            for verb, op in entry["ops"].items():
                lines += [
                    f"    {verb}:",
                    f"      operationId: {op['op_id']}",
                    f"      summary: {_yaml_str(op['summary'])}",
                    f"      tags: [{name}]",
                ]
                if op["legacy_cls"]:
                    lines.append(f"      description: {_yaml_str('Legacy class: ' + op['legacy_cls'])}")
                # Roles → security requirement extension
                if op["roles"]:
                    lines.append("      x-required-roles:")
                    for role in op["roles"]:
                        lines.append(f"        - {_yaml_str(role)}")
                # iter-14.20.3 — legacy provenance for auditors
                legacy_routes = op.get("legacy_routes") or []
                if legacy_routes:
                    lines.append("      x-legacy:")
                    for lr in legacy_routes:
                        lines.append(
                            f"        - {{ verb: {lr.get('verb') or '?'}, "
                            f"path: {_yaml_str(lr.get('path') or '')}, "
                            f"action: {_yaml_str(lr.get('action') or '')}, "
                            f"class: {_yaml_str(lr.get('class') or '')} }}"
                        )
                # Request body for write verbs
                if verb in {"post", "put", "patch"} and op["body_ref"]:
                    lines += [
                        "      requestBody:",
                        "        required: true",
                        "        content:",
                        "          application/json:",
                        "            schema:",
                        f"              $ref: '{op['body_ref']}'",
                    ]
                # Responses
                lines.append("      responses:")
                if verb == "delete":
                    lines += [
                        "        '204':",
                        "          description: Deleted",
                    ]
                elif verb == "get" and not entry["params"] and op["list_ref"]:
                    lines += [
                        "        '200':",
                        "          description: OK",
                        "          content:",
                        "            application/json:",
                        "              schema:",
                        f"                $ref: '{op['list_ref']}'",
                    ]
                elif op["body_ref"]:
                    lines += [
                        "        '200':",
                        "          description: OK",
                        "          content:",
                        "            application/json:",
                        "              schema:",
                        f"                $ref: '{op['body_ref']}'",
                    ]
                else:
                    lines += ["        '200':", "          description: OK"]
                for status, code in [("400", "VALIDATION_FAILED"), ("401", "UNAUTHENTICATED"),
                                     ("403", "FORBIDDEN"), ("404", "NOT_FOUND"), ("500", "INTERNAL")]:
                    lines += [
                        f"        '{status}':",
                        f"          description: {code}",
                        "          content:",
                        "            application/json:",
                        "              schema:",
                        "                $ref: '#/components/schemas/Error'",
                    ]

    # Components
    lines += [
        "components:",
        "  securitySchemes:",
        "    bearerAuth:",
        "      type: http",
        "      scheme: bearer",
        "      bearerFormat: JWT",
        "  schemas:",
    ]
    lines += schema_lines

    return "\n".join(lines) + "\n"
