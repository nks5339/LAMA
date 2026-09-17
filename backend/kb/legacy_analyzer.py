"""Deep legacy-logic analysis (iter-13.17).

Single-pass LLM analysis of an entire parsed legacy codebase that emits a
STRUCTURED JSON object describing the business logic in language the SRS
generator and Stage-4 CodeGen can both consume.

Why a pre-pass?
- Build KB only extracts *structural* facts (classes, tables, columns, FKs).
- The SRS section prompts then see TOON + a handful of RAG chunks per
  section. They can describe what exists, but NOT what the system DOES —
  the state machines, approval chains, validation rules, calculation logic
  that span multiple files / classes / SQL procs.
- This module fills that gap by giving the LLM the whole TOON skeleton +
  curated RAG chunks across multiple semantic dimensions in ONE call, with
  a directive to extract those cross-file behaviours into a strict schema.

The output is then:
1. Persisted to `legacy_analysis` collection (one doc / project / version).
2. Injected as a SHARED PREAMBLE into every SRS section's system prompt.
3. Written to StageContext.outputs.legacy_analysis on Discovery freeze so
   downstream stages (DataModel, Architecture, CodeGen) read the same
   single source-of-truth instead of re-deriving it.
"""

from __future__ import annotations

import json
import logging
import re as _re
from datetime import datetime, timezone

from db import kb_toon, kb_entities, projects, audit_log, legacy_analysis, prompts
from llm import fabric_call as chat_completion
from kb.vector_store import search as qdrant_search

logger = logging.getLogger("lama.legacy_analyzer")


# Semantic dimensions we pull RAG evidence for. Each becomes a focused
# "facet" inside the prompt so the model sees concrete examples per area
# rather than a generic blob.
_FACETS = [
    ("workflows",     "approval submission status transition workflow process flow"),
    ("calculations",  "calculation formula percentage discount tax compute amount"),
    ("validations",   "validate validation required mandatory regex check error"),
    ("integrations",  "payment gateway api soap rest http external service token"),
    ("rbac",          "role permission access authorize @PreAuthorize filter login session"),
    ("state_machine", "status state pending approved rejected draft submitted closed"),
    ("scheduling",    "cron schedule batch job scheduler queue retry"),
    ("data_flow",     "INSERT UPDATE DELETE trigger procedure cursor SELECT JOIN"),
]


_OUTPUT_SCHEMA = """{
  "version": 1,
  "summary": "1-paragraph plain-english summary of what this system does",
  "domain_glossary": [
    {"term": "<domain term>", "definition": "...", "source": "<file or table>"}
  ],
  "actors": [
    {"id": "A-01", "name": "<role>", "responsibilities": ["..."], "source": "<file/role table>"}
  ],
  "domain_entities": [
    {
      "name": "<entity>",
      "purpose": "...",
      "key_tables": ["tbl_foo", "tbl_bar"],
      "key_classes": ["FooController", "FooService"],
      "owner_actor": "A-01"
    }
  ],
  "workflows": [
    {
      "id": "WF-01",
      "name": "<e.g. Tender Approval>",
      "primary_actor": "<role>",
      "description": "...",
      "steps": [
        {"n": 1, "actor": "<role>", "action": "...", "system_op": "...", "source": "<file:method or sql>"}
      ],
      "states": ["DRAFT","SUBMITTED","APPROVED","REJECTED"],
      "transitions": [
        {"from": "DRAFT", "to": "SUBMITTED", "trigger": "<event>", "guard": "<rule>"}
      ],
      "tables_touched": ["..."]
    }
  ],
  "business_rules": [
    {"id": "BR-01", "rule": "<one-line rule statement>", "scope": "<module|global>", "source": "<file:line or sql:proc>"}
  ],
  "validation_rules": [
    {"field": "<screen.field>", "rule": "<regex|range|mandatory|...>", "source": "<file>"}
  ],
  "calculations": [
    {"name": "<e.g. GST on bill>", "formula_plain": "<plain-english>", "source": "<file:method or sql>"}
  ],
  "integrations": [
    {"vendor": "<name>", "direction": "outbound|inbound", "protocol": "REST|SOAP|SFTP|...",
     "auth": "<basic|oauth|api-key>", "purpose": "...", "source": "<file>"}
  ],
  "user_journeys": [
    {
      "id": "UJ-01", "actor": "<role>", "goal": "<...>",
      "screens": ["<screen1>","<screen2>"],
      "happy_path": ["step1","step2","..."],
      "edge_cases": ["..."]
    }
  ],
  "data_flows": [
    {"name": "<flow>", "trigger": "<event>", "writes": ["tbl_a"], "reads": ["tbl_b"],
     "side_effects": ["audit_log entry","email"], "source": "<file or sql proc>"}
  ],
  "open_questions": [
    "Things that look ambiguous in the code and require SME confirmation"
  ],
  "coverage": {
    "controllers_analysed": 0,
    "tables_analysed": 0,
    "evidence_density": "low|medium|high"
  }
}"""


