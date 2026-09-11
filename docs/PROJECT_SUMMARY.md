# LAMA — Project Summary & Flow Reference

> A quick-reference companion to `AGENTS.md`, `CLAUDE.md`, `memory/PRD.md` and
> `docs/ARCHITECTURE.md`. Read this first to get oriented; then dive into the
> deeper docs for specifics.

---

## 1. What LAMA Is

**LAMA — Legacy Application Modernization & Alignment.**
A full-stack AI-assisted *legacy → cloud-native* migration studio. A Migration
Architect (or Domain SME) points LAMA at a folder of legacy code
(PHP/CodeIgniter, classic ASP, JSP, .NET, Python, JS, etc.) and LAMA walks the
application through a deterministic **5-stage pipeline**, producing
freezable, GitHub-pushable artifacts at each stage.

**Reference pilot:** PMIS Migration Pilot —
`PHP 8 / CodeIgniter 4 / MariaDB → FastAPI / Python 3.12 / PostgreSQL`.

**Tenancy:** single-tenant, single active project (no project switcher UI;
internal project APIs remain for seed/admin use).

---

## 2. The 5-Stage Pipeline (single source of truth)

| # | Stage          | Key artifacts                                                       | Freeze gate writes…                            | Unlocks       |
|---|----------------|---------------------------------------------------------------------|------------------------------------------------|---------------|
| 1 | **Discovery**  | KB (OWL/YAML/TOON), IEEE-830 SRS (≈12 sections incl. ER), Business Ontology | `StageContext(Discovery)`                      | DataModel     |
| 2 | **DataModel**  | OLTP DDL, OLAP DDL, Bus Matrix, 3 migration scripts                 | `StageContext(DataModel)` (OLTP+OLAP frozen)   | Architecture  |
| 3 | **Architecture**| Service Map, HLD, LLD, API Contracts, Sequence Diagrams            | `StageContext(Architecture)`                   | CodeGen       |
| 4 | **CodeGen**    | Per-service source tree, Dockerfiles, ZIP, optional GitHub push     | `StageContext(CodeGen)`                        | Living        |
| 5 | **Living**     | Selenium tests, SRS-drift detection, runtime observability (P1)     | —                                              | —             |

**Strict sequencing.** Every stage-N+1 route calls
`pipeline.require_stage_context(project_id, stage="N", calling_stage="N+1")`
and returns HTTP 400 if upstream is not frozen.

**Implicit project promotion.** When stage N is frozen, the freeze handler
(a) writes StageContext and (b) sets `project.stage_status[N+1] = "available"`.

---

