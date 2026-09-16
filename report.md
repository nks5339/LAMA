# LAMA — Function Status Report

**Generated** 2026-09-16 · branch `refactor/zero-waste` · commit `391a1e2`+
**Scope** the whole application — **1,552 backend functions** and **365
frontend exports**, measured separately and then against each other.

This issue supersedes the previous one. It re-measures everything after
iter-20 (the migration-fidelity, Azure-ladder and export-gate work), and it
covers the **frontend at function level** for the first time — earlier
issues reported frontend *suites* but never asked whether every exported
symbol is actually reachable. That question found real dead code.

---

## How to read this

You asked for each function marked **working** or **not working**. Where I
have evidence, that is what you get. Where I do not, this report says so
rather than guessing — a green tick I cannot defend is worse than an honest
blank, because it is the one you would stop checking.

Every status below comes from something I ran, not from reading the code:

| Evidence | Method | Result |
|---|---|---|
| **Backend execution trace** | `sys.setprofile` over the full suite, recording every backend frame that really ran | 1,172 passed / 129 skipped · **692 frames observed** |
| **Frontend unit + contract suite** | Jest 27 + React Testing Library 16, `yarn test:ci` | **229 passed / 13 suites** · 0 failed |
| **Live HTTP sweep** | Real app + real Mongo, every `GET` I could address | **113 routes · 0 responses ≥ 500** |
| **Reference analysis (backend)** | AST over `Name` / `Attribute` / `ImportFrom` / string constants, including test files and framework decorators | **0 orphans of 1,552** |
| **Reference analysis (frontend)** | Every exported symbol vs. every reference across `src/` | **0 unreferenced of 365** |
| **API contract** | Every URL `lib/api.js` calls, matched against the live OpenAPI schema | **194 URLs · 0 mismatches** |
| **Live model round-trips** | Real calls to your Azure account | gpt-5.1 serving, detailed below |

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
| ✅ Executed — proven working | **514** | 33.1% |
| 🟡 Reachable — no fault found | 754 | 48.6% |
| ⏸️ Mutating — not exercised | 167 | 10.8% |
| ⚪ GET, no fixture | 117 | 7.5% |
| ❌ Orphan | **0** | 0.0% |
| **Total** | **1,552** | |

> **The basis changed since the last issue** and these counts are not
> comparable to it. This inventory includes **nested functions and class
> methods**, which the previous one did not — hence 1,552 where the last
> issue said 1,524, and a lower `EXECUTED` share against a larger
> denominator. Nothing regressed: raw frames observed went **up**,
> 663 → 692.

**Nothing is marked "not working."** Across 113 live HTTP routes and 1,172
tests, no function raised, no endpoint returned 5xx, and the backend boots
with zero tracebacks and zero ERROR lines.

## By subsystem

| Subsystem | Total | ✅ Executed | 🟡 Reachable | ⏸️ Mutating | ⚪ GET-unhit | ❌ Orphan |
|---|---:|---:|---:|---:|---:|---:|
| `routes/` | 867 | 246 | 340 | 167 | 114 | **0** |
| core (top-level) | 261 | 113 | 145 | 0 | 3 | **0** |
| `kb/` | 231 | 85 | 146 | 0 | 0 | **0** |
| `codegen/` | 60 | 17 | 43 | 0 | 0 | **0** |
| `fabric/` | 58 | 43 | 15 | 0 | 0 | **0** |
| `datamodel/` | 39 | 0 | 39 | 0 | 0 | **0** |
| `integrations/` | 23 | 2 | 21 | 0 | 0 | **0** |

`datamodel/` shows 0 executed because its generators are reached only
through Stage-2 route handlers, which are all `POST` — they are
**⏸️ Mutating**, not broken. `fabric/` is the best-covered subsystem at 74%
executed, which is what you want from the module every LLM call passes
through.

## Largest modules

