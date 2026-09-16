# Direct Transform — Intake Report (Phase 0)

Read-only analysis of `/Users/nikunj/Desktop/lama/driod files` before any
integration. Nothing was moved, modified or deleted to produce this document.

The incoming feature calls itself **DCTE — Direct Code Transformation Engine**
(iter-18 / iter-19). It ships as a fourth item for the **Tools** area, to sit
after Console, Integrations and Prompt Library under the nav label
**Direct Transform**.

**Which pipeline does it belong to?** *Neither.* The code settles this:
`dcte_1.py`'s own module docstring says *"Independent module. Bypasses the
5-stage pipeline entirely: no KB, no SRS, no stage_context. Contract-compliant
(same posture as routes/tools.py)."* It never imports `pipeline.py`, never calls
`require_stage_context`, and never reads `project_id`. It is a **third
standalone track** alongside Gap Analyzer and Transformer — it does not extend
Multi-Agent CodeGen (`routes/codegen.py`) and it does not extend the Transformer
(`routes/tools.py`). It gets its own route module and its own collections.

---

## 0.1 Manifest

32 Python/JSX source files + 3 CI templates + 6 sample fixtures. `__pycache__`
(20 `.pyc`) and `.DS_Store` are build/OS droppings, not source.

### Backend — REST layer

| Source path | Kind | Purpose | Language |
|---|---|---|---|
| `dcte_1.py` | backend | FastAPI router: 17 endpoints under `/api/dcte`, the background job runner, stall watchdog, and 5 sync↔async agent wrappers | Python 3.11 / FastAPI |

### Backend — `dcte/` engine package

| Source path | Kind | Purpose | Language |
|---|---|---|---|
| `dcte/__init__.py` | backend | Package facade — re-exports engine, registry, detector, models | Python |
| `dcte/models.py` | backend | Pydantic v2 models: `DcteJob`, `ServiceConfig`, `DcteJobStatus`(13), `DcteTransformRecord`, `DcteEvent`, `DcteReportDoc`, `DetectRequest/Response`, `CreateJobRequest`, `JobActionResponse` | Python / Pydantic 2 |
| `dcte/engine.py` | backend | `TransformationEngine.run_job()` — the orchestrator. Sink-driven (record/event/report/status) so it stays sync and Mongo-free | Python |
| `dcte/job_manager.py` | backend | Motor-backed persistence for jobs / events / transforms / reports | Python / Motor |
| `dcte/plugin_base.py` | backend | ABC `TransformationPlugin` + dataclasses `TransformContext`, `AnalysisResult`, `TransformResult`, `ValidationResult`, `ReportBundle` | Python |
| `dcte/plugin_registry.py` | backend | `PluginRegistry` + module-level singleton `get_registry()` / `reset_registry()` | Python |
| `dcte/project_detector.py` | backend | Fingerprints a folder → `helidon-mp` / `helidon-se` / `spring-boot` / `oracle` / `java-gradle` / `java-generic` | Python |
| `dcte/dependency_migrator.py` | backend | Rewrites `pom.xml` → Spring Boot 3.3.4 / Java 21 | Python |
| `dcte/validator.py` | backend | Java bracket-balance + SQL leftover-Oracle-ism checks | Python |
| `dcte/impact_analyzer.py` | backend | Deterministic weighted effort/risk estimate | Python |
| `dcte/report_generator.py` | backend | Writes `traceability_matrix.md/.json` + `impact_analysis.md/.json` | Python |
| `dcte/cicd_generator.py` | backend | Copies a CI template into `<output_root>/cicd/` | Python |
| `dcte/ai_refactor.py` | backend | **LLM.** Concurrent per-file AI transform sweep + residue fix-up rounds. `agent_key="dcte.transformer"` | Python / `llm.fabric_call` |
| `dcte/build_agent.py` | backend | **LLM.** Runs `mvn`/`gradle`, parses javac errors, LLM-patches failing files, retries. `agent_key="dcte.build_fixer"` | Python / subprocess + `fabric_call` |
| `dcte/devops_agent.py` | backend | **LLM.** Patches structural gaps javac can't see (missing `@SpringBootApplication`, `application.yml`, maven plugin). `agent_key="dcte.devops"` | Python / `fabric_call` |
| `dcte/tester_agent.py` | backend | **LLM.** Endpoint + config parity checks, plus a real boot smoke against `/actuator/health`. `agent_key="dcte.tester"` | Python / subprocess + `fabric_call` |
| `dcte/droid_agent.py` | backend | **LLM/agentic.** iter-19 autonomous mode — one `droid exec --auto medium` per service via `fabric.factory_cli._run_droid_exec`, with a sha256 pre/post tree diff | Python |
| `dcte/narrator.py` | backend | **LLM.** Decorative live "currently doing X…" line every ~6s. `agent_key="dcte.narrator"` | Python / `fabric_call` |
| `dcte/plugins/__init__.py` | backend | empty namespace marker | Python |
| `dcte/plugins/helidon_to_spring/__init__.py` | backend | plugin package marker | Python |
| `dcte/plugins/helidon_to_spring/plugin.py` | backend | `HelidonToSpringPlugin` — analyze/transform/validate/report | Python |
| `dcte/plugins/helidon_to_spring/mappings.py` | backend | Import + annotation replacement tables | Python |
| `dcte/plugins/helidon_to_spring/endpoint_transformer.py` | backend | JAX-RS resource class → `@RestController` | Python |
| `dcte/plugins/helidon_to_spring/config_transformer.py` | backend | `microprofile-config.properties` → `application.properties` | Python |
| `dcte/plugins/helidon_to_spring/bootstrap_writer.py` | backend | Emits `Application.java` + `SecurityConfig.java` | Python |
| `dcte/plugins/oracle_to_postgres/__init__.py` | backend | plugin package marker | Python |
| `dcte/plugins/oracle_to_postgres/plugin.py` | backend | `OracleToPostgresPlugin` | Python |
| `dcte/plugins/oracle_to_postgres/sql_translator.py` | backend | Regex Oracle → PG SQL rewrites | Python |

