# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Project memory for **Claude Code** working on the LAMA repository.
> **Reflects the tree as of iter-17.18.** `memory/PRD.md` is append-only and
> always newer than this file — when the two disagree, PRD.md wins.
> Auto-discovered (root-level `CLAUDE.md`). Keep this file under ~350 lines
> (raised from 250 at iter-17 — multi-tenancy, the Factory modes and the
> Tools track are all load-bearing and none of them are discoverable by
> skimming the tree).
> For exhaustive operational rules see `AGENTS.md`; for iteration history
> see `memory/PRD.md`; for deep architecture see `docs/ARCHITECTURE.md`.

---

## Project Overview

**LAMA — Legacy Application Modernization & Alignment.** A full-stack
AI-assisted *legacy → cloud-native* migration studio. Point it at a folder
of legacy code (PHP/CodeIgniter, JSP, .NET, Python, JS, etc.) and it walks
the application through a deterministic **5-stage pipeline**, producing
freezable, GitHub-pushable artifacts at each stage.

Reference pilot: **PMIS Migration Pilot** —
`PHP 8 / CodeIgniter 4 / MariaDB → FastAPI / Python 3.12 / PostgreSQL`.

| # | Stage | Output | Freeze gate unlocks |
|---|-------|--------|---------------------|
| 1 | **Discovery**     | KB (YAML/TOON), IEEE-830 SRS (12 sections), Business Ontology | DataModel |
| 2 | **DataModel**     | OLTP DDL, OLAP DDL, Bus Matrix, 3 migration scripts            | Architecture |
| 3 | **Architecture**  | Service Map, HLD, LLD, API Contracts, Sequence Diagrams        | CodeGen |
| 4 | **CodeGen**       | Per-service source tree, Dockerfiles, ZIP, GitHub push         | Living |
| 5 | **Living**        | Selenium tests, SRS-drift detection, runtime observability     | — |

**Stages are strictly sequential.** Every stage-N+1 route calls
`pipeline.require_stage_context(project_id, stage="N", calling_stage="N+1")`
and returns HTTP 400 if upstream is not frozen. **Do not bypass this.**
(`routes/pipeline.py`, iter-13.66, can mark an intermediate stage *skipped* —
that is the only sanctioned bypass.)

**Two tracks exist besides the pipeline** — don't assume all work flows
through the 5 stages:

- **Tools** (`routes/tools.py`) — **Gap Analyzer** (`/api/tools/gap-analyzer/*`,
  has its own freeze/unfreeze + export) and **Transformer**
  (`/api/tools/transformer/*`), a standalone code-transform super-agent.
  Neither requires a frozen upstream stage.
- **Multi-Agent CodeGen** (iter-17, `/api/codegen/{pid}/multi-agent/*`) — an
  agentic alternative to single-shot Stage-4 that DOES require frozen
  Architecture. Agents: `context_manager → planner → coder_be | coder_fe →
  verifier → reviewer → tester → traceability_gate → finalizer`. BE/FE routing
  is Planner LLM + a deterministic override (`_route_task_to_coder`) — a FE
  file must never reach the BE coder. Backend + tests only; **the UI still
  drives the single-shot flow**, so don't assume the page exercises it.

---

## Tech Stack & Versions (do not silently bump)

