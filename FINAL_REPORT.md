# Direct Transform — Integration Final Report

**Branch:** `feat/direct-transform` (6 commits off `35fabc0`)
**Source:** `/Users/nikunj/Desktop/lama/driod files` — **deleted**, see below.

Direct Transform is the fourth section in the Tools area, after Console,
Integrations and Prompt Library. It is the incoming **DCTE** (Direct Code
Transformation Engine): point it at a folder on the server and it migrates
the code in place — Helidon MicroProfile → Spring Boot 3, Oracle →
PostgreSQL — deterministic plugins first, optional AI pass on top.

## Where it plugs in

Neither pipeline. The incoming code settles this itself: it never imports
`pipeline.py`, never calls `require_stage_context`, and never reads a
`project_id`. It is a **third standalone Tools track** beside Gap Analyzer
and Transformer, with its own route module (`backend/routes/dcte.py`) and
its own four collections. Multi-Agent CodeGen and the Transformer are
untouched — 0 lines changed in `routes/codegen.py`, `routes/tools.py`,
`pipeline.py`, `routes/pipeline.py`, `confidence.py` and `llm.py`.

## Registration — symmetrical with the three siblings

| Registry | Entry |
|---|---|
| `backend/db.py` | `dcte_jobs`, `dcte_transforms`, `dcte_events`, `dcte_reports` (59 → 63 collections) + their indexes |
| `backend/server.py` | `dcte_router`, mounted last, mirroring the `tools_router` pair |
| `backend/fabric/model_fabric.py` | 5 `dcte.*` `AGENT_COMPLEXITY` tiers |
| `backend/seed.py` | the 5 matching `agent_configs` rows, `stage: "Tools"` |
| `frontend/src/components/Sidebar.jsx` | 4th Tools item, `data-testid="nav-direct-transform"` |
| `frontend/src/App.js` | lazy import + `<Route path="/direct-transform">` |
| `frontend/src/lib/routes.ts` | prefetch chunk |
| `frontend/src/components/TopToolbar.jsx` | breadcrumb label |
| `frontend/src/components/CommandPalette.jsx` | ⌘K entry |
| `frontend/src/lib/api.js` + `api.d.ts` | 14 `dcte*` helpers, declared so `tsc` gates them |

**Deliberately not registered:** the collapsed sidebar rail. That strip is
Console / Prompt Library / Settings / Audit — **Integrations is absent from
it**, so it is a curated shortcut list, not a Tools registry. Adding a
fourth tool would have made Direct Transform more prominent than a sibling.
`tools-nav-registration.test.js` pins both halves of that decision.

Model selection is tier-based throughout. Every LLM call goes through
`llm.fabric_call` with an `agent_key`; no vendor or model string appears
anywhere in the feature outside a test fixture.

## Errors found in the incoming code, and fixed

