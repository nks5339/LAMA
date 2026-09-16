# LAMA — Function Status Report

**Generated** 2026-09-16 · branch `feat/direct-transform` · commit `dcf7ad0`+
**Scope** the whole application — **1,724 backend functions** and **352
frontend exports**, measured separately and then against each other.

This issue supersedes the previous one. It re-measures everything after the
**Direct Transform** integration (the fourth Tools section, `routes/dcte.py`
+ `backend/dcte/`), and it adds a class of evidence the earlier issues never
had: the migration output is **compiled with a real JDK and Maven**, not just
inspected. That found four defects the unit suites could not see.

---

## How to read this

You asked for each function marked **working** or **not working**. Where I
have evidence, that is what you get. Where I do not, this report says so
rather than guessing — a green tick I cannot defend is worse than an honest
blank, because it is the one you would stop checking.

Every status below comes from something I ran, not from reading the code:

| Evidence | Method | Result |
|---|---|---|
| **Backend execution trace** | `sys.setprofile` over the full suite, recording every backend frame that really ran | 1,243 passed / 129 skipped · **907 frames observed** |
| **Frontend unit + contract suite** | Jest 27 + React Testing Library 16, `yarn test:ci` | **239 passed / 14 suites** · 0 failed |
| **Live HTTP sweep** | Real app + real Mongo, authenticated, every `GET` I could address | **102 routes · 0 responses ≥ 500** |
| **Live pipeline run** | A real Direct Transform job over HTTP, both plugins | completed 100%, output **compiled by `mvn`** |
| **Reference analysis (backend)** | AST over `Name` / `Attribute` / `ImportFrom` / string constants, plus decorator registration and frontend text | **0 orphans of 1,724** |
| **Reference analysis (frontend)** | Every exported symbol vs. every reference across `src/` | **0 unreferenced of 352** |
| **API contract** | Every URL `lib/api.js` calls, matched against the live OpenAPI schema | **231 URLs · 0 mismatches** |

### Status vocabulary

| Status | Means | Confidence |
|---|---|---|
| ✅ **EXECUTED** | Observed running in a trace above. It works. | Proven |
| 🟡 **REACHABLE** | Called from code that executes, but I did not observe this frame. Not evidence of a fault. | Inferred |
| ⏸️ **MUTATING** | A `POST`/`PUT`/`DELETE`/`PATCH` handler. **Deliberately not invoked** — firing these would create projects, start LLM jobs and write to your live database. | Untested here |
| ⚪ **GET-UNHIT** | A `GET` needing an id I had no fixture for. | Untested here |
| ❌ **ORPHAN** | Defined; referenced nowhere. Dead code. | Proven |

---

# Part 1 — Backend

## Headline

| | Count | Share |
|---|---:|---:|
| ✅ Executed — proven working | **627** | 36.4% |
| 🟡 Reachable — no fault found | 814 | 47.2% |
| ⏸️ Mutating — not exercised | 166 | 9.6% |
| ⚪ GET, no fixture | 117 | 6.8% |
| ❌ Orphan | **0** | 0.0% |
| **Total** | **1,724** | |

Executed is up from 514 to 627 (+113) and the proportion from 33.1% to
36.4%. The whole gain is Direct Transform: its 174 functions arrived with
three unit suites and an E2E suite that drives the REST layer, so they
landed already traced rather than inferred.

> **The basis is the same as the previous issue** — nested functions and
> class methods are counted, so these numbers are comparable to it. The
> total rose 1,552 → 1,724 (+172) from the new subsystem.

## By subsystem

| Subsystem | Total | ✅ Executed | 🟡 Reachable | ⏸️ Mutating | ⚪ GET-unhit | ❌ Orphan |
|---|---:|---:|---:|---:|---:|---:|
| `routes/` | 918 | 274 | 364 | 166 | 114 | **0** |
| `core (top-level)` | 259 | 107 | 149 | 0 | 3 | **0** |
| `kb/` | 234 | 84 | 150 | 0 | 0 | **0** |
| **`dcte/`** | **131** | **104** | 27 | 0 | 0 | **0** |
| `codegen/` | 62 | 16 | 46 | 0 | 0 | **0** |
| `fabric/` | 58 | 41 | 17 | 0 | 0 | **0** |
| `datamodel/` | 39 | 0 | 39 | 0 | 0 | **0** |
| `integrations/` | 23 | 1 | 22 | 0 | 0 | **0** |