| Layer | Tech |
|-------|------|
| Backend | Python 3.11, FastAPI 0.110, Motor 3.3, Pydantic 2.13, httpx 0.28, PyYAML 6.0, PyGithub 2.9 |
| LLM    | OpenRouter / Anthropic / OpenAI / Azure / Gemini / Groq / Ollama via `backend/fabric/model_fabric.py`. **iter-13.30:** NO hard-coded vendor defaults at call sites — `AGENT_COMPLEXITY[agent_key]` + Console's `provider.routing[tier]` resolve the model for every call. **iter-19:** six tiers — `trivial / low / medium / high / critical / reasoning` (`TIER_ORDER`). `resolve_tier_model` walks `TIER_FALLBACK_CHAIN` when a row lacks a tier, so pre-iter-19 rows (low/medium/high only) keep routing. `reasoning` is a sideways step for diagnosis (o-series), not a rung above `critical`. |
| Vector DB | Qdrant — the collection is auto-created on Build KB, but the whole subsystem is **inert** unless `QDRANT_URL` **or** `QDRANT_PATH` is set (`kb/vector_store.py:44-48`); callers then degrade to TOON-only. `QDRANT_PATH` selects the embedded on-disk engine — no server needed. |
| Mongo   | MongoDB 7 — system of record for LAMA itself (PostgreSQL is the migration *target*, not the store) |
| Frontend| React 19, react-router-dom 7, Tailwind 3.4, Radix UI (9 packages — 22 unused ones removed 2026-09), D3 7.9, Mermaid 11, Monaco, `react-resizable-panels@2.1.7` *(pinned — do not upgrade)* |
| Build   | CRA 5 via `@craco/craco 7`, **yarn 1.22 (corepack)** — **never `npm install`** |
| Runtime | `python:3.11-slim-bookworm`, supervisord runs nginx + mongod + uvicorn. Image: `mishramesh/lama:latest`. Port 8382. |

---

## Common Commands

### Local dev (split processes — recommended for fast iteration)
```bash
# backend (hot reload via uvicorn)
cd backend && uvicorn server:app --reload --port 8000

# frontend (CRA dev server, proxy via REACT_APP_BACKEND_URL)
cd frontend && yarn install && yarn start
```

### Single-image (production-like, with code bind-mounts for live edits)
```bash
docker compose up -d                      # uses mishramesh/lama:latest on :8382
docker compose logs -f lama
curl http://127.0.0.1:8382/health         # → {"ok":true}

# After Python edits:  docker compose restart lama   (uvicorn reloads)
# After React edits:   (cd frontend && yarn build) && docker compose restart lama
#   → nginx serves the freshly-built bundle via the ./frontend/build bind-mount
```

### Tests
```bash
pytest backend/tests/                     # ~70 suites under backend/tests/
pytest backend/tests/test_lama_v4.py -k test_chat -x   # single test
pytest backend/tests/test_iter17_codegen_multiagent.py -q

# Suites are named test_iter<N>_<topic>.py and pin the behaviour of ONE
# iteration — read the matching memory/PRD.md entry before editing a suite.
# Most use the in-memory FakeCollection fixture pattern (no live Mongo);
# test_iter1541_traceability_gate.py is the canonical example to copy.
# `_smoke_*.py` files are manual scripts, not collected by pytest.

pyflakes backend/routes/codegen.py        # the PRD's standing verification step
# Frontend smoke runs are driven by the testing agent and land in test_reports/
```
`502` on chat-edit tests is acceptable (LLM env timeout) — the test asserts
`in (200, 502)`.

### Lint / format
No project-wide formatter is enforced. Match the surrounding file's style.

```bash
ruff check backend                # backend/ruff.toml — the waste bar (E9,F,ARG)
cd frontend && yarn lint          # frontend/eslint.config.js — flat config
```

ESLint stays disabled at *build* time (`DISABLE_ESLINT_PLUGIN=true`) and
`yarn lint` is a separate step on purpose: yarn hoists eslint 9.23.0, but
react-scripts depends on ^8.3.0 and nests its own 8.57.1, which is the one
CRA's webpack plugin resolves. Flat config and eslintrc cannot be shared
between them, so wiring lint into the build would mean downgrading. Both
bars are currently clean: 0 ruff findings, 0 eslint errors.

---

## Repository Layout (high signal only)

