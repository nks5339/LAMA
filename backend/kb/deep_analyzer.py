"""Deep KB Analysis Module (iter-14.80).

Unified LLM-powered analysis that runs during Build KB to extract:
1. Business Rules — with preconditions/postconditions, enforcement layer, evidence
2. Role Analysis — privilege mappings, approval chains, persistence model
3. Field Traceability — UI → API → DB mappings with validation gaps
4. Validation Guidelines — parity checklist for downstream stages

The output is:
- Structured JSON persisted to `kb_deep_analysis` collection
- Human-readable MD summary for SRS, CodeGen, TestCase prompts

Governance principles (from core.yml):
- EVIDENCE_ONLY: Every fact must cite source file/line/method
- ZERO_ASSUMPTION: Do not infer behavior not explicitly in code
- FAIL_CLOSED: Mark anything uncertain as NOT_EVIDENCED
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from db import kb_toon, kb_entities, kb_files
from llm import fabric_call as chat_completion
from kb.vector_store import search as qdrant_search

logger = logging.getLogger("lama.kb.deep_analyzer")


# ---------------------------------------------------------------------------
# Output Schema — structured JSON the LLM must produce
# ---------------------------------------------------------------------------
OUTPUT_SCHEMA = """{
  "version": 1,
  "analysis_timestamp": "<ISO timestamp>",
  
  "business_rules": [
    {
      "rule_id": "BR-{MODULE}-{SEQ}",
      "rule_type": "BUSINESS_RULE | PRECONDITION | POSTCONDITION",
      "description": "Natural language description of the rule",
      "enforcement_layer": "UI | API | SERVICE | DB | CONFIG",
      "source_reference": "file path + class/method/procedure name + line or condition",
      "use_case_id": "UC-XX or NOT_IDENTIFIED",
      "confidence": "VERIFIED | NOT_EVIDENCED",
      "scan_target": "branching_logic | role_check | state_guard | calculation | date_cutoff | config_flag | trigger | precondition | postcondition"
    }
  ],
  
  "role_analysis": {
    "roles": [
      {
        "role_id": "ROLE-{SEQ}",
        "name": "<role name>",
        "source": "DB table | config file | code annotation",
        "source_reference": "<exact location>"
      }
    ],
    "role_privilege_map": [
      {
        "role_id": "ROLE-{SEQ}",
        "allowed_actions": ["<action1>", "<action2>"],
        "allowed_screens": ["<screen1>", "<screen2>"],
        "source_reference": "<evidence>"
      }
    ],
    "approval_chains": [
      {
        "chain_id": "APPR-{SEQ}",
        "workflow": "<workflow name>",
        "steps": [
          {"step": 1, "role": "<role>", "action": "approve|reject|forward", "next_step": 2}
        ],
        "source_reference": "<evidence>"
      }
    ],
    "role_persistence": {
      "storage_mechanism": "DB table | session | JWT | config",
      "table_or_location": "<table name or file>",
      "assignment_method": "<how roles are assigned to users>"
    }
  },
  
  "field_traceability": [
    {
      "field_name": "<field identifier>",
      "screen_module": "<screen or module name>",
      "ui_label": "<visible label>",
      "api_attribute": "<request/response attribute>",
      "db_table": "<table name>",
      "db_column": "<column name>",
      "operation": "READ | WRITE | READ-WRITE | IMPLICIT_WRITE | VALIDATION_ONLY | TRANSFORMATION",
      "validation_rule": "<regex | range | mandatory | FK | none>",
      "gap_flag": "OK | MISSING_FIELD | VALIDATION_GAP | LAYER_MISMATCH"
    }
  ],
  
  "validation_gaps": [
    {
      "gap_id": "GAP-{SEQ}",
      "gap_type": "MISSING_FLOW | MISSING_VALIDATION | MISSING_FIELD | MISSING_PRECONDITION | MISSING_POSTCONDITION | VALIDATION_GAP | IMPLICIT_WRITE",
      "description": "<what is missing or inconsistent>",
      "affected_entity": "<screen/API/table>",
      "severity": "CRITICAL | HIGH | MEDIUM | LOW",
      "source_reference": "<where the gap was detected>"
    }
  ],
  
  "use_cases": [
    {
      "use_case_id": "UC-{SEQ}",
      "name": "<use case name>",
      "primary_actor": "<role>",
      "preconditions": ["<precondition 1>", "<precondition 2>"],
      "postconditions": ["<postcondition 1>", "<postcondition 2>"],
      "main_flow": ["<step 1>", "<step 2>"],
      "business_rules_applied": ["BR-XXX-001"],
      "source_references": ["<file:method>"]
    }
  ],
  
  "coverage_stats": {
    "classes_analyzed": 0,
    "tables_analyzed": 0,
    "routes_analyzed": 0,
    "business_rules_extracted": 0,
    "roles_identified": 0,
    "fields_traced": 0,
    "gaps_detected": 0,
    "evidence_density": "LOW | MEDIUM | HIGH"
  }
}"""


# ---------------------------------------------------------------------------
# Semantic facets for RAG evidence gathering
# ---------------------------------------------------------------------------
_FACETS = [
    ("business_rules", "if else switch condition validation rule check guard"),
    ("roles_rbac", "role permission access authorize admin user @PreAuthorize security"),
    ("state_machine", "status state pending approved rejected draft submitted closed transition"),
    ("calculations", "calculate formula percentage discount tax compute amount total sum"),
    ("validations", "validate mandatory required regex check error exception throw"),
    ("field_mapping", "field column attribute input output request response form"),
    ("workflow", "approval submit process flow chain step forward reject"),
    ("triggers", "trigger procedure function stored INSERT UPDATE DELETE cursor"),
]


def _extract_toon_section(toon: str, name: str, limit: int) -> str:
    """Extract a named section from TOON, capped at limit chars."""
    if not toon:
        return ""
    out: list[str] = []
    in_sec = False
    total = 0
    for line in toon.split("\n"):
        if line.startswith(f"# {name}"):
            in_sec = True
            continue
        if in_sec and line.startswith("# "):
            break
        if in_sec:
            out.append(line)
            total += len(line)
            if total >= limit:
                out.append("...[truncated]")
                break
    return "\n".join(out)


async def _gather_evidence(project_id: str) -> dict:
    """Build comprehensive evidence block for the deep analysis prompt.
    
    Combines:
    - TOON slices (CLASSES, TABLES, ROUTES, ROLES, COLUMNS)
    - Per-facet RAG hits for semantic coverage
    - File inventory for source citation anchoring
    """
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    full_toon = (toon_doc or {}).get("toon", "")
    summary = (toon_doc or {}).get("summary", "")
    stats = (toon_doc or {}).get("stats", {})

    toon_slices = {
        "CLASSES": _extract_toon_section(full_toon, "CLASSES", 10000),
        "TABLES": _extract_toon_section(full_toon, "TABLES", 10000),
        "ROUTES": _extract_toon_section(full_toon, "ROUTES", 6000),
        "ROLES": _extract_toon_section(full_toon, "ROLES", 3000),
        "COLUMNS": _extract_toon_section(full_toon, "COLUMNS", 5000),
    }

    # RAG evidence per semantic facet
    rag_by_facet: dict[str, list[str]] = {}
    for facet, query in _FACETS:
        try:
            hits = await qdrant_search(project_id, query, top_k=10)
        except Exception as exc:
            logger.warning("Qdrant RAG for facet %s failed: %s", facet, exc)
            hits = []
        seen: set[str] = set()
        clean: list[str] = []
        for h in hits or []:
            key = (h or "")[:200]
            if key and key not in seen:
                seen.add(key)
                clean.append(h)
        rag_by_facet[facet] = clean

    # File inventory for source anchoring
    allowed_files: list[str] = []
    async for f in kb_files.find(
        {"project_id": project_id},
        {"_id": 0, "filename": 1},
    ).sort("size", -1).limit(500):
        n = (f.get("filename") or "").strip()
        if n:
            allowed_files.append(n)

    # Entity counts
    n_classes = await kb_entities.count_documents({"project_id": project_id, "type": "CLASS"})
    n_tables = await kb_entities.count_documents({"project_id": project_id, "type": "TABLE"})
    n_routes = await kb_entities.count_documents({"project_id": project_id, "type": "ROUTE"})
    n_roles = await kb_entities.count_documents({"project_id": project_id, "type": "ROLE"})

    return {
        "summary": summary,
        "stats": stats,
        "toon_slices": toon_slices,
        "rag_evidence": rag_by_facet,
        "allowed_files": allowed_files[:200],
        "entity_counts": {
            "classes": n_classes,
            "tables": n_tables,
            "routes": n_routes,
            "roles": n_roles,
        },
    }


def _build_system_prompt() -> str:
    """Build the system prompt with governance rules and output schema."""
    return f"""You are an **Enterprise Legacy System Forensic Analyst** with expertise in:
