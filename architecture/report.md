# Verification Report

**Author:** Nikunj
**Date:** 2026-09-14 · **diagrams refreshed 2026-09-15**
**Repository revision:** `43af5b8cd68735e67dac9d1e2b0f681ed6f03f73` (branch `refactor/zero-waste`)
**Previously verified at:** `30b8ef0845b0545a1c890524b0d47614914cdce8` (branch `main`)
**Archify version:** 2.17 (`tt-a1i/archify`, MIT), installed at `.claude/skills/archify`
**Node:** v26.8.2

This report covers two audits:

- **Part A** — the Archify setup guide's factual claims, checked against Archify v2.17's own source.
- **Part B** — the five generated diagrams, checked against LAMA's actual code with `file:line` evidence.

Part C records documentation drift found along the way. Part D records the acceptance evidence.

---

## Part A — Setup guide vs. Archify v2.17

The source guide was written by `woke_engineer`. Its rewritten counterpart in this folder
([`README.md`](README.md)) is authored by **Nikunj**. Archify itself remains credited to `tt-a1i`
under MIT; the vendored skill is unmodified, so its `author: tt-a1i` metadata was deliberately
left alone — rewriting it would misattribute third-party work.

Eleven checkable claims. **6 accurate, 3 incomplete, 1 wrong, 1 misleading.**

| # | Guide claim | Verdict | Evidence | Correction |
|---|---|---|---|---|
| A1 | Visual presets are `default`, `classic`, `signal-flow`, `blueprint` | **WRONG** | `schemas/common.schema.json` → `visualPreset` enum is `["classic","signal-flow","blueprint","editorial"]` | There is **no `default` preset**. `classic` *is* the default (reached by omitting `meta.visual_preset`). The guide also **omits `editorial`**. |
| A2 | Exports: PNG, SVG, WebM, 1200×630 card | **INCOMPLETE** | `references/viewer-runtime.md`, "Canonical exports" | Also **JPEG** and **WebP**; the SVG is **dual-theme**; there are **three** card variants — Share Card, **Route** Share Card, **Reach** Share Card. |
| A3 | Workflow is `validate` → open the HTML | **INCOMPLETE** | `SKILL.md` §"Delivery" | Omits **`deliver`**, which `SKILL.md` names *the* final acceptance command (freezes spec bytes, renders, atomically commits, reports SHA-256), and **`visual-check`** for browser evidence. |
| A4 | `validate <type> <file> --json` | **MISLEADING** | `SKILL.md` step 4 | Without `--quality showcase` you get **4 basic checks, not the 9** required for showcase acceptance. `SKILL.md`: *"A receipt with only 4 artifact checks is basic validation, never showcase acceptance."* |
| A5 | Install via `npx skills add tt-a1i/archify -g` | **ACCURATE** | Ran it; skill CLI v1.5.26 | Verified. For project scope, drop `-g`. The agent id is `claude-code`, not `claude` — see A6. |
| A6 | Project scope is `.claude/skills/`, global is `~/.claude/skills/` | **ACCURATE** | Installed to `.claude/skills/archify` | Verified. Caveat the guide omits: `-a claude` is **rejected**; the valid identifier is `claude-code`. |
| A7 | `cd archify && node bin/archify.mjs doctor` verifies the install | **ACCURATE** | Exit 0, 15 `[ok]` checks, `Archify is ready.` | Verified. |
| A8 | Update check ~every 72 h; disable with `ARCHIFY_UPDATE_CHECK_DISABLED=1` | **ACCURATE** | `scripts/check-update.mjs:28` → `CHECK_TTL_MS = 72 * 60 * 60 * 1_000`; env var present | Verified exactly. |
| A9 | Keyboard table: `?` `/` `R` `L` `M` `P` `[` `]` `F` `S` `T` `E` `+` `-` `0` | **ACCURATE** | All 15 bindings found in `diagrams/lama-runtime.html` | Verified against real generated output. `t: 'theme'`, `s: 'preset'`, `e: 'export'`; `]`/`[` step chapters; `p` calls `togglePlayback()`. |
| A10 | Five diagram types | **ACCURATE** | `schemas/` + `doctor` output | `architecture`, `workflow`, `sequence`, `dataflow`, `lifecycle`. |
| A11 | Output is a single self-contained HTML file, no external deps | **ACCURATE** | 788–794 KB per file, font/CSS/runtime inlined | Verified. The size is a consequence of self-containment, not a defect. |