| # | Finding | Fix |
|---|---|---|
| C-1 | The AI-fabric preflight read **`LAMA_DISABLE_OPENROUTER_FALLBACK`**, removed repo-wide in 2026-09 and read by no code since iter-14.31. Dead clause. | Removed |
| C-2 | The warning that clause printed told operators to "unset `LAMA_DISABLE_OPENROUTER_FALLBACK`" — a flag that no longer exists, in the one message a stuck user reads. | Rewritten to point at Console → Model Fabric |
| C-3 | Relative paths resolved against **`os.getcwd()`** — `backend/` under split-dev, `/app/backend` in the image, so the same typed path landed in two different places and, in the container, inside the app tree. | `$LAMA_DCTE_WORKSPACE` or `$HOME`, which is what `/fs/browse` already used |
| C-4 | 77 raw hex literals + `bg-white` in the page. | Mapped to design tokens; verified they compile into the built CSS |
| C-5 | 32 `text-[10px]` / `text-[11px]`. The type scale is *replaced* and floored at 12px on purpose; the repo had zero of either. | `text-micro` / `text-xs` |
| CM-1 | The events pane scrolled to **top**, commented "events come newest-first". They don't — `events_for` sorts descending to take the newest N, then reverses. The pane parked on the oldest event and a running job looked frozen, while the narration banner two lines above read the same array as oldest-first. | Pinned to the bottom |
| CM-2 | `POST /jobs/{id}/cicd` took a bare `dict`. | `CicdRequest` model (CLAUDE.md convention #3) |
| — | `ai_refactor.suggest_refactors`: a back-compat shim whose comment claimed "the routes/engine still import" it. Neither did — the route imported the name and never called it. | 38 dead lines + the F401 removed |
| — | `devops_agent` declared `_DEFAULT_MAX_LLM_CALLS` / `_MAX_CHARS_PER_FILE`, read neither, and its docstring promised an LLM path with no call site. | Constants removed, docstring corrected, decision DT-1 raised |
| — | `build_agent` declared a logger it never called, making `import logging` dead too. | Both removed |
| — | Page loaded plugins and jobs with `.catch(() => {})` — a down backend rendered an empty `<select>` the user cannot act on, silently. | Both toast; picker shows "Plugins unavailable" |
| — | 7 ruff `ARG` findings. | Fixed at the cause — `ruff.toml` untouched, no `noqa` added |

## Verification

| Gate | Baseline | After |
|---|---|---|
| `pytest backend/tests/` | 1172 passed, 129 skipped | **1234 passed, 129 skipped** (+62) |
| `ruff check backend` | clean | **clean** |
| `yarn lint` | 0 errors, 19 warnings | **0 errors, 19 warnings** (none in new files) |
| `yarn typecheck` | clean | **clean** |
| `yarn build` | compiles | **compiles**, same 7 pre-existing warning files |
| `yarn test:ci` | 13 suites | **15 suites, 245 passed** (+16) |

App boots with 330 routes; `/api/health` 200. All four Tools sections answer:
console/providers (2), integrations/catalog (23), prompts (58), dcte/plugins
(2). Direct Transform live: detect on the shipped Helidon sample →
`helidon-mp @ 0.92 → spring-boot-3`. Pipelines intact: stage 10, multi-agent
CodeGen 39, Transformer 37, Gap Analyzer 10 routes.

Auth posture matches the siblings exactly: `/api/dcte` has 17 routes and **0**
route-level auth dependencies — the same as tools (50/0), console (28/0),
integrations (5/0) and prompts (5/0), while `/api/admin` has 9/9. One shared
`CORSMiddleware` for the whole app.

The 19-test E2E suite drives the REST layer start to finish, including a real
engine run that writes a Spring Boot tree to disk, plus an invalid input, a
backend error, and an empty/edge case.

## Open decisions (non-blocking)

`HUMAN_INTERVENTION.md` **Blocked** is empty. Four product calls are recorded
there as DT-1…DT-4: the two registered-but-uninvoked agent keys, the DCTE
prompts living in code rather than the Prompt Library, two working endpoints
with no UI, and the collapsed-rail omission.

## Source folder — deleted

All four gates passed first. Independence was proved by renaming the folder to
`driod files.pending-delete` and re-running everything green before deleting:
backend 1234 passed, build/lint/typecheck clean, 245 frontend tests passing.
`git grep driod` finds nothing in tracked source.

41 source files were integrated (32 byte-identical, 9 adapted). 27 files were
**not needed**: 26 `__pycache__/*.pyc` (interpreter bytecode from the other
machine) and one `.DS_Store` (macOS Finder metadata) — both already gitignored.
Zero orphans.

### Old → new file map

| Source path (in `driod files/`) | Destination in repo | | What changed |
|---|---|---|---|
| `DirectTransform_1.jsx` | `frontend/src/pages/DirectTransform.jsx` | adapted | renamed; tokens, type scale, page shell, scroll fix, error states |
| `dcte/__init__.py` | `backend/dcte/__init__.py` | verbatim | — |
| `dcte/ai_refactor.py` | `backend/dcte/ai_refactor.py` | adapted | dead `suggest_refactors` shim removed |
| `dcte/build_agent.py` | `backend/dcte/build_agent.py` | adapted | dead logger + its import removed; _maven_cmd param renamed |
| `dcte/cicd_generator.py` | `backend/dcte/cicd_generator.py` | verbatim | — |
| `dcte/dependency_migrator.py` | `backend/dcte/dependency_migrator.py` | verbatim | — |
| `dcte/devops_agent.py` | `backend/dcte/devops_agent.py` | adapted | 2 dead constants removed; docstring corrected; _patch param renamed |
| `dcte/droid_agent.py` | `backend/dcte/droid_agent.py` | verbatim | — |
| `dcte/engine.py` | `backend/dcte/engine.py` | adapted | no-op sink lambda varargs named as unused |
| `dcte/impact_analyzer.py` | `backend/dcte/impact_analyzer.py` | verbatim | — |
| `dcte/job_manager.py` | `backend/dcte/job_manager.py` | verbatim | — |
| `dcte/models.py` | `backend/dcte/models.py` | verbatim | — |
| `dcte/narrator.py` | `backend/dcte/narrator.py` | verbatim | — |
| `dcte/plugin_base.py` | `backend/dcte/plugin_base.py` | adapted | no-op sink lambda varargs named as unused |
| `dcte/plugin_registry.py` | `backend/dcte/plugin_registry.py` | verbatim | — |
| `dcte/plugins/__init__.py` | `backend/dcte/plugins/__init__.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/__init__.py` | `backend/dcte/plugins/helidon_to_spring/__init__.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/bootstrap_writer.py` | `backend/dcte/plugins/helidon_to_spring/bootstrap_writer.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/config_transformer.py` | `backend/dcte/plugins/helidon_to_spring/config_transformer.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/endpoint_transformer.py` | `backend/dcte/plugins/helidon_to_spring/endpoint_transformer.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/mappings.py` | `backend/dcte/plugins/helidon_to_spring/mappings.py` | verbatim | — |
| `dcte/plugins/helidon_to_spring/plugin.py` | `backend/dcte/plugins/helidon_to_spring/plugin.py` | adapted | ABC param renamed; _readme_md lost an unread param |
| `dcte/plugins/oracle_to_postgres/__init__.py` | `backend/dcte/plugins/oracle_to_postgres/__init__.py` | verbatim | — |
| `dcte/plugins/oracle_to_postgres/plugin.py` | `backend/dcte/plugins/oracle_to_postgres/plugin.py` | adapted | ABC params renamed |
| `dcte/plugins/oracle_to_postgres/sql_translator.py` | `backend/dcte/plugins/oracle_to_postgres/sql_translator.py` | verbatim | — |
| `dcte/project_detector.py` | `backend/dcte/project_detector.py` | verbatim | — |
| `dcte/report_generator.py` | `backend/dcte/report_generator.py` | verbatim | — |
| `dcte/samples/helidon_sample/pom.xml` | `backend/dcte/samples/helidon_sample/pom.xml` | verbatim | — |
| `dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectResource.java` | `backend/dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectResource.java` | verbatim | — |
| `dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectService.java` | `backend/dcte/samples/helidon_sample/src/main/java/com/example/pmis/ProjectService.java` | verbatim | — |
| `dcte/samples/helidon_sample/src/main/resources/microprofile-config.properties` | `backend/dcte/samples/helidon_sample/src/main/resources/microprofile-config.properties` | verbatim | — |
| `dcte/samples/oracle_sample/schema.sql` | `backend/dcte/samples/oracle_sample/schema.sql` | verbatim | — |
| `dcte/templates/cicd/Jenkinsfile` | `backend/dcte/templates/cicd/Jenkinsfile` | verbatim | — |
| `dcte/templates/cicd/azure-pipelines.yml` | `backend/dcte/templates/cicd/azure-pipelines.yml` | verbatim | — |
| `dcte/templates/cicd/github-actions.yml` | `backend/dcte/templates/cicd/github-actions.yml` | verbatim | — |
| `dcte/tester_agent.py` | `backend/dcte/tester_agent.py` | verbatim | — |
| `dcte/validator.py` | `backend/dcte/validator.py` | verbatim | — |
| `dcte_1.py` | `backend/routes/dcte.py` | adapted | renamed; imports hoisted, logger, C-1/C-2/C-3, CicdRequest, dead import |
| `test_iter1817_dcte_devops_tester.py` | `backend/tests/test_iter1817_dcte_devops_tester.py` | verbatim | — |
| `test_iter18_dcte_1.py` | `backend/tests/test_iter18_dcte.py` | verbatim | renamed (dropped the _1 draft suffix) |
| `test_iter19_droid_agent.py` | `backend/tests/test_iter19_droid_agent.py` | verbatim | — |

---

**Deletion recorded:** `driod files/` (740 KB, 68 entries) was removed with
`rm -rf` after all four gates passed. Both `driod files` and
`driod files.pending-delete` are absent from the working tree.

DIRECT TRANSFORM INTEGRATED — FOLDER REMOVED
