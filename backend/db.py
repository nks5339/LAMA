"""MongoDB connection and collection accessors."""
import os
from motor.motor_asyncio import AsyncIOMotorClient

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Collections
projects = db.projects
kb_files = db.kb_files
kb_chunks = db.kb_chunks
kb_entities = db.kb_entities
kb_toon = db.kb_toon
kb_deep_analysis = db.kb_deep_analysis  # iter-14.80 — deep analysis results
conversations = db.conversations
messages = db.messages
srs_documents = db.srs_documents
prompts = db.prompts
project_prompts = db.project_prompts
freeze_gates = db.freeze_gates
audit_log = db.audit_log
stage_context = db.stage_context

# Stage 2 — Data Model
data_models = db.data_models
bus_matrix = db.bus_matrix
olap_models = db.olap_models
migration_artifacts = db.migration_artifacts

# Stage 3 — Architecture
arch_documents = db.arch_documents
arch_services = db.arch_services

# Stage 4 — Code Generation
codegen_files = db.codegen_files
codegen_runs = db.codegen_runs
# iter-13.119 — Automated validation + improvement loop (parity_loop).
# One doc per auto-validate run holding the per-iteration confidence
# trajectory + the final report. Surfaced by the CodeGen "Auto-Validate
# & Improve" button and persisted as a markdown artifact in codegen_files
# (_lama/reports/auto-validate-<ts>.md) for audit / GitHub push.
parity_runs = db.parity_runs

# Console — Model Fabric / Agent Fabric / Usage Logs
model_providers = db.model_providers
agent_configs = db.agent_configs
token_usage_log = db.token_usage_log

# GitHub configs (also referenced in routes/codegen.py)
github_configs = db.github_configs

# Stage 5 — Living System (Selenium / JMeter / Drift / SRS-diff)
living_artifacts = db.living_artifacts
living_runs = db.living_runs
ontology_snapshots = db.ontology_snapshots
business_ontologies = db.business_ontologies

# Iter 13.8 — live data-source ingestion (DB credentials + app URL hints)
data_sources = db.data_sources

# Tools — standalone utilities (bypass pipeline)
gap_analyses = db.gap_analyses          # Gap Analyzer results
gap_analysis_files = db.gap_analysis_files  # Stored files for async processing
transformations = db.transformations    # Code Transformer runs
transform_files = db.transform_files    # Generated files from transformer
tools_kb = db.tools_kb                  # Shared KB for Tools (Gap Analyzer + Transformer)

# Multi-Agent Transformer (iter-16) — orchestrated pipeline for Code Transformer
transformer_envelopes = db.transformer_envelopes  # API-to-DB envelopes discovered by Context Manager
transformer_tasks = db.transformer_tasks          # Task list created by Planner agent
transformer_agent_runs = db.transformer_agent_runs  # Agent execution timeline / audit log
# iter-15.19 — Per-transformation agent prompt/model overrides. One doc per
# (transform_id, agent). Never touches the shared Prompt Library — lets a
# user tweak a single transformation's Context Manager/Planner/Coder/
# Verifier/Tester prompt or model, save, and rerun just that pipeline.
transformer_agent_configs = db.transformer_agent_configs

# ---------------------------------------------------------------------------
# Multi-Agent CodeGen (iter-17) — same shape as the transformer_* block above
# but scoped to the CodeGen stage of a `legacy_migration` project. Kept in
# distinct collections so the two pipelines can evolve independently and so
# that a project's CodeGen run never accidentally shares state with a
# standalone Transformer job. Consumed by
# `backend/routes/codegen.py::_run_multi_agent_codegen` and friends.
# ---------------------------------------------------------------------------
codegen_envelopes = db.codegen_envelopes            # API-to-DB envelopes (frozen Architecture-derived)
codegen_tasks = db.codegen_tasks                    # Task list from CodeGen Planner (coder_be | coder_fe)
codegen_agent_runs = db.codegen_agent_runs          # Agent execution timeline / audit log
codegen_pipeline_state = db.codegen_pipeline_state  # Singleton FSM state doc per project

# Iter 13.17 — Deep legacy-logic analysis (runs after Build KB, before SRS).
# One doc per project, versioned. The analysis is the SHARED source-of-truth
# that every SRS section prompt cites AND that Stage-4 CodeGen reads to know
# WHAT to rebuild. Without this, SRS sections only saw raw TOON + RAG chunks
# and missed cross-file business logic (state machines, approval chains,
# calculation rules) that no single chunk reveals on its own.
legacy_analysis = db.legacy_analysis