```
backend/                     # FastAPI app
  server.py                  # 21 router mounts from 20 route modules
                             #   (datamodel.py exports router AND factory_router)
  db.py                      # Motor + all 59 collection accessors — SINGLE SOURCE OF TRUTH
  pipeline.py                # Inter-stage handoff (get/require/save_stage_context)
  llm.py                     # fabric_call() — the ONLY LLM entry point (3 modes, contract #4)
  seed.py                    # Idempotent startup seed: PMIS pilot + prompts + agent_configs
  auth.py                    # JWT → {user, tenant}; get_current_user / require_super_admin
  context_bundler.py         # build_stage_context() — canonical per-stage context gather
  confidence.py              # Multi-model confidence vote → stage_confidence / freeze badges
  agent_memory.py            # Rolling session memory for fabric_call(session_id=...)
  factory_orchestrator.py    # Factory (api.factory.ai) session bridge, per-(tenant,project)
  fabric/model_fabric.py     # Provider presets, AGENT_COMPLEXITY tiers, budgets
  fabric/factory_cli.py      # Local `droid` CLI transport (LAMA_FACTORY_MODE=cli)
  kb/                        # Discovery engine: parsers, tech_detector, owl_extractor,
                             #   owl_export (YAML), toon, business_ontology, vector_store
  codegen/                   # Stage-4 helpers: file_templates, zip_builder, parity_loop
  datamodel/                 # Stage-2 generators: oltp, olap, bus_matrix, migration
  integrations/              # Catalog + templates (audit_logger, dpg_india)
  routes/                    # One file per concern — see the inventory above
frontend/src/
  lib/api.js                 # ALL backend calls — keep in sync with routes/*
  state/ProjectContext.jsx   # The one active project within the signed-in tenant
  pages/                     # DiscoveryV2 (NOT Discovery.jsx) + the pages listed above
memory/PRD.md                # Append-only iteration log — the "why" behind every contract
AGENTS.md                    # Exhaustive operational rules (this file is the summary)
```

---

## Critical Architectural Contracts (load-bearing — don't violate)

1. **Stage handoff = `stage_context` collection.** Read via `get_stage_context`,
   require via `require_stage_context`, write via `save_stage_context` (auto
   increments `version`). One doc per `(project_id, stage)`.
2. **Project promotion is implicit.** When stage N is frozen, the freeze
   handler (a) writes StageContext and (b) sets
   `project.stage_status[N+1] = "available"`.
3. **Multi-tenant since iter-13.68.** JWT bearer auth in `backend/auth.py`:
   `get_current_user` resolves token → `{user, tenant}` (401 invalid, 403
   disabled user/tenant); `require_super_admin` gates `routes/admin.py`
   (tenants/users/dashboard CRUD). A super-admin has `tenant_id == "*"`.
   Within one tenant the UI still drives **one active project** via
   `ProjectContext` — that part of the original single-tenant design holds,
   but "no auth, no tenants" is no longer true. New routes that read project
   data must scope by tenant, not just `project_id`.
4. **All LLM calls go through `llm.fabric_call()`** — never `httpx` directly.
   Stage routes use `from llm import fabric_call as chat_completion`.
   **There is no OpenRouter env-var fallback.** iter-14.31 removed it for
   every generation path: when the configured provider and Factory both
   fail, `fabric_call` tries a Console-registered Ollama provider and then
   raises. `LAMA_DISABLE_OPENROUTER_FALLBACK` was removed outright in
   2026-09 — no code had read it since iter-14.31 (verified in
   `docs/RECON.md` §D3), and it is no longer exported by compose or `.env`
   either. The behaviour it described is unconditional.
   **Three execution modes** resolve inside `fabric_call` — know which one is
   live before debugging a prompt:
   a. **Console routing** (default) — `AGENT_COMPLEXITY[agent_key]` picks a
      tier, Console `provider.routing[tier]` picks the model.
   b. **Factory API** — when a project sets
      `settings.factory_orchestrator.enabled`, every call is brokered to
      `api.factory.ai` via `factory_orchestrator.py` (poll-based; agents in
      `_SLOW_FACTORY_AGENTS` get the long timeout + shrink-and-retry path).
   c. **Factory CLI** — `LAMA_FACTORY_MODE=cli` drives the local `droid` CLI
      via `fabric/factory_cli.py`.
   Factory context is isolated per `(tenant, project)`; several suites
   (`test_iter139*_factory_*`) exist purely to pin that isolation. Don't
   weaken them.