### Backend — assets

| Source path | Kind | Purpose |
|---|---|---|
| `dcte/templates/cicd/github-actions.yml` | asset | CI template, read by `cicd_generator` |
| `dcte/templates/cicd/azure-pipelines.yml` | asset | CI template |
| `dcte/templates/cicd/Jenkinsfile` | asset | CI template |
| `dcte/samples/helidon_sample/pom.xml` | asset | Test fixture (Helidon MP) |
| `dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectResource.java` | asset | Test fixture |
| `dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectService.java` | asset | Test fixture |
| `dcte/samples/helidon_sample/src/main/resources/microprofile-config.properties` | asset | Test fixture |
| `dcte/samples/oracle_sample/schema.sql` | asset | Test fixture (Oracle SQL) |

### Tests

| Source path | Kind | Purpose |
|---|---|---|
| `test_iter18_dcte_1.py` | test | 30 tests — detector, registry, transformers, plugins end-to-end, engine lifecycle, build agent |
| `test_iter1817_dcte_devops_tester.py` | test | DevOps + Tester agents, fabric key registration, new job statuses |
| `test_iter19_droid_agent.py` | test | Droid agent isolated + engine droid branch/fallback |

### Frontend

| Source path | Kind | Purpose | Language |
|---|---|---|---|
| `DirectTransform_1.jsx` | frontend | The whole page: 3-column layout (config / progress+events / reports), plus an inline `FolderPicker` modal | React 19 / Tailwind |

### Not source

| Source path | Disposition |
|---|---|
| `.DS_Store`, `dcte/**/__pycache__/*.pyc` (20 files) | **Not needed** — OS/interpreter droppings. `.gitignore` already excludes both. |

---

## 0.2 Backend surface

