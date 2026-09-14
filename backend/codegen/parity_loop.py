"""Parity-confidence scoring + iterative recovery loop for LAMA CodeGen.

iter-13.119 — automated validation + improvement loop introduced per the
"95% threshold" PRD ask.

LAMA cannot execute the user's generated Spring Boot / FastAPI / etc.
code inside its own container (no JVM, no test DB, no service mesh). So
"continuous testing" maps to **static parity scoring** — the same checks
a senior reviewer would run by hand:

  • STRUCTURAL  — file has the correct framework markers for its type
                  (@RestController for Controller, @Entity for Entity, …)
  • PARITY      — no TODO carpets / Unsupported throws / scaffold headers
                  (signals the LLM produced real code, not a fallback)
  • COVERAGE    — every legacy endpoint maps to a Controller method,
                  every legacy table maps to an @Entity
  • SCHEMA      — every DDL column appears in the Entity with @Column
  • EVIDENCE    — file cites legacy class / method names via // LEGACY:
                  comments (proves the LLM consulted the KB)
  • REQUIREMENT — file references SRS BR-* / UC-* / NFR-* identifiers
                  (proves the LLM bound to frozen requirements)

Each file is scored 0-100; service score is the file-weighted mean;
run score is the service-weighted mean. Files below the threshold are
fed back into `_gap_recover_one_file` to enrich. The loop repeats up
to `max_iterations` or until `run_score >= threshold` (default 95).

This module is import-safe — no circular dependencies on
`routes/codegen.py`. The orchestrator there imports `score_run`,
`select_recovery_targets`, and calls the existing gap-recovery infra
file-by-file.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from db import (
    codegen_files,
    arch_services,
    stage_context as stage_context_col,
    kb_entities,
)

logger = logging.getLogger("lama.codegen.parity_loop")


# ────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────

@dataclass
class ComponentScore:
    """One scoring axis result for one file."""
    name: str
    score: float            # 0..100
    weight: float           # 0..1 (sum across components = 1.0)
    detail: str = ""        # short human-readable reason


@dataclass
class FileConfidence:
    file_path: str
    file_type: str
    service: str
    score: float            # weighted average of components
    components: List[ComponentScore]
    issues: List[str]
    recoverable: bool       # is this file a candidate for Gap Recovery?

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "file_type": self.file_type,
            "service": self.service,
            "score": round(self.score, 2),
            "issues": self.issues,
            "recoverable": self.recoverable,
            "components": [
                {"name": c.name, "score": round(c.score, 2),
                 "weight": c.weight, "detail": c.detail}
                for c in self.components
            ],
        }


@dataclass
class ServiceConfidence:
    service: str
    score: float
    file_count: int
    files: List[FileConfidence]
    endpoint_coverage_pct: float
    table_coverage_pct: float
    column_coverage_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "score": round(self.score, 2),
            "file_count": self.file_count,
            "endpoint_coverage_pct": round(self.endpoint_coverage_pct, 2),
            "table_coverage_pct": round(self.table_coverage_pct, 2),
            "column_coverage_pct": round(self.column_coverage_pct, 2),
            "files": [f.to_dict() for f in self.files],
        }


@dataclass
class RunConfidence:
    project_id: str
    iteration: int
    overall_score: float
    threshold: float
    services: List[ServiceConfidence]
    files_below_threshold: List[Dict[str, Any]]  # [{file_path, service, score}]
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "iteration": self.iteration,
            "overall_score": round(self.overall_score, 2),
            "threshold": self.threshold,
            "summary": self.summary,
            "services": [s.to_dict() for s in self.services],
            "files_below_threshold": self.files_below_threshold,
        }


# ──────────────────────────────────────────────────��─────────────────
# Component weights — sum MUST equal 1.0
# ────────────────────────────────────────────────────────────────────

COMPONENT_WEIGHTS = {
    "structural":  0.10,
    "parity":      0.15,   # iter-14.21 — was 0.20; freed 0.05 for semantic
    "coverage":    0.25,
    "schema":      0.15,
    "evidence":    0.10,   # iter-14.21 — was 0.15; freed 0.05 for semantic
    "requirement": 0.15,
    # iter-14.21 — HF-backed semantic similarity vs legacy corpus. When
    # HF is unavailable OR the legacy corpus is empty, the score is
    # pass-through (100) so this axis never drags a file below
    # threshold; when HF *is* available it rewards migrations that stay
    # faithful to legacy behaviour AND penalises invented scaffolds
    # (which score much lower against real PHP/JSP source than a real
    # migration does).
    "semantic":    0.10,
}
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9


# ───────────────────────────────��────────────────────────────────────
# Pattern banks (kept centralised for unit-testability)
# ────────────────────────────────────────────────────────────────────

# Scaffold-emitter headers — presence means LLM failed and fallback fired.
_SCAFFOLD_HEADERS = (
    "// LAMA: deterministic scaffold",
    "// LAMA: emergency scaffold",
    "// LAMA: quality-gate scaffold",
    "// LAMA scaffold",
    "# LAMA scaffold",
    "/* LAMA scaffold",
)

# TODO-carpet markers — LLM emitted shell code instead of real logic.
# iter-14.35 — extended to catch the "Placeholder for business logic",
# "Your code here", "Implementation goes here", "Business logic here",
# "Add implementation", "Insert logic here" family of lazy stubs that
# LLMs slip past the earlier `TODO: backfill` filter. If any of these
# appear the file is treated as unfinished and downgraded to scaffold.
_TODO_CARPET = (
    # Original iter-13 markers
    "TODO: backfill from legacy KB",
    "throw new UnsupportedOperationException",
    "raise NotImplementedError",
    "panic(\"unimpl",
    "throw new NotImplementedException",
    "// TODO: backfill",
    "# TODO: backfill",
    "// TODO backfill",
    # iter-14.35 — generic "I'll do it later" idioms
    "Placeholder for business logic",
    "Placeholder for the business logic",
    "placeholder for business logic",
    "// Placeholder",
    "# Placeholder",
    "/* Placeholder",
    "// Your code here",
    "# Your code here",
    "// Your logic here",
    "# Your logic here",
    "// Implementation here",
    "# Implementation here",
    "// Implementation goes here",
    "# Implementation goes here",
    "// Business logic goes here",
    "# Business logic goes here",
    "// Business logic here",
    "# Business logic here",
    "// Add implementation",
    "# Add implementation",
    "// Add business logic",
    "# Add business logic",
    "// Insert logic",
    "# Insert logic",
    "// Fill in",
    "# Fill in",
    "// stub",
    "# stub",
    "pass  # stub",
    "// FIXME: implement",
    "# FIXME: implement",
)

# Required framework markers per file type (Java first; other langs
# fall back to a lenient "has a class/function" check).
_REQUIRED_MARKERS_JAVA = {
    "controller":  ("@RestController", "@RequestMapping"),
    "service":     ("@Service",),
    "entity":      ("@Entity", "@Table"),
    "repository":  ("JpaRepository",),
    "dto":         ("public record", "public class"),  # at least one
    "mapper":      ("@Component",),
    "validator":   ("@Component",),
    "exception":   ("extends RuntimeException", "extends Exception"),
    "test":        ("@Test", "@SpringBootTest"),
    "security":    ("SecurityFilterChain", "@Configuration"),
    "errors":      ("@RestControllerAdvice", "public record ErrorResponse"),
    "bootstrap":   ("@SpringBootApplication", "SpringApplication.run"),
}

# Files we score; others (configs, dockerfile, README, compose) get pass-through.
# iter-14.43 — added FE file_types so React/TSX pages are scored too. Before
# this fix, the CodeGen confidence popover always showed Frontend = 0 because
# _SCORED_TYPES only listed Java backend types.
_SCORED_TYPES = frozenset({
    "controller", "service", "entity", "repository", "dto", "mapper",
    "validator", "exception", "test", "security", "errors", "bootstrap",
    # ── Frontend (iter-14.43) ────────────────────────────────────────
    "page", "component", "route", "hook", "style", "layout", "store",
    "api_client", "guard",
})

# iter-14.43 — Frontend types get pass-through 100 on axes that only make
# sense for backend code (coverage vs OpenAPI paths, schema vs @Table).
_FRONTEND_TYPES = frozenset({
    "page", "component", "route", "hook", "style", "layout", "store",
    "api_client", "guard",
})

# Files Gap Recovery can meaningfully fix (the codegen.gap_recovery prompt
# is tuned for source code — configs are out of scope).
_RECOVERABLE_TYPES = frozenset({
    "controller", "service", "entity", "repository", "dto", "mapper",
    "validator", "exception", "test", "errors",
})


# ──────────────────────────────────────────────────────────────���─────
# DDL column parser (light copy of routes/codegen.py::_parse_ddl_columns
# to avoid a cross-module import that would pull in the entire route
# module's heavy startup chain into this scorer.)
# ─────────────────────────────────────────────────────────────���──────

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
        table = m.group(1).split(".")[-1].lower()
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
        cols: List[str] = []
        for c in chunks:
            cstrip = c.strip().rstrip(",")
            head = cstrip.lower()
            if not cstrip or head.startswith((
                "primary key", "foreign key", "unique ", "unique(",
                "constraint ", "check ", "key ", "index ",
            )):
                continue
            mm = re.match(r"[\"`]?(\w+)[\"`]?\s+[A-Za-z]", cstrip)
            if mm:
                cols.append(mm.group(1).lower())
        if cols:
            out[table] = cols
    return out


# ────────────────────────────────────────────────────────────────────
# Per-component scorers (pure functions of content + context)
# ────────────────────────────────────────────────────────────────────

def _score_structural(content: str, file_type: str, language: str) -> ComponentScore:
    """100 if the file has every required framework marker for its type."""
    if language != "java":
        # Lenient default for other languages — has at least one
        # function / class / module-level def.
        ok = bool(re.search(r"\b(class|def|function|export)\b", content))
        return ComponentScore("structural", 100.0 if ok else 30.0,
                              COMPONENT_WEIGHTS["structural"],
                              "" if ok else "no class/def/function found")
    required = _REQUIRED_MARKERS_JAVA.get(file_type, ())
    if not required:
        return ComponentScore("structural", 100.0,
                              COMPONENT_WEIGHTS["structural"], "n/a for type")
    # For tuple-of-tuples (dto allows "record" OR "class"), at least one
    # element must be present.
    hit = all(any(m in content for m in (req if isinstance(req, tuple) else (req,)))
              if isinstance(req, tuple)
              else (req in content)
              for req in required)
    if hit:
        return ComponentScore("structural", 100.0,
                              COMPONENT_WEIGHTS["structural"],
                              "all framework markers present")
    missing = [r if isinstance(r, str) else "|".join(r) for r in required
               if not (r in content if isinstance(r, str)
                       else any(m in content for m in r))]
    score = max(0.0, 100.0 - 30.0 * len(missing))
    return ComponentScore("structural", score,
                          COMPONENT_WEIGHTS["structural"],
                          f"missing: {', '.join(missing)}")


def _score_parity(content: str) -> ComponentScore:
    """100 if no TODO carpet and no scaffold-fallback header; otherwise
    proportional decrement. iter-14.23 — `⚠ EVIDENCE GAP` markers now also
    penalise so files that dropped KB-provided fields fall below threshold
    and get selected for gap-recovery regeneration."""
    scaffold_hits = sum(content.count(h) for h in _SCAFFOLD_HEADERS)
    todo_hits = sum(content.count(t) for t in _TODO_CARPET)
    gap_hits = content.count("⚠ EVIDENCE GAP")
    if scaffold_hits == 0 and todo_hits == 0 and gap_hits == 0:
        return ComponentScore("parity", 100.0, COMPONENT_WEIGHTS["parity"],
                              "no scaffold / no TODO carpet / no evidence-gap")
    # Scaffold header = LLM failed entirely; heavy penalty.
    # TODO hits = LLM produced shell; lighter per-hit penalty.
    # Evidence-gap markers = dropped field evidence; per-hit penalty.
    penalty = 60.0 if scaffold_hits else 0.0
    penalty += min(40.0, todo_hits * 8.0)
    penalty += min(45.0, gap_hits * 15.0)
    score = max(0.0, 100.0 - penalty)
    parts = []
    if scaffold_hits:
        parts.append(f"scaffold-header×{scaffold_hits}")
    if todo_hits:
        parts.append(f"todo×{todo_hits}")
    if gap_hits:
        parts.append(f"evidence-gap×{gap_hits}")
    return ComponentScore("parity", score, COMPONENT_WEIGHTS["parity"],
                          " ".join(parts))


def _score_evidence(content: str, legacy_class_names: List[str],
                    legacy_method_names: List[str]) -> ComponentScore:
    """100 if file cites legacy class/method names via // LEGACY: comments
    OR direct name references. Proves the LLM consulted the KB."""
    if not legacy_class_names and not legacy_method_names:
        # No legacy corpus to cite against — neutral score.
        return ComponentScore("evidence", 75.0, COMPONENT_WEIGHTS["evidence"],
                              "no legacy corpus available")
    cl = content.lower()
    cites = sum(1 for n in legacy_class_names if n and n.lower() in cl)
    cites += sum(1 for n in legacy_method_names if n and n.lower() in cl)
    legacy_comments = content.count("// LEGACY:") + content.count("# LEGACY:")
    # Scoring curve: 1 cite = 50, 3 cites = 75, 6+ cites = 100.
    base = min(100.0, 25.0 + cites * 12.5)
    if legacy_comments > 0:
        base = min(100.0, base + 15.0)  # bonus for explicit annotation
    return ComponentScore(
        "evidence", base, COMPONENT_WEIGHTS["evidence"],
        f"{cites} name-cite(s), {legacy_comments} LEGACY-comment(s)",
    )


_BR_ID_RE = re.compile(r"\b(BR|UC|NFR|FR)-[A-Z0-9_]+", re.IGNORECASE)


def _score_requirement(content: str, srs_id_pool: set[str]) -> ComponentScore:
    """100 if file cites SRS BR-* / UC-* / NFR-* identifiers that exist
    in the SRS pool. Soft scoring — 0 cites is allowed for types where
    the prompt doesn't request them (entities, repositories)."""
    cited_in_file = set(m.group(0).upper() for m in _BR_ID_RE.finditer(content))
    if not srs_id_pool:
        # SRS has no machine-extractable IDs (legacy SRS, not IEEE-830-style).
        return ComponentScore("requirement", 75.0,
                              COMPONENT_WEIGHTS["requirement"],
                              "SRS has no BR/UC/NFR identifiers")
    hits = cited_in_file & srs_id_pool
    if not hits:
        return ComponentScore("requirement", 40.0,
                              COMPONENT_WEIGHTS["requirement"],
                              f"no SRS IDs cited ({len(srs_id_pool)} available)")
    # 1 hit = 65, 3 = 85, 5+ = 100.
    score = min(100.0, 50.0 + len(hits) * 12.0)
    sample = ", ".join(sorted(hits)[:4])
    return ComponentScore("requirement", score,
                          COMPONENT_WEIGHTS["requirement"],
                          f"cites {len(hits)} SRS IDs: {sample}")