5. **TOON pruning is stage-aware** (`routes/chat.py::prune_toon`). Adding a
   new stage = add the corresponding slice.
6. **Architecture stage does NOT use SSE.** Long jobs use background tasks +
   2s polling because production K8s ingress has a 60s timeout. DataModel +
   SRS may use SSE — they stream short chunks.
7. **Freeze gates are typed-confirmation in the UI** (typed "RESET" /
   "FREEZE"). Don't weaken these.
8. **Skip-patterns on folder scan** are deliberate: `node_modules`, `.git`,
   `vendor`, `__pycache__`, `*.bak`, `*.save`, `*_bkp`, `*_old`,
   `*_backup`, `*.php_*`. Order matters (Iter-2 bug).
9. **`@app.on_event("startup") → run_seed()`** boots on every backend start.
   Seed is idempotent and uses `force_update=True` for prompt rev-bumps.
   Migrating to FastAPI `lifespan` is on the P2 backlog — don't do it casually.
10. **CORS** is wide-open by default (`CORS_ORIGINS=*`). Tighten in prod.
11. **Discovery model dropdown drives SRS generation.** The model picked in
    `ChatPanel` (lifted into `Discovery` state, persisted to
    `localStorage["lama:chat:model"]`) is forwarded into the
    `/api/srs/generate/stream` POST body as `model`, and also drives the
    chat → SRS auto-trigger via `routes/chat.py`.
12. **Build stage context with `context_bundler.build_stage_context()`** —
    SRS / DataModel / Architecture / CodeGen all share it. Don't hand-roll
    per-section context assembly: SRS fans out 12+ sections and re-querying
    Mongo per section is the exact problem it was written to kill.
13. **Freeze gates consult the confidence engine** (`confidence.py`,
    iter-13.70) — 2-3 independent models vote per section; results land in
    `stage_confidence` and surface as per-stage badges. Evaluator responses
    that don't match the JSON schema are discarded from the vote.

---

## Conventions (rules of engagement)

1. **Read `memory/PRD.md` first** — append-only iteration log, closest thing
   to a changelog. Recent iterations override earlier ones.
2. **Never invent stages, collections or routes.** Use the tables here and
   the lists in `db.py` / `routes/`. New collection → add to `db.py`. New
   route → register in `server.py`. New API call → add to `lib/api.js`.
3. **Pydantic over `dict` payloads** for any new POST body (P1 backlog
   specifically calls out `/api/kb/scan-folder`, `/api/srs/*`,
   `/api/architecture/*`, `/api/codegen/*`).
4. **One file per route concern.** Don't cross-import between
   `routes/datamodel.py` and `routes/architecture.py` — go through
   `pipeline.py`.
5. **Frontend pages own their layout** (resizable `PanelGroup`). Reuse
   `ERDiagram.jsx`, `MiniConsole.jsx` rather than re-rolling.
   (`ChatPanel.jsx` was removed in 2026-09 — it had no render site; the live
   chat is `FloatingChat.jsx`.)
6. **`data-testid` attributes are part of the contract** — the testing agent
   asserts on them. Verified present as of 2026-09:
   `stage-{key}-badge-{frozen|ready|skipped}`, `generate-srs-btn`,
   `srs-edit-btn-{section}`, `freeze-btn`, `unfreeze-btn`, `export-pdf-btn`.
   Keep them stable. 723 distinct ids exist in total.
   **Three ids this list used to name do not exist in the code** and were
   removed rather than left as a false contract: `owl-export-btn` and
   `refresh-kb-health` (zero occurrences anywhere), and
   `stage-{key}-badge-locked` — the third badge variant the code actually
   emits is `-skipped` (`Sidebar.jsx`). See HUMAN_INTERVENTION.md DEC-8.
