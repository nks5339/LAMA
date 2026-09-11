# AGENTS.md — LAMA Project Operating Guide

> Companion file for AI coding agents (Claude / Copilot / Cursor / Codex).
> Read this **first** before making any change. Treat as a contract.
> Last reviewed: Iteration 12.

---

## 1. What is LAMA?

**LAMA — Legacy Application Modernization & Alignment** is a full-stack
AI-assisted *legacy → cloud-native* migration studio. A Migration Architect or
Domain SME points LAMA at a folder of legacy code (PHP/CodeIgniter, classic ASP,
JSP, etc.) and LAMA walks the application through a deterministic **5-stage
pipeline**, producing freezable, GitHub-pushable artifacts at each stage.

The reference pilot is **PMIS Migration Pilot** —
`PHP 8 / CodeIgniter 4 / MariaDB → FastAPI / Python 3.12 / PostgreSQL`.

### The 5 stages (single source of truth)

| # | Stage          | Output Artifacts                                                          | Freeze Gate                                         | Unlocks       |
|---|----------------|---------------------------------------------------------------------------|-----------------------------------------------------|---------------|
| 1 | **Discovery**  | KB (OWL/TOON), IEEE-830 SRS (9 sections incl. ER), Business Ontology      | SRS frozen → `StageContext(Discovery)` written       | DataModel     |
| 2 | **DataModel**  | OLTP DDL, OLAP DDL, Bus Matrix, 3 migration scripts                       | OLTP + OLAP both frozen → `StageContext(DataModel)`  | Architecture  |
| 3 | **Architecture**| Service Map, HLD, LLD, API Contracts, Sequence Diagrams                  | All frozen → `StageContext(Architecture)`            | CodeGen       |
| 4 | **CodeGen**    | Per-service source tree, Dockerfiles, ZIP, optional GitHub push           | Freeze → `StageContext(CodeGen)`                     | Living        |
| 5 | **Living**     | Selenium tests, SRS-drift detection, runtime observability *(P1 backlog)* | —                                                   | —             |

Stages are **strictly sequential**. Every backend route in stage N+1 calls
`pipeline.require_stage_context(project_id, stage="N", calling_stage="N+1")`
and returns HTTP 400 if upstream is not frozen. **Do not bypass this.**

---

## 2. Repository Layout