def _score_coverage_controller(content: str, expected_paths: List[str]) -> Tuple[float, str]:
    """% of expected endpoint paths that are wired in this controller."""
    if not expected_paths:
        return 100.0, "no expected endpoints"
    cl = content
    hit = 0
    for path in expected_paths:
        # Strip {id} → {*} for matching tolerance.
        norm = re.sub(r"\{[^}]+\}", "{*}", path)
        # Look for any @*Mapping("…path…") where the path tail matches.
        # Match either full path or the segment after the controller prefix.
        seg = norm.rsplit("/", 1)[-1] or norm
        if seg and seg in cl:
            hit += 1
            continue
        # Also accept the route-tail without leading slash
        if seg.lstrip("/") and seg.lstrip("/") in cl:
            hit += 1
    pct = (hit / len(expected_paths)) * 100.0
    return pct, f"{hit}/{len(expected_paths)} endpoints wired"


def _score_coverage_entity(content: str, expected_table: str) -> Tuple[float, str]:
    """100 if @Table(name = "<table>") present."""
    if not expected_table:
        return 100.0, "no expected table"
    pat = re.compile(rf'@Table\s*\(\s*name\s*=\s*"{re.escape(expected_table)}"',
                     re.IGNORECASE)
    if pat.search(content):
        return 100.0, f"@Table('{expected_table}') present"
    if expected_table.lower() in content.lower():
        return 60.0, f"table name '{expected_table}' present but not in @Table"
    return 0.0, f"table '{expected_table}' missing from entity"