7. **Audit-log everything that changes state** via `audit_log` collection.
8. **Never call `npm install`** — yarn-only via corepack.
9. **Never bump `react-resizable-panels` past 2.1.7** without verifying
   panel-collapse on Discovery + DataModel pages.
10. **OWL extractor is language-agnostic** (iter 13). Adding a new language
    = add `extract_<lang>` in `backend/kb/owl_extractor.py` AND wire it into
    `extract()` and `extract_zip()`.
11. **KB context export is YAML, not OWL/JSON-LD** (iter 13.1). The
    endpoint `/api/kb/{pid}/owl-export` is still registered and still
    returns `application/x-yaml`, but **nothing in the UI calls it any
    more**: the `owlExportUrl` helper had zero callers and was removed with
    the other 44 dead `lib/api.js` exports, and no `owl-export-btn` exists.
    The route is deliberately kept (it is a working endpoint an operator or
    script can still hit); wiring a button back up is DEC-8.
    Code should call `export_kb_yaml(...)` for downloads and
    `export_kb_context(...)` / `export_owl(...)` (alias) for in-process
    consumers.

---

## Environment Variables

```env
OPENROUTER_API_KEY=sk-or-...                # required for any meaningful LLM call
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
LAMA_DEFAULT_MODEL=                          # iter-13.30: empty by default — Console.routing[tier] picks per agent_key
LAMA_REVALIDATION_MODEL=                    # optional override; else cross-tier pick from Console.routing
LAMA_BR_ENFORCE=                            # iter-13.30: "1" → block SRS/Arch/CodeGen freeze when BR coverage < threshold
LAMA_BR_MIN_COVERAGE=100                    # iter-13.30: minimum BR coverage % required when LAMA_BR_ENFORCE=1
LAMA_GAP_ANALYSIS_MODEL=                    # codegen gap-recovery override; empty = cross-tier from Console

QDRANT_URL=http://<host>:6333               # remote Qdrant; required for RAG chat / SRS RAG
QDRANT_API_KEY=...
QDRANT_PATH=                                # embedded on-disk engine instead of a server.
                                            #   Set EITHER this or QDRANT_URL — with neither,
                                            #   vector search is off and callers fall back to TOON.

# Auth / multi-tenancy (iter-13.68)
LAMA_JWT_SECRET=                            # HS256 signing key for bearer tokens
LAMA_SESSION_SIGNING_KEY=                   # HMAC for agent_memory summaries — a
                                            #   sig mismatch wipes the summary
# Factory orchestrator (see Critical Contract #4)
LAMA_FACTORY_MODE=                          # "cli" → drive local `droid` via fabric/factory_cli.py
FACTORY_API_BASE_URL=https://api.factory.ai/api/v0
LAMA_FACTORY_CLI_SLIM=1                     # iter-14.10 token-spend guardrail
# LAMA_DISABLE_OPENROUTER_FALLBACK        # GONE (2026-09). Read by no code since
                                            #   iter-14.31 removed the fallback
                                            #   outright; no longer exported by
                                            #   compose or .env either.
LAMA_CONFIDENCE_ENGINE=                     # langgraph | fabric | unset(=auto).
                                            #   NOT an on/off switch since
                                            #   iter-14.29 — unset means auto,
                                            #   which picks langgraph whenever it
                                            #   imports and the droid CLI is not
                                            #   connected. See
                                            #   confidence_langgraph.py::
                                            #   resolve_confidence_engine.

MONGO_URL=mongodb://127.0.0.1:27017         # bundled mongod in single-image deploy
DB_NAME=lama
CORS_ORIGINS=*                              # comma-separated

# Optional — SSL policy for httpx calls when behind a corporate proxy
LAMA_DISABLE_SSL_VERIFY=1
LAMA_CA_BUNDLE=/path/to/ca.pem
```

A `backend/.env` is used during local dev; it is `rm -f`'d from the image at
build time so production env comes from `-e` flags / compose `environment:`.

---

