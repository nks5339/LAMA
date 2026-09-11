"""
Shared Knowledge-Base builder for Tools (Gap Analyzer + Transformer).

Given uploaded code files (and optionally doc files), this module:
  1. Extracts code entities (classes, methods, routes, tables, columns, UI components)
     via the existing owl_extractor.
  2. Extracts documentation requirements (deterministic regex on FR-/NFR-/shall/must).
  3. Builds a UI → API → DB traceability map by cross-referencing extracted entities.
  4. Aggregates statistics + tech-stack detection.

The returned KB is a compact, LLM-friendly context that phase-2 verifiers /
planners consume. It is also persisted to `tools_kb` so the UI can display
the extracted knowledge before analysis starts (trust + transparency).

Both Gap Analyzer and Transformer call `build_tools_kb()` in phase 1;
phase 2 injects `render_kb_for_prompt(kb)` into their LLM prompts.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from kb.owl_extractor import extract, enrich_routes, aggregate_stats, _collect_string_constants


# ─── Constants ─────────────────────────────────────────────────────────────

_EXT_MAP = {
    ".java": "java", ".jsp": "jsp", ".jspx": "jsp",
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js",
    ".php": "php", ".phtml": "php",
    ".sql": "sql",
    ".cs": "dotnet", ".vb": "dotnet",
    ".xml": "xml", ".config": "xml",
    ".html": "js", ".htm": "js",
    ".vue": "js", ".svelte": "js",
}

_UI_EXT = {".html", ".htm", ".jsp", ".jspx", ".vue", ".svelte", ".tsx", ".jsx", ".ftl"}
_UI_ENTITY_TYPES = {"COMPONENT", "PAGE", "VIEW", "TEMPLATE"}
_API_ENTITY_TYPES = {"ROUTE", "ENDPOINT", "METHOD", "HANDLER"}
_DB_ENTITY_TYPES = {"TABLE", "TABLE_HINT", "COLUMN", "VIEW_DB"}

_REQ_ID_RE = re.compile(
    # iter-15.7 — Dropped the `SEC` prefix: it was matching `SECTION-3`,
    # `SECTIONS-3` etc. from SRS ToC / heading rows and turning them into
    # phantom "requirements" with garbage snippets in the coverage matrix.
    # Also tightened the pattern to require a hyphen/underscore separator
    # so bare words like "PERF" or "UI" inside prose don't over-match.
    r"\b((?:FR|NFR|REQ|US|BR|UC|UI|SR|FRS|SRS|PERF)[-_][A-Z0-9]{1,10}(?:[-_]\d{1,4})?)\b",
    re.IGNORECASE,
)
_SHALL_RE = re.compile(
    r"(?im)^\s*(?:[-*\d.]+\s*)?(.{10,300}?\b(?:shall|must|should)\b.{5,400})$"
)

# iter-15.7 — Sentinels that indicate we grabbed a table header / RACI
# matrix / ToC row instead of the actual requirement text. When the
# extracted snippet contains any of these we look for a better window
# elsewhere in the document.
_REQ_TEXT_JUNK_MARKERS = (
    "actor uc-",
    "r = responsible",
    "a = approver",
    "o = observer",
    "= responsible/initiator",
    "source file ■",
    "symbol data objects",
    "confidence summary",
    "score (0-100)",
    "section score",
    "not evaluated by the verifier",
    "table of contents",
    "list of figures",
    "list of tables",
)


def _pick_requirement_text(content: str, rid: str, start: int, end: int) -> str:
    """iter-15.7 — Return the CLEANEST possible requirement text for `rid`.

    Preference order:
      1. A sentence in the 500-char window around this hit that contains
         the ID AND a "shall|must|should" verb.
      2. The first sentence AFTER the ID in the window that contains
         "shall|must|should".
      3. The raw normalised window (existing behaviour).

    Also strips out obvious table-header / RACI-matrix junk so the LLM
    prompt isn't polluted.
    """
    win_start = max(0, start - 200)
    win_end = min(len(content), end + 500)
    window = content[win_start:win_end]
    # Split into sentences on ., !, ?, or newline
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", window) if s.strip()]

    def _is_junky(s: str) -> bool:
        lc = s.lower()
        return any(m in lc for m in _REQ_TEXT_JUNK_MARKERS) or len(s) < 20

    id_upper = rid.upper()

    # Pass 1: sentence containing both the ID and shall/must/should
    for s in sentences:
        if _is_junky(s):
            continue
        if id_upper in s.upper() and re.search(r"\b(shall|must|should)\b", s, re.I):
            return re.sub(r"\s+", " ", s).strip()[:400]
    # Pass 2: any non-junky shall/must/should sentence NEAR the ID.
    # iter-15.7 — Only accept a shall sentence from Pass 2 if the ID
    # doesn't have a better window elsewhere in the doc. To avoid
    # cross-contamination (e.g. UC-02 stealing FR-PGS-001's sentence when
    # UC-02 appears in a RACI matrix), require that the shall sentence
    # does NOT contain another distinct requirement ID.
    other_id_re = re.compile(
        r"\b((?:FR|NFR|REQ|US|BR|UC|UI|SR|FRS|SRS|PERF)[-_][A-Z0-9]{1,10}(?:[-_]\d{1,4})?)\b",
        re.I,
    )
    for s in sentences:
        if _is_junky(s):
            continue
        if re.search(r"\b(shall|must|should)\b", s, re.I):
            other_ids = {mm.group(1).upper() for mm in other_id_re.finditer(s)}
            other_ids.discard(id_upper)
            if other_ids:
                continue  # sentence belongs to a DIFFERENT requirement
            return re.sub(r"\s+", " ", s).strip()[:400]
    # Pass 3: any non-junky sentence containing the ID
    for s in sentences:
        if _is_junky(s):
            continue
        if id_upper in s.upper():
            return re.sub(r"\s+", " ", s).strip()[:400]

    # Fallback: normalised raw window, junk-stripped
    fallback = re.sub(r"\s+", " ", content[max(0, start - 80): min(len(content), end + 320)]).strip()
    return fallback[:400]


# ─── Entity extraction ────────────────────────────────────────────────────

def _filetype_from_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _EXT_MAP.get(ext, "unknown")


def extract_code_entities(files: List[Dict]) -> List[Dict]:
    """Extract OWL entities from parsed code files. Adds `source_file`."""
    entities: List[Dict] = []

    # iter-15.20 — Pre-pass: collect `String NAME = "value";` constants from
    # every Java file up front so `extract_java()` can resolve class-level
    # `@Path(SOME_CONSTANT)` / `@RequestMapping(SOME_CONSTANT)` prefixes that
    # reference a constant declared in a *different* file (the extremely
    # common "shared ApiUrlPatterns/constants class + static import" pattern).
    # Without this, every controller using that pattern silently produced
    # ZERO route entities — see iter-15.18/15.19 postmortem.
    global_constants: Dict[str, str] = {}
    for f in files:
        path = f.get("path", "")
        if _filetype_from_path(path) != "java":
            continue
        try:
            global_constants.update(_collect_string_constants(f.get("content", "") or ""))
        except Exception:
            continue

    for f in files:
        content = f.get("content", "") or ""
        path = f.get("path", "")
        ftype = _filetype_from_path(path)
        if ftype == "unknown":
            continue
        try:
            file_ents = extract(ftype, content, path, global_constants=global_constants) if ftype == "java" else extract(ftype, content, path)
        except Exception:
            continue
        for e in file_ents:
            e.setdefault("source_file", path)
        entities.extend(file_ents)
    # Cross-link routes to methods (enriches routes with handler class + return type)
    try:
        entities = enrich_routes(entities)
    except Exception:
        pass
    return entities


# ─── Requirement extraction ───────────────────────────────────────────────

def extract_doc_requirements(doc_files: List[Dict]) -> List[Dict]:
    """Extract structured requirements from documentation files.

    Combines two strategies:
      1. Explicit requirement IDs (FR-XXX-001, NFR-001, US-001, etc.) —
         canonical requirements with formal IDs.
      2. "shall/must/should" imperative sentences — informal requirements
         auto-assigned IMP-### IDs.
    """
    reqs: List[Dict] = []
    seen_ids: set = set()

    for f in doc_files:
        path = f.get("path", "")
        content = f.get("content", "") or ""
        # iter-15.18 — carry per-file doc type through into each requirement
        doc_type = f.get("doc_type", "srs")
        doc_type_label = f.get("doc_type_label", "")

        # 1. Explicit IDs — iter-15.7: collect ALL hits per rid so we can
        # choose the BEST snippet (usually the one whose sentence contains
        # both the ID and a shall/must/should verb) instead of blindly
        # taking the first hit (which is often a RACI-matrix or ToC row).
        hits_by_rid: Dict[str, List[re.Match]] = {}
        for m in _REQ_ID_RE.finditer(content):
            rid = m.group(1).upper().replace(" ", "-").replace("_", "-")
            if rid.startswith(("SECTION-", "SECTIONS-", "TOC-", "APPENDIX-")):
                continue
            hits_by_rid.setdefault(rid, []).append(m)

        for rid, hits in hits_by_rid.items():
            if rid in seen_ids:
                continue
            # Rank each hit — prefer snippets that contain the ID AND a
            # shall/must/should verb; penalise junky windows.
            best: Optional[str] = None
            best_score = -1
            for m in hits:
                snippet = _pick_requirement_text(content, rid, m.start(), m.end())
                lc = snippet.lower()
                if any(mk in lc for mk in _REQ_TEXT_JUNK_MARKERS):
                    continue
                score = 0
                if rid.upper() in snippet.upper():
                    score += 2
                if re.search(r"\b(shall|must|should)\b", snippet, re.I):
                    score += 2
                if 40 <= len(snippet) <= 400:
                    score += 1
                if score > best_score:
                    best_score = score
                    best = snippet
            if not best:
                continue
            seen_ids.add(rid)
            reqs.append({
                "id": rid,
                "type": "EXPLICIT",
                "text": best[:400],
                "source": path,
                "doc_type": doc_type,
                "doc_type_label": doc_type_label,
                "category": _classify_requirement(rid, best),
            })

        # 2. Implicit shall/must
        if len(reqs) < 500:
            for m in _SHALL_RE.finditer(content):
                txt = re.sub(r"\s+", " ", m.group(1)).strip()
                if len(txt) < 15:
                    continue
                imp_id = f"IMP-{len(reqs) + 1:03d}"
                reqs.append({
                    "id": imp_id,
                    "type": "IMPLICIT",
                    "text": txt[:400],
                    "source": path,
                    "doc_type": doc_type,
                    "doc_type_label": doc_type_label,
                    "category": _classify_requirement(imp_id, txt),
                })
                if len(reqs) >= 500:
                    break

    return reqs[:500]


def _classify_requirement(rid: str, text: str) -> str:
    """Rough categorization for the KB UI to group by."""
    lc = (rid + " " + text).lower()
    if any(k in lc for k in ("security", "auth", "login", "password", "role", "permission")):
        return "security"
    if any(k in lc for k in ("perf", "throughput", "latency", "response time")):
        return "performance"
    if any(k in lc for k in ("ui", "display", "screen", "page", "form", "button", "layout")):
        return "ui"
    if any(k in lc for k in ("data", "database", "table", "field", "column", "record")):
        return "data"
    if any(k in lc for k in ("api", "endpoint", "route", "service")):
        return "api"
    return "functional"


# ─── UI → API → DB traceability map ───────────────────────────────────────

def _norm_route(route: str) -> str:
    if not route:
        return ""
    r = route.strip().lower()
    r = re.sub(r"\{[^}]+\}", "{}", r)  # {id} → {}
    r = re.sub(r":\w+", "{}", r)
    r = r.rstrip("/")
    return r


def _extract_api_refs_from_ui(content: str) -> List[str]:
    """Find API URL patterns referenced in UI code."""
    if not content:
        return []
    refs: set = set()
    # fetch('/api/...'), axios.get('/api/...'), url: "/api/..."
    for m in re.finditer(r"""(?:fetch|axios(?:\.\w+)?|url|href|action)\s*[(:=]\s*[`'"]([/][^`'"?\s]{1,200})[`'"]""", content):
        refs.add(m.group(1))
    # Angular / React interpolated: `${API}/users` — capture the tail
    for m in re.finditer(r"""[`'"]/(api|rest|v\d)/[^`'"?\s]{1,200}[`'"]""", content):
        refs.add("/" + m.group(0).strip("`'\""))
    return sorted(refs)[:50]


def _extract_db_refs_from_method(entity: Dict, all_entities: List[Dict]) -> List[str]:
    """Find table references inside a method's body content."""
    body = entity.get("body") or entity.get("content") or entity.get("snippet") or ""
    if not body:
        return []
    # iter-15.18 — TABLE_HINT is what owl_extractor emits for JPA/ORM
    # `@Table(name=...)`-style annotations (Java/`.NET`/etc source with no
    # separate SQL DDL file). Previously only literal SQL "TABLE" entities
    # were counted here, so Java-only uploads always reported 0 DB tables.
    tables = {e.get("name", "").lower() for e in all_entities if e.get("type") in ("TABLE", "TABLE_HINT") and e.get("name")}
    if not tables:
        return []
    refs: set = set()
    for tbl in tables:
        if re.search(rf"\b(?:from|join|update|into|table)\s+`?{re.escape(tbl)}`?\b", body, re.IGNORECASE):
            refs.add(tbl)
    return sorted(refs)


# ─── iter-15.26 — Cross-file API→Service→Repository→DB full trace ─────────
#
# `_extract_db_refs_from_method` above only ever finds tables when a ROUTE
# entity itself carries a `body`/`content`/`snippet` field — which
# owl_extractor's Java ROUTE entities never do (the controller method body
# isn't captured, only its signature). Real business logic (and therefore
# the actual DB table usage) lives one or two classes downstream: the
# controller injects a Service/UseCase, which injects a Repository/Dao,
# which is the class that actually touches the database. This was the
# root cause of "DB Tables: None detected" on every single envelope even
# for a real, well-formed codebase — the deterministic pass never looked
# past the controller's own (table-free) file.
#
# The helpers below build three lightweight file-scoped indexes purely
# from entities owl_extractor ALREADY emits (CLASS.methods[].tables,
# CDI_INJECTION.injected_type, TABLE_HINT.name) and use them to walk
# Controller-file → Service-file → Repository-file, unioning whatever
# tables are found at each hop. This is still a static, per-file
# heuristic (not a real call graph), but it is a huge step up from never
# looking beyond the controller's own file, and it is 100% stack-agnostic
# — nothing here is hardcoded to any class/table name.

# iter-15.26 — The legacy inline-SQL fallback regex in owl_extractor's
# CLASS/method table scan (`JAVA_SQL_INLINE_RE`) is prone to picking up
# SQL keywords/clause fragments as if they were table names (e.g. "count",
# "set", "distinct") when a query doesn't match one of the more precise
# FROM/INTO/UPDATE/JOIN regexes. This was effectively dead/unused before
# this iteration (nothing consumed `CLASS.methods[].tables` cross-file),
# so the noise was invisible; now that it feeds real DB-trace envelopes,
# filter it before it reaches the UI. "dual" is intentionally NOT in this
# list — it's a real, extremely common Oracle pseudo-table.
_SQL_TABLE_NAME_DENYLIST = {
    "count", "set", "distinct", "select", "where", "group", "order", "by",
    "from", "into", "update", "join", "table", "values", "as", "and", "or",
    "null", "not", "in", "on", "left", "right", "inner", "outer", "having",
    "limit", "offset", "case", "when", "then", "else", "end",
}


def _build_class_index(entities: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """simple class name -> {file, tables} where `tables` is the union of
    every table referenced by any method body owl_extractor scanned for
    that class."""
    index: Dict[str, Dict[str, Any]] = {}
    for e in entities:
        if e.get("type") != "CLASS":
            continue
        name = e.get("name") or ""
        if not name:
            continue
        tables: set = set()
        for m in (e.get("methods") or []):
            tables.update(
                t.lower() for t in (m.get("tables") or [])
                if t.lower() not in _SQL_TABLE_NAME_DENYLIST
            )
        entry = index.setdefault(name, {"file": e.get("source_file", ""), "tables": set()})
        entry["tables"].update(tables)
        if not entry["file"]:
            entry["file"] = e.get("source_file", "")
    return index


def _build_injection_index(entities: List[Dict]) -> Dict[str, List[str]]:
    """source_file -> [simple injected type names], deduped, in first-seen
    order. Reused for both service-layer and repository-layer resolution."""
    index: Dict[str, List[str]] = {}
    for e in entities:
        if e.get("type") != "CDI_INJECTION":
            continue
        src = e.get("source_file") or e.get("source") or ""
        itype = (e.get("injected_type") or "").strip()
        if not (src and itype):
            continue
        simple = itype.rsplit(".", 1)[-1].rstrip("<>").split("<")[0]
        bucket = index.setdefault(src, [])
        if simple not in bucket:
            bucket.append(simple)
    return index


def _build_file_table_index(entities: List[Dict]) -> Dict[str, set]:
    """source_file -> {table names} from TABLE_HINT/TABLE entities found
    anywhere in that file (JPA @Table annotations, inline SQL DDL, etc.) —
    file-scoped rather than class-scoped since an @Entity class and its
    repository/DAO are frequently in separate files but the same package."""
    index: Dict[str, set] = {}
    for e in entities:
        if e.get("type") not in ("TABLE", "TABLE_HINT"):
            continue
        name = (e.get("name") or "").lower()
        src = e.get("source_file") or e.get("source") or ""
        if name and src:
            index.setdefault(src, set()).add(name)
    return index


def _build_implements_index(entities: List[Dict]) -> Dict[str, str]:
    """interface/abstract-class simple name -> first concrete class name
    found implementing/extending it. Needed because the CDI-injected type
    on a controller is almost always the INTERFACE (`VehicleHiringUseCase`),
    but the interface's own file never contains the repository injections
    that do the actual DB work — only the implementation class
    (`VehicleHiringService implements VehicleHiringUseCase`) does."""
    index: Dict[str, str] = {}
    for e in entities:
        if e.get("type") != "CLASS":
            continue
        cls_name = e.get("name") or ""
        for iface in (e.get("implements") or []):
            simple = iface.rsplit(".", 1)[-1].strip()
            if simple and simple not in index:
                index[simple] = cls_name
    return index


def _pick_layer_class(candidates: List[str], suffixes: tuple) -> str:
    for c in candidates:
        if c.endswith(suffixes):
            return c
    return ""


def _resolve_full_trace(
    route: Dict[str, Any],
    class_index: Dict[str, Dict[str, Any]],
    injection_index: Dict[str, List[str]],
    file_table_index: Dict[str, set],
    implements_index: Dict[str, str],
) -> Dict[str, Any]:
    """Walk Controller-file -> Service(-impl)-file -> Repository-file for
    one inbound ROUTE, unioning every table found along the way. Returns a
    dict with service/repository class+file, the resolved table set, and
    a `table_trace` breakdown (one entry per layer actually resolved) so
    the UI can show the reviewer exactly where each table came from
    instead of a single opaque list."""
    controller_file = route.get("source_file", "")
    trace: List[Dict[str, Any]] = []
    tables: set = set()

    own_tables = file_table_index.get(controller_file, set())
    if own_tables:
        trace.append({"layer": "controller", "class": route.get("class", ""), "file": controller_file, "tables": sorted(own_tables)})
        tables.update(own_tables)

    # `service_class` is what the controller sees (usually an interface,
    # e.g. an `@UseCase`/`Service` type). `service_impl_class` — resolved
    # via `implements_index` — is the concrete class that actually holds
    # the repository injections; that impl's FILE (not the interface's)
    # is what we search for the next hop.
    service_class = _pick_layer_class(
        injection_index.get(controller_file, []), ("Service", "UseCase", "Facade", "Manager")
    )
    service_iface_file = class_index.get(service_class, {}).get("file", "") if service_class else ""
    service_impl_class = implements_index.get(service_class, "") if service_class else ""
    service_impl_file = class_index.get(service_impl_class, {}).get("file", "") if service_impl_class else ""
    # Prefer the impl's file for injection-lookup purposes; fall back to
    # the interface's own file (some codebases put logic directly there).
    service_lookup_file = service_impl_file or service_iface_file

    repository_class = ""
    repository_file = ""
    if service_lookup_file:
        repository_iface = _pick_layer_class(
            injection_index.get(service_lookup_file, []), ("Repository", "Dao", "Mapper")
        )
        if not repository_iface:
            # Some services inject the repository under a name that
            # doesn't match the suffix heuristic — fall back to any
            # injected type whose OWN file (or impl's file) has table hits.
            for cand in injection_index.get(service_lookup_file, []):
                cf = class_index.get(cand, {}).get("file", "")
                impl_cf = class_index.get(implements_index.get(cand, ""), {}).get("file", "")
                if (cf and file_table_index.get(cf)) or (impl_cf and file_table_index.get(impl_cf)):
                    repository_iface = cand
                    break
        # iter-15.26 — Repository is frequently ALSO just an interface
        # (`ProcurementRepository`) with a separate `*Impl` doing the real
        # JPA/SQL work and carrying the @Table-annotated entity reference.
        # Same interface→impl resolution as the service layer above.
        repository_impl = implements_index.get(repository_iface, "") if repository_iface else ""
        repository_class = repository_impl or repository_iface
        repository_iface_file = class_index.get(repository_iface, {}).get("file", "") if repository_iface else ""
        repository_impl_file = class_index.get(repository_impl, {}).get("file", "") if repository_impl else ""
        repository_file = repository_impl_file or repository_iface_file

    if service_lookup_file:
        svc_tables = (
            file_table_index.get(service_lookup_file, set())
            | class_index.get(service_impl_class or service_class, {}).get("tables", set())
        )
        if svc_tables:
            trace.append({
                "layer": "service",
                "class": service_impl_class or service_class,
                "file": service_lookup_file,
                "tables": sorted(svc_tables),
            })
            tables.update(svc_tables)

    if repository_file:
        repo_tables = file_table_index.get(repository_file, set()) | class_index.get(repository_class, {}).get("tables", set())
        if repo_tables:
            trace.append({"layer": "repository", "class": repository_class, "file": repository_file, "tables": sorted(repo_tables)})
            tables.update(repo_tables)

    return {
        "service_class": service_impl_class or service_class,
        "service_interface": service_class if service_impl_class else "",
        "service_file": service_lookup_file,
        "repository_class": repository_class,
        "repository_file": repository_file,
        "tables": sorted(tables),
        "table_trace": trace,
    }


def build_traceability_map(entities: List[Dict], code_files: List[Dict]) -> Dict[str, Any]:
    """Cross-reference UI ↔ API ↔ DB.

    Returns:
      {
        "ui_to_api":   [{ui, api_refs}],
        "api_to_db":   [{api, method, class, tables}],
        "orphaned_ui": [ui_files with no api refs],
        "orphaned_api":[routes with no ui caller],
        "orphaned_db": [tables with no api reader],
        "chains":      [{ui, api, tables}],   # fully resolved traces
      }
    """
    ui_to_api: List[Dict] = []
    for f in code_files:
        ext = os.path.splitext(f.get("path", ""))[1].lower()
        if ext not in _UI_EXT:
            continue
        api_refs = _extract_api_refs_from_ui(f.get("content", ""))
        if api_refs:
            ui_to_api.append({
                "ui": f["path"],
                "api_refs": api_refs,
            })

    # API → DB via method bodies
    api_to_db: List[Dict] = []
    routes = [e for e in entities if e.get("type") in _API_ENTITY_TYPES]
    # iter-15.26 — Build the cross-file indexes ONCE, not per-route.
    class_index = _build_class_index(entities)
    injection_index = _build_injection_index(entities)
    file_table_index = _build_file_table_index(entities)
    implements_index = _build_implements_index(entities)
    for r in routes:
        own_tables = _extract_db_refs_from_method(r, entities)
        full_trace = _resolve_full_trace(
            {**r, "class": r.get("class") or r.get("controller") or r.get("handler_class", "")},
            class_index, injection_index, file_table_index, implements_index,
        )
        tables = sorted(set(own_tables) | set(full_trace["tables"]))
        api_to_db.append({
            "api": r.get("path") or r.get("name", ""),
            "method": r.get("http_method") or r.get("verb") or r.get("name", ""),
            # iter-15.20 — owl_extractor's Java ROUTE entities carry the
            # controller/resource class under `handler_class`, not `class`
            # or `controller`. Those two keys are basically always absent
            # for Java uploads, so the "Controller" column in the envelope
            # UI showed "-" for every single row despite the class being
            # known. Fall back to `handler_class` so it's actually populated.
            "class": r.get("class") or r.get("controller") or r.get("handler_class", ""),
            "source_file": r.get("source_file", ""),
            "tables": tables,
            # iter-15.26 — Full Controller→Service→Repository→DB trace,
            # resolved cross-file via CDI-injection + class-name indexes
            # (see _resolve_full_trace above). `table_trace` breaks down
            # exactly which layer/file each table came from so the UI can
            # show real provenance instead of one opaque list.
            "service_class": full_trace["service_class"],
            "service_interface": full_trace["service_interface"],
            "service_file": full_trace["service_file"],
            "repository_class": full_trace["repository_class"],
            "repository_file": full_trace["repository_file"],
            "table_trace": full_trace["table_trace"],
            "request_body_class": r.get("request_body_class", ""),
            "response_body_class": r.get("response_body_class", ""),
            "framework": r.get("framework", ""),
            # iter-15.20 — Outbound REST-CLIENT interface (this service
            # CALLING another service) vs. an inbound resource this service
            # EXPOSES. See JAXRS_CLIENT_MARKER_RE in owl_extractor.py.
            "is_outbound_client": bool(r.get("is_outbound_client")),
        })

    # Fully resolved chains
    api_index: Dict[str, Dict] = {}
    for row in api_to_db:
        key = _norm_route(row["api"])
        if key:
            api_index[key] = row

    chains: List[Dict] = []
    for row in ui_to_api:
        for ref in row["api_refs"]:
            key = _norm_route(ref)
            match = None
            # Try direct match, then substring match (route may have prefix)
            if key in api_index:
                match = api_index[key]
            else:
                for k, v in api_index.items():
                    if key.endswith(k) or k.endswith(key):
                        match = v
                        break
            chains.append({
                "ui": row["ui"],
                "api_ref": ref,
                "api_resolved": match["api"] if match else None,
                "http_method": match["method"] if match else None,
                "tables": match["tables"] if match else [],
                "resolved": match is not None,
            })

    # Orphans
    used_apis = {_norm_route(c["api_resolved"]) for c in chains if c["resolved"]}
    orphaned_api = [r["api"] for r in api_to_db if _norm_route(r["api"]) not in used_apis]

    used_tables: set = set()
    for r in api_to_db:
        used_tables.update(r["tables"])
    all_tables = {e.get("name", "").lower() for e in entities if e.get("type") in ("TABLE", "TABLE_HINT")}
    orphaned_db = sorted(all_tables - used_tables)

    ui_with_calls = {r["ui"] for r in ui_to_api}
    all_ui = {f["path"] for f in code_files if os.path.splitext(f.get("path", ""))[1].lower() in _UI_EXT}
    orphaned_ui = sorted(all_ui - ui_with_calls)

    # iter-15.20 — Split inbound resources (this service's own API surface)
    # from outbound REST-CLIENT interfaces (calls this service makes to
    # OTHER services). Both are useful to a migration reviewer, but they
    # must not be conflated in the "how many APIs does this service have"
    # count — that was the root cause of the Transformer's "Discovered
    # Architecture" step showing only a handful of client-stub methods
    # while silently dropping every real controller endpoint.
    inbound_api_to_db = [r for r in api_to_db if not r["is_outbound_client"]]
    outbound_api_to_db = [r for r in api_to_db if r["is_outbound_client"]]

    return {
        "ui_to_api": ui_to_api,
        "api_to_db": api_to_db,
        "outbound_clients": outbound_api_to_db,
        "chains": chains,
        "orphaned_ui": orphaned_ui[:50],
        "orphaned_api": orphaned_api[:50],
        "orphaned_db": orphaned_db[:50],
        "stats": {
            "ui_files": len(all_ui),
            "api_routes": len(inbound_api_to_db),
            "outbound_client_calls": len(outbound_api_to_db),
            "db_tables": len(all_tables),
            "resolved_chains": sum(1 for c in chains if c["resolved"]),
            "unresolved_chains": sum(1 for c in chains if not c["resolved"]),
            "orphaned_ui": len(orphaned_ui),
            "orphaned_api": len(orphaned_api),
            "orphaned_db": len(orphaned_db),
        },
    }


# ─── Tech stack fingerprint (light-weight; heavy detection lives in tools.py) ───

def light_tech_summary(entities: List[Dict], code_files: List[Dict]) -> Dict[str, Any]:
    """Produce a small dict summarising detected tech from entities + file exts."""
    exts: Dict[str, int] = {}
    for f in code_files:
        e = os.path.splitext(f.get("path", ""))[1].lower()
        if e:
            exts[e] = exts.get(e, 0) + 1
    counts = aggregate_stats(entities) if entities else {}
    return {
        "extensions": dict(sorted(exts.items(), key=lambda x: -x[1])[:20]),
        "entity_counts": counts,
        "top_types": sorted(counts.items(), key=lambda x: -x[1])[:8],
    }


# ─── Public API ───────────────────────────────────────────────────────────

def build_tools_kb(
    code_files: List[Dict],
    doc_files: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Build a compact knowledge base from uploaded code + docs.

    Returns a dict with these top-level keys:
      - entities:           List[Dict]  — all extracted code entities
      - requirements:       List[Dict]  — extracted doc requirements (may be [])
      - traceability:       Dict        — UI→API→DB map (see build_traceability_map)
      - tech_summary:       Dict        — extensions + entity counts
      - stats:              Dict        — headline counts for the UI
      - file_index:         List[Dict]  — {path, size, filetype} for UI file browser
    """
    code_files = code_files or []
    doc_files = doc_files or []

    entities = extract_code_entities(code_files)
    requirements = extract_doc_requirements(doc_files) if doc_files else []
    traceability = build_traceability_map(entities, code_files)
    tech_summary = light_tech_summary(entities, code_files)

    file_index = [
        {
            "path": f.get("path", ""),
            "size": f.get("size") or len(f.get("content", "") or ""),
            "filetype": _filetype_from_path(f.get("path", "")),
            "kind": "ui" if os.path.splitext(f.get("path", ""))[1].lower() in _UI_EXT else "code",
        }
        for f in code_files
    ][:500]

    stats = {
        "code_files": len(code_files),
        "doc_files": len(doc_files),
        "entities": len(entities),
        "requirements": len(requirements),
        **traceability["stats"],
    }

    # iter-14.94 — Backend-signal diagnostic. If the uploaded code lacks any
    # backend entities (routes/tables/columns), the analyzer cannot honestly
    # verify docs → code coverage. This flag lets the report layer degrade
    # gracefully instead of returning a fake "100% covered".
    backend_types = {"ROUTE", "ENDPOINT", "METHOD", "HANDLER", "TABLE", "COLUMN", "VIEW_DB"}
    backend_entities = [e for e in entities if e.get("type", "").upper() in backend_types]
    # UI-only entities do not count as backend signal
    stats["backend_entities"] = len(backend_entities)
    stats["has_backend_code"] = (
        len(backend_entities) > 0
        and (stats.get("db_tables", 0) > 0 or stats.get("api_routes", 0) > 0)
        and stats.get("resolved_chains", 0) + stats.get("db_tables", 0) > 0
    )
    # "UI-only" means we found UI files & entities but no server-side routes/tables
    stats["is_ui_only"] = (
        stats.get("ui_files", 0) > 0
        and stats.get("db_tables", 0) == 0
        and len(backend_entities) == 0
    )

    return {
        "entities": entities,
        "requirements": requirements,
        "traceability": traceability,
        "tech_summary": tech_summary,
        "stats": stats,
        "file_index": file_index,
    }


# ─── LLM prompt renderer ──────────────────────────────────────────────────

def render_kb_for_prompt(kb: Dict[str, Any], max_chars: int = 40000) -> str:
    """Render the KB into an LLM-friendly context string.

    Prioritises: stats → requirements → resolved UI→API→DB chains →
    grouped entity listing → orphans. Truncates at max_chars.
    """
    parts: List[str] = []
    stats = kb.get("stats", {})
    parts.append(
        "## KB STATS\n" +
        "\n".join(f"- {k}: {v}" for k, v in stats.items())
    )

    reqs = kb.get("requirements", []) or []
    if reqs:
        parts.append(
            "\n## DOCUMENTED REQUIREMENTS ({} total)\n".format(len(reqs)) +
            "\n".join(f"- [{r['id']} | {r['category']}] {r['text']} (source: {r['source']})"
                      for r in reqs[:250])
        )

    trace = kb.get("traceability", {})

    # iter-15.20 — Explicit, unambiguous ground truth for what this
    # service's OWN API surface is vs. what it merely CALLS on other
    # services. Placed early/prominently because LLMs default to treating
    # any @Path/@GetMapping-annotated interface as "an API of this
    # service", which is wrong for REST-CLIENT stubs (MicroProfile Rest
    # Client / OpenFeign) and was the direct cause of the Transformer
    # showing a handful of outbound client calls instead of the service's
    # real, much larger set of inbound endpoints.
    inbound_rows = [r for r in (trace.get("api_to_db") or []) if not r.get("is_outbound_client")]
    if inbound_rows:
        parts.append(
            "\n## INBOUND API ENDPOINTS — this service's OWN exposed API surface ({} total)\n".format(len(inbound_rows)) +
            "\n".join(
                f"- {r.get('method') or 'ANY'} {r.get('api')}  (class: {r.get('class') or '?'}, "
                f"file: {r.get('source_file') or '?'}, tables: [{', '.join(r.get('tables') or []) or 'none'}])"
                for r in inbound_rows[:200]
            ) +
            (f"\n  ... and {len(inbound_rows) - 200} more" if len(inbound_rows) > 200 else "")
        )
    outbound_rows = trace.get("outbound_clients") or []
    if outbound_rows:
        parts.append(
            "\n## OUTBOUND REST-CLIENT CALLS — NOT this service's own API; these are calls "
            "this service MAKES to other services ({} total). Do NOT list these as this "
            "service's endpoints — capture them under `external_calls` on whichever inbound "
            "endpoint invokes them instead.\n".format(len(outbound_rows)) +
            "\n".join(
                f"- {r.get('method') or 'ANY'} {r.get('api')}  (client interface: {r.get('class') or '?'}, "
                f"file: {r.get('source_file') or '?'})"
                for r in outbound_rows[:100]
            )
        )

    chains = trace.get("chains", []) or []
    resolved = [c for c in chains if c["resolved"]]
    if resolved:
        parts.append(
            "\n## RESOLVED UI → API → DB CHAINS ({} of {})\n".format(len(resolved), len(chains)) +
            "\n".join(
                f"- {c['ui']}  →  {c['http_method'] or 'ANY'} {c['api_resolved']}  →  tables: [{', '.join(c['tables']) or 'none'}]"
                for c in resolved[:150]
            )
        )
    unresolved = [c for c in chains if not c["resolved"]]
    if unresolved:
        parts.append(
            "\n## UNRESOLVED UI → API REFERENCES ({})\n".format(len(unresolved)) +
            "\n".join(f"- {c['ui']} calls {c['api_ref']} (no matching backend route)"
                      for c in unresolved[:80])
        )

    # Group entities by type
    by_type: Dict[str, List[str]] = {}
    for e in kb.get("entities", []) or []:
        et = e.get("type", "OTHER")
        name = e.get("name") or e.get("path") or ""
        src = e.get("source_file", "")
        by_type.setdefault(et, []).append(f"{name} @ {src}" if src else name)
    for et in sorted(by_type):
        items = by_type[et]
        parts.append(
            f"\n## CODE ENTITIES — {et} ({len(items)})\n" +
            "\n".join(f"- {x}" for x in items[:120]) +
            (f"\n  ... and {len(items) - 120} more" if len(items) > 120 else "")
        )

    orphans = []
    if trace.get("orphaned_api"):
        orphans.append("### API routes with no UI caller\n" +
                       "\n".join(f"- {x}" for x in trace["orphaned_api"][:30]))
    if trace.get("orphaned_db"):
        orphans.append("### DB tables with no API reader\n" +
                       "\n".join(f"- {x}" for x in trace["orphaned_db"][:30]))
    if trace.get("orphaned_ui"):
        orphans.append("### UI files with no API calls\n" +
                       "\n".join(f"- {x}" for x in trace["orphaned_ui"][:30]))
    if orphans:
        parts.append("\n## ORPHANS (potential dead code or missing wiring)\n" + "\n\n".join(orphans))

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [KB truncated at {max_chars} chars]"
    return text


def summarize_kb_for_ui(kb: Dict[str, Any]) -> Dict[str, Any]:
    """Compact summary suitable for the frontend `/kb` endpoint.

    Excludes full entity content to keep payload small (< 200KB).
    """
    trace = kb.get("traceability", {})
    return {
        "stats": kb.get("stats", {}),
        "tech_summary": kb.get("tech_summary", {}),
        "requirements": [
            {"id": r["id"], "type": r["type"], "category": r["category"],
             "text": r["text"][:300], "source": r["source"]}
            for r in (kb.get("requirements") or [])[:200]
        ],
        "chains": trace.get("chains", [])[:200],
        "outbound_clients": trace.get("outbound_clients", [])[:100],
        "orphaned_ui": trace.get("orphaned_ui", [])[:30],
        "orphaned_api": trace.get("orphaned_api", [])[:30],
        "orphaned_db": trace.get("orphaned_db", [])[:30],
        "entity_sample": [
            {"type": e.get("type"), "name": e.get("name"),
             "source_file": e.get("source_file")}
            for e in (kb.get("entities") or [])[:200]
        ],
        "file_index": kb.get("file_index", [])[:200],
    }