```
lama-main/
├── Dockerfile                  # Single-image bundle: React + FastAPI + Mongo + Nginx + supervisord
├── docker-compose.yml          # Hostinger deploy (image mishramesh/lama:latest, port 8382)
├── docker/                     # nginx.conf, supervisord.conf, entrypoint.sh, build-and-push.sh
├── backend/                    # FastAPI app (Python 3.11)
│   ├── server.py               # FastAPI entrypoint — mounts all /api routers
│   ├── db.py                   # Motor (async Mongo) + collection accessors
│   ├── models.py               # Pydantic models (Project, KB*, SRS, StageContext, Arch*, Codegen*, Console*)
│   ├── pipeline.py             # Inter-stage handoff (get/require/save_stage_context)
│   ├── llm.py                  # OpenRouter client + fabric_call() drop-in (routes through fabric if configured)
│   ├── seed.py                 # Auto-seeds PMIS pilot, 12 prompts, 22 agents on startup
│   ├── fabric/model_fabric.py  # Multi-provider LLM fabric (presets, routing, budgets, usage logging)
│   ├── kb/                     # Knowledge-base engine
│   │   ├── parsers.py          # File/ZIP/folder parsing, skip-patterns
│   │   ├── owl_extractor.py    # PHP + SQL → OWL classes/methods/tables/columns/routes/roles
│   │   ├── owl_export.py       # JSON-LD export with migration hints
│   │   ├── toon.py             # OWL → TOON (compact LLM-friendly serialisation)
│   │   ├── business_ontology.py# Deterministic clustering + LLM enrichment → business entities
│   │   ├── module_inventory_parser.py
│   │   └── vector_store.py     # Qdrant (semantic RAG for SRS sections)
│   ├── codegen/                # Stage 4 helpers (file_templates, zip_builder)
│   ├── routes/                 # All /api routers (one file per concern)
│   │   ├── projects.py, kb.py, chat.py, srs.py, prompts.py, github.py, audit.py
│   │   ├── datamodel.py        # Stage 2 (12 endpoints, SSE)
│   │   ├── architecture.py     # Stage 3 (recommend/HLD/LLD/sequence; jobs not SSE — K8s 60s ingress)
│   │   ├── codegen.py          # Stage 4 (generate, files CRUD, ZIP, GitHub push)
│   │   ├── living.py           # Stage 5 (skeleton)
│   │   └── console.py          # Model Fabric / Agent Fabric / Prompts / Usage
│   └── tests/                  # pytest — see §7
├── frontend/                   # React 19 + CRA/CRACO + Tailwind + Radix + shadcn/ui
│   ├── package.json            # craco scripts (start/build/test)
│   ├── plugins/health-check/   # Webpack plugin exposing /health endpoints
│   └── src/
│       ├── App.js              # BrowserRouter routes (one route per stage page)
│       ├── components/         # Sidebar, ChatPanel, ERDiagram (D3), SRSPanel, UploadPanel, MiniConsole, ui/* (shadcn)
│       ├── pages/              # Discovery, DataModel, Architecture, CodeGen, Living, Console, OntologyStudio, PromptLibrary, AuditLog, GitHubSettings
│       ├── lib/api.js          # All backend calls (axios) — keep in sync with routes/*
│       └── state/ProjectContext.jsx  # Single active project (single-tenant)
├── memory/PRD.md               # Append-only iteration log + acceptance status — read for history
├── test_reports/               # JSON + pytest XML — one file per iteration
└── tests/                      # Top-level placeholder (real tests live in backend/tests)
```

---

## 3. Tech Stack & Versions (do not silently bump)

| Layer        | Tech                                                                                   |
|--------------|----------------------------------------------------------------------------------------|
| Backend      | Python 3.11, FastAPI 0.110, Motor 3.3, Pydantic 2.13, httpx 0.28, PyGithub 2.9         |
| LLM          | OpenRouter (deepseek-chat, deepseek-coder, qwen2.5-72b) via fabric (multi-provider)    |
| Vector DB    | Qdrant (`QDRANT_URL` / `QDRANT_API_KEY`) — collection auto-created on Build KB         |
| DB           | MongoDB 7 (bundled in the runtime image; data at `/data/db`)                           |
| Frontend     | React 19, react-router-dom 7, Tailwind 3.4, Radix UI, shadcn/ui, D3 7.9, Mermaid 11, Monaco |
| Layout       | `react-resizable-panels@2.1.7` (do not upgrade — known break)                          |
| Build        | CRA 5 via `@craco/craco 7`, yarn 1.22 (Corepack)                                       |
| Runtime img  | `python:3.11-slim-bookworm`, supervisord runs nginx + mongod + uvicorn                 |
| Container    | Public port **8382**, image `mishramesh/lama:latest`, volume `lama_mongo_data → /data/db` |

---

## 4. Critical Architectural Contracts

These are load-bearing. Breaking any one of them silently breaks the pipeline.

1. **Stage handoff = `stage_context` collection.** Read with `get_stage_context`,
   require with `require_stage_context`, write with `save_stage_context` (auto
   increments `version`). One doc per `(project_id, stage)`.
2. **Project promotion is implicit.** When stage N is frozen, its freeze
   handler must (a) write StageContext and (b) set `project.stage_status[N+1] = "available"`.
3. **Single-tenant, single active project.** There is **no project switcher
   UI**. The frontend reads the active project from `ProjectContext`; the
   backend project APIs still exist for internal/seed use.
