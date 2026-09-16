# LAMA — Function Status Report

**Generated** 2026-09-16 · branch `refactor/zero-waste` · commit `e9a5378`+
**Revised** after the orphan sweep — see *Corrections to the first issue* at the end
**Revised again** after the frontend modernisation — a JS test runner now
exists, so the *Frontend* section below reports measured results instead of
declining to. Backend grew 19 tests (streaming chat + citation retrieval).
**Scope** every function in `backend/` (1,542) plus the frontend surface

---

## How to read this

You asked for each function marked **working** or **not working**. Where I
have evidence, that is what you get. Where I do not, this report says so
rather than guessing — a green tick I cannot defend is worse than an honest
blank, because it is the one you would stop checking.

Every status below comes from one of three things I actually ran:

| Evidence | Method | Result |
|---|---|---|
| **Test execution trace** | `sys.setprofile` over the full suite, recording every backend function that really ran | 1,055 passed / 129 skipped · 663 functions observed |
| **Frontend unit + contract suite** | Jest 27 + React Testing Library 16, `yarn test:ci` | **221 passed / 12 suites** · 0 failed |
| **Live HTTP trace** | `TestClient` against the real app + real Mongo, every parameterless `GET` under `/api` | 31 routes · **0 responses ≥ 500** |
| **Project-scoped HTTP trace** | Same, for every `GET` taking only `{project_id}` — run against *PMIS Migration Pilot* | 54 routes · **0 responses ≥ 500** |

Plus live model round-trips against your Azure account (detailed below).

### Status vocabulary

| Status | Means | Confidence |
|---|---|---|
| ✅ **EXECUTED** | Observed running in a trace above. It works. | Proven |
| 🟡 **REACHABLE** | Called from code that executes, but I did not observe this frame. Not evidence of a fault. | Inferred |
| ⏸️ **MUTATING** | A `POST`/`PUT`/`DELETE` handler. **Deliberately not invoked** — firing these would create projects, start LLM jobs and write to your live database. | Untested here |
| ⚪ **GET-UNHIT** | A `GET` needing an id I had no fixture for (`{transform_id}`, `{analysis_id}`). | Untested here |
| ❌ **ORPHAN** | Defined; referenced nowhere. Dead code. | Proven |

---

## Headline

| | Count | Share |
|---|---:|---:|
| ✅ Executed — proven working | **595** | 39.0% |
| 🟡 Reachable — no fault found | 719 | 47.2% |
| ⏸️ Mutating — not exercised | 166 | 10.9% |
| ⚪ GET, no fixture | 44 | 2.9% |
| ❌ Orphan | **0** | 0.0% |
| **Total** | **1,524** | |

**Nothing is marked "not working," and there is no longer any dead code.**
Across 85 live HTTP routes and 1,055 backend tests, no function raised, no
endpoint returned 5xx, and the backend boots with zero tracebacks and zero
ERROR lines. A further **221 frontend tests** now cover the UI, which the
previous issue could not measure at all. The defects found were 18 orphans
and 3 backend bugs — **all fixed** — plus 6 frontend defects found by the
new contract suite and the P0 audit, also all fixed.

---

## By subsystem

| Subsystem | Fns | ✅ Exec | 🟡 Reach | ⏸️ Mut | ⚪ Unhit | ❌ Orphan |
|---|---:|---:|---:|---:|---:|---:|
| HTTP route modules | 857 | 301 | 346 | 166 | 44 | **0** |
| Core (top-level) | 259 | 129 | 130 | 0 | 0 | **0** |
| Knowledge base (`kb/`) | 232 | 98 | 134 | 0 | 0 | **0** |
| CodeGen helpers (`codegen/`) | 62 | 17 | 45 | 0 | 0 | **0** |
| LLM fabric (`fabric/`) | 52 | 42 | 10 | 0 | 0 | **0** |
| DataModel generators | 39 | 4 | 35 | 0 | 0 | **0** |
| Integrations | 23 | 4 | 19 | 0 | 0 | **0** |

The fabric — the part every other subsystem depends on — has the highest
proven coverage (81%). No subsystem carries dead code any more.