## 3. End-to-End Flow (happy path)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVERY                                                                │
│    a. UploadPanel → POST /api/kb/scan-folder (skip-patterns applied)        │
│    b. parsers.py → ZIP/file walk → kb/owl_extractor.py                      │
│       → CLASSES, METHODS, TABLES, COLUMNS, ROUTES, ROLES (kb_entities)      │
│    c. kb/toon.py serialises OWL → TOON (LLM-friendly), cached in kb_toon    │
│    d. kb/vector_store.py (Qdrant) indexes chunks for RAG                    │
│    e. kb/business_ontology.py clusters entities → business domains          │
│    f. kb/kb_graph.build_kb_graph() builds the property graph and runs the   │
│       GRAPHIFY ENRICHMENT pass (Anthropic high-tier via fabric, agent_key   │
│       "kb.graphify") — adds inferred CALLS/READS/WRITES edges and           │
│       BusinessEntity nodes; merged into the same kb_graph doc with          │
│       provenance="llm". Toggle: env LAMA_GRAPHIFY_ENRICH (default ON).      │
│    g. ChatPanel uses RAG + TOON-pruned-by-stage; intent="generate SRS"      │
│       auto-triggers SRS generation                                          │
│    h. /api/srs/generate/stream → SSE per section; uses PROMPT LIBRARY       │
│       governance bundle (gov.core → gov.role_analysis → ... → srs.spec)     │
│       and injects {graph_subgraph} via kb/graph_retriever                   │
│    i. SRSPanel edit/freeze; PDF + markdown export; optional GitHub push     │
│    → Freeze → StageContext(Discovery) written, DataModel unlocked           │
├─────────────────────────────────────────────────────────────────────────────┤
│ 2. DATAMODEL  (routes/datamodel.py, SSE allowed)                            │
│    Uses prompts: datamodel.oltp, datamodel.olap, datamodel.bus_matrix,      │
│    datamodel.chat → generates target PostgreSQL DDLs + migration scripts.   │
├─────────────────────────────────────────────────────────────────────────────┤
│ 3. ARCHITECTURE (routes/architecture.py, NO SSE — bg jobs + 2s poll)        │
│    Uses prompts: arch.recommend, arch.hld, arch.lld, arch.sequence,         │
│    arch.api_contracts, arch.chat → Service Map / HLD / LLD / Sequence /     │
│    API Contracts. Each prompt receives {graph_subgraph} sliced from the     │
│    GRAPHIFY-enriched kb_graph (via kb/graph_retriever).                     │
├─────────────────────────────────────────────────────────────────────────────┤
│ 4. CODEGEN  (routes/codegen.py)                                             │
│    _ensure_kb_graph() auto-builds the graph if missing. Uses prompts:       │
│    codegen.service, codegen.frontend, codegen.gap_recovery, codegen.chat,   │
│    codegen.docs → per-service file tree, Dockerfiles, ZIP, GitHub push.     │
│    Each prompt receives a service-scoped {graph_subgraph} from kb_graph.    │
├─────────────────────────────────────────────────────────────────────────────┤
│ 5. LIVING  (routes/living.py — skeleton)                                    │
│    Uses prompts: test.selenium, test.jmeter, drift.detector, diff.srs.      │
└─────────────────────────────────────────────────────────────────────────────┘
```

All LLM calls go through `llm.fabric_call()` (multi-provider fabric with
env-var OpenRouter fallback). Every state-changing action writes to
`audit_log`.

---

## 3.1 Graphified Knowledge Base (iter-13.19 → 13.32)

The KB is not just OWL + TOON + Qdrant — it also includes a **property
graph** built per project during *Build KB* and consumed by every
downstream prompt that needs structural grounding.

| Aspect | Where |
|--------|-------|
| Build / persist | `backend/kb/kb_graph.py::build_kb_graph()` → Mongo `kb_graph` |
| LLM enrichment ("graphify") | `kb_graph._graphify_with_llm()` → `fabric_call(agent_key="kb.graphify")` — tier **high** → **Anthropic Opus** by default |
| Retrieval | `backend/kb/graph_retriever.py::subgraph_yaml_for_terms / for_section` |
| Per-project on/off | Sidebar **Graph KB** toggle → `projects.settings.use_graph_kb` (resolved by `kb/graph_config.py`) |
| Env switches | `LAMA_USE_GRAPH_KB` (downstream injection), `LAMA_GRAPHIFY_ENRICH` (enrichment pass) |

**Pipeline:**

```
Build KB ─► OWL extract ─► business ontology ─► build_kb_graph()
                                                ├─ deterministic edges
                                                └─ _graphify_with_llm()  ← Anthropic
                                                       │
                                                       ▼
                                              kb_graph (Mongo)
                                                       │
                ┌──────────────────────────────────────┼─────────────────────────────┐
                ▼                                      ▼                             ▼
        subgraph_yaml_for_section          subgraph_yaml_for_terms          subgraph_yaml_for_terms
                │                                      │                             │
                ▼                                      ▼                             ▼
            SRS gen                       Architecture (HLD/LLD/Seq/API)      CodeGen (svc/fe/gap)