# Iter 13.19 — Graphified KB (property graph derived from OWL entities +
# business ontology). One doc per project containing {nodes, edges, version,
# updated_at, stats}. Used by SRS / Architecture / CodeGen prompts when
# LAMA_USE_GRAPH_KB=1 to retrieve compact, role-aware subgraphs instead of
# (or alongside) raw RAG chunks. MongoDB-backed adjacency lists — zero new
# infra; replaceable with Neo4j/Graphiti later without touching call-sites.
kb_graph = db.kb_graph

# Iter-14.24 — Journey KB (Phase 1). Vertical DB-column → repository →
# service → controller → route → UI-field chunks materialised from
# kb_graph. `kb_journeys` holds one doc per journey (many per project);
# `kb_journey_config` holds one doc per project with the enable toggle
# and per-stage allow-list. Opt-in via LAMA_USE_JOURNEY_KB=1 or the
# per-project override. Reads/writes are additive; when the toggle is
# off, every prompt/SRS/CodeGen path is unchanged.
kb_journeys = db.kb_journeys
kb_journey_config = db.kb_journey_config

# Iter-13.60 — Govt-service integrations selected per project (PAN, Aadhaar,
# GSTIN, DigiLocker, e-Sign, UPI, …). Each enabled entry's code template is
# injected into `codegen_files` so the generated service ships with a
# configurable client + router stub. One doc per (project_id, integration_id).
project_integrations = db.project_integrations

# Iter-13.63 — Git-cloned legacy sources (alongside zip/folder/file ingest).
# `kb_git_sources` keeps one doc per clone {url, branch, commit, local_path,
# cloned_at, file_count}. Token is NEVER persisted — read once from the
# request, injected into the clone URL, then dropped.
# `kb_module_selection` keeps one doc per project: {excluded: [path_prefix, …]}
# that chat / SRS / build paths honour by skipping kb_files whose `filename`
# starts with any excluded prefix. Lets the user untick irrelevant legacy
# folders (vendor/, tests/, …) after a wide-net git clone or folder scan.
kb_git_sources = db.kb_git_sources
kb_module_selection = db.kb_module_selection

# Iter-13.68 — Multi-tenant.
# `tenants` — one doc per organisation/customer. Every Project gets a
# `tenant_id` (backfilled to "tenant_default" by the seed). `users` —
# username/bcrypt-hashed-password + role ("super_admin" | "tenant_admin"
# | "tenant_user") + tenant_id ("*" for super_admin). Auth is stateless
# JWT (HS256, secret from LAMA_JWT_SECRET) — no session collection.
tenants = db.tenants
users = db.users

# Iter-13.70 — Confidence engine.
# `stage_confidence` — one doc per (project_id, stage) holding the latest
# multi-model section-by-section score envelope produced by
# `backend/confidence.py`. Read by every freeze gate + the Living-stage
# Accuracy Report.
# `living_reports` — full Accuracy Report payloads (KB vs latest generated
# artifacts) for the Living tab "Accuracy Report" view. Keyed by id; we
# keep the last N per project.
stage_confidence = db.stage_confidence
living_reports = db.living_reports


# Iter-13.81.9 — Factory.ai filesystem materialisation state.
# One doc per project: {project_id, tenant_id, computer_id, cwd, path,
# file_count, bytes, sha, materialized_at}. Lets us detect "nothing has
# changed since last materialise" (sha match) and surface the real Droid
# path inside the INLINE-KB GROUNDING prompt block so Factory can
# ls / cat / grep its own filesystem instead of complaining the KB is
# empty.
factory_workspaces = db.factory_workspaces


# Iter-13.100 — Rolling-memory agent sessions (droid-handoff aware).
# One doc per (project_id, stage, agent_key, session_id). Holds the
# durable conversational/working context for LLM calls that opt in via
# `fabric_call(..., session_id=...)`. Survives browser refresh, droid
# swap, and LLM context-window overflow (rollover summarises older
# turns and keeps last-K verbatim). See backend/agent_memory.py for
# the read/write/rollover helpers and the HMAC signing contract.
#
# NOT to be confused with `settings.factory_orchestrator.sessions_by_agent`
# in the `projects` collection — that cache holds Factory.ai Droid
# session IDs (vendor-side concept), whereas `agent_sessions` holds
# LAMA-side rolling context envelopes (our concept).
agent_sessions = db.agent_sessions