## Largest modules

| Module | Fns | ✅ | 🟡 | ⏸️ | ⚪ | ❌ |
|---|---:|---:|---:|---:|---:|---:|
| `routes/codegen.py` | 178 | 76 | 75 | 25 | 2 | 0 |
| `routes/tools.py` | 174 | 88 | 50 | 21 | 13 | 0 |
| `routes/architecture.py` | 99 | 4 | 75 | 16 | 4 | 0 |
| `routes/srs.py` | 86 | 53 | 22 | 10 | 1 | 0 |
| `routes/kb.py` | 82 | 33 | 28 | 20 | 1 | 0 |
| `routes/living.py` | 59 | 3 | 41 | 11 | 4 | 0 |
| `factory_orchestrator.py` | 51 | 13 | 38 | 0 | 0 | 0 |
| `routes/datamodel.py` | 40 | 2 | 19 | 16 | 3 | 0 |
| `fabric/model_fabric.py` | 35 | 30 | 5 | 0 | 0 | 0 |
| `llm.py` | 34 | 21 | 13 | 0 | 0 | 0 |
| `routes/console.py` | 34 | 13 | 2 | 17 | 2 | 0 |
| `kb/journey_materializer.py` | 29 | 25 | 4 | 0 | 0 | 0 |
| `arch_deterministic.py` | 29 | 0 | 29 | 0 | 0 | 0 |

**`routes/architecture.py`, `routes/living.py`, `routes/datamodel.py` and
`arch_deterministic.py` are the thin spots** — their work happens inside
`POST` generation jobs that this audit deliberately did not fire. Their
low ✅ count reflects what I chose not to run, not a fault I found.

---

## Dead code — 18 found, 18 removed

All eliminated. `ORPHAN` is now zero across all 1,524 functions.

| Module | Removed |
|---|---|
| `pipeline.py` | `get_module_context`, `get_toon_summary`, `get_srs_section`, `get_domain_map`, `get_er_model` |
| `kb/deep_analyzer.py` | `get_analysis_summary`, `get_analysis_json` |
| `kb/kb_graph.py` | `get_graph_summary` |
| `kb/toon.py` | `serialise_individual` |
| `routes/tools.py` | `_summarize_code_entities`, `_extract_doc_requirements_local` |
| `routes/living.py` | `_chunk`, `_render_endpoint_batch`, `_clean_jmeter_fragment` |
| `routes/architecture.py` | `_safe_llm_call`, `_should_abort_for_transport` |
| `codegen/confidence_graph.py` | `default_threshold` |
| `codegen/zip_builder.py` | `estimate_zip_size` |
| `codegen/hf_confidence.py` | `preload` |
| `factory_orchestrator.py` | `_looks_like_windows_shell`, `_estimate_prompt_tokens` |

Three of these are worth a note because "dead" was the wrong first read:

- **`pipeline.py`'s five getters** were a whole family of stage-context
  accessors — `get_toon_summary`, `get_srs_section`, `get_domain_map`,
  `get_er_model`, `get_module_context`. The live path uses
  `get_stage_context` / `require_stage_context`; these were the API that
  lost. The largest single cluster.
- **`architecture.py`'s `_safe_llm_call` / `_should_abort_for_transport`**
  looked alarming: a transport-error guard whose own comment said callers
  use it to avoid persisting a broken artifact after a network failure.
  Nothing called it. But the guard is **not** missing — iter-13.51
  replaced it with a superset (`_classify_llm_error_kind` at :4229,
  `_should_abort_job` at :4253, both live), and `_should_abort_job`'s
  docstring says so outright. These were superseded leftovers, and the
  docstring naming the removed function has been corrected.
- **`hf_confidence.preload`** was an advertised LangGraph warm-up hook —
  the module docstring offered it for pinning to the loop's `START` node
  and no orchestrator ever pinned it. The corpus still warms lazily on the
  first `score_file`, so removing it changes nothing at runtime. Docstring
  corrected.