- Reverse-engineering legacy codebases
- Business rule extraction and cataloguing
- Role-based access control analysis
- Data flow and field traceability mapping

## GOVERNANCE RULES (NON-NEGOTIABLE)

### Truth Mode: STRICT_VERIFIED
1. **EVIDENCE_ONLY**: Every fact you report MUST cite an exact source (file:method:line or table:column)
2. **ZERO_ASSUMPTION**: Do NOT infer behavior not explicitly present in the code
3. **FAIL_CLOSED**: If evidence is missing, mark as "NOT_EVIDENCED" — never guess

### Source of Truth (in priority order)
1. Source code (Java, JSP, PHP, Python, .NET, JS, etc.)
2. Config files (XML, properties, YAML)
3. Database schema (tables, columns, triggers, procedures)
4. UI artifacts (HTML forms, field labels)

### Non-Negotiable Rules
- No inferred behavior — only what the code explicitly does
- No assumed defaults from "industry best practices"
- Implementation OVERRIDES documentation when in conflict
- Every branching condition (if/else/switch) = one distinct business rule
- Do NOT deduplicate — identical logic in different layers = distinct rules
- Preconditions and postconditions are MANDATORY for every use case

## OUTPUT SCHEMA

Return a single JSON object matching this structure:

{OUTPUT_SCHEMA}