## Known Hazards / Footguns

- **Python version skew — deliberate, not drift.** The image is
  `python:3.11-slim-bookworm`; the local venv at repo-root **`.venv`** (not
  `backend/venv`) is **3.14**. Reviewed 2026-09 and kept: `scipy` ships no
  cp314 manylinux wheel, and it is a hard transitive dep of
  `sentence-transformers`, which `confidence_langgraph.py` imports. The four
  pins that differ are documented in `backend/requirements-dev-macos.txt`.
  Bytecode caches (`.pyc`) and any C-extension wheel built locally will not
  match the container — reproduce version-sensitive bugs inside
  `docker compose`, not the local venv.
- **`backend/.env` holds real `OPENROUTER_API_KEY` / `QDRANT_API_KEY`** and is
  now gitignored. Root `.env` is committed but carries only non-secret compose
  defaults — keep that split.
- **`routes/projects.py`** had a syntax-corruption regression in Iter-5
  (`e")` instead of `@router.get(...)`). If the backend won't boot, grep
  for truncated decorators first.
- **`fabric_call` may silently route through an inactive provider** whose
  `is_active=True` but the API key is invalid → cascade of 401s. Iter-12
  added empty/exception fallback to env-var OpenRouter; the backlog item
  for a `/api/console/providers/validate/{id}` ping is **not** done yet.
- **`Sidebar.jsx`** nests a Radix Tooltip trigger inside `<button>` (React
  hydration warning). Known, P2.
- **CodeGen file tree is intentionally flattened** (non-recursive) because
  of a babel `visual-edits` plugin bug. Don't "fix" by recursing without
  testing.
- **SRS auto-trigger from chat** (`srs_triggered: true`) silently swallows
  failures. P2 backlog.
- **`@app.on_event` deprecation** — migrating to `lifespan` will reorder
  seed/db init; coordinate with `run_seed()` idempotency before touching.
- **LLM-returned ```` ```markdown ```` fences** — handled by
  `SRSPanel.jsx::normaliseSectionContent` (iter 13.4). If new render paths
  appear, reuse it.

---

## When You Need To …

| Goal | Open |
|------|------|
| Understand the pipeline | `backend/pipeline.py` + `memory/PRD.md` |
| Add a new stage handoff field | `models.py::StageContext.outputs`, then writer |
| Add a new LLM provider | `backend/fabric/model_fabric.py` `PROVIDER_PRESETS` dict |
| Add a new model to the dropdown | `backend/llm.py::AVAILABLE_MODELS` + the relevant preset's `model_catalogue` |
| Add a new SRS section | `routes/srs.py::SECTION_CONFIGS` (currently 12) |
| Wire a new frontend page | `App.js` route → `pages/` → `lib/api.js` helpers |
| Find which collection stores X | `backend/db.py` — **59 collections**, single source of truth |
| Add auth/tenant scoping to a route | `backend/auth.py::get_current_user` / `require_super_admin` |
| Gather context for a stage prompt | `backend/context_bundler.py::build_stage_context` |
| Debug "which model actually ran" | `llm.py::fabric_call` → `llm_traces` collection + `token_usage_log` |
| Work on the agentic CodeGen flow | `routes/codegen.py` multi-agent section + iter-17 PRD entry |
| See latest known-working state | `test_reports/iteration_<N>.json` |
| Understand container boot order | `docker/supervisord.conf` + `docker/entrypoint.sh` |

---

## Out of Scope

- Multi-tenant / multi-project UI (intentionally removed in Iter-2).
- Migration to non-PostgreSQL targets — target stack is fixed per project.
- Frontend rewrites to Vite / Next.js / TanStack — CRA + CRACO is baseline.
- Replacing MongoDB — it's LAMA's system-of-record (PostgreSQL is only the
  *migration target* of user projects).

---

*Keep this file lean. Detailed flow & sequence diagrams live in
`docs/ARCHITECTURE.md`; the why-and-when log lives in `memory/PRD.md`.*