**Not orphans, despite what a scanner says:** `_suppress_all.__enter__` /
`__exit__` in `fabric/factory_cli.py` — invoked by the `with` protocol at
line 559, not by name. A naive dead-code pass flags them; they stay.

---

## Defects found and fixed during this audit

| # | Where | Defect | Status |
|---|---|---|---|
| 1 | `codegen.planner` | Returned an **empty string** on every call. Routes to gpt-5 and asks for 3,000 tokens; the model spent all 3,000 on internal reasoning and emitted nothing. The earlier 2,000-token floor only protected calls asking for *less* than the floor. | ✅ Fixed — reserve is now additive (+6,000). 12/12 agents parse. |
| 2 | `test_srs_streaming.py::_Cursor` | The Mongo test double implemented `sort()`/`to_list()` but **not `__aiter__`**, while a real Motor cursor is async-iterable. Any production code doing `async for d in col.find(...)` raised `TypeError`, had it swallowed by its own `except Exception`, and returned an empty fallback. The path *looked* exercised and never was. | ✅ Fixed |
| 3 | `routes/living.py::_chunk` | Orphaned. I missed it in the earlier cleanup because `grep -c "_chunk"` matched the unrelated `kb_chunks` import and I read the count as a caller. | ✅ Removed |

---

## New backend tests — streaming chat

`routes/chat.py` was POST-only; `llm.fabric_call_stream` already existed and
already handled the three execution modes, so streaming was wiring rather
than new infrastructure. Two suites were added with it.

| Suite | Tests | What it pins |
|---|---:|---|
| `test_iter20_chat_stream.py` | 10 | The SSE wire contract, pipeline parity between the two transports, degraded-provider behaviour, persist-exactly-once, session turn-appends, SRS-error reporting, 404-before-stream |
| `test_iter20_vector_sources.py` | 9 | `search_with_sources` — citation metadata, content parity with `search()`, and every degradation path returning `[]` |

Three of these encode decisions that are easy to get wrong later:

- **Pipeline parity.** `_prepare_turn` builds prompt, RAG and history for
  *both* transports. `test_both_transports_build_the_same_llm_messages`
  asserts the two produce identical `llm_messages`, so a future change to
  one path cannot silently diverge from the other.
- **Degraded providers.** `fabric_call_stream` yields one buffered chunk on
  Anthropic-native and Factory. `test_non_streaming_provider_yields_one_chunk`
  pins that the route still emits a valid stream — the feature works on
  every provider, it just stops being progressive.
- **Session parity.** The buffered path gets turn-appending free from
  `fabric_call_with_session`; the streaming path bypasses that wrapper.
  `test_session_mode_appends_both_turns` catches the regression where every
  streamed exchange would vanish from session memory.

A defect this work also closed: the SRS auto-trigger used to swallow every
failure into `srs_triggered = False`, so a user who asked for an SRS and got
nothing had no way to find out why (it was on the P2 backlog). It now returns
`srs_error`, logs server-side, and the UI raises a toast.

**Not covered:** a live end-to-end token stream against a real model. The
wire contract, persistence, session appends and degradation are all pinned
against a stubbed `fabric_call_stream`; the first real stream is still worth
watching once.

---

## Live model verification

All 12 JSON-parsed agents, each through its **real seeded prompt** and its
**real parser**, against your Azure account:

| Agent | Tier | Model | Parsed |
|---|---|---|---|
| `tools.transformer.super_agent` | low | gpt-4.1-mini | ✅ |
| `tools.transformer.tester` | low | gpt-4.1-mini | ✅ |
| `tools.transformer.context_manager` | medium | gpt-4.1 | ✅ |
| `tools.transformer.validator` | medium | gpt-4.1 | ✅ |
| `tools.transformer.diagnostician` | reasoning | o4-mini | ✅ |
| `tools.transformer.planner` | high | gpt-5 | ✅ |
| `tools.transformer.verifier` | high | gpt-5 | ✅ |
| `tools.transformer.pattern` | high | gpt-5 | ✅ |
| `tools.transformer.devops_expert` | critical | gpt-5.1 | ✅ |
| `tools.transformer.devops_audit` | critical | gpt-5.1 | ✅ |
| `codegen.planner` | high | gpt-5 | ✅ *(was empty — defect 1)* |
| `codegen.tester` / `reviewer` / `verifier` / `context_manager` / `traceability_gate` | mixed | mixed | ✅ |