## SCAN TARGETS FOR BUSINESS RULES

Look for these patterns in the code:
1. if/else/switch branching logic
2. Role-based checks and conditions
3. State transition guards
4. Amount and financial calculations
5. Date cutoffs and deadline enforcement
6. Configuration flags affecting behavior
7. DB triggers, stored procedures, functions
8. Use-case entry criteria (Preconditions)
9. Use-case completion criteria (Postconditions)

## FIELD TRACEABILITY RULES

For each field, classify operation type:
- READ: Field only displayed
- WRITE: Field only saved
- READ-WRITE: Both displayed and saved
- VALIDATION_ONLY: Checked but not persisted
- TRANSFORMATION: Value changes between layers
- IMPLICIT_WRITE: DB column written without UI/API input (trigger/default)

Flag gaps:
- MISSING_FIELD: Exists in legacy but absent in mapping
- VALIDATION_GAP: Validation differs between UI and DB layer
- LAYER_MISMATCH: API attribute doesn't map cleanly to DB column

Return ONLY the JSON object, no markdown fences, no explanation."""


def _build_user_prompt(evidence: dict) -> str:
    """Build the user prompt with all evidence."""
    toon_block = ""
    for name, content in evidence.get("toon_slices", {}).items():
        if content.strip():
            toon_block += f"\n### {name}\n{content}\n"

    rag_block = ""
    for facet, chunks in evidence.get("rag_evidence", {}).items():
        if chunks:
            rag_block += f"\n### {facet.upper()} EVIDENCE\n"
            for i, c in enumerate(chunks[:8], 1):
                rag_block += f"{i}. {c[:500]}...\n" if len(c) > 500 else f"{i}. {c}\n"

    files_sample = ", ".join(evidence.get("allowed_files", [])[:50])
    counts = evidence.get("entity_counts", {})

    return f"""Analyze this legacy codebase and extract ALL business rules, role mappings, field traceability, and validation gaps.

## CODEBASE SUMMARY
{evidence.get("summary", "No summary available")}