**`dcte/` is now the best-covered subsystem at 79.4% executed**, ahead of
`fabric/` at 70.7%. That is a property of how it was built, not of how
important it is: the engine is sink-driven and takes no Mongo handle, so a
test can run a whole migration in-process. `datamodel/` still shows 0
executed because its generators are reached only through Stage-2 `POST`
handlers — **⏸️ Mutating**, not broken.

## Largest modules

| Module | Fns | ✅ | 🟡 | ⏸️ | ⚪ | ❌ |
|---|---:|---:|---:|---:|---:|---:|
| `routes/tools.py` | 185 | 94 | 53 | 21 | 17 | **0** |
| `routes/codegen.py` | 178 | 65 | 76 | 25 | 12 | **0** |
| `routes/architecture.py` | 99 | 2 | 75 | 16 | 6 | **0** |
| `routes/srs.py` | 86 | 51 | 22 | 10 | 3 | **0** |
| `routes/kb.py` | 82 | 3 | 31 | 20 | 28 | **0** |
| `routes/living.py` | 59 | 0 | 41 | 12 | 6 | **0** |
| `factory_orchestrator.py` | 51 | 13 | 38 | 0 | 0 | **0** |
| **`routes/dcte.py`** | **43** | **31** | 12 | 0 | 0 | **0** |
| `fabric/model_fabric.py` | 41 | 35 | 6 | 0 | 0 | **0** |
| `routes/datamodel.py` | 40 | 0 | 20 | 15 | 5 | **0** |
| `llm.py` | 34 | 19 | 15 | 0 | 0 | **0** |
| `routes/console.py` | 34 | 2 | 4 | 17 | 11 | **0** |
| `kb/journey_materializer.py` | 29 | 24 | 5 | 0 | 0 | **0** |
| `arch_deterministic.py` | 29 | 0 | 29 | 0 | 0 | **0** |
| `routes/pipeline.py` | 29 | 10 | 9 | 7 | 3 | **0** |
| `kb/owl_extractor.py` | 27 | 22 | 5 | 0 | 0 | **0** |

`routes/dcte.py` is the only route module with **zero** ⏸️ and **zero** ⚪:
its E2E suite drives the mutating handlers with FakeCollection, so `POST
/jobs`, `/start`, `/pause`, `/cicd` and `DELETE /jobs/{id}` are observed
rather than assumed. Every other route module's `POST` handlers remain
deliberately unexercised.

**`routes/living.py` still shows 0 executed.** Stage 5 has no unit suite and
every entry point is a `POST`. It is the least-verified subsystem in the
backend and the honest place to look first if something misbehaves.

## Routes

| | Count |
|---|---:|
| Distinct paths | **300** |
| Path + method operations | **326** |
| `GET` | 137 |
| `POST` | 154 |
| `PUT` / `PATCH` / `DELETE` | 15 / 7 / 13 |

Direct Transform contributed 15 paths / 17 operations. 285 → 300.

| Sweep | Routes | ≥ 500 |
|---|---:|---:|
| Addressable `GET`, authenticated | **102** | **0** |
| Unaddressable (no fixture for a path param) | 35 | — |

Status distribution across the 102: **87 × 200**, 10 × 404, 3 × 400,
2 × 422 — every non-200 a deliberate, typed refusal (unknown id, bad
argument), none an unhandled failure. The sweep authenticates as the seeded
super-admin; the previous issue's run was anonymous, which is why it could
address 113 routes on a smaller schema but got 401s on the project-scoped
ones.

## App boot

Booted `uvicorn server:app` against real Mongo three times during this pass
(before the fix, after each fix). Every boot: **0 tracebacks, 0 `ERROR`
lines**, `GET /api/health` → 200.

---

# Part 2 — Frontend

## Inventory

| | Count |
|---|---:|
| Source files (`.js/.jsx/.ts/.tsx`) | 80 |
| Test files | 14 |
| **Exported symbols** | **352** |
| Exports referenced by a test | 74 |
| **Exports referenced nowhere** | **0** |
| Pages | 18 |
| Components | 45 |
| Distinct `data-testid` | **751** |

| Directory | Exports | Tested | ❌ Unreferenced |
|---|---:|---:|---:|
| `lib/` | 255 | 25 | **0** |
| `components/` | 55 | 31 | **0** |
| `hooks/` | 19 | 16 | **0** |
| `pages/` | 18 | 1 | **0** |
| `state/` | 4 | 1 | **0** |
| `App.js` | 1 | 0 | **0** |