- **10 / 10** Azure chat deployments reachable across tiers + 429 siblings
- JSON mode confirmed on o4-mini, gpt-5.1, gpt-5, gpt-4.1
- Console **Test Connection**: Azure `ok:true`, `model_used: gpt-5.1`

---

## Frontend

**Now traced.** The previous issue said "there is no JS test runner in this
repo, so I will not assert per-function status I did not measure." There is
one now: Jest 27 (shipped with react-scripts) + React Testing Library 16,
wired through CRACO with the `@/` alias and a `react-router-dom` resolution
map that Jest 27 needs because that package's `main` points at a file it
does not ship.

```
yarn test:ci          # 221 tests, 12 suites
yarn test:coverage    # same, with coverage
yarn typecheck        # tsc --noEmit, 0 errors
```

Three toolchain snags worth recording, because each would stop the suite
running on a fresh checkout:

- **`react-router-dom` 7 will not resolve under Jest 27.** Its
  `package.json` `main` points at `dist/main.js`, which the package does
  not ship; Node is rescued by the `exports` map, which Jest 27 does not
  read. `craco.config.js` maps `react-router-dom`, `react-router` and
  `react-router/dom` to the CJS builds they actually ship.
- **TypeScript had to be pinned to 5.6.3.** `yarn add -D typescript`
  resolved 7.0.2, which CRA 5's `fork-ts-checker` cannot use.
- **`jsconfig.json` was removed.** react-scripts refuses to start with both
  it and a `tsconfig.json`; tsconfig supplies the same `@/*` paths and the
  same `include`, plus `allowJs`.

### Suites

| Suite | Tests | What it pins |
|---|---:|---|
| `__tests__/design-system.test.js` | 19 | Codebase-wide token, type-scale, a11y and bundle invariants. Asserts on **source**, so a regression fails the moment it is written. |
| `lib/__tests__/streamMessage.test.js` | 15 | The SSE wire contract for `POST /api/chat/stream` — split frames, degraded providers, aborts, malformed data. |
| `hooks/__tests__/hooks.test.jsx` | 34 | `useBreakpoint` (matchMedia), `usePolling` (hidden-tab pause), `useJobProgress` (elapsed, phase labels). |
| `components/ui/__tests__/button.test.jsx` | 16 | Loading-as-variant, `aria-busy`, WCAG 2.5.8 target sizes, token-only colours. |
| `components/ui/__tests__/feedback.test.jsx` | 28 | Skeletons, `JobProgress`, `ThinkingDots`, `AgentTimeline` — every live region. |
| `components/ui/__tests__/status.test.jsx` | 21 | The three-channel status vocabulary (icon + text + colour) and `ProgressBar` semantics. |
| `components/ui/__tests__/form.test.jsx` | 19 | `Field` label/aria wiring, error announcement, focus-moving error summary. |
| `components/__tests__/StageProgress.test.jsx` | 16 | The rewritten stepper: one tab stop per stage, no red-for-unreached, project-type and transformer-phase behaviour. |
| `components/ux/__tests__/Cards.test.jsx` | 26 | `MetricCard` keyboard path, `StepCard` blocked-state reason, `EmptyState` action. |
| `pages/__tests__/DiscoveryV2.test.jsx` | 14 | The **P0-1 regression guard** — see below. |
| `hooks/__tests__/useAutoSaveTracker.test.jsx` | 12 | The hook extracted to get recharts off the critical path. |
| `lib/__tests__/routes.test.js` | 11 | Route-chunk registry, prefetch idempotence, `cn()` merge order. |
| **Total** | **221** | |

### The P0-1 guard is verified, not assumed

`DiscoveryV2` called `kbStatus(active.id)` while importing only `skipStage`.
The call resolved to the `useState` variable of the same name, threw
`TypeError: kbStatus is not a function`, and an empty `catch (_) {}` hid it
— so every metric tile on the landing page read `0` and `kbReady` never
became true, gating the Generate-SRS step behind a card that looked
pressable and did nothing.