`router = APIRouter(prefix="/dcte", tags=["dcte"])` — identical shape to
`routes/tools.py`, `routes/console.py`, `routes/integrations.py`,
`routes/prompts.py`. **No auth dependency**, which also matches all four
siblings (LAMA gates the SPA with `RequireAuth`; `api.js` attaches the bearer
token; no route module declares `Depends(get_current_user)`).

### Endpoints (17)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/api/dcte/plugins` | — | `{plugins: [{id, display_name, source_stack, target_stack, version}]}` |
| POST | `/api/dcte/projects/detect` | `DetectRequest{source_path}` | `DetectResponse{path, exists, is_valid, detected_stack, confidence, hints[], suggested_target}` |
| GET | `/api/dcte/fs/browse` | `?path=` | `{path, parent, entries:[{name,path,is_dir}], warning}` |
| POST | `/api/dcte/jobs` | `CreateJobRequest` | `DcteJob` dict |
| GET | `/api/dcte/jobs` | `?tenant_id=` | `{jobs: [DcteJob]}` |
| GET | `/api/dcte/jobs/{id}` | — | `DcteJob` dict |
| DELETE | `/api/dcte/jobs/{id}` | — | `{deleted: bool}` |
| POST | `/api/dcte/jobs/{id}/start` | — | `JobActionResponse{id,status,message}` |
| POST | `/api/dcte/jobs/{id}/pause` | — | `JobActionResponse` |
| POST | `/api/dcte/jobs/{id}/resume` | — | `JobActionResponse` |
| POST | `/api/dcte/jobs/{id}/rollback` | — | `JobActionResponse` |
| GET | `/api/dcte/jobs/{id}/report` | — | `{reports: [...]}` |
| GET | `/api/dcte/jobs/{id}/events` | `?limit=1..1000` | `{events: [...]}` |
| GET | `/api/dcte/jobs/{id}/transforms` | — | `{transforms: [...]}` |
| POST | `/api/dcte/jobs/{id}/cicd` | `dict{providers:[]}` | `{emitted: [paths]}` |
| GET | `/api/dcte/jobs/{id}/artifact` | `?path=` | `FileResponse` (guarded to `output_root`) |
| GET | `/api/dcte/debug/env` | — | `{workspaces_root, ai_refactor_enabled_default, plugins[]}` |

### Services / modules it calls

`dcte.TransformationEngine`, `dcte.ProjectDetector`, `dcte.get_registry`,
`dcte.job_manager.JobManager`, `dcte.ai_refactor.transform_files`,
`dcte.build_agent.build_and_fix`, `dcte.devops_agent.run_devops`,
`dcte.tester_agent.run_tester`, `dcte.droid_agent.run_droid_agent`,
`dcte.narrator.narration_loop`, `llm.fabric_call` (indirectly, from the
agent modules only), `fabric.factory_cli._run_droid_exec` /
`cli_binary_available`.

### DB / state

Four **new** collections, none of which exist in `backend/db.py` today:
`dcte_jobs`, `dcte_transforms`, `dcte_events`, `dcte_reports`. Also writes the
existing `audit_log` (`dcte_job_created`, `dcte_job_rollback`,
`dcte_job_completed`) and reads the existing `model_providers` for a preflight.

### Background tasks

`_spawn_job_task` → `asyncio.create_task(_run_job_background)` held in a
module-level strong-ref set, plus two companion tasks per job: `_stall_watchdog`
and `narration_loop`. It deliberately does **not** use `BackgroundTasks`
(documented reason: Starlette ties those to the response cycle and silently
cancelled multi-hour jobs). It does **not** use SSE — consistent with critical
contract #6.

### Registry entries it assumes exist but do not

1. `AGENT_COMPLEXITY` keys `dcte.transformer`, `dcte.build_fixer`,
   `dcte.devops`, `dcte.tester`, `dcte.narrator` — **absent**. Without them
   every DCTE LLM call falls through to `resolve_model`'s generic `"medium"`
   default. Two incoming tests assert three of these five keys.