`lib/` grew 239 → 255 (the 14 `dcte*` helpers plus their siblings) and
`pages/` 17 → 18. The export count fell 365 → 352 because `api.d.ts` is now
excluded from the inventory: it *declares* `api.js`'s exports rather than
adding new symbols, so counting both double-counted the API surface. That is
a correction to the previous issue's method, not a removal of code.

## Suites

| Suite | Tests | What it pins |
|---|---:|---|
| `hooks/__tests__/hooks.test.jsx` | 34 | `useBreakpoint` (matchMedia), `usePolling` (hidden-tab pause), `useJobProgress` |
| `components/ui/__tests__/feedback.test.jsx` | 28 | Skeletons, `JobProgress`, `ThinkingDots`, `AgentTimeline` — every live region |
| `components/ux/__tests__/Cards.test.jsx` | 26 | `MetricCard` keyboard path, `StepCard` blocked-state reason, `EmptyState` action |
| `components/ui/__tests__/status.test.jsx` | 21 | Three-channel status vocabulary (icon + text + colour), `ProgressBar` semantics |
| `__tests__/design-system.test.js` | 19 | Codebase-wide token, type-scale, a11y and bundle invariants — asserted on **source** |
| `components/ui/__tests__/form.test.jsx` | 19 | `Field` label/aria wiring, error announcement, focus-moving error summary |
| `components/ui/__tests__/button.test.jsx` | 16 | Loading-as-variant, `aria-busy`, WCAG 2.5.8 target sizes, token-only colours |
| `components/__tests__/StageProgress.test.jsx` | 16 | One tab stop per stage, no red-for-unreached, transformer-phase behaviour |
| `lib/__tests__/streamMessage.test.js` | 15 | SSE wire contract for `POST /api/chat/stream` — split frames, aborts, malformed data |
| `pages/__tests__/DiscoveryV2.test.jsx` | 14 | The P0-1 regression guard |
| `hooks/__tests__/useAutoSaveTracker.test.jsx` | 12 | The hook extracted to get recharts off the critical path |
| `lib/__tests__/routes.test.js` | 11 | Route-chunk registry, prefetch idempotence, `cn()` merge order |
| **`pages/__tests__/DirectTransform.test.jsx`** | **8** | **New** — the fourth Tools page's primary flow and its failure states |
| **`__tests__/tools-nav-registration.test.js`** | **8** | **New** — Tools nav symmetry across all five registries |
| **Total** | **239** | |

### The two new suites

**`DirectTransform.test.jsx`** covers the page's own behaviour: plugins load
into the stack picker, detect fills the stacks from the fingerprint,
create-and-start posts a well-formed job with the roots derived from service
1, and each of three failure paths lands as a toast rather than a blank
screen — a rejected create, a plugin list that will not load, and a job list
that will not load. The last two exist because the page originally swallowed
both with `.catch(() => {})`.

**`tools-nav-registration.test.js`** pins registration *symmetry* rather than
rendering. A Tools section is not wired up because its page renders; it is
wired up when it appears in the same registries its siblings appear in. The
suite asserts all four tools are present in the sidebar accordion in order
(Direct Transform last), in `App.js` routes, in the `ROUTE_CHUNKS` prefetch
registry, in the breadcrumb map and in the command palette — and that Direct
Transform is **absent** from the collapsed rail, where Integrations is absent
too, so that omission cannot be "fixed" into an asymmetry later.

## Static gates

| Gate | Result |
|---|---|
| `yarn test:ci` | ✅ **239 passed** / 14 suites |
| `yarn typecheck` (`tsc --noEmit`) | ✅ 0 errors |
| `yarn lint` | ✅ **0 errors** (19 warnings, all `react-hooks/exhaustive-deps`, all pre-existing) |
| `yarn build` | ✅ succeeds · main bundle **322.87 kB** |
| Unreferenced export scan | ✅ **0 of 352** |

The main bundle grew 322.3 → 322.87 kB (**+0.57 kB**) despite adding a
722-line page, because the route is lazy: Direct Transform ships as its own
**24 kB** chunk, fetched on navigation.

---

# Part 3 — The seam between them

Both halves are verified in isolation above. This is the only check that
crosses the boundary — the one that would catch a frontend calling a route
the backend does not serve.