I reintroduced the original defect and re-ran the suite: **6 of the 14
Discovery tests fail**, including *"calls the API function, not the state
variable of the same name"*. Restored, all 14 pass. The guard works.

The same bug class is now caught a second way, at compile time:
`src/lib/api.d.ts` declares all 228 exports of `lib/api.js`, so importing a
name that does not exist is a `tsc` error (TS2305).

### Coverage — modules authored in this work

| Module | Stmts | Branch | Funcs |
|---|---:|---:|---:|
| `ui/button.jsx` | 100% | 100% | 100% |
| `ui/status.jsx` | 100% | 89% | 100% |
| `ui/skeleton.jsx` | 100% | 50% | 100% |
| `ui/job-progress.jsx` | 100% | 83% | 100% |
| `ui/form.jsx` | 100% | 92% | 100% |
| `hooks/useAutoSaveTracker.ts` | 100% | 95% | 100% |
| `hooks/useJobProgress.ts` | 100% | 94% | 100% |
| `hooks/usePolling.ts` | 95% | 86% | 100% |
| `StageProgress.jsx` | 93% | 84% | 83% |
| `ux/Cards.jsx` | 92% | 97% | 80% |
| `hooks/useBreakpoint.ts` | 75% | 83% | 54% |
| **All authored modules** | **90%** | **89%** | **76%** |

The 60-odd pre-existing page components are **not** covered by unit tests.
Their behaviour is asserted indirectly by the source-level contract suite,
and by lint, typecheck and build. Stated plainly so the number is not read
as application-wide coverage.

### Defects the contract suite found and fixed

Writing the invariants surfaced real issues that lint had not:

| # | Finding | Count | Resolution |
|---|---|---:|---|
| 1 | `onClick` on a `div`/`span`/`li` with no role — no keyboard path | 32 | Classified and fixed by kind: 16 modal scrims → `aria-hidden`; 11 event-containment wrappers → `role="presentation"`; 5 genuine controls (3 file dropzones, a tree row, a Regenerate action) → real keyboard operation |
| 2 | Empty `catch {}` swallowing a failure silently — the class that hid P0-1 | 39 | Every one annotated with why swallowing is safe (localStorage unavailable / one failed poll tick / best-effort enrichment). A reviewer can now judge each. |
| 3 | `<span role="button">` performing a real action | 1 | `AccuracyReport` Regenerate is now a real `<button>` with an accessible name |
| 4 | `role="button"` on a Radix tooltip trigger that performs no action | 1 | `HelpIcon` — role removed; it stays focusable, but no longer announces a control that does nothing |
| 5 | `border-white`, missed by the palette codemod | 1 | `ModernAccordion` → `border-surface`; a dead commented-out dot in `TopToolbar` removed |

### Static gates

| Check | Result |
|---|---|
| Pages | 17 |
| `lib/api.js` exports | 228 (all declared in `api.d.ts`) |
| `yarn test:ci` | ✅ **221 passed / 12 suites** |
| `yarn typecheck` (`tsc --noEmit`) | ✅ 0 errors |
| `yarn lint` | ✅ 0 errors (19 warnings, down from 44) |
| `yarn build` | ✅ succeeds |
| First-paint JS | **315 KB gzip** (was 769 KB — 59% smaller) |
| Hardcoded hex literals | **0** (was 2,449) |
| Fixed-palette greys | **0** (was ~1,900) |
| Type below 12px | **0** (was 1,179) |
| `console.log` in shipped code | **0** |
| Empty `catch {}` | **0** |
| `data-testid` contract | 710 (0 removed from the 691 baseline) |

---

## Gate summary