### A note on A9

This claim was nearly recorded as a false finding. A first pass matched only `key === 'p'` and
found nothing, suggesting the guide had invented the `P` binding. It is in fact bound through
`event.key.toLowerCase() === 'p'`. **The guide's keyboard table is completely accurate** — the
initial search was at fault, not the guide.

### Claims that could not be verified

- **"Plugs into Cursor, Codex CLI, and OpenCode."** All three appear in the `skills` CLI's valid-agent
  list, but only the `claude-code` install was exercised here.
- **Perceptual visual quality.** See Part D — `visual-check` reports `visualReview: "pending"` by
  design; automated browser evidence is not a substitute for human review.

---

## Part B — Diagrams vs. LAMA's genuine architecture

Every node and edge below was checked against code at revision `30b8ef0`.
**Discrepancies: 0.** Deliberate simplifications are listed per diagram and are not defects — they
are the cost of a high-level, ≤12-node view.

### B1 · `lama-runtime` (architecture)

| Element | Claim | Verified at |
|---|---|---|
| `nginx` | edge on :8382, the only public port | `docker/nginx.conf:24` `listen 8382 default_server` |
| `nginx → api` | `/api/` proxied to :8001 | `docker/nginx.conf:30` `proxy_pass http://127.0.0.1:8001` |
| `api` | FastAPI, **21 router mounts** | `backend/server.py` — 21 `api_router.include_router` calls |
| container boundary | supervisord runs mongod, uvicorn, nginx | `docker/supervisord.conf:24,37,56` — exactly 3 `[program:]` blocks |
| `mongo` | MongoDB, **59 collections**, system of record | `backend/db.py` — 59 `<name> = db.<name>` accessors; client at `db.py:6` |
| `qdrant` (dashed, "optional") | off unless `QDRANT_URL` **or** `QDRANT_PATH` is set | `backend/kb/vector_store.py:44-48` `return bool(QDRANT_URL or QDRANT_PATH)` |
| `fabric` | "in-process router" | `backend/llm.py:1043` `_fabric_call_impl` — a module in the uvicorn process, **not** a separate service |
| `providers` | "8 provider presets" | `backend/fabric/model_fabric.py` → `PROVIDER_PRESETS` has 8 keys: `openrouter, anthropic, openai, azure, gemini, groq, ollama, custom`. Azure and Gemini were added after this diagram was first authored; the label said "5 vendor presets" until 2026-09. |
| `factory` / `droid` | two Factory transports | `factory_orchestrator.py:22` (`FACTORY_API_BASE`); `fabric/factory_cli.py:667` (`route_via_factory_cli`) |
| `github` | PyGithub | `backend/routes/github.py:289` |

**Deliberate simplifications.** 21 router mounts collapse into one `FastAPI` node. The eight provider
presets collapse into one `LLM Providers` node (named individually on the violet card). `LLM Fabric`
is drawn as a distinct box for legibility although it is in-process with the API — the sublabel
says "in-process router" so the diagram does not imply a separate service.

### B2 · `lama-pipeline` (workflow)

| Element | Claim | Verified at |
|---|---|---|
| five stages, strict order | Discovery → DataModel → Architecture → CodeGen → Living | `CLAUDE.md` stage table; enforced per B2 below |
| `blocked` behaviour | unfrozen upstream returns **HTTP 400** | `backend/pipeline.py:18-26` — `require_stage_context` raises `HTTPException(400, "... not frozen ...")` |
| `freeze → stagectx` | `save_stage_context()` writes one doc per `(project, stage)` | `backend/pipeline.py:75-105`, upsert on `{project_id, stage}` |
| "version counter increments" | auto-increment on save | `backend/pipeline.py:90` |
| `skipped` | only DataModel / Architecture / Living may be skipped | `backend/routes/pipeline.py` module docstring: *"Discovery and CodeGen are never skippable"* |
| `skipped → codegen` | a skipped stage writes a minimal StageContext so downstream gates pass | same docstring |

**Deliberate simplifications.** The freeze handoff is drawn **once**, for Discovery → DataModel;
the emerald card states it repeats at all four transitions. An `HTTP 400` node was authored and
then removed: its edge crossed the handoff lane and had no feasible route, so the fact moved to the
rose card instead — a content decision, not a silent drop. The "Sanctioned Bypass" lane was merged
into "Stage Handoff" to remove a lane and fix a 68px vertical overflow (Part D).