4. **LLM calls go through `llm.fabric_call()`**, never `httpx` directly.
   Stage routes import it via the alias `from llm import fabric_call as chat_completion`.
   `fabric_call` falls back to env-var OpenRouter when fabric is unconfigured
   OR when a configured provider returns empty / raises (this fallback was the
   Iter-12 P0 fix for empty SRS sections).
5. **TOON pruning is stage-aware.** `routes/chat.py::prune_toon()` keeps only
   the slices relevant to the current stage (e.g. Discovery → CLASSES/ROUTES;
   DataModel → TABLES). Add a new slice if you add a stage.
6. **Architecture stage does NOT use SSE.** Long jobs use background tasks +
   2-second polling because the production K8s ingress has a 60s timeout.
   DataModel + SRS *do* use SSE but only because they stream short chunks.
7. **Freeze gates are typed-confirmation only in the UI** (typed "RESET" /
   "FREEZE"). Do not weaken these.
8. **Skip-patterns on folder scan are deliberate**: `node_modules`, `.git`,
   `vendor`, `__pycache__`, `*.bak`, `*.save`, `*_bkp`, `*_old`, `*_backup`,
   `*.php_*`. Order matters — there was an ordering bug fixed in Iter-2.
9. **Backend boots on `@app.on_event("startup")` → `run_seed()`.** Seed is
   idempotent and uses `force_update=True` for prompt rev-bumps. Migrating to
   FastAPI `lifespan` is on the P2 backlog — do not do it casually.
10. **CORS** is wide-open by default (`CORS_ORIGINS=*`). Tighten via env-var
    in production deployments.

---

## 5. Environment Variables

Required for any meaningful run:

```env
OPENROUTER_API_KEY=sk-or-...        # required for LLM
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LAMA_DEFAULT_MODEL=deepseek/deepseek-chat
QDRANT_URL=http://<host>:6333       # required for RAG chat / SRS RAG
QDRANT_API_KEY=...
MONGO_URL=mongodb://127.0.0.1:27017 # bundled mongod in single-image deploy
DB_NAME=lama
CORS_ORIGINS=*                       # comma-separated
```

A `backend/.env` is used during local dev; it is `rm -f`'d from the image at
build time so production env comes from `-e` flags / compose `environment:`.

---

## 6. How to Run

### Local dev (split processes)
```bash
# backend
cd backend && uvicorn server:app --reload --port 8000
# frontend (proxy via REACT_APP_BACKEND_URL or craco proxy)
cd frontend && yarn install && yarn start
```

### Single-image (production-like)
```bash
docker compose up -d           # uses mishramesh/lama:latest on port 8382
docker compose logs -f lama
# Health: curl http://127.0.0.1:8382/health  → 200
```

### Build & push image (maintainer-only)
```bash
./docker/build-and-push.sh     # see docker/README.md for tag conventions
```

---

## 7. Testing

- **Backend:** `pytest backend/tests/` — current count covered in `test_reports/`.
  Key suites: `test_lama_v2.py`, `test_lama_v4.py`, `test_datamodel.py`,
  `test_arch_codegen.py`, `test_console.py`, `test_iter10_ontology.py`,
  `test_iter11_living_diff.py`.
- **Frontend smoke:** Playwright-style runs done by the testing agent; results
  in `test_reports/iteration_*.json`.
- **What is NOT tested end-to-end** (would burn OpenRouter tokens, do only on
  explicit request): full SRS generation, DataModel SSE, Architecture
  HLD/LLD/Sequence, CodeGen ZIP+GitHub push, Living drift detection.
- **502s on chat-edit tests are acceptable** (LLM env timeout) — the test
  asserts `in (200, 502)`.

---

## 8. Conventions for AI Agents (rules of engagement)

1. **Read `memory/PRD.md` first.** It is the append-only iteration log and
   the closest thing to a changelog. Recent iterations override earlier ones.