2. `db.py` accessors for the four collections — **absent**; `from db import
   dcte_jobs, …` is an ImportError today.
3. `server.py` router mount — **absent**.
4. `seed.py` `agent_configs` rows — absent. All ten `tools.transformer.*`
   siblings have one; `resolve_model` reads `agent_configs.complexity` *before*
   `AGENT_COMPLEXITY`, and the Console → Agent Fabric tab lists exactly these
   rows. Without rows the DCTE agents are invisible/unoverridable in Console.

### Env vars it reads

`LAMA_DCTE_WORKSPACE`, `LAMA_DCTE_STALL_SECONDS`,
`LAMA_DCTE_TRANSFORMER_CONCURRENCY`, `LAMA_DCTE_FIXUP_MAX_ROUNDS`,
`LAMA_DCTE_BOOT_SMOKE_TIMEOUT_S`, `LAMA_DCTE_TESTER_BOOT_SMOKE`,
`LAMA_DCTE_DROID_AGENT_DEFAULT`, `LAMA_DCTE_DROID_AGENT_AUTO`,
`LAMA_DCTE_DROID_AGENT_TIMEOUT_SEC` — all new, all namespaced `LAMA_DCTE_*`,
all with defaults, none required. Plus `OPENROUTER_API_KEY` and
`LAMA_DISABLE_OPENROUTER_FALLBACK` in one preflight (see 0.5).

---

## 0.3 Frontend surface

Single default-export page component `DirectTransformPage`, plus a
module-private `FolderPicker` modal.

* **Nav/section registration it expects:** none — the file has no route, no
  sidebar entry, no `ROUTE_CHUNKS` key. Every registration must be written.
* **API calls:** 13 named helpers imported from `@/lib/api` —
  `dcteListPlugins`, `dcteDetectProject`, `dcteCreateJob`, `dcteListJobs`,
  `dcteGetJob`, `dcteStartJob`, `dctePauseJob`, `dcteResumeJob`,
  `dcteRollbackJob`, `dcteDeleteJob`, `dcteGetReports`, `dcteGetEvents`,
  `dcteGetTransforms`, `dcteBrowseFs`. **None of the 14 exist** in
  `lib/api.js`, and because `tsconfig` declares every `api.js` export in
  `lib/api.d.ts`, they must be declared there too or `yarn typecheck` fails.
* **State:** local `useState` only (17 pieces), `useMemo` for the CI-provider
  list, two `localStorage`-persisted prefs (`lama:dcte:droidAgent`,
  `lama:dcte:model`), and a 1.5 s `setInterval` poll of 5 endpoints while a job
  is selected. No `ProjectContext` — correct, the feature is project-less.
* **Styling:** Tailwind, but with **raw hex literals and sub-12px arbitrary
  font sizes** — see 0.5. It does not import any of the repo's `ui/` or `ux/`
  primitives.
* **Toasts:** `sonner` — matches the repo.
* **Assets:** none.

---

## 0.4 Contract check — frontend calls vs backend answers

Matching the 14 helpers the page imports against the 18 endpoints the router
defines. The **shapes all line up** — this is one coherent feature, not two
drafts glued together. Confirmed pairs:

| FE helper | BE endpoint | Payload | Verdict |
|---|---|---|---|
| `dcteListPlugins()` | `GET /dcte/plugins` | — → `{plugins}` | ✅ |
| `dcteDetectProject(path)` | `POST /dcte/projects/detect` | `{source_path}` → `DetectResponse` | ✅ |
| `dcteBrowseFs(p)` | `GET /dcte/fs/browse?path=` | → `{path,parent,entries,warning}` | ✅ |
| `dcteCreateJob(body)` | `POST /dcte/jobs` | FE sends `{name, source_root, output_root, services, ai_refactor, generate_cicd, model, use_droid_agent}` — every key exists on `CreateJobRequest` | ✅ |
| `dcteListJobs()` | `GET /dcte/jobs` | → `{jobs}`; FE reads `j.name/.id/.status/.progress/.services.length` — all present on `DcteJob` | ✅ |
| `dcteGetJob(id)` | `GET /dcte/jobs/{id}` | → `DcteJob` | ✅ |
| `dcteStartJob/Pause/Resume/Rollback(id)` | the four POSTs | → `JobActionResponse` | ✅ |
| `dcteDeleteJob(id)` | `DELETE /dcte/jobs/{id}` | → `{deleted}` | ✅ |
| `dcteGetReports(id)` | `GET /dcte/jobs/{id}/report` | → `{reports}`; FE reads `r.id/.kind/.path` | ✅ |
| `dcteGetEvents(id, 200)` | `GET /dcte/jobs/{id}/events?limit=` | → `{events}`; FE reads `e.id/.at/.phase/.level/.message` | ✅ |
| `dcteGetTransforms(id)` | `GET /dcte/jobs/{id}/transforms` | → `{transforms}`; FE reads `t.id/.status/.kind/.source_file/.target_file` | ✅ |

### Mismatches found — 4, all minor

| # | Mismatch | Side to fix |
|---|---|---|
| CM-1 | FE's polling `tick()` scrolls the events pane to **top** because "events come newest-first from the API". They do not: `JobManager.events_for` sorts `at` descending, takes `limit`, then `return list(reversed(out))` — **oldest-first**. The narration banner separately does `[...events].reverse()` and takes `.find(...)`, which is only correct for oldest-first. So the data is oldest-first, one consumer assumes it, and the scroll comment assumes the opposite. | Frontend — scroll to bottom (newest) and fix the comment. |
| CM-2 | `POST /dcte/jobs/{id}/cicd` takes a bare `body: dict`. No FE helper calls it; CLAUDE.md convention #3 requires Pydantic for new POST bodies. | Backend — add a `CicdRequest` model. |
| CM-3 | `GET /dcte/jobs/{id}/artifact` has no FE helper and no UI affordance — reports render as plain text paths, not links. Endpoint is reachable but unused. | Neither — leave the endpoint (operator/script surface, same posture as `/api/kb/{pid}/owl-export`); record as a decision. |
| CM-4 | `GET /dcte/debug/env` has no FE helper; the page has no diagnostics panel despite the docstring saying "for the DCTE page's diagnostics". | Neither — leave as an operator endpoint; record as a decision. |

---

## 0.5 Contamination scan

Scanned every non-`.pyc` file for absolute paths, home directories, hostnames,
credentials, machine-specific assumptions and hardcoded vendors/models.

### Clean — nothing found

* **Absolute paths / `/Users/` / `/home/` / `nikunj` / `Desktop` / drive
  letters:** 0 hits. (The single grep match in `dcte_1.py:138` is the word
  `~/projects` inside an explanatory comment, not a path in code.)
* **Secrets / API keys / tokens / bearer strings:** 0. Every "token" hit is
  either `max_tokens=`, the phrase "token efficiency" in a prompt, or a
  parser's `*_POM_TOKENS` constant.
* **Bare `except:`:** 0.
* **Hardcoded vendor or model IDs at call sites:** 0. All five LLM call sites
  go through `llm.fabric_call(..., agent_key="dcte.*")` and let
  `AGENT_COMPLEXITY` + Console `routing[tier]` resolve the model — exactly
  critical contract #4. The only literal model string in the whole folder is
  `"openai/gpt-5"` inside a *test* (`test_iter19_droid_agent.py:97`) as a
  pass-through argument fixture, which is correct.

### Findings — 5