### B3 · `lama-fabric-call` (sequence)

| Element | Claim | Verified at |
|---|---|---|
| `wrapper` | `fabric_call()` is a **tracing wrapper only** | `backend/llm.py:986` — mints `trace_id`, delegates, records; no routing |
| `impl` | all routing lives here | `backend/llm.py:1043` `_fabric_call_impl` |
| `probe` | every call probes providers first, else hard-fails | `backend/llm.py:1071` then `:1078-1100` raising `RuntimeError` |
| probe order | Factory → Azure → Anthropic → OpenAI → Gemini → Groq → Ollama | `backend/llm.py:923` `_preferred_order = ["azure", "anthropic", "openai", "gemini", "groq", "ollama"]`. Ollama is last of the configured providers on purpose — free and local, so the right thing to fall back *to* and the wrong thing to prefer over a paid endpoint (`llm.py:918-922`). |
| `factory` | one Factory door from `llm.py` | `backend/llm.py:1406` `route_via_factory_orchestrator(...)` |
| API vs CLI split | happens **inside the orchestrator**, not in `llm.py` | `backend/factory_orchestrator.py:2049-2063` |
| `console` | failover across active providers | `backend/llm.py:1584-1592` → `fabric_chat_with_failover` |
| `traces` | trace written on success **and** failure | `backend/llm.py:1015`, `:1035`, failure path `:1033-1043` |

This diagram deliberately encodes the three facts a naive drawing gets wrong: the wrapper is not
the router, the health probe is a real runtime edge rather than diagnostics, and Factory-vs-Console
are alternatives rather than a chain (message labels read `if enabled:` / `else:`).

**Deliberate simplification.** The Factory participant merges the HTTP API and the `droid` CLI into
one lifeline, because from `llm.py` there genuinely is only one Factory door; the split is one
level down and is called out on the orange card.

### B4 · `lama-discovery-kb` (dataflow)

| Element | Claim | Verified at |
|---|---|---|
| `legacy` | folder · ZIP · git ingestion | `backend/routes/kb.py` (`scan-folder`, upload, `/clone-git`) |
| skip patterns | `node_modules`, `.git`, `vendor`, backups; order matters | `CLAUDE.md` contract 8 |
| `extract` | parsers + language-agnostic OWL extractor | `backend/kb/parsers.py`, `backend/kb/owl_extractor.py` |
| `kbcolls` | entities · chunks · TOON in Mongo | `backend/db.py` — `kb_files`, `kb_chunks`, `kb_entities`, `kb_toon` |
| `qdrant` (dashed) | optional; callers degrade to TOON-only | `backend/kb/vector_store.py:44-48`; `vector_store.py` docstring |
| `bundler` | one gather per stage, cached and version-keyed | `backend/context_bundler.py:375-400`; cache key `<project>:<stage>:<kb_version>:<srs_version>:<target_tech>` |
| "SRS fans out to 12 sections" | 12 section configs | `backend/routes/srs.py::SECTION_CONFIGS`; `context_bundler.py:1-39` rationale |

**Deliberate simplification.** `parsers.py` and `owl_extractor.py` are merged into a single
`KB Extraction` node. This was forced: the `dataflow` schema caps `stages` at **5**, and the merge
was preferred over an intra-stage flow. Both module names appear on the cyan card.

### B5 · `lama-codegen-multiagent` (lifecycle)

| Element | Claim | Verified at |
|---|---|---|
| state names | `idle`, `envelopes_*`, `tasks_*`, `executing`, `traceability_gate`, `completed`, `failed` | `backend/routes/codegen.py:7944-7952` — all nine constants confirmed verbatim |
| restart surface | only from idle / failed / completed | `backend/routes/codegen.py:7954-7958` `_CODEGEN_STARTABLE_STATUSES` |
| "deterministic router overrides the planner" | applied **after** the Planner runs | `backend/routes/codegen.py:8028-8029` docstring of `_route_task_to_coder` |
| "a frontend file never reaches the BE coder" | routing returns only `coder_be` or `coder_fe` | `backend/routes/codegen.py:8040-8053` |
| "Architecture must be frozen first" | gated | `require_stage_context(project_id, "Architecture", "CodeGen")` |
| BE/FE waves | tasks split by `assigned_to` | `backend/routes/codegen.py:10078-10081` |