```

- **Node types:** `Module, Class, Method, Table, Column, Route, Role, BusinessEntity`.
- **Edge types:** `HAS_METHOD, HAS_COLUMN, CALLS, READS, WRITES, EXPOSES, GUARDED_BY, BELONGS_TO_MODULE, BELONGS_TO_ENTITY, REFERENCES_TABLE`.
- **Enriched** nodes/edges carry `provenance="llm"` for traceability; the LLM is constrained to use only existing node names (no hallucinated endpoints) — the only new node type it may add is `BusinessEntity`.
- Stats are surfaced at `kb_graph.stats.graphify = {added_nodes, added_edges, model, skipped}` and via `GET /api/kb/{pid}/graph-summary`.

---

## 4. Tech Stack (pinned — do not silently bump)

| Layer        | Tech                                                                              |
|--------------|-----------------------------------------------------------------------------------|
| Backend      | Python 3.11, FastAPI 0.110, Motor 3.3, Pydantic 2.13, httpx 0.28, PyGithub 2.9    |
| LLM fabric   | OpenRouter (default), Anthropic, OpenAI, Groq, Ollama — `backend/fabric/model_fabric.py` |
| Vector DB    | Qdrant (`QDRANT_URL` / `QDRANT_API_KEY`)                                          |
| DB           | MongoDB 7 (system-of-record for LAMA; PostgreSQL is the migration *target*)       |
| Frontend     | React 19, react-router-dom 7, Tailwind 3.4, Radix UI, shadcn/ui, D3 7.9, Mermaid 11, Monaco |
| Layout       | `react-resizable-panels@2.1.7` (PINNED)                                           |
| Build        | CRA 5 + `@craco/craco 7`, yarn 1.22 via corepack (**never `npm install`**)        |
| Runtime img  | `python:3.11-slim-bookworm`; supervisord runs nginx + mongod + uvicorn            |
| Container    | Port **8382**, image `mishramesh/lama:latest`                                     |

---

## 5. Repo Map (high signal only)

```
backend/
  server.py            mounts every /api router
  db.py                Motor + every Mongo collection accessor (SOT)
  models.py            Pydantic models
  pipeline.py          Stage handoff (get/require/save_stage_context)
  llm.py               fabric_call() drop-in (OpenRouter fallback)
  seed.py              idempotent: PMIS pilot + global prompts + agents
  fabric/              multi-provider LLM fabric, budgets, usage logging
  kb/                  parsers, tech_detector, owl_extractor, owl_export,
                       toon, business_ontology, vector_store (Qdrant)
  codegen/             file_templates, zip_builder
  routes/              projects, kb, chat, srs, prompts, github, audit,
                       datamodel, architecture, codegen, living, console
  tests/               pytest suites (test_lama_v2, _v4, _datamodel,
                       _arch_codegen, _console, _iter10_ontology,
                       _iter11_living_diff, _migrationos)