| # | File:line | Finding | Fix |
|---|---|---|---|
| **C-1** | `dcte_1.py:467` | Reads **`LAMA_DISABLE_OPENROUTER_FALLBACK`**. That variable was removed repo-wide in 2026-09 (`docker-compose.yml:180`, `.env:18` both record the removal; no backend code reads it). The condition is dead and silently makes the preflight stricter than reality. | Delete the clause. |
| **C-2** | `dcte_1.py:475` | The user-facing warning tells operators to *"set OPENROUTER_API_KEY + unset LAMA_DISABLE_OPENROUTER_FALLBACK"* — advice about a flag that no longer exists. This is the message that shows up in the job log when no provider is configured, i.e. exactly when a confused user reads it. | Rewrite to point at Console → Model Fabric. |
| **C-3** | `dcte_1.py:66` | `_resolve_path` falls back to **`os.getcwd()`** when `LAMA_DCTE_WORKSPACE` is unset. The backend's CWD is `backend/` in split-dev and `/app/backend` in the image — so the same relative path resolves to two different places, and in the container it points *inside the app image*. | Default to `~` (which is what `/fs/browse` already does), so both halves agree and neither writes into the app tree. |
| **C-4** | `DirectTransform_1.jsx` (throughout) | **77 raw hex literals** (7 distinct) — `#F6F6FA`, `#E6E6E6`, `#2E2E38`, `#FFE600`, `#747480`, `#FFFCE5`, `#B0B0B8` — plus `bg-white`. The repo moved to CSS-variable design tokens; app source outside `ERDiagram.jsx` (a D3 canvas) and two comments contains essentially none. Hex literals do not respond to the token layer at all. | Map to `bg-bg` / `border-border` / `text-fg` / `text-fg-muted` / `bg-surface` / `bg-brand` / `text-brand-fg` / `bg-brand-tint` / `border-brand`. |
| **C-5** | `DirectTransform_1.jsx` (throughout) | **32 `text-[10px]` / `text-[11px]`** uses (9 + 23). `tailwind.config.js` *replaces* the font scale and floors it at 12px on purpose ("9/10/11px are deliberately absent… removing those steps turns each one into a build-visible arbitrary value you can grep for"). The repo currently has **0** of either. This file would reintroduce all of them. | `text-micro` (12px) / `text-xs` (13px). |

### Machine-specific assumptions — reviewed, all legitimate

* `dcte/tester_agent.py:277` `http://127.0.0.1:{port}/actuator/health` — probes
  a JVM this same process just spawned. Correct by construction.
* `dcte/devops_agent.py:83` `jdbc:postgresql://localhost:5432/...` and
  `bootstrap_writer.py:86` `http://localhost:8080/swagger-ui.html` — these are
  *generated output* written into the migrated Spring Boot app, not runtime
  config of LAMA.
* `shutil.which("mvn"/"gradle"/"java")` — guarded; every caller degrades to
  "skipped" when the tool is absent.

---

## 0.6 Placement plan

### File moves

| Source | Destination | What must change |
|---|---|---|
| `dcte_1.py` | `backend/routes/dcte.py` | Drop the `_1` draft suffix. Hoist the stray mid-file `import` block (`dcte.models`, `dcte.job_manager`, `dcte.ai_refactor` currently sit *after* a function definition at L67-78) to the top with the rest. Logger `lama.routes.dcte` → **`lama.dcte`** (repo convention is `lama.<module>`: `lama.tools`, `lama.codegen`, `lama.living`). Fix C-1/C-2/C-3. Add `CicdRequest` for CM-2. |
| `dcte/**` (23 `.py`) | `backend/dcte/**` | Imports are already all relative (`from .models import …`) or repo-absolute (`from llm import fabric_call`, `from fabric.factory_cli import …`) — both resolve unchanged once the package sits under `backend/`. No import rewriting needed. |
| `dcte/templates/cicd/*` (3) | `backend/dcte/templates/cicd/*` | none — `cicd_generator` resolves them via `Path(__file__).parent`. |
| `dcte/samples/**` (5) | `backend/dcte/samples/**` | none — the tests already expect `BACKEND/"dcte"/"samples"`. |
| `test_iter18_dcte_1.py` | `backend/tests/test_iter18_dcte.py` | Drop `_1`. `BACKEND = Path(__file__).parent.parent` already resolves to `backend/` from `backend/tests/`. |
| `test_iter1817_dcte_devops_tester.py` | `backend/tests/test_iter1817_dcte_devops_tester.py` | none |
| `test_iter19_droid_agent.py` | `backend/tests/test_iter19_droid_agent.py` | none |
| `DirectTransform_1.jsx` | `frontend/src/pages/DirectTransform.jsx` | Drop `_1`. Fix C-4/C-5 and CM-1. |
| `.DS_Store`, `**/__pycache__/*.pyc` | — | **not integrated** (OS/interpreter droppings; both already gitignored) |