**Method.** Extract every URL `lib/api.js` issues — the axios-instance form
(`api.post("/srs/freeze", …)`, resolved against its `/api` baseURL), absolute
literals (`` `${API}/…` ``), and the `XMLHttpRequest`/`fetch` upload paths —
then match each against the **live** OpenAPI schema, checking the method too.

| | Count |
|---|---:|
| URLs extracted from `lib/api.js` | **231** |
| Backend routes available | 300 |
| **Mismatches** | **0** |
| of which Direct Transform | 14 |

All 14 `dcte*` helpers resolve to a real `/api/dcte/*` route that accepts
their method. Routes with no frontend caller are operator/script surface —
`/api/kb/{pid}/owl-export` is the documented example, and Direct Transform
adds two more (`/jobs/{id}/artifact`, `/debug/env`), recorded as decision
DT-3 in `HUMAN_INTERVENTION.md`.

---

# Part 4 — Defects found and fixed in this pass

Four, all in Direct Transform, all found by **running a real job through the
live API and compiling what came out**. None was visible to the unit suites,
because every one of them produced output that *looked* migrated.

The proof is a real compiler. Before the fixes, `mvn -B -q -DskipTests
compile` on the generated service exited **1**. After, it exits **0** and
emits five class files.

| # | Defect | Root cause | Fix |
|---|---|---|---|
| 1 | `import io.helidon.security.annotations.Authenticated;` survived into the output — leaving `io.helidon` residue **and** no import for the `@PreAuthorize` the annotation above it had already become. | `_rewrite_imports` ran three regexes covering `jakarta.ws.rs`, `jakarta.inject`/`enterprise.context` and `org.eclipse.microprofile.`**config**. **11 of the 32 rows in `IMPORT_REPLACEMENTS` fall outside all three** and could never match — MicroProfile Health, Metrics and OpenAPI, and both `io.helidon.security.*` rows. They were dead lookup rows that read as working mappings. | One regex over every namespace the table names; `@PreAuthorize` added to the import-injection list. |
| 2 | `@Value(name = "pmis.default.page.size", defaultValue = "25")` — `javac`: *"annotation @Value is missing a default value for the element 'value'"*. | `@ConfigProperty` → `@Value` was a blind token swap. MicroProfile carries the key and default as **attributes**; Spring carries them inside one placeholder string. | Translate the attributes: `@Value("${key:default}")`. Three source forms handled (`name=`+`defaultValue=`, `name=` alone, positional). |
| 3 | `@Autowired` left stacked on the config field. Spring would look for an `int` bean and fail at startup. | `@Inject @ConfigProperty` is the MicroProfile idiom for a config field; rewritten pairwise it becomes `@Autowired @Value`. | Drop `@Inject` only when the next annotation is the config one. Plain `@Inject` still becomes `@Autowired` — pinned by its own test. |
| 4 | A clean migration reported *"1 file still carries legacy markers"* and flagged it `needs_manual`. | The residue scan was a raw substring match. It flagged DCTE's **own** generated `Application.java`, whose Javadoc explains what happened to the Helidon entrypoint, and the `// TODO(dcte):` markers the plugin deliberately leaves. Not cosmetic: with `ai_refactor` on, every fix-up round re-sent a correct file to the model to "fix" a sentence. | Blank comments before scanning, quote-aware so a `"http://x"` literal is not mistaken for a comment opener. |

**Evidence, end to end.** After the fixes, a live job against the shipped
Helidon sample: `completed` at 100%, no error; DCTE's own `scan_residual`
reports **0 files flagged**; `mvn compile` **exit 0**. The Oracle →
PostgreSQL plugin, same run: `completed` 100%, residue **clean**.

Sixteen tests pin all four in `test_iter20_dcte_migration_fidelity.py`,
including the two that stop a fix from over-reaching: real residue in *code*
is still caught, and ordinary `@Inject` dependency injection still becomes
`@Autowired`.

### Defect 4's blast radius, stated plainly

Fix 4 changes what the DCTE Tester agent scores and what the AI transformer
re-sends. It makes the gate **less** noisy, not less strict — a marker in
executable code is still residue. The one behavioural consequence worth
naming: files that were previously flagged and re-sent for fix-up rounds now
pass first time, so a job with `ai_refactor=True` will make fewer LLM calls.

---

# Part 5 — Direct Transform, verified live

The feature integrated in this branch, exercised against the running app
rather than described.