| Module | Fns | ✅ | 🟡 | ⏸️ | ⚪ | ❌ |
|---|---:|---:|---:|---:|---:|---:|
| `routes/tools.py` | 185 | 94 | 53 | 21 | 17 | **0** |
| `routes/codegen.py` | 178 | 67 | 74 | 25 | 12 | **0** |
| `routes/architecture.py` | 99 | 3 | 74 | 16 | 6 | **0** |
| `routes/srs.py` | 86 | 52 | 21 | 10 | 3 | **0** |
| `routes/kb.py` | 82 | 4 | 30 | 20 | 28 | **0** |
| `routes/living.py` | 59 | 0 | 41 | 12 | 6 | **0** |
| `factory_orchestrator.py` | 51 | 16 | 35 | 0 | 0 | **0** |
| `fabric/model_fabric.py` | 41 | 37 | 4 | 0 | 0 | **0** |
| `routes/datamodel.py` | 40 | 4 | 15 | 16 | 5 | **0** |
| `llm.py` | 34 | 20 | 14 | 0 | 0 | **0** |
| `routes/console.py` | 34 | 2 | 4 | 17 | 11 | **0** |
| `arch_deterministic.py` | 29 | 1 | 28 | 0 | 0 | **0** |

`routes/tools.py` is the most-exercised large module (94 of 185 traced) —
it holds the Transformer, which iter-20 rebuilt. **`routes/living.py` shows
0 executed**: Stage 5 has no unit suite and every entry point is a `POST`.
It is the least-verified subsystem in the backend and the honest place to
look first if something misbehaves.

## Routes

| | Count |
|---|---:|
| Distinct paths | **285** |
| Path + method operations | **309** |
| `GET` | 128 |
| `POST` | 147 |
| `PUT` / `PATCH` / `DELETE` | 15 / 7 / 12 |

| Sweep | Routes | ≥ 500 |
|---|---:|---:|
| Parameterless `GET` | 31 | **0** |
| Project-scoped `GET` (`{project_id}`) | 54 | **0** |
| All addressable `GET` (incl. `{transform_id}`) | **113** | **0** |

---

# Part 2 — Frontend

Measured at function level for the first time. Earlier issues reported the
suite count but never asked whether every exported symbol is reachable —
the frontend equivalent of the backend orphan scan.

## Inventory

| | Count |
|---|---:|
| Source files (`.js/.jsx/.ts/.tsx`) | 78 |
| Test files | 13 |
| **Exported symbols** | **365** |
| Exports referenced by a test | 56 |
| **Exports referenced nowhere** | **0** |
| Pages | 17 |
| Components | 51 |
| Distinct `data-testid` | **547** |

| Directory | Exports | Tested | ❌ Unreferenced |
|---|---:|---:|---:|
| `lib/` | 239 | 8 | **0** |
| `components/` | 90 | 31 | **0** |
| `pages/` | 17 | 1 | **0** |
| `hooks/` | 14 | 14 | **0** |
| `state/` | 4 | 1 | **0** |
| `App.js` | 1 | 1 | **0** |

`hooks/` is fully covered — 14 of 14 exports have a test. `lib/` shows 8 of
239 because the bulk of it is `api.js`: 239 thin axios wrappers, verified
structurally instead by the API-contract check in Part 3, which is the
right shape of evidence for a one-line wrapper.

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
| **`__tests__/export-gate.test.js`** | **6** | **New (iter-20)** — every export affordance sits behind the build gate |
| **Total** | **229** | |

### The new export-gate suite

iter-20 stopped the UI offering a download the backend now refuses with a
409. Six assertions pin it, on **source** rather than on a render:
`Transformer.jsx` is ~5,900 lines with a large mock surface, and the
invariant is structural. A render test proves one case; this proves there
is no second case.

- The page reads the backend's verdict (`download_blocked_reason`) rather
  than recomputing it from `status` strings, so the button and the 409
  cannot disagree.