def _score_schema_entity(content: str, expected_cols: List[str]) -> Tuple[float, str]:
    """% of OLTP DDL columns present as @Column in the entity."""
    if not expected_cols:
        return 100.0, "no DDL columns to check"
    cl = content.lower()
    hits = sum(1 for col in expected_cols
               if f'name = "{col}"' in cl or f"name=\"{col}\"" in cl
               or f"@column(name=\"{col}\"" in cl)
    pct = (hits / len(expected_cols)) * 100.0
    return pct, f"{hits}/{len(expected_cols)} columns mapped"


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────

async def score_run(
    project_id: str,
    iteration: int = 0,
    threshold: float = 95.0,
    only_service: Optional[str] = None,
) -> RunConfidence:
    """Score every generated code file in the project (or one service).

    Reads codegen_files + arch_services + OLTP DDL + SRS + KB entities.
    Returns a RunConfidence with per-file / per-service / overall scores
    plus a list of files below threshold (sorted ascending by score so
    callers can prioritise the worst offenders).
    """
    # ── Load context ────────────────────────────────────────────────
    svc_q = {"project_id": project_id}
    if only_service:
        svc_q["name"] = only_service
    services_list = await arch_services.find(svc_q, {"_id": 0}).to_list(200)

    dm = await stage_context_col.find_one(
        {"project_id": project_id, "stage": "DataModel"}, {"_id": 0, "outputs": 1},
    )
    oltp_ddl = ((dm or {}).get("outputs", {}) or {}).get("oltp_ddl", "") or ""
    ddl_cols_by_table = _parse_ddl_columns(oltp_ddl)

    # Pull legacy class + method names for evidence scoring.
    legacy_classes: List[str] = []
    legacy_methods: List[str] = []
    try:
        cur = kb_entities.find(
            {"project_id": project_id, "type": {"$in": ["CLASS", "METHOD"]}},
            {"_id": 0, "name": 1, "type": 1, "methods": 1},
        ).limit(2000)
        async for ent in cur:
            n = (ent.get("name") or "").strip()
            if not n or len(n) < 4:
                continue
            if (ent.get("type") or "").upper() == "CLASS":
                legacy_classes.append(n)
                for m in (ent.get("methods") or [])[:10]:
                    mn = m.get("name") if isinstance(m, dict) else str(m)
                    if mn and len(mn) >= 4:
                        legacy_methods.append(mn)
            else:
                legacy_methods.append(n)
    except Exception as e:  # noqa: BLE001
        logger.warning("evidence pool load skipped: %s", e)
    legacy_classes = list(dict.fromkeys(legacy_classes))[:300]
    legacy_methods = list(dict.fromkeys(legacy_methods))[:600]

    # Pull SRS ID pool (BR-*, UC-*, NFR-*, FR-*).
    srs_id_pool: set[str] = set()
    try:
        srs_doc = await stage_context_col.find_one(
            {"project_id": project_id, "stage": "Discovery"}, {"_id": 0, "outputs": 1},
        )
        srs_text = ""
        for k, v in ((srs_doc or {}).get("outputs", {}) or {}).items():
            if isinstance(v, str):
                srs_text += "\n" + v
        for m in _BR_ID_RE.finditer(srs_text):
            srs_id_pool.add(m.group(0).upper())
    except Exception as e:  # noqa: BLE001
        logger.warning("SRS ID pool load skipped: %s", e)

    # ── Load generated files ────────────────────────────────────────
    # iter-14.43 — Include FE files too. Before this fix the query was
    # hard-coded to `^services/` so all files under `frontend/` were
    # excluded, causing Frontend score = 0 in the confidence popover.
    file_q = {
        "project_id": project_id,
        "file_path": {"$regex": "^(services/|frontend/)"},
    }
    if only_service:
        file_q["service_name"] = only_service
    docs = await codegen_files.find(file_q, {"_id": 0}).to_list(5000)
    docs = [d for d in docs if (d.get("file_type") or "") in _SCORED_TYPES]

    # ── Pre-compute per-service expected endpoints / tables / columns
    svc_endpoint_paths: Dict[str, List[str]] = {}
    svc_table_to_cols: Dict[str, Dict[str, List[str]]] = {}
    for s in services_list:
        eps = []
        for ep in (s.get("api_endpoints") or []):
            parts = str(ep).split(None, 1)
            if len(parts) == 2 and parts[0].upper() in (
                "GET", "POST", "PUT", "DELETE", "PATCH"
            ):
                eps.append(parts[1])
            else:
                eps.append(str(ep))
        svc_endpoint_paths[s["name"]] = eps
        tbl_map: Dict[str, List[str]] = {}
        for t in (s.get("tables") or []):
            tl = str(t).lower()
            tbl_map[tl] = ddl_cols_by_table.get(tl, [])
        svc_table_to_cols[s["name"]] = tbl_map

    # ── Score per file ──────────────────────────────────────────────
    per_svc_files: Dict[str, List[FileConfidence]] = {}
    for doc in docs:
        svc_name = doc.get("service_name") or "_unknown"
        content = doc.get("content") or ""
        file_type = (doc.get("file_type") or "").lower()
        language = (doc.get("language") or "").lower()
        comps: List[ComponentScore] = []

        # Structural
        comps.append(_score_structural(content, file_type, language))
        # Parity (content-agnostic — TODO/scaffold markers work on any code)
        comps.append(_score_parity(content))
        # Evidence — iter-14.43: Java class-name matching doesn't apply to
        # TSX/JSX. For FE types we pass through 100 with a "n/a" note; the
        # UI-parity axis (added below) does the FE-specific check.
        if file_type in _FRONTEND_TYPES:
            comps.append(ComponentScore(
                "evidence", 100.0, COMPONENT_WEIGHTS["evidence"],
                "n/a for frontend (see UI parity axis)",
            ))
        else:
            comps.append(_score_evidence(content, legacy_classes, legacy_methods))
        # Requirement
        comps.append(_score_requirement(content, srs_id_pool))

        # Coverage — only meaningful for controllers and entities
        coverage_score = 100.0
        coverage_detail = "n/a for type"
        if file_type == "controller":
            coverage_score, coverage_detail = _score_coverage_controller(
                content, svc_endpoint_paths.get(svc_name, []),
            )
        elif file_type == "entity":
            # Pick the table whose @Table the file points at; fall back to
            # the first table the service owns.
            expected_table = ""
            for tbl in svc_table_to_cols.get(svc_name, {}).keys():
                if re.search(rf'@Table\s*\(\s*name\s*=\s*"{re.escape(tbl)}"',
                             content, re.IGNORECASE):
                    expected_table = tbl
                    break
            if not expected_table:
                tbls = list(svc_table_to_cols.get(svc_name, {}).keys())
                expected_table = tbls[0] if tbls else ""
            coverage_score, coverage_detail = _score_coverage_entity(
                content, expected_table,
            )
        comps.append(ComponentScore("coverage", coverage_score,
                                    COMPONENT_WEIGHTS["coverage"], coverage_detail))

        # Schema — only Entity files
        schema_score = 100.0
        schema_detail = "n/a for type"
        if file_type == "entity":
            expected_table = ""
            for tbl in svc_table_to_cols.get(svc_name, {}).keys():
                if re.search(rf'@Table\s*\(\s*name\s*=\s*"{re.escape(tbl)}"',
                             content, re.IGNORECASE):
                    expected_table = tbl
                    break
            cols = svc_table_to_cols.get(svc_name, {}).get(expected_table, [])
            schema_score, schema_detail = _score_schema_entity(content, cols)
        comps.append(ComponentScore("schema", schema_score,
                                    COMPONENT_WEIGHTS["schema"], schema_detail))

        # iter-14.21 — Semantic. HF cosine similarity vs pooled legacy
        # corpus. Pass-through 100 when HF unavailable/off so the axis
        # never drags a good file down.
        semantic_score = 100.0
        semantic_detail = "n/a (HF disabled or no legacy corpus)"
        try:
            from codegen.hf_confidence import score_file as _hf_score
            hf_val = await _hf_score(project_id, content)
            if hf_val is not None:
                semantic_score = float(hf_val)
                semantic_detail = f"HF cosine vs legacy corpus → {hf_val:.1f}"
        except Exception as _he:  # noqa: BLE001
            semantic_detail = f"HF unavailable: {_he}"
        comps.append(ComponentScore("semantic", semantic_score,
                                    COMPONENT_WEIGHTS["semantic"], semantic_detail))

        # Weighted file score
        file_score = sum(c.score * c.weight for c in comps)

        # Issues list — short, actionable
        issues: List[str] = []
        for c in comps:
            if c.score < 70.0:
                issues.append(f"{c.name}({c.score:.0f}): {c.detail}")

        fc = FileConfidence(
            file_path=doc.get("file_path") or "",
            file_type=file_type,
            service=svc_name,
            score=file_score,
            components=comps,
            issues=issues[:6],
            recoverable=(file_type in _RECOVERABLE_TYPES),
        )
        per_svc_files.setdefault(svc_name, []).append(fc)

    # ── Aggregate per service ───────────────────────────────────────
    svc_confidences: List[ServiceConfidence] = []
    for svc_name, files in sorted(per_svc_files.items()):
        # Service-level coverage rollups
        controller_files = [f for f in files if f.file_type == "controller"]
        entity_files = [f for f in files if f.file_type == "entity"]
        ep_cov = (
            sum(next((c.score for c in cf.components if c.name == "coverage"), 0.0)
                for cf in controller_files) / max(1, len(controller_files))
        ) if controller_files else 100.0
        tbl_cov = (
            sum(next((c.score for c in cf.components if c.name == "coverage"), 0.0)
                for cf in entity_files) / max(1, len(entity_files))
        ) if entity_files else 100.0
        col_cov = (
            sum(next((c.score for c in cf.components if c.name == "schema"), 0.0)
                for cf in entity_files) / max(1, len(entity_files))
        ) if entity_files else 100.0
        svc_score = sum(f.score for f in files) / max(1, len(files))
        svc_confidences.append(ServiceConfidence(
            service=svc_name,
            score=svc_score,
            file_count=len(files),
            files=sorted(files, key=lambda f: f.score),
            endpoint_coverage_pct=ep_cov,
            table_coverage_pct=tbl_cov,
            column_coverage_pct=col_cov,
        ))

    # ── Overall ─────────────────────────────────────────────────────
    if svc_confidences:
        overall = sum(s.score * s.file_count for s in svc_confidences) / \
                  max(1, sum(s.file_count for s in svc_confidences))
    else:
        overall = 0.0

    # Files below threshold, sorted ascending by score (worst first)
    below: List[Dict[str, Any]] = []
    for s in svc_confidences:
        for f in s.files:
            if f.score < threshold and f.recoverable:
                below.append({
                    "file_path": f.file_path,
                    "service": f.service,
                    "file_type": f.file_type,
                    "score": round(f.score, 2),
                    "issues": f.issues,
                })
    below.sort(key=lambda r: r["score"])

    summary = (
        f"iter={iteration} overall={overall:.2f}% threshold={threshold:.1f}% "
        f"services={len(svc_confidences)} files={sum(s.file_count for s in svc_confidences)} "
        f"below_threshold={len(below)}"
    )
    logger.info("parity-loop %s", summary)

    return RunConfidence(
        project_id=project_id,
        iteration=iteration,
        overall_score=overall,
        threshold=threshold,
        services=svc_confidences,
        files_below_threshold=below,
        summary=summary,
    )


def select_recovery_targets(
    report: RunConfidence,
    max_files_per_iter: int = 20,
) -> List[Dict[str, Any]]:
    """Pick the worst-scoring recoverable files for the next Gap Recovery
    pass. Capped so we don't burn a full Ollama token budget on a single
    iteration of a 1000-file project."""
    return report.files_below_threshold[:max_files_per_iter]