| Gate | Result |
|---|---|
| `pytest backend/tests/` | ✅ **1,055 passed** / 129 skipped |
| `ruff check backend` | ✅ clean |
| `pyflakes` (changed files) | ✅ clean |
| `import server` | ✅ 313 routes (+1: `POST /api/chat/stream`) |
| Live boot | ✅ 0 tracebacks, 0 ERROR lines |
| 85 live GET routes | ✅ 0 responses ≥ 500 |
| `yarn test:ci` | ✅ **221 passed** / 12 suites |
| `yarn typecheck` | ✅ 0 errors |
| `yarn lint` | ✅ 0 errors (19 warnings) |
| `yarn build` | ✅ succeeds · 315 KB gzip first paint |

---

## What this report does not prove

Stated plainly so the numbers are not read as more than they are:

1. **The 166 mutating handlers are unverified here.** Proving them means
   running a real migration — creating a project, scanning a legacy tree,
   freezing five stages, generating code. That is a pipeline run, not an
   audit, and it writes to your live database.
2. **🟡 REACHABLE is inference, not proof.** It means "called from code
   that runs and nothing contradicts it", not "I watched it work".
3. **Execution ≠ correctness.** A traced function ran without raising. For
   the LLM agents I checked output *shape* against the real parsers; I did
   not grade answer quality beyond the planted-defect tests.
4. **Frontend coverage is 90% of the modules authored in the
   modernisation, not of the application.** The 60-odd pre-existing page
   components have no unit tests. They are covered indirectly — by the
   source-level contract suite, lint, typecheck and build — which is
   weaker than execution. `Transformer.jsx` (5,850 lines) and
   `GapAnalyzer.jsx` (2,192) are the largest untested surfaces.
5. **No end-to-end test exists.** Nothing here drives a browser through a
   real migration. Backend routes and frontend units are each verified in
   isolation; the seam between them is verified only by the two suites
   that pin the same SSE contract from both sides.

The honest one-line summary: **nothing in this application is known to be
broken, 595 backend functions are proven to run, 221 frontend tests now
cover the UI where there were none, and the dead code is gone.**

---

## Corrections to the first issue

This report claimed accuracy, so its own errors belong in it.

| # | First issue said | Actually |
|---|---|---|
| 1 | "**14 orphans**", in five places | **15** at that point. The headline disagreed with its own table, which listed 15 rows. An arithmetic slip reading the subsystem breakdown. |
| 2 | The orphan list was complete | It **missed three**: `architecture.py::_safe_llm_call`, `architecture.py::_should_abort_for_transport`, `hf_confidence.py::preload`. My scanner walked *nested* functions with `ast.walk`, which diluted the reference counts; restricting it to top-level defs surfaced them. True total: **18**. |
| 3 | `Core (top-level)` had 6 orphans | **7** — `pipeline.py` (5) + `factory_orchestrator.py` (2). |

Both mistakes came from the same habit: trusting a quick count instead of
a precise one. The same habit produced the `_chunk` miss recorded as
defect 3 above, where `grep -c "_chunk"` matched the unrelated `kb_chunks`
import. The final sweep used AST reference analysis — resolving `Name`,
`Attribute`, `ImportFrom` **and string constants**, so `getattr`-style
dispatch counts as a use — over top-level definitions only.

Verified after removal: **0 orphans of 1,524 functions.**

## Deep test — final state

Re-run in full after every removal above.

| Gate | Result |
|---|---|
| `pytest backend/tests/` | ✅ **1,055 passed** / 129 skipped |
| `yarn test:ci` | ✅ **221 passed** / 12 suites |
| `ruff check backend` | ✅ clean |
| `pyflakes` (changed files) | ✅ clean |
| `import server` | ✅ 313 routes |
| Live boot | ✅ 0 tracebacks, 0 ERROR lines |
| Test-execution trace | ✅ 663 functions observed |
| 31 parameterless GET routes | ✅ 0 responses ≥ 500 |
| 54 project-scoped GET routes | ✅ 0 responses ≥ 500 |
| Orphan scan (AST) | ✅ **0 of 1,524** |
| `yarn typecheck` (`tsc --noEmit`) | ✅ 0 errors |
| `yarn lint` | ✅ 0 errors |
| `yarn build` | ✅ succeeds |
| P0-1 regression guard | ✅ verified — fails on the reintroduced bug |

**Combined: 1,276 automated tests across both halves of the application,
all passing.**