## ENTITY COUNTS
- Classes: {counts.get("classes", 0)}
- Tables: {counts.get("tables", 0)}
- Routes: {counts.get("routes", 0)}
- Roles: {counts.get("roles", 0)}

## STRUCTURAL SKELETON (TOON)
{toon_block}

## SEMANTIC EVIDENCE (RAG)
{rag_block}

## FILES IN SCOPE (for source_reference citations)
{files_sample}

---

Now produce the complete JSON analysis following the STRICT_VERIFIED governance rules.
Every rule, role, field mapping MUST have a source_reference from the files listed above.
Mark anything without explicit evidence as confidence: "NOT_EVIDENCED"."""


def compute_analysis_hash(project_id: str, toon: str, entity_count: int) -> str:
    """Compute a content hash to detect when re-analysis is needed."""
    h = hashlib.sha256()
    h.update(project_id.encode())
    h.update(str(entity_count).encode())
    h.update((toon or "")[:10000].encode())
    return h.hexdigest()[:16]


async def run_deep_analysis(
    project_id: str,
    *,
    force: bool = False,
    model: str = "",
) -> dict:
    """Run the deep KB analysis and return structured results.
    
    Args:
        project_id: The project to analyze
        force: If True, re-run even if cached result exists
        model: Override LLM model (defaults to fabric routing)
    
    Returns:
        Dict with keys: analysis, summary_md, cached, error
    """
    from db import kb_deep_analysis

    # Check cache
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    if not toon_doc or not toon_doc.get("toon"):
        return {"error": "No KB built yet — run Build KB first", "cached": False}

    entity_count = await kb_entities.count_documents({"project_id": project_id})
    content_hash = compute_analysis_hash(
        project_id,
        toon_doc.get("toon", ""),
        entity_count,
    )

    if not force:
        cached = await kb_deep_analysis.find_one(
            {"project_id": project_id},
            {"_id": 0},
        )
        if cached and cached.get("content_hash") == content_hash:
            logger.info("Deep analysis cache hit for %s (hash=%s)", project_id, content_hash)
            return {
                "analysis": cached.get("analysis", {}),
                "summary_md": cached.get("summary_md", ""),
                "cached": True,
            }

    # Gather evidence
    logger.info("Running deep analysis for %s (force=%s)", project_id, force)
    evidence = await _gather_evidence(project_id)

    # Build prompts
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(evidence)

    # Call LLM
    try:
        response = await chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model or "",
            agent_key="kb_deep_analysis",
        )
        raw_text = (response or "").strip()
    except Exception as exc:
        logger.error("Deep analysis LLM call failed: %s", exc)
        return {"error": str(exc), "cached": False}

    # Parse JSON response
    try:
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
        analysis = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("Deep analysis JSON parse failed: %s\nRaw: %s", exc, raw_text[:500])
        return {"error": f"JSON parse error: {exc}", "raw": raw_text[:2000], "cached": False}

    # Generate MD summary
    summary_md = _generate_summary_md(analysis, project_id)

    # Persist
    await kb_deep_analysis.update_one(
        {"project_id": project_id},
        {"$set": {
            "project_id": project_id,
            "content_hash": content_hash,
            "analysis": analysis,
            "summary_md": summary_md,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    logger.info(
        "Deep analysis complete for %s: %d BRs, %d roles, %d fields, %d gaps",
        project_id,
        len(analysis.get("business_rules", [])),
        len((analysis.get("role_analysis") or {}).get("roles", [])),
        len(analysis.get("field_traceability", [])),
        len(analysis.get("validation_gaps", [])),
    )

    return {
        "analysis": analysis,
        "summary_md": summary_md,
        "cached": False,
    }


def _generate_summary_md(analysis: dict, project_id: str) -> str:
    """Generate a human-readable MD summary for downstream prompts."""
    lines: list[str] = []
    lines.append(f"# Deep Analysis Summary — {project_id}")
    lines.append(f"\n_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")

    # Coverage stats
    stats = analysis.get("coverage_stats", {})
    if stats:
        lines.append("## Coverage")
        lines.append(f"- Classes analyzed: {stats.get('classes_analyzed', 0)}")
        lines.append(f"- Tables analyzed: {stats.get('tables_analyzed', 0)}")
        lines.append(f"- Routes analyzed: {stats.get('routes_analyzed', 0)}")
        lines.append(f"- Business rules extracted: {stats.get('business_rules_extracted', 0)}")
        lines.append(f"- Roles identified: {stats.get('roles_identified', 0)}")
        lines.append(f"- Fields traced: {stats.get('fields_traced', 0)}")
        lines.append(f"- Gaps detected: {stats.get('gaps_detected', 0)}")
        lines.append(f"- Evidence density: {stats.get('evidence_density', 'N/A')}")
        lines.append("")

    # Business Rules
    brs = analysis.get("business_rules", [])
    if brs:
        lines.append("## Business Rules Catalogue")
        lines.append("")
        lines.append("| Rule ID | Type | Description | Layer | Confidence |")
        lines.append("|---------|------|-------------|-------|------------|")
        for br in brs[:30]:  # Cap for readability
            lines.append(
                f"| {br.get('rule_id', '?')} | {br.get('rule_type', '?')} | "
                f"{br.get('description', '')[:60]}... | {br.get('enforcement_layer', '?')} | "
                f"{br.get('confidence', '?')} |"
            )
        if len(brs) > 30:
            lines.append(f"\n_...and {len(brs) - 30} more rules_")
        lines.append("")

    # Roles
    role_analysis = analysis.get("role_analysis", {})
    roles = role_analysis.get("roles", [])
    if roles:
        lines.append("## Roles Identified")
        lines.append("")
        for r in roles[:15]:
            lines.append(f"- **{r.get('name', '?')}** ({r.get('role_id', '?')}) — Source: {r.get('source', '?')}")
        lines.append("")

    # Approval Chains
    chains = role_analysis.get("approval_chains", [])
    if chains:
        lines.append("## Approval Chains")
        lines.append("")
        for ch in chains[:10]:
            steps_str = " → ".join(
                f"{s.get('role', '?')}:{s.get('action', '?')}"
                for s in ch.get("steps", [])
            )
            lines.append(f"- **{ch.get('workflow', '?')}**: {steps_str}")
        lines.append("")

    # Validation Gaps
    gaps = analysis.get("validation_gaps", [])
    if gaps:
        lines.append("## Validation Gaps")
        lines.append("")
        critical = [g for g in gaps if g.get("severity") == "CRITICAL"]
        high = [g for g in gaps if g.get("severity") == "HIGH"]
        if critical:
            lines.append("### Critical")
            for g in critical[:10]:
                lines.append(f"- **{g.get('gap_type', '?')}**: {g.get('description', '')}")
        if high:
            lines.append("### High")
            for g in high[:10]:
                lines.append(f"- **{g.get('gap_type', '?')}**: {g.get('description', '')}")
        lines.append("")

    # Use Cases
    ucs = analysis.get("use_cases", [])
    if ucs:
        lines.append("## Use Cases")
        lines.append("")
        for uc in ucs[:15]:
            lines.append(f"### {uc.get('use_case_id', '?')}: {uc.get('name', '?')}")
            lines.append(f"**Actor**: {uc.get('primary_actor', '?')}")
            if uc.get("preconditions"):
                lines.append(f"**Preconditions**: {', '.join(uc['preconditions'][:3])}")
            if uc.get("postconditions"):
                lines.append(f"**Postconditions**: {', '.join(uc['postconditions'][:3])}")
            lines.append("")

    # Field Traceability summary
    fields = analysis.get("field_traceability", [])
    if fields:
        lines.append("## Field Traceability")
        lines.append(f"\nTotal fields traced: {len(fields)}")
        gap_fields = [f for f in fields if f.get("gap_flag") not in ("OK", None, "")]
        if gap_fields:
            lines.append(f"\n**Fields with gaps**: {len(gap_fields)}")
            lines.append("")
            lines.append("| Field | Screen | Gap |")
            lines.append("|-------|--------|-----|")
            for f in gap_fields[:20]:
                lines.append(
                    f"| {f.get('field_name', '?')} | {f.get('screen_module', '?')} | "
                    f"{f.get('gap_flag', '?')} |"
                )
        lines.append("")

    return "\n".join(lines)