frontend/src/
  App.js               BrowserRouter — one route per stage page
  components/          Sidebar, ChatPanel, ERDiagram, SRSPanel, UploadPanel,
                       MiniConsole, ui/* (shadcn)
  pages/               Discovery, DataModel, Architecture, CodeGen, Living,
                       Console, OntologyStudio, PromptLibrary, AuditLog,
                       GitHubSettings
  lib/api.js           all backend calls (axios) — keep in sync with routes/*
  state/ProjectContext.jsx   single active project

memory/PRD.md          append-only iteration log (the "why")
test_reports/          per-iteration JSON + pytest XML
docs/ARCHITECTURE.md   detailed flow & sequence diagrams
```

---

## 6. The Prompt Library — how it plugs into the flow

The Prompt Library is the **third architectural layer** of LAMA (alongside
Project Manager and the KB Engine). Every LLM call across all five stages
ultimately pulls its system / governance text from this library, so editing
a prompt is how an architect "tunes" the pipeline without touching code.

### 6.1 Storage

Two MongoDB collections (`backend/db.py`):

| Collection         | Purpose                                       |
|--------------------|-----------------------------------------------|
| `prompts`          | **Global** prompt library (versioned)         |
| `project_prompts`  | **Per-project overrides** (versioned, upsert) |

A `Prompt` document has: `key, stage, template, description, version, updated_at`
(see `backend/models.py::Prompt` / `ProjectPrompt`).

### 6.2 Seeding (`backend/seed.py`)

On every backend boot, `@app.on_event("startup") → run_seed()` runs
idempotently and writes the canonical **`GLOBAL_PROMPTS`** list — currently
~30 prompts grouped by stage:

| Stage         | Prompt keys                                                                                                                |
|---------------|----------------------------------------------------------------------------------------------------------------------------|
| Discovery     | `gov.core`, `gov.role_analysis`, `gov.field_traceability`, `gov.business_rule_extraction`, `legacy.deep_analyzer`, `srs.spec.ieee29148`, `srs.generate`, `srs.revalidation`, `srs.gap_question`, `srs.edit` |
| DataModel     | `datamodel.oltp`, `datamodel.olap`, `datamodel.bus_matrix`, `datamodel.chat`, `datamodel.optimise`                         |
| Architecture  | `arch.recommend`, `arch.hld`, `arch.lld`, `arch.sequence`, `arch.api_contracts`, `arch.chat`                               |
| CodeGen       | `codegen.service`, `codegen.frontend`, `codegen.gap_recovery`, `codegen.chat`, `codegen.docs`                              |
| Living        | `test.selenium`, `test.jmeter`, `drift.detector`, `diff.srs`                                                               |

> **Routing model (iter-13.30+):** call sites pass only an `agent_key`; the
> concrete model is resolved by `AGENT_COMPLEXITY[agent_key]` → tier
> (`low` / `medium` / `high`) → `Console.routing[tier]` of the active
> provider. Defaults: `high` = Anthropic Opus, `medium` = Anthropic Sonnet.
>
> **Agent-only keys (no DB-stored prompt):** some routing keys do not have
> a row in the `prompts` collection — their system prompt lives next to
> the call site. The most notable is `kb.graphify` (tier=high), whose
> system prompt is `kb/kb_graph.py::_GRAPHIFY_SYS_PROMPT`. It is editable
> per provider via `Console → Agent Fabric → kb.graphify` (model only),
> but the prompt text itself is code-owned to keep the strict-JSON
> contract stable.

Each seed entry may carry `force_update: True`; the seeder bumps `version`
**only** when `force_update=True` AND the stored template differs — so
rev-bumps are intentional, not noisy.

### 6.3 HTTP API (`backend/routes/prompts.py`)

| Method | Path                                          | Purpose                                          |
|--------|-----------------------------------------------|--------------------------------------------------|
| GET    | `/api/prompts`                                | List all global prompts (sorted by key)          |
| PUT    | `/api/prompts/{key}`                          | Update a global prompt → auto-bumps `version`    |
| GET    | `/api/prompts/project/{project_id}`           | List project-specific overrides                  |
| PUT    | `/api/prompts/project/{project_id}/{key}`     | Upsert a project override (version starts at 1)  |
| DELETE | `/api/prompts/project/{project_id}/{key}`     | Remove override → falls back to global           |

### 6.4 Runtime resolution

Each stage route uses a tiny `_get_prompt(project_id, key)` helper that:

1. Looks in `project_prompts` first (project override wins).
2. Falls back to the global `prompts` collection.
3. Returns `""` if not found (caller decides how to handle).

For SRS the Discovery stage builds a **governance bundle**
(`srs.py::_load_governance_bundle`) by concatenating, in strict order:
`gov.core → gov.role_analysis → gov.field_traceability → gov.business_rule_extraction → srs.spec.ieee29148`,
prepended with the detected legacy-stack block and target-stack guardrails.
The bundle is then sent as the system prompt for each section generation.

DataModel / Architecture / CodeGen / Living each pull their stage-prefixed
prompts the same way (`datamodel.*`, `arch.*`, `codegen.*`, `test.*` /
`drift.*` / `diff.*`).

### 6.5 Console preview / test-run (`backend/routes/console.py`)

The Console page resolves any prompt template against the current project and
substitutes variables before previewing or test-running it:

- `{project_name}` → `project.name`
- `{source_tech}`  → `project.source_tech`
- `{target_tech}`  → `project.target_tech`
- `{toon_context}` → first ~6000 chars of `kb_toon`
- Unknown vars → rendered as `<var_name>` placeholders

Token cost is estimated and displayed before execution.

### 6.6 Frontend page (`frontend/src/pages/PromptLibrary.jsx`)

Tabs: **Global** vs **Project**. Each prompt is rendered as a `PromptCard`:

- `data-testid="prompt-card-{key}"`
- `data-testid="prompt-textarea-{key}"`
- `data-testid="prompt-save-{key}"` (disabled until dirty)
- `data-testid="prompt-reset-{key}"` (Project tab only — deletes override)
- Tabs: `tab-global`, `tab-project`, container `prompt-tabs`
- Version badge `v{N}` per card; bumps on every save.

API helpers in `frontend/src/lib/api.js`:
`listPrompts`, `updatePrompt`, `listProjectPrompts`, `updateProjectPrompt`.

### 6.7 Mental model

> The Prompt Library is the **policy layer** for the pipeline. The KB Engine
> provides the *facts* (OWL/TOON/RAG). The Prompt Library provides the
> *governance & instructions*. Project overrides let one tenant deviate
> without forking the global defaults, and rev-bumps give you a primitive
> change-log per prompt.

---

## 7. Critical Contracts (load-bearing — see `AGENTS.md` §4 for the full list)

1. **Stage handoff = `stage_context`** collection (one doc per `(project_id, stage)`).
2. **All LLM calls go through `llm.fabric_call()`**, never `httpx` directly.
3. **TOON pruning is stage-aware** (`routes/chat.py::prune_toon`) — add a slice when adding a stage.
4. **Architecture stage does NOT use SSE** (K8s ingress 60s timeout).
5. **Freeze gates are typed-confirmation** (`"FREEZE"` / `"RESET"`) in the UI.
6. **Skip-patterns on folder scan** order matters (Iter-2 fix).
7. **`data-testid` attributes are part of the contract** — tests assert on them.
8. **Audit-log every state change** via the `audit_log` collection.
9. **Never `npm install`** (yarn + corepack only); never bump
   `react-resizable-panels` past 2.1.7 without manual UI verification.

---

## 8. Common Commands

```bash
# Local dev — split processes
cd backend  && uvicorn server:app --reload --port 8000
cd frontend && yarn install && yarn start

# Single-image (production-like)
docker compose up -d                       # mishramesh/lama:latest on :8382
docker compose logs -f lama
curl http://127.0.0.1:8382/health          # → {"ok":true}

# Tests
pytest backend/tests/
pytest backend/tests/test_lama_v4.py -k test_chat -x
```

---

## 9. Where to look first

| If you need to …                              | Open                                              |
|-----------------------------------------------|---------------------------------------------------|
| Understand the pipeline                       | `backend/pipeline.py` + `memory/PRD.md`           |
| Trace which prompt a stage uses               | `backend/seed.py::GLOBAL_PROMPTS` + the route file |
| Edit / version-bump a prompt                  | `frontend/src/pages/PromptLibrary.jsx`            |
| Predict / pick a modern target stack          | `backend/kb/stack_suggester.py` + `frontend/src/components/TargetStackSuggester.jsx` |
| Add a new stage handoff field                 | `models.py::StageContext.outputs` + writer        |
| Add a new LLM provider                        | `backend/fabric/model_fabric.py` presets          |
| Add a new SRS section                         | `routes/srs.py::SECTION_CONFIGS`                  |
| Wire a new frontend page                      | `App.js` route → `pages/` → `lib/api.js` helper   |
| Find which collection stores X                | `backend/db.py` (single source of truth)          |
| See latest known-working state                | `test_reports/iteration_<N>.json`                 |
| Container boot order                          | `docker/supervisord.conf` + `docker/entrypoint.sh`|

---

*Keep this file lean. The "why-and-when" log is `memory/PRD.md`; exhaustive
operational rules are in `AGENTS.md`; deep flow diagrams in
`docs/ARCHITECTURE.md`.*