### Registration edits

Determined by asking, for each registry: *do all three siblings appear here?*

**Yes — Direct Transform must be added (9 sites):**

| Site | Edit |
|---|---|
| `backend/db.py` | 4 accessors `dcte_jobs/_transforms/_events/_reports` (59 → 63 collections) + `job_id` indexes in `ensure_indexes` alongside the iter-17 block |
| `backend/server.py` | `from routes.dcte import router as dcte_router` + `api_router.include_router(dcte_router)`, last in the list, mirroring the `tools_router` line |
| `backend/fabric/model_fabric.py` | 5 `AGENT_COMPLEXITY` keys. Tiers pinned by the incoming tests where asserted: `dcte.build_fixer="high"`, `dcte.devops="medium"`, `dcte.tester="low"`. `dcte.transformer="high"` (it writes whole migrated files — same job as `tools.transformer.coder`, one rung below its `critical` because DCTE also has a deterministic regex layer underneath). `dcte.narrator="low"` (its own docstring says tier=low). |
| `backend/seed.py` | 5 `agent_configs` rows in `seed_agents()`, `stage: "Tools"`, complexity identical to the map above so seeding changes no routing |
| `frontend/src/lib/api.js` | 14 helpers, `export const` + `api.<verb>(...).then(r => r.data)` style |
| `frontend/src/lib/api.d.ts` | 14 declarations, inserted alphabetically (hard requirement — `tsc` gates it) |
| `frontend/src/App.js` | `const DirectTransformPage = lazy(() => import("@/pages/DirectTransform"));` + `<Route path="/direct-transform" element={plain(<DirectTransformPage />)} />` |
| `frontend/src/lib/routes.ts` | `"/direct-transform": () => import("@/pages/DirectTransform")` |
| `frontend/src/components/Sidebar.jsx` | 4th button in the Tools `AccordionContent`, **after** `nav-prompts`, `data-testid="nav-direct-transform"`, same class strings, with a `HelpIcon` like all three siblings |
| `frontend/src/components/TopToolbar.jsx` | `"/direct-transform": { label: "Direct Transform", path: "/direct-transform" }` |
| `frontend/src/components/CommandPalette.jsx` | `{ type: "tool", label: "Direct Transform", … }` |

**No — must NOT be added:**

| Site | Why not |
|---|---|
| `Sidebar.jsx` collapsed rail (L468-479) | Holds Console, Prompt Library, Settings, Audit. **Integrations is absent**, so this is a curated shortcut strip, not a Tools registry. Adding a 4th tool here would be asymmetric with a sibling. |
| `backend/pipeline.py` / `stage_context` | The feature bypasses the pipeline by design. |
| `seed.py` prompts | The DCTE agents carry their own inline system prompts (`_SYSTEM`, `_FIXUP_SYSTEM`, `_AGENTIC_BRIEF`). Moving them into the Prompt Library would be **inventing behavior** — recorded as a Decision, not done. |

### Env vars

Nine new `LAMA_DCTE_*` flags, all optional with defaults. Documented in
`CLAUDE.md`'s env block and added commented-out to `docker-compose.yml`
alongside the other `LAMA_*` entries — **not** added to `.env` (that file is
committed and carries only non-secret compose defaults; these all have code
defaults already).

---

## 0.7 Ledger

`INTEGRATION_LEDGER.md` generated from the above — leaves first (db/fabric/seed
before routes before wiring), each row with an executable verify command.