- The verdict is assigned with `?? null` on **every** poll, so the export
  unblocks the moment the build goes green instead of keeping a stale
  reason on screen.
- All three ZIP links **and** GitHub push sit inside the gated branch.
- The second export site (the Tester tab's "Tests ZIP") is gated too —
  gating the kebab menu alone would have left a hole in the same gate.
- A blocked state **explains itself** rather than showing nothing, because
  an export that silently disappears reads as a bug.
- Exactly **4** export call sites exist. A fifth fails the suite, so a new
  download affordance has to be gated deliberately.

## Static gates

| Gate | Result |
|---|---|
| `yarn test:ci` | ✅ **229 passed** / 13 suites |
| `yarn typecheck` (`tsc --noEmit`) | ✅ 0 errors |
| `yarn lint` | ✅ **0 errors** (19 warnings, all `react-hooks/exhaustive-deps`) |
| `yarn build` | ✅ succeeds · main bundle 322.3 kB |
| Unreferenced export scan | ✅ **0 of 365** |

---

# Part 3 — The seam between them

Both halves are verified in isolation above. This is the only check that
crosses the boundary — the one that would catch a frontend calling a route
the backend does not serve.

**Method.** Extract every URL `lib/api.js` issues — both the axios-instance
form (`api.post("/srs/freeze", …)`, resolved against its `/api` baseURL) and
absolute literals (`` `${API}/…` ``) — then match each against the **live**
OpenAPI schema, checking the HTTP method too.

| | Count |
|---|---:|
| URLs extracted from `lib/api.js` | **194** |
| Backend routes available | 285 |
| **Mismatches** | **0** |

Every URL the frontend calls resolves to a real backend route that accepts
that method. Routes with no frontend caller are operator/script surface —
`/api/kb/{pid}/owl-export` is the documented example, which CLAUDE.md
records as deliberately kept with no UI button.

---

# Part 4 — Defects found and fixed in this pass

Four dead frontend exports, found by the reference scan and **verified by
hand before removal** — a scanner saying "unused" is a claim, not a finding.

| # | Symbol | Why it was really dead | Action |
|---|---|---|---|
| 1 | `components/ux/Cards.jsx::ActionPanel` | A sticky bottom action bar, 12 lines of JSX. No callers, no test. Its three siblings in the same file (`MetricCard`, `StepCard`, `EmptyState`) are all used and tested — it is the odd one out, not part of a symmetric family. | **Removed** |
| 2 | `hooks/useBreakpoint.ts::useIsSmall` | I mapped the whole family before deciding: `useMediaQuery` (1 use), `useIsMobile` (10), `usePrefersReducedMotion` (2) and `useBreakpoint` (8) are all used **and** tested. `useIsSmall` had **0 uses and 0 tests**. Its module docstring advertised it, so that line went too — leaving it would have made the docstring false. | **Removed** |
| 3 | `lib/queryClient.ts::jobPollInterval` | A React Query `refetchInterval` helper. **Nothing in the app issues a React Query query at all** — `useQuery` appears only inside `queryClient.ts`. `usePolling` is the live polling solution. This was written for a migration that was started (the provider is mounted in `App.js`) and never carried through. | **Removed** |
| 4 | `lib/queryClient.ts::TERMINAL` | **Cascade from #3** — `jobPollInterval` was its only consumer, and `hooks/usePolling.ts` already declares an identical set. Removing #3 without this would have left a fresh orphan behind. | **Removed** |

> **Left in place deliberately:** the `QueryClientProvider` in `App.js`. It
> wraps the tree with zero queries behind it, which is waste by the letter
> of the bar — but it is a mounted foundation for a migration someone
> intended, and removing it is a design decision, not a defect fix.
> Recorded here so the next person inherits the knowledge rather than
> rediscovering it.

Also fixed during the backend sweep:

| Symbol | Defect | Action |
|---|---|---|
| `routes/tools.py::get_transformation_file` | Passed `file_id` straight to `ObjectId()`, which raises `bson.errors.InvalidId` on a malformed id → **HTTP 500**. Its sibling `regenerate_transformation_file` has guarded this since iter-15.10; this route was simply missed. | **Fixed** — now 400 |
| `routes/tools.py::get_transformation_status` | Computed `download_blocked_reason` from a **projected** document that omitted `compile_green` and `build_tools`, so the gate reported "not blocked" for a job the download endpoint was correctly 409-ing. The UI would have shown a button that fails when clicked. | **Fixed** |

**Verified after every removal:** 229 frontend tests pass, typecheck clean,
lint 0 errors, build succeeds, re-scan reports **0 unreferenced of 365**;
backend 1,172 tests pass, ruff clean, **113 GET routes with 0 responses
≥ 500**.

---

# Part 5 — Live model verification

Real calls to your Azure account, not mocks.

| Check | Result |
|---|---|
| Test Connection | ✅ `ok: true` · **gpt-5.1** · 5,104 ms |
| Endpoint | `…/eyq/as/api/openai/deployments/gpt-5.1` |
| `tools.transformer.coder` resolves to | ✅ **gpt-5.1** (tier `critical`) — was gpt-5 |
| `.verifier` / `.planner` / `.devops_expert` | ✅ **gpt-5.1** each |
| `tools.transformer.regenerator` (new) | ✅ **gpt-5.1**, answers correctly |
| `tools.transformer.tester` (light agent) | ✅ gpt-4.1-mini — correctly **not** promoted |
| Exhaustion ladder, resolved from your catalogue | ✅ `gpt-5.1 → gpt-5 → gpt-4.1 → gpt-4o → gpt-5-mini → gpt-4.1-mini → gpt-4o-mini` |
| Tier migration | ✅ applied, and idempotent on a second run |

**The residue gate, run against your real output.** The single most
valuable measurement here: the now-armed source-stack gate was run over all
150 generated files of your `negotiation-service` job.

| | |
|---|---|
| Files scanned | 150 |
| **Rejected for source-stack residue** | **7** |
| `logback.xml`, `logging.properties` | still reference `io.helidon` |
| 5 entity/model classes | still import `jakarta.json` / `javax.json` |

All seven previously shipped with an ACCEPT verdict attached, because the
gate that should have caught them was never executing.

**A correction to the original complaint:** the generated `pom.xml` was in
fact **clean of Helidon coordinates** — its only mention is a comment, which
the new comment-aware scanning correctly ignores. Its real defect was
different and nobody had named it: **zero `springdoc`**, so the Swagger you
asked for was never added at all. The per-target playbook now requires it
by name.

---

# Gate summary

| Gate | Result |
|---|---|
| `pytest backend/tests/` | ✅ **1,172 passed** / 129 skipped |
| `ruff check backend` | ✅ clean |
| Backend execution trace | ✅ **692 frames** observed |
| Backend orphan scan (AST) | ✅ **0 of 1,552** |
| Live boot | ✅ 0 tracebacks, 0 ERROR lines |
| Routes registered | ✅ 285 paths / 309 operations |
| 113 live `GET` routes | ✅ **0 responses ≥ 500** |
| `yarn test:ci` | ✅ **229 passed** / 13 suites |
| `yarn typecheck` | ✅ 0 errors |
| `yarn lint` | ✅ 0 errors |
| `yarn build` | ✅ succeeds |
| Frontend orphan scan | ✅ **0 of 365** |
| API contract (194 URLs) | ✅ **0 mismatches** |
| Test Connection | ✅ `ok:true` on gpt-5.1 |

**Combined: 1,401 automated tests across both halves, all passing.**

---

# What this report does not prove

Stated plainly so the numbers are not read as more than they are:

1. **The 167 mutating handlers are unverified here.** Proving them means
   running a real migration — creating a project, scanning a legacy tree,
   freezing five stages, generating code. That is a pipeline run, not an
   audit, and it writes to your live database.
2. **🟡 REACHABLE is inference, not proof.** It means "called from code
   that runs and nothing contradicts it", not "I watched it work". At 754
   functions it is the largest bucket in this report.
3. **Execution ≠ correctness.** A traced function ran without raising. For
   the LLM agents I checked output *shape* against the real parsers; I did
   not grade answer quality.
4. **`routes/living.py` has 0 executed functions.** Stage 5 has no unit
   suite and every entry point is a `POST`. Least-verified subsystem here.
5. **Frontend coverage is structural for most of the application.** 56 of
   365 exports have a direct test; the rest are covered by lint, typecheck,
   build, the source-contract suites and the API-contract check — weaker
   than execution. `Transformer.jsx` (~5,900 lines) and `GapAnalyzer.jsx`
   (~2,200) remain the largest surfaces without a render test, which is
   exactly why the export gate was pinned on source.
6. **No end-to-end test exists.** Nothing drives a browser through a real
   migration. The seam is verified by the API-contract check, which proves
   the *addresses* line up — not that the payloads do.
7. **iter-20 has not been proven on a full live migration.** The residue
   gate, the model ladder and the export gate are each verified in
   isolation and against your existing artifacts. Re-running
   `negotiation-service` end to end is the test that would settle it, and
   it has not been run.

**One-line summary: nothing in this application is known to be broken, 514
backend functions and 229 frontend tests are proven to run, both halves
have zero dead code, and all 194 frontend API calls resolve to real backend
routes.**

---

# Corrections — this issue's own errors

This report claims accuracy, so the mistakes I made *producing it* belong
in it. All were in my measurement scripts, and each would have shipped a
false finding.

| # | The script said | Actually | Cause |
|---|---|---|---|
| 1 | **286 backend orphans** | **0** | I subtracted definition sites from the reference count — but `ast.FunctionDef` stores its name as a plain **string attribute**, not a `Name` node, so a definition never contributed to that count in the first place. Subtracting it double-penalised every function in the codebase. |
| 2 | 2 orphans (`server.py::on_startup`, `on_shutdown`) | **0** | Both are `@app.on_event` handlers, invoked by FastAPI rather than by name — the same class of false positive as `__enter__`/`__exit__`. The boot log proves `on_startup` runs: it prints the seed banner. |
| 3 | **5 API-contract mismatches** | **0** | `routes.find()` returns the *first* regex match, so the wildcard route `/api/srs/{project_id}` swallowed the literal `/api/srs/freeze` and reported a working `POST` endpoint as "GET only". Fixed by ranking candidates by fewest wildcard segments. |

A fourth was caught before it produced a number: the first API-contract
script escaped regex metacharacters **after** substituting the `[^/]+`
wildcard, escaping the wildcard itself and matching almost nothing (3 of
12).

The pattern in all four is the one the previous issue also recorded: a
scanner's output is a **claim**, not a finding. Every removal in Part 4 was
confirmed by hand — reading the symbol, checking its siblings, and in the
`useIsSmall` case mapping the whole hook family — before anything was
deleted. #1 and #3 are exactly what happens when that step is skipped.

## Corrections carried forward from the previous issue

Kept because they remain the honest record of how that issue was wrong:

| # | It said | Actually |
|---|---|---|
| 1 | "**14 orphans**", in five places | **15** at that point — the headline disagreed with its own table. |
| 2 | The orphan list was complete | It **missed three**. `ast.walk` descended into nested functions and diluted the reference counts; restricting it to top-level defs surfaced `_safe_llm_call`, `_should_abort_for_transport` and `hf_confidence::preload`. True total **18**. |
| 3 | `Core (top-level)` had 6 orphans | **7** — `pipeline.py` (5) + `factory_orchestrator.py` (2). |