2. **Never invent stages, collections, or routes.** Use the tables in §1 and
   the lists in `db.py` / `routes/`. New collection → add to `db.py`. New
   route → register in `server.py`. New API call → add to
   `frontend/src/lib/api.js`.
3. **Pydantic over `dict` payloads** for any new POST body (P1 backlog
   explicitly calls this out for `/api/kb/scan-folder`, `/api/srs/*`,
   `/api/architecture/*`, `/api/codegen/*`).
4. **One file per route concern.** Do not cross-import between
   `routes/datamodel.py` and `routes/architecture.py` — go through
   `pipeline.py`.
5. **No hard dependency on `httpx` for LLM calls.** Use `fabric_call`.
6. **Frontend pages own their layout** (resizable PanelGroup). Reuse
   `ERDiagram.jsx`, `ChatPanel.jsx`, `MiniConsole.jsx` rather than re-rolling.
7. **`data-testid` attributes are part of the contract** (the testing agent
   asserts on them). Format: `stage-{key}-badge-{frozen|ready|locked}`,
   `owl-export-btn`, etc. Keep them stable.
8. **Audit-log everything that changes state** via `audit_log` collection.
9. **Never call `npm install`** — this repo is yarn-only (corepack).
10. **Never bump `react-resizable-panels` past 2.1.7** without verifying
    panel-collapse behaviour on Discovery and DataModel pages.

---

## 9. Known Hazards / Footguns

- `routes/projects.py` had a syntax-corruption regression in Iter-5
  (`e")` instead of `@router.get(...)`). If the backend won't boot, grep for
  truncated decorators first.
- `fabric_call` will silently route through an inactive provider if its
  `is_active=True` but the API key is invalid → cascade of 401s. Iter-12 added
  empty/exception fallback; the P2 backlog item to add a `validate/{id}` ping
  endpoint is **not** done yet.
- `Sidebar.jsx` nests a Radix Tooltip trigger inside `<button>` (React
  hydration warning). Known, P2.
- CodeGen file tree is intentionally flattened (non-recursive) because of a
  babel `visual-edits` plugin bug. Don't "fix" by recursing without testing.
- SRS auto-trigger from chat (`srs_triggered: true`) silently swallows
  failures. P2 backlog.
- Migrating from `@app.on_event` to FastAPI `lifespan` will reorder seed/db
  init — coordinate with `run_seed()` idempotency.

---

## 10. Where to Look First

| If you need to …                                | Open                                              |
|--------------------------------------------------|---------------------------------------------------|
| Understand the pipeline                          | `backend/pipeline.py` + `memory/PRD.md` §Stage 2  |
| Add a new stage handoff field                    | `models.py::StageContext.outputs`, then writer    |
| Add a new LLM provider                           | `backend/fabric/model_fabric.py` presets dict     |
| Add a new SRS section                            | `routes/srs.py::SECTION_CONFIGS` (currently 9)    |
| Wire a new frontend page                         | `App.js` route → `pages/` → `lib/api.js` helpers  |
| Find which collection stores X                   | `backend/db.py` (single source of truth)          |
| See latest known-working state                   | `test_reports/iteration_<N>.json`                 |
| Understand container boot order                  | `docker/supervisord.conf` + `docker/entrypoint.sh`|

---

## 11. Out of Scope

- Multi-tenant / multi-project UI (intentionally removed in Iter-2).
- Migration to non-PostgreSQL targets (target stack is fixed per project).
- Front-end rewrites to Vite / Next.js / TanStack (CRA + CRACO is the chosen baseline).
- Replacing MongoDB with anything else (it is the *system-of-record* for LAMA itself; PostgreSQL is only the *migration target* of user projects).

---

*End of AGENTS.md — keep this file under ~400 lines. Detailed flow & sequence
diagrams live in `docs/ARCHITECTURE.md`.*