# Iter-13.101 — LLM call traces (audit "Detail Log Trace" feature).
# One doc per fabric_call invocation. Captures the full request prompt,
# response prompt, request/response wall-clock times, elapsed_ms, stage,
# agent_key, status (SUCCESS/FAIL) and error_reason. Surfaced by the
# Audit page via GET /api/audit/trace/{trace_id}. The matching audit_log
# entry carries only the trace_id pointer (so the human-friendly log
# stays compact and the heavy prompts only load on demand).
llm_traces = db.llm_traces


# ---------------------------------------------------------------------------
# Indexes (iter-13.120 — performance)
# ---------------------------------------------------------------------------
# Before this, no indexes existed on any collection, so every query filtered
# by `project_id` (i.e. almost every query in the app) did a full COLLSCAN.
# Symptom reported by users: "delete project takes forever + the whole UI
# feels sluggish". `delete_project` alone fires ~25 sequential `delete_many`
# calls, each of which scanned an entire collection. Adding a project_id
# index alone cuts p95 delete latency by ~50x on a project with any real KB.
#
# `create_index` is idempotent (Mongo will no-op if an equivalent index
# already exists), so this is safe to call on every startup. We use
# `background=True` to avoid blocking writes if the collection is large on
# an upgrade path.
_PROJECT_SCOPED = [
    kb_files, kb_chunks, kb_entities, kb_toon,
    conversations, messages,
    srs_documents, project_prompts, freeze_gates, audit_log, stage_context,
    data_models, bus_matrix, olap_models, migration_artifacts,
    arch_documents, arch_services,
    codegen_files, codegen_runs, parity_runs,
    token_usage_log,
    github_configs,
    living_artifacts, living_runs, living_reports,
    ontology_snapshots, business_ontologies,
    data_sources, legacy_analysis, kb_graph,
    kb_journeys, kb_journey_config,
    project_integrations,
    kb_git_sources, kb_module_selection,
    stage_confidence,
    factory_workspaces,
    agent_sessions, llm_traces,
]

async def ensure_indexes() -> None:
    """Create indexes we rely on for hot-path queries. Idempotent."""
    # Every project-scoped collection is filtered by project_id.
    for col in _PROJECT_SCOPED:
        try:
            await col.create_index("project_id", background=True)
        except Exception:
            # A pre-existing index with the same key but different options
            # would raise here. Don't block startup on that.
            pass

    # Extra hot-path composites.
    try:
        await projects.create_index("id", background=True, unique=False)
        await projects.create_index("tenant_id", background=True)
        await stage_context.create_index(
            [("project_id", 1), ("stage", 1)], background=True,
        )
        await messages.create_index(
            [("project_id", 1), ("conversation_id", 1)], background=True,
        )
        await kb_files.create_index(
            [("project_id", 1), ("filename", 1)], background=True,
        )
        await kb_chunks.create_index(
            [("project_id", 1), ("file_id", 1)], background=True,
        )
        await audit_log.create_index("at", background=True)
        await users.create_index("username", background=True)
        await tenants.create_index("id", background=True)
        # iter-17 — Multi-Agent CodeGen collections. Every hot-path query
        # filters by project_id; envelopes/tasks additionally look up by
        # (project_id, envelope_id) / (project_id, task_id).
        for _col in (codegen_envelopes, codegen_tasks, codegen_agent_runs, codegen_pipeline_state):
            try:
                await _col.create_index("project_id", background=True)
            except Exception:
                pass
        try:
            await codegen_envelopes.create_index(
                [("project_id", 1), ("envelope_id", 1)], background=True,
            )
            await codegen_tasks.create_index(
                [("project_id", 1), ("task_id", 1)], background=True,
            )
            await codegen_tasks.create_index(
                [("project_id", 1), ("wave", 1)], background=True,
            )
            await codegen_pipeline_state.create_index(
                "project_id", unique=True, background=True,
            )
        except Exception:
            pass
        # br_table_links is optional — some older deployments don't have it.
        try:
            await db.br_table_links.create_index("project_id", background=True)
        except Exception:
            pass
    except Exception:
        pass