**Deliberate simplifications.** `envelopes_pending`/`envelopes_confirmed` collapse into one
`Envelopes` state with the sublabel `pending → confirmed` (same for tasks) — the lifecycle main
rail allows only columns 0–4. The ten-agent chain
(`super_agent → context_manager → build_tool_selector → planner → coder_be|coder_fe → verifier →
reviewer → tester → traceability_gate → finalizer`) is **not** drawn; it lives inside the
`Executing` state and is summarised on the cyan card. A dedicated agent-chain diagram would be the
natural next addition.

---

## Part C — Documentation drift found while verifying

| Finding | Status |
|---|---|
| `CLAUDE.md`: "59 collections" | **Accurate** — 59 accessors in `backend/db.py` |
| `CLAUDE.md`: "mounts every /api router (**20 routers**)" | **Imprecise** — there are **21 mounts from 20 modules**; `backend/routes/datamodel.py` exports both `router` and `factory_router`. **Fixed 2026-09-15.** |
| Two `/kb` routers (`kb.py`, `db_ingest.py`) and two `/projects` routers (`projects.py`, `datamodel.py::factory_router`) share prefixes | **Real**, intentional — not a diagram error |
| `docs/ARCHITECTURE.md` self-declares iteration **14.27**; `memory/PRD.md` is at **17.18** | **Stale by ~3 major iterations.** Its own header already defers to `memory/PRD.md` on conflict |
| `CLAUDE.md`: Qdrant "auto-created on Build KB" | **Needs qualification** — the subsystem is inert unless `QDRANT_URL` or `QDRANT_PATH` is set (`vector_store.py:44-48`); `QDRANT_PATH` (embedded mode) was not documented in `CLAUDE.md`'s env block. **Fixed 2026-09-15** — the row is qualified and `QDRANT_PATH` was added to the env block. |

These were originally reported and not fixed, because `CLAUDE.md` and `memory/PRD.md` were
outside that exercise's scope.

**Updated 2026-09-15.** The two `CLAUDE.md` findings have since been fixed during a separate
maintenance pass on that file — the router count is now stated as "21 router mounts from 20 route
modules" with the `datamodel.py` reason inline, and the Qdrant row is qualified with the
`vector_store.py:44-48` condition plus a `QDRANT_PATH` entry in the env block.

Still open, and still deliberately not fixed here: `docs/ARCHITECTURE.md` self-declares iteration
**14.27** while `memory/PRD.md` is at **17.18**. That is a whole-document refresh, not a line edit,
and the file's own header already defers to `PRD.md` on conflict.

---

## Part D — Acceptance evidence

All five diagrams: **9/9 artifact checks, 0 composition errors, 0 warnings**, `deliver` exit 0,
`visual-check` pass. Receipts in [`evidence/`](evidence/).

| Diagram | Spec SHA-256 (first 16) | Artifact SHA-256 (first 16) | Bytes |
|---|---|---|---|
| `lama-runtime` | `33d7467dc7fb09d6` | `aa3c9416b65f84ec` | 812 195 |
| `lama-pipeline` | `d1b0e57465873abc` | `08935f7d17446228` | 810 345 |
| `lama-fabric-call` | `0c436d1fede6e39b` | `bfcf5a33dddc46c9` | 812 954 |
| `lama-discovery-kb` | `770de0d701ceb7e0` | `41387ebf96175156` | 806 664 |
| `lama-codegen-multiagent` | `266583ae2412678d` | `285161df56ce5bd3` | 807 793 |

Browser evidence was collected by headless Chrome at **1440×900, 1600×1000, 1920×1080 and
2048×1320, in both light and dark themes**. Containment passes at every size; no horizontal or
vertical overflow. Each `visual-check` receipt's `artifact.sha256` matches the corresponding
`deliver` receipt, confirming the delivered file was the one inspected.

### Three claims kept separate

Following Archify's own truth boundary:

1. **`deliver` proves deterministic artifact checks.** Passed, all five.
2. **`visual-check` proves bounded behaviour in a real browser.** Passed, all five.
3. **Perceptual visual review requires a human or image-capable reviewer.** **Performed by an
   image-capable reviewer, not a human.** All five rendered PNGs were inspected at 1440×900 in
   both themes. The receipts still record `visualReview: "pending"`, because that field tracks a
   review recorded *through the tool*, which was not done. Findings below. A human sign-off on
   visual quality has **not** taken place.