| Check | Result |
|---|---|
| Registered in the route table | ✅ 15 paths / 17 operations under `/api/dcte` |
| Auth posture matches its three siblings | ✅ 0 route-level auth deps — same as `tools` (50/0), `console` (28/0), `integrations` (5/0), `prompts` (5/0); `admin` is 9/9 |
| CORS | ✅ one shared `CORSMiddleware` for the whole app |
| All four Tools sections answer | ✅ console/providers (2), integrations/catalog (23), prompts (58), dcte/plugins (2) |
| Plugin registry | ✅ `helidon-mp-to-spring-boot-3`, `oracle-to-postgres` |
| Detect, on the shipped sample | ✅ `helidon-mp` @ 0.92 → `spring-boot-3` |
| **Helidon → Spring job, live** | ✅ completed 100%, 9 files, residue clean, **`mvn compile` exit 0** |
| **Oracle → PostgreSQL job, live** | ✅ completed 100%, residue clean |
| Function coverage | ✅ **135 of 174 executed (77.6%)** |
| Model routing | ✅ 5 `dcte.*` agent keys, all tier-resolved; **no vendor or model string anywhere in the feature** outside a test fixture |

Pipelines unaffected: stage pipeline 10 routes, Multi-Agent CodeGen 39,
Transformer 37, Gap Analyzer 10 — and `routes/codegen.py`, `routes/tools.py`,
`pipeline.py`, `confidence.py` and `llm.py` have **0 lines changed** on this
branch.

---

# The export gate, removed (iter-20.1)

The previous issue reported the iter-20 export gate as a verified feature.
It has since been **removed on the operator's instruction**, so this issue
records that rather than leaving a stale claim standing.

iter-20 made `download_transformed_code` and `push_transformation_to_github`
return **409** unless `compile_green AND production_ready`, and hid the
buttons in the UI. The intent was sound: a broken Helidon → Spring Boot
tree had reached their disk looking finished.

**Why it was wrong.** A gate is only defensible when the condition it gates
on is reliably achievable. It is not: four escalation rungs and a
regenerator still do not produce a green build on their real services. So
the gate never actually stopped bad code shipping — it stopped the operator
**retrieving their own code**, which is strictly worse, because the old
behaviour at least let them take the folder to another tool and fix it by
hand, which is what they had been doing successfully all along.

*A gate that fires on the normal case is not a quality control, it is an
outage.*

| Removed | Kept |
|---|---|
| `_build_readiness_gate` | `compile_green` in the `/status` projection |
| Both `409` raises | The compile panel and DevOps audit panel |
| `download_blocked_reason` | `production_ready` reporting |
| The frontend blocked-state panel | The whole four-rung escalation ladder |
| `LAMA_ALLOW_UNVERIFIED_DOWNLOAD` | The armed residue gate, playbooks, Azure ladder |
| `export-gate.test.js` (6 tests) | — |

The distinction that survives: **build state is reported, not enforced.**

**Guarded rather than merely deleted.** Three tests in
`test_iter20_devops_convergence.py` assert on source that the helper is
gone, that `download_transformed_code` raises only 404/400 and never 409,
and that the push path raises no 409 either. A future iteration that thinks
a gate is a good idea fails them first.

**Verified live** against all three of the operator's real red-build jobs —
`Procument-plan-service`, `negotiation-service`, `dsc service`, every one
`compile_green: false`:

| Check | Result |
|---|---|
| 9 downloads (3 jobs × `code`/`tests`/`all`) | ✅ **9 × HTTP 200** |
| `negotiation-service` ZIP opens | ✅ valid, **150 files** |
| `/status` carries `download_blocked_reason` | ✅ no longer present |
| `/status` still reports `compile_green` | ✅ `false` — honest, not hidden |

---

# Gate summary

| Gate | Result |
|---|---|
| `pytest backend/tests/` | ✅ **1,243 passed** / 129 skipped |
| `ruff check backend` | ✅ clean |
| Backend execution trace | ✅ **907 frames** observed |
| Backend orphan scan (AST) | ✅ **0 of 1,724** |
| Live boot | ✅ 0 tracebacks, 0 ERROR lines |
| Routes registered | ✅ 300 paths / 326 operations |
| 102 live `GET` routes | ✅ **0 responses ≥ 500** |
| Live migration, compiled | ✅ **`mvn compile` exit 0** |
| `yarn test:ci` | ✅ **239 passed** / 14 suites |
| `yarn typecheck` | ✅ 0 errors |
| `yarn lint` | ✅ 0 errors |
| `yarn build` | ✅ succeeds · 322.87 kB |
| Frontend orphan scan | ✅ **0 of 352** |
| API contract (231 URLs) | ✅ **0 mismatches** |