def _extract_toon_section(toon: str, name: str, limit: int) -> str:
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
    """Build the evidence block the deep-analyzer prompt will see.

    Combines:
      • The full CLASSES / TABLES / ROUTES / ROLES TOON slices (cap ~8 KB each)
      • Per-facet RAG hits (top-8 chunks for each semantic dimension)

    Returns a single dict, JSON-ready, that the prompt embeds inline.
    """
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    full_toon = (toon_doc or {}).get("toon", "")
    summary = (toon_doc or {}).get("summary", "")
    stats = (toon_doc or {}).get("stats", {})

    toon_slices = {
        "CLASSES": _extract_toon_section(full_toon, "CLASSES", 8000),
        "TABLES":  _extract_toon_section(full_toon, "TABLES", 8000),
        "ROUTES":  _extract_toon_section(full_toon, "ROUTES", 4000),
        "ROLES":   _extract_toon_section(full_toon, "ROLES", 2000),
    }

    rag_by_facet: dict[str, list[str]] = {}
    for facet, query in _FACETS:
        try:
            hits = await qdrant_search(project_id, query, top_k=8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant RAG for facet %s failed: %s", facet, exc)
            hits = []
        seen: set[str] = set()
        clean: list[str] = []
        for h in hits or []:
            key = (h or "")[:160]
            if key and key not in seen:
                seen.add(key)
                clean.append(h)
        rag_by_facet[facet] = clean

    n_classes = await kb_entities.count_documents({"project_id": project_id, "type": "CLASS"})
    n_tables = await kb_entities.count_documents({"project_id": project_id, "type": "TABLE"})
    n_routes = await kb_entities.count_documents({"project_id": project_id, "type": "ROUTE"})

    # iter-13.95 — surface the scope-checked file basenames so the
    # analyzer prompt can pin every `source` citation to a real file
    # in THIS project. Without this anchor the LLM happily hallucinates
    # PMIS-shaped paths from training data and persists them to Mongo,
    # which then feed every subsequent SRS for 24h via the digest cache.
    allowed_files: list[str] = []
    try:
        from db import kb_files as _kbf
        async for f in _kbf.find(
            {"project_id": project_id},
            {"_id": 0, "filename": 1},
        ).sort("size", -1).limit(300):
            n = (f.get("filename") or "").strip()
            if not n:
                continue
            bn = n.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
            if bn and bn not in allowed_files:
                allowed_files.append(bn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy_analyzer: allowed_files load failed: %s", exc)
        allowed_files = []

    return {
        "summary": summary,
        "stats": stats,
        "toon_slices": toon_slices,
        "rag_by_facet": rag_by_facet,
        "counts": {
            "classes": n_classes,
            "tables": n_tables,
            "routes": n_routes,
        },
        "allowed_files": allowed_files,
    }


def _build_prompt(project: dict, evidence: dict) -> str:
    """Compose the deep-analyzer system prompt."""
    detected = project.get("detected_tech") or {}
    rag_block_parts = []
    for facet, chunks in evidence["rag_by_facet"].items():
        joined = "\n\n--- chunk ---\n\n".join(chunks) if chunks else "(no evidence)"
        if len(joined) > 10000:
            joined = joined[:10000] + "\n...[truncated]"
        rag_block_parts.append(f"=== FACET: {facet} ===\n{joined}")
    rag_block = "\n\n".join(rag_block_parts)

    toon_block_parts = []
    for key, slc in evidence["toon_slices"].items():
        if slc.strip():
            toon_block_parts.append(f"# {key}\n{slc}")
    toon_block = "\n\n".join(toon_block_parts)

    # iter-13.95 — emit an explicit per-project ALLOWED FILES list so
    # the deep-analyzer LLM cannot satisfy "cite a real file path" by
    # hallucinating PMIS-shaped `src/com/ahct/*` paths from training
    # data when the actual evidence is thin. The SRS prompt sanitizer
    # in routes/srs.py is the safety net; this anchor is the
    # belt-and-braces upstream fix that keeps Mongo clean.
    allowed_files = (evidence.get("allowed_files") or [])[:200]
    allowed_block = ""
    if allowed_files:
        allowed_block = (
            "ALLOWED SOURCE-FILE BASENAMES (iter-13.95 — scope guard):\n"
            "Every `source` field you emit MUST reference one of these files\n"
            "VERBATIM (basename match). Any other path is a CROSS-PROJECT\n"
            "LEAK and a hard contract violation — the SRS sanitizer will\n"
            "redact it downstream and the analysis will be marked invalid.\n"
            + "\n".join(f"  - {n}" for n in allowed_files)
            + "\n"
        )

    return f"""You are a Forensic System Reverse-Engineer reading a legacy codebase.

GOAL
Produce a SINGLE strict-JSON object that captures EVERYTHING a senior business
analyst would need to write an exhaustive IEEE-29148 SRS and that a senior
engineer would need to re-implement the system 1:1 in a modern stack.

PROJECT
  name:    {project.get('name', '')}
  source:  {project.get('source_tech', '')}
  target:  {project.get('target_tech', '')}
  detected stack:
    summary:    {detected.get('summary') or '(none)'}
    frameworks: {', '.join(detected.get('frameworks') or []) or '(none)'}
    database:   {detected.get('database') or '(none)'}

{allowed_block}
STRUCTURAL SKELETON (TOON)
{toon_block}

SEMANTICALLY GROUPED EVIDENCE (RAG)
{rag_block}

COVERAGE COUNTERS
  classes: {evidence['counts']['classes']}
  tables:  {evidence['counts']['tables']}
  routes:  {evidence['counts']['routes']}

OUTPUT SCHEMA (return EXACTLY this JSON shape — no markdown fences, no
preamble, no trailing prose. Every array may be empty if no evidence exists,
but every key MUST be present.):
{_OUTPUT_SCHEMA}

HARD RULES
1. EVIDENCE-ONLY. Every "source" field MUST cite a real file path / method
   name / table / SQL procedure that appears in the blocks above. Make-believe
   sources are a contract violation. When ALLOWED SOURCE-FILE BASENAMES is
   listed, the `source` filename component MUST match one of them verbatim;
   ANY other path will be redacted by the SRS sanitizer and the rule
   discarded.
2. CROSS-FILE BEHAVIOUR. Extract workflows / state machines that SPAN
   multiple files (e.g. "controller A.save sets status=SUBMITTED, trigger
   T_audit fires on update, service B.approve sets status=APPROVED"). These
   are what SRS section prompts miss and what CodeGen needs most.
3. NAME REAL THINGS. Use the exact class / table / column / role names from
   the TOON. No `[placeholder]`, no `<TBD>`.
4. PREFER STRUCTURE OVER PROSE. The "description" / "rule" / "purpose"
   fields must be ONE concise sentence. Detail lives in the structured
   children (steps, transitions, tables_touched).
5. NO REFUSAL. If a section truly has no evidence (e.g. no payment
   integrations in the codebase), set its array to `[]`. Never produce
   the marker `> ⚠️ Section generation failed`.
6. STRICT JSON. The first character of your response MUST be `{{` and the
   last MUST be `}}`. No code fences. No commentary."""


async def run_legacy_analysis(
    project_id: str,
    model: str = "",  # iter-13.30: empty → fabric_call resolves via Console (srs.generate = high)
    force: bool = False,
) -> dict:
    """Execute one deep-analysis pass and persist the result.

    `force=False` (default): if a fresh analysis already exists for this
    project (within the last 24h) return it unchanged. The SRS auto-trigger
    uses this so we don't burn tokens on every Regenerate.

    iter-14.8 — Persist FAILURE markers as well. Prior behaviour:
    when the LLM call raised (LLM misconfigured, factory-cli timeout,
    OpenRouter key missing) NOTHING was persisted, so the very next
    `_load_srs_context` call — which fires once per section regen —
    ran the whole 240s pass again. For a 12-section SRS that turned a
    single droid outage into ~48 minutes of user-visible "loading" per
    project. We now write a lightweight `{"status": "failed", ...}`
    doc with a 15-minute cooldown; within the cooldown window we skip
    the LLM call entirely and return an empty digest so section
    generation proceeds with TOON+RAG only (as designed). `force=True`
    from the Console explicit-refresh button bypasses the cooldown so
    operators can retry immediately after fixing the underlying issue.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise RuntimeError(f"Project not found: {project_id}")

    if not force:
        existing = await legacy_analysis.find_one({"project_id": project_id}, {"_id": 0})
        if existing:
            # iter-14.8 — failure-marker cool-down (see docstring).
            if existing.get("status") == "failed":
                failed_at = existing.get("failed_at", "")
                try:
                    dt = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
                    age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
                    if age_minutes < 15:
                        logger.info(
                            "Legacy analysis for %s skipped — in failure cool-down "
                            "(%.1f min since last failure, will retry after 15 min; "
                            "click Refresh KB or pass force=True to override)",
                            project_id, age_minutes,
                        )
                        # Return the marker; callers already treat non-dict
                        # results as "no digest available" via
                        # `build_analysis_digest(analysis_doc) if analysis_doc else ""`
                        # and the marker has no `workflows` etc. so the
                        # digest builder emits an empty string.
                        return existing
                except Exception:  # noqa: BLE001
                    pass
            else:
                updated = existing.get("updated_at", "")
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
                    if age_hours < 24:
                        logger.info(
                            "Legacy analysis for %s reused (age=%.1fh)", project_id, age_hours
                        )
                        return existing
                except Exception:  # noqa: BLE001
                    pass

    logger.info("Running deep legacy analysis for project %s with model %s", project_id, model)
    evidence = await _gather_evidence(project_id)
    system_prompt = _build_prompt(proj, evidence)

    # The seeded `legacy.deep_analyzer` row says of itself: "This template is
    # APPENDED to whatever the legacy_analyzer module composes at runtime …
    # Edit this file to tighten the rules WITHOUT touching
    # legacy_analyzer.py." That append had never been written, so the row sat
    # in Prompt Library editable and inert — an operator tightening it
    # changed nothing. Appended (not prepended) so the module's own STRICT
    # JSON clause still reads last.
    try:
        _row = await prompts.find_one({"key": "legacy.deep_analyzer"}, {"_id": 0})
        _tpl = ((_row or {}).get("template") or "").strip()
        if _tpl:
            system_prompt = f"{system_prompt}\n\n{_tpl}"
    except Exception:  # noqa: BLE001 — a Mongo blip must not kill the analysis
        logger.warning("legacy.deep_analyzer prompt row unavailable; using module prompt only")

    try:
        result = await chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Produce the JSON analysis now."},
            ],
            model=model,
            # Previously omitted, so the frame-walk in `fabric_call` fell
            # through to "unknown" -> the default `medium` tier. This is the
            # heaviest read in Discovery and its own docstring asks for the
            # strong tier; name the key that the seeded prompt already uses.
            agent_key="legacy.deep_analyzer",
            temperature=0.2,
            max_tokens=14000,
            timeout=240.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Deep legacy analysis LLM call failed: %s", exc)
        # iter-14.8 — persist failure marker so subsequent section regens
        # short-circuit for 15 minutes instead of re-running the same
        # 240s call for every one of 12 sections. Best-effort: a Mongo
        # failure here is silently swallowed so we still raise the
        # original LLM exception to the caller.
        try:
            now = datetime.now(timezone.utc).isoformat()
            await legacy_analysis.update_one(
                {"project_id": project_id},
                {"$set": {
                    "project_id": project_id,
                    "status": "failed",
                    "failed_at": now,
                    "failure_reason": str(exc)[:500],
                    "updated_at": now,
                }},
                upsert=True,
            )
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                "Legacy analysis failure marker not persisted for %s: %s",
                project_id, persist_exc,
            )
        raise

    raw = (result.get("content") or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:].lstrip()

    try:
        data = json.loads(raw)
    except Exception:
        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise RuntimeError(
                f"Legacy analyzer returned unparseable response: {raw[:300]!r}"
            )
        data = json.loads(m.group(0))

    now = datetime.now(timezone.utc).isoformat()
    existing = await legacy_analysis.find_one({"project_id": project_id}, {"_id": 0})
    doc = {
        "project_id": project_id,
        "analysis": data,
        "model_used": model,
        "tokens": result.get("usage", {}).get("total_tokens", 0),
        "updated_at": now,
        "version": (existing.get("version", 0) + 1) if existing else 1,
        "coverage": data.get("coverage", {}),
    }
    await legacy_analysis.update_one(
        {"project_id": project_id},
        {"$set": doc},
        upsert=True,
    )
    try:
        await audit_log.insert_one({
            "action": "kb.deep_analysis",
            "project_id": project_id,
            "at": now,
            "details": {
                "model": model,
                "tokens": doc["tokens"],
                "version": doc["version"],
                "workflows": len((data.get("workflows") or [])),
                "rules": len((data.get("business_rules") or [])),
                "integrations": len((data.get("integrations") or [])),
            },
        })
    except Exception:  # noqa: BLE001
        pass
    return doc


def build_analysis_digest(analysis_doc: dict | None, max_chars: int = 6000) -> str:
    """Compact markdown digest the SRS section prompts can embed.

    Designed to be ≤6 KB so it fits inside every section's system prompt
    without bloating token usage. Drops detail that a section author wouldn't
    cite (full step lists, edge cases) and keeps the IDs + names + scopes
    so sections can REFER to the analysis by ID instead of duplicating it.
    """
    if not analysis_doc or not analysis_doc.get("analysis"):
        return ""
    a = analysis_doc["analysis"]
    parts: list[str] = []
    summary = (a.get("summary") or "").strip()
    if summary:
        parts.append(f"**System Summary:** {summary}")

    actors = a.get("actors") or []
    if actors:
        parts.append("**Actors:** " + ", ".join(
            f"{x.get('id', '?')} {x.get('name', '')}"
            for x in actors[:25]
        ))

    entities = a.get("domain_entities") or []
    if entities:
        ent_lines = [
            f"- **{e.get('name', '?')}** — {(e.get('purpose') or '').strip()[:120]} "
            f"(tables: {', '.join((e.get('key_tables') or [])[:5])})"
            for e in entities[:30]
        ]
        parts.append("**Domain Entities:**\n" + "\n".join(ent_lines))

    workflows = a.get("workflows") or []
    if workflows:
        wf_lines = []
        for w in workflows[:40]:
            states = ", ".join((w.get("states") or [])[:8])
            wf_lines.append(
                f"- **{w.get('id', '?')} {w.get('name', '?')}** "
                f"(actor: {w.get('primary_actor', '?')}, states: {states})"
            )
        parts.append("**Workflows:**\n" + "\n".join(wf_lines))

    rules = a.get("business_rules") or []
    if rules:
        rl_lines = [
            f"- {r.get('id', '?')}: {(r.get('rule') or '').strip()[:200]} "
            f"_(scope: {r.get('scope', '?')}, src: {r.get('source', '?')})_"
            for r in rules[:80]
        ]
        parts.append("**Business Rules:**\n" + "\n".join(rl_lines))

    integrations = a.get("integrations") or []
    if integrations:
        int_lines = [
            f"- **{i.get('vendor', '?')}** ({i.get('direction', '?')} {i.get('protocol', '?')}, "
            f"auth: {i.get('auth', '?')}) — {(i.get('purpose') or '').strip()[:120]}"
            for i in integrations[:20]
        ]
        parts.append("**Integrations:**\n" + "\n".join(int_lines))

    journeys = a.get("user_journeys") or []
    if journeys:
        uj_lines = [
            f"- **{j.get('id', '?')}** ({j.get('actor', '?')}): {(j.get('goal') or '').strip()[:140]}"
            for j in journeys[:25]
        ]
        parts.append("**User Journeys:**\n" + "\n".join(uj_lines))

    open_qs = a.get("open_questions") or []
    if open_qs:
        parts.append(
            "**Open Questions / Gaps:**\n"
            + "\n".join(f"- {q[:200]}" for q in open_qs[:10])
        )

    digest = "\n\n".join(parts)
    if len(digest) > max_chars:
        digest = digest[:max_chars] + "\n\n_[digest truncated — full analysis in `legacy_analysis` collection]_"
    return digest