#### Perceptual review findings

| Diagram | Finding | Action |
|---|---|---|
| `lama-pipeline` | The workflow renderer's default legend labelled LAMA's stages with an agent-workflow vocabulary — "Discovery" appeared as *Agent logic*, "Skip Stage" as *Tool action*. Misleading in a document about accuracy. | **Fixed.** `meta.legend.entries` now relabels each kind to what it holds here (*Generation stage*, *Data artifact*, *Design stage*, *Runtime stage*, *Human gate*, *Bypass*). Re-delivered and re-checked. |
| `lama-codegen-multiagent` | The lifecycle renderer draws three fixed lane bands; band 2 (*Interruptions + recovery*) renders empty because this state machine has no waiting state between `executing` and its terminals. Leaves a visible vertical gap. | **Accepted.** Inventing a state to fill the band would fabricate architecture. Cosmetic only. |
| `lama-codegen-multiagent` | Cards carry two items each rather than three. | **Accepted.** Restoring the third item pushes the page to 903px against the 900px limit; two rounds of shortening did not recover the 3px, so the passing version was kept. The omitted facts are in Part B5 above. |
| `lama-runtime` | Some empty space upper-left of the diagram panel; the `droid exec` edge takes a long path around `Factory.ai`. | **Accepted.** Within tolerance; all clearance and rhythm checks pass. |
| `lama-fabric-call`, `lama-discovery-kb` | No issues. Legends accurate; the `if enabled:` / `else:` message labels make the two transports read as alternatives. | None needed. |

### Repairs made during authoring

Recorded because they explain why the specs look the way they do:

- **Source pins removed.** Archify's `sources` field requires `meta.repository` with a real
  HTTP(S)/SSH URL and a 40-char revision (`repository-evidence/repository-required`). This repo has
  **no git remote**, and inventing one would have fabricated evidence. All `file:line` citations
  live in Part B instead.
- **`lama-runtime`**: 6 errors → 0 over three rounds (column gutters widened; boundary label
  shortened; `labelDy`/`labelDx` applied as the validator suggested; the provider sublabel moved to
  a card after failing the 6px projected-font floor).
- **`lama-fabric-call`**: `column_fit: "spread"` applied per contract *before* shortening labels;
  viewBox narrowed 1120 → 1080 to lift projected font from 5.81px over the 6px floor. The full
  `SRS · Arch · CodeGen` sublabel was **restored** once measurement showed font size was fixed at
  7px and text length had never been the cause.
- **`lama-pipeline` / `lama-codegen-multiagent`**: both initially failed `visual-check` with
  vertical overflow (968px and 1229px against a 900px viewport). Repaired per Archify's stated
  order — compact spacing and remove redundant content before shrinking anything — by merging a
  lane, widening the viewBox aspect, pulling terminal states up with `yOffset`, and reducing card
  rows. No overflow was hidden with `overflow: hidden` or a clipped panel.

---

## Part D2 — 2026-09-15 refresh

The codebase changed after this report was first written, so the two affected
specs were corrected and all five diagrams re-delivered.

### Diagram claims that had gone stale

| Diagram | Was | Now | Evidence |
|---|---|---|---|
| `lama-runtime` | node sublabel "5 vendor presets"; card listed OpenRouter, Anthropic, OpenAI, Groq, Ollama | "8 provider presets"; card names all eight | `backend/fabric/model_fabric.py` → `PROVIDER_PRESETS` has 8 keys. Azure and Gemini were added after the original authoring |
| `lama-fabric-call` | "Probe order is Factory, Anthropic, OpenAI, Groq, then Ollama" | "Probe order is Factory, then Azure, Anthropic, OpenAI, Gemini, Groq, Ollama" | `backend/llm.py:923` `_preferred_order` |

The other three specs were not edited. Their HTML re-delivered **byte-identical**
(`lama-pipeline` and `lama-codegen-multiagent` artifact SHAs are unchanged in the
table above), which is the renderer behaving deterministically on unchanged input.

### A containment regression, caused and fixed here

Naming all eight presets on the `lama-runtime` Model routing card wrapped the
bullet to a third line and pushed the artifact **15 px past the 1440×900 fold**:

```
viewer/viewport-overflow (error)
  innerHeight 900, scrollHeight 915, overflowY true
```

`visual-check` caught it — the receipt went `ok: false`, `containment: fail` — and
it would have shipped unnoticed had the receipt not been re-read. This is exactly
the failure mode [`README.md`](README.md) §5 warns about ("Watch vertical
overflow… reducing card rows are the two effective levers").

Fixed by merging two bullets that already read as a pair —
"fabric_call() is a tracing wrapper only" and "Routing decisions live in
_fabric_call_impl" became "fabric_call() only traces; routing lives in
_fabric_call_impl" — which bought the line back without dropping a fact. The card
went from 5 items to 4. Re-checked: `scrollHeight` 900, containment `pass`,
0 diagnostics.

### Re-acceptance

All five: `validate` 9/9 showcase checks with 0 errors and 0 warnings, `deliver`
exit 0, `visual-check` `ok: true` with containment and readability `pass` and zero
error diagnostics. Both `deliver` and `visual-check` receipts in `evidence/` were
regenerated; every receipt's recorded spec and artifact SHA-256 was re-verified
against the files on disk.

The five `<name>.png` stills were deleted and regenerated. Each is 1440×900 with
corner pixels RGB (248, 250, 252), and all five were visually inspected rather
than accepted on file size alone.

---

## Part E — Repository impact

Nothing under `backend/` or `frontend/` was touched. The full footprint of this exercise:

| Path | Status | Size |
|---|---|---|
| `architecture/` | committed | 6.3 MB — 3.9 MB delivered HTML, 1.4 MB handbook PDF, 732 KB diagram renders, 204 KB specs/docs/receipts (measured 2026-09-15) |
| `.claude/skills/archify/` | new, **not** committed | 8.4 MB — excluded by the `.claude/` rule below |
| `.gitignore` | **modified by the `skills` CLI**, not by hand | +3 lines |
| `skills-lock.json` | new, untracked | pins `tt-a1i/archify` by content hash |

Two consequences worth a decision:

- **The `skills` CLI appended `.claude/` to `.gitignore`** during install. That is a change to a
  tracked file made by the installer as a side effect, not a deliberate edit. Its effect is that
  the vendored skill is **not** version-controlled, so a fresh clone must re-run the install before
  regenerating anything. The delivered HTML is self-contained and readable without any tooling.
  The rule it added was preceded by a comment carrying an absolute local path
  (`# /Users/nikunj/Desktop/lama/.claude`) and had landed in the *Secrets* block.
  **Tidied 2026-09-15:** the absolute path is gone and `.claude/` now sits under
  *Caches / scratch* with a comment explaining why the skill is not committed.
  Verified with `git check-ignore -v` that the ignore behaviour is unchanged.
- **Only one render per flow is retained.** `visual-check` writes four screenshots per diagram
  (1440×900 and 2048×1320, each light and dark) plus a contact sheet, and it also drops a receipt
  copy next to the artifact that is byte-identical to the one in `evidence/`. Twenty near-identical
  pictures of five structures is confusing rather than useful, so the 1440×900 light render is
  promoted to `diagrams/<name>.png` and the rest are removed — 3.2 MB down to 724 KB.
  `build-handbook.sh` re-applies this automatically, so re-running `visual-check` cannot
  reintroduce the duplicates. The full browser evidence for every viewport and theme is unchanged
  and still recorded in the `evidence/*.visual-check.json` receipts.

---

## Reproducing this report

```bash
node .claude/skills/archify/bin/archify.mjs doctor

S=.claude/skills/archify
node $S/bin/archify.mjs validate     architecture architecture/src/lama-runtime.architecture.json --quality showcase --json
node $S/bin/archify.mjs deliver      architecture architecture/src/lama-runtime.architecture.json architecture/diagrams/lama-runtime.html --quality showcase --json
node $S/bin/archify.mjs visual-check architecture/diagrams/lama-runtime.html --json
```

Spot-check any Part B citation directly, for example:

```bash
sed -n '7944,7958p' backend/routes/codegen.py    # the nine pipeline states
sed -n '44,48p'     backend/kb/vector_store.py   # the Qdrant enablement gate
grep -c "api_router.include_router" backend/server.py   # 21
grep -cE "^[a-z_]+ = db\." backend/db.py                # 59
```