**Combined: 1,482 automated tests across both halves, all passing.**

---

# What this report does not prove

Stated plainly so the numbers are not read as more than they are:

1. **The 166 mutating handlers are unverified here.** Proving them means
   running a real migration — creating a project, scanning a legacy tree,
   freezing five stages, generating code. That is a pipeline run, not an
   audit, and it writes to your live database. Direct Transform is the
   exception: its mutating handlers *are* exercised, because it has no
   upstream stage to freeze and its engine takes no Mongo handle.
2. **`routes/living.py` has no coverage at all.** 0 of 59 functions
   executed. Stage 5 is the honest blind spot.
3. **The compile proof covers one service.** `mvn compile` was run on the
   migrated Helidon sample — a three-class fixture, not a 150-file
   production tree. It proves the four defects are fixed; it does not prove
   every Helidon construct migrates cleanly.
4. **`mvn compile` is not `mvn test`, and neither is a boot.** The service
   compiles; nothing here started it and probed `/actuator/health`. The
   Tester agent does that, and it was not run in this pass.
5. **No LLM path was exercised.** Every measurement here ran with
   `ai_refactor=False` so the deterministic layer was the only thing
   touching disk. The AI transformer, build fixer, devops and narrator
   agents are ⏸️/🟡, not ✅.
6. **🟡 Reachable is an inference.** It means "called from code that
   executes and I found no fault", not "tested".

---

# Corrections — this issue's own errors

This report claims accuracy, so the mistakes I made *producing it* belong in
it. All were in my measurement scripts, and each would have shipped a false
finding.

| # | The script said | Actually | Cause |
|---|---|---|---|
| 1 | **298 backend orphans** | **0** | The scan counted a function as referenced only if its *name* appeared elsewhere. FastAPI route handlers, pytest fixtures, `@property` and `@app.on_event` handlers are registered **by their decorator** and are never called by name, so all 333 decorated functions read as dead. Fixed by treating a decorator as registration. |
| 2 | **5 API-contract mismatches**, incl. `POST /api/srs/freeze` "route exists but methods=['get']" | **0** | My path matcher let a route's `{param}` wildcard match a *literal* frontend segment, so `/api/srs/{project_id}` swallowed the literal `/api/srs/freeze`, and `.../envelopes/{envelope_id}` swallowed `.../envelopes/confirm`. |
| 3 | **7 mismatches** after fixing #2 | **0** | Over-corrected: I then scored a frontend `${expr}` against a route *literal* as highly as against a route `{param}`, so `/api/srs/${projectId}` matched `/api/srs/generate`. Fixed by ranking literal==literal > route-param > FE-interpolated-literal. |
| 4 | `jakarta.annotation.PostConstruct` is an unreachable mapping row | Reachable | A flaw in the **test**, not the code: that row maps the class to *itself*, so "the old import is still present" is equally true whether the row fired or never matched. Re-asserted against the regex directly. |

The pattern in all four is the one the previous issues also recorded: a
scanner's output is a **claim**, not a finding. #1 and #2 would each have
reported a large, alarming, entirely false number.

The four *product* defects in Part 4 went the other way — they were invisible
to every static check and to 1,234 passing tests, and only a real compiler
found them. Both halves of that lesson are worth keeping: do not trust a
scanner's finding without checking it by hand, and do not trust a green suite
as proof that generated output is correct.

## Corrections carried forward from earlier issues

Kept because they remain the honest record of how those issues were wrong:

| # | It said | Actually |
|---|---|---|
| 1 | "**14 orphans**", in five places | **15** at that point — the headline disagreed with its own table. |
| 2 | The orphan list was complete | It **missed three**. `ast.walk` descended into nested functions and diluted the reference counts; restricting it to top-level defs surfaced `_safe_llm_call`, `_should_abort_for_transport` and `hf_confidence::preload`. True total **18**. |
| 3 | `Core (top-level)` had 6 orphans | **7** — `pipeline.py` (5) + `factory_orchestrator.py` (2). |
| 4 | **286 backend orphans** (previous issue) | **0** — definition sites were subtracted from the reference count, but `ast.FunctionDef` stores its name as a string attribute, not a `Name` node, so definitions never contributed to that count in the first place. |
