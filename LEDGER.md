# LEDGER

State lives here, not in agent context. Resume with *"Continue per LEDGER.md."*

`status ∈ { todo, in_progress, done, blocked, blocked(dep) }`

**Baseline (docs/RECON.md §0.7): 92 failed, 632 passed, 46 errors.**
Every task's bar is *no new failures, no new errors* against that.

Standing verification appended to every task's own `verify`:

```bash
.venv/bin/python -m pytest backend/tests/ -q -p no:randomly --ignore=backend/tests/test_console.py
.venv/bin/python -c "import sys; sys.path.insert(0,'backend'); import server; print(len(server.app.routes),'routes')"
```

---

## Phase 0 — Reconnaissance

| id | phase | task | deps | status | attempts | verify | evidence | notes |
|----|-------|------|------|--------|----------|--------|----------|-------|
| 0.1 | 0 | Inventory: file list, line counts, responsibilities | — | done | 0 | `wc -l` sweep | 116,632 LOC backend across 160 files; 43,852 frontend. Largest: codegen.py 10,878 / tools.py 10,551 / srs.py 8,721 / seed.py 8,614 | In docs/RECON.md |
| 0.2 | 0 | Reachability: ruff + vulture + registry string sweep | 0.1 | done | 0 | `ruff check backend --select F,ARG`; `vulture --min-confidence 80`; `scratchpad/reach.py` | 88 backend ruff findings (37 F401, 24 ARG001, 15 F841, 10 F541, 1 F811, 1 F821); 7 vulture; 68 prompt keys → 1 unreachable; 52 tier keys → 0; 137 env vars → 1 genuinely dead | Raw in docs/recon/ |
| 0.3 | 0 | Classify every UNREACHABLE item | 0.2 | done | 0 | manual audit, each verified by grep | 4 live defects (D1–D4), 2 registered-unused → decisions, 62 dead(safe), 24 unknown(investigate), rest flag-gated/fallback/error-path | docs/RECON.md §Candidate list |
| 0.4 | 0 | Author `docs/LAMA_STORY.md` from the code | 0.3 | todo | 0 | `grep -c "(corrected in Phase 0)" docs/LAMA_STORY.md` | | File does not exist in repo. Author from code, cross-check vs PRD.md + ARCHITECTURE.md |
| 0.5 | 0 | Split proposal incl. monkeypatch seam map | 0.2 | done | 0 | seam grep over `backend/tests/*.py` | codegen.py: 4 files / 10 names, all collections. tools.py: 15 files / 24 names, mostly orchestration | docs/RECON.md §monkeypatch seam map; DEC-3 |
| 0.6 | 0 | Generate LEDGER.md | 0.3,0.5 | done | 0 | this file exists | | |
| 0.7 | 0 | Capture red test baseline to a committed file | — | done | 0 | see header | 92 failed / 632 passed / 46 errors / 33.15s | docs/recon/pytest-baseline.txt |

---

## Phase 1 — Quality track  ·  branch `feat/llm-quality`

Ordered leaves-first. 1.0 and 1.5 are independent of everything.

| id | phase | task | deps | status | attempts | verify | evidence | notes |
|----|-------|------|------|--------|----------|--------|----------|-------|
| 1.0 | 1 | **D2** Fix `_todo_hits` NameError on the structural-recovery path | — | todo | 0 | `ruff check backend/routes/codegen.py --select F821` → 0; new `test_codegen_structural_recovery.py` | | codegen.py:3751 reads a name assigned at 3784. Message also wrong for its branch — should cite `missing` |
| 1.1 | 1 | **D1** Thread `response_format` + `top_p`/`seed`/`stop` through to the provider payload | — | todo | 0 | `pytest backend/tests/test_json_mode_routing.py -q` | | 3 forwarding sites in llm.py (534, 1586, 2010) + payload build in model_fabric.py:669 |
| 1.2 | 1 | Per-provider JSON-mode mapping `_json_mode_payload(ptype, payload)` | 1.1 | todo | 0 | same suite, one case per provider_type | | openai/azure/groq/openrouter → `response_format`; ollama → top-level `"format":"json"`; anthropic → prefill |
| 1.3 | 1 | Add `azure` + `gemini` to `PROVIDER_PRESETS` | — | todo | 0 | `pytest backend/tests/test_provider_azure.py -q` | | azure needs deployment-in-path + `api-version` query + `api-key` header |
| 1.4 | 1 | Widen `resolve_model()` to a 4-tuple carrying `request_params` | 1.3 | todo | 0 | 4 unpack sites updated; suite green | | fabric_chat, fabric_chat_stream, console.py:188, console.py:334 |
| 1.5 | 1 | **D4** Remove dead `rebuildKbGraph` / `getKbGraph` | — | todo | 0 | `grep -rn "rebuildKbGraph\|getKbGraph" frontend/src` → 0; `yarn build` | | Evidence = route grep + caller grep in commit body |
| 1.6 | 1 | **D3** Implement the `LAMA_DISABLE_OPENROUTER_FALLBACK` read | — | todo | 0 | `pytest backend/tests/test_openrouter_killswitch.py -q` | | Documented 6×, defaulted to 1 in compose, read nowhere |
| 1.7 | 1 | Fix `detect_provider_from_key` returning non-existent `"google"` preset | 1.3 | todo | 0 | unit case: `AIza…` → a preset that exists | | model_fabric.py:266 returns `"google"`; no such key in PROVIDER_PRESETS |
| 1.8 | 1 | `model_capabilities()` resolver | 1.2 | todo | 0 | unit table per provider × model | | `{json_mode, tool_use, max_context, tier_floor}` |
| 1.9 | 1 | One-shot schema-repair retry when JSON mode is unavailable | 1.8 | todo | 0 | suite: malformed → repaired → parsed, exactly one retry | | Bounded at 1 so spend stays capped |
| 1.10 | 1 | Shared `backend/quality/` — promote placeholder + structural guards | 1.0 | todo | 0 | both pipelines' suites green | | Shared *validator*, not shared pipeline. Callers stay separate |
| 1.11 | 1 | Minimum-tier floor warning for structural agents | 1.8 | todo | 0 | warning row lands in `llm_traces` | | Never silently downgrade without telling the operator |
| 1.12 | 1 | Prompt hardening: 7 JSON-returning agents | 1.2 | todo | 0 | parse-success rate before/after on a fixture run | | codegen.{verifier,reviewer,tester,traceability_gate,planner}, tools.transformer.{verifier,planner} |
| 1.13 | 1 | Console UI: provider-type dropdown gains azure + gemini | 1.3 | todo | 0 | `yarn build`; manual add-provider round-trip | | Console.jsx:1105 |
| 1.14 | 1 | Wire real Azure credentials into `backend/.env`, verify live | 1.4,1.13 | todo | 0 | `POST /api/console/providers/{id}/test` → `ok:true`; `/api/health/providers` chooses azure | | Keys go to gitignored .env only. See DEC-5 |
| 1.15 | 1 | `docs/quality/OLLAMA_BASELINE.md` — local-model accuracy before/after | 1.9,1.12 | todo | 0 | fixture run against `qwen2.5-coder:7b`, both configurations | | Ollama is UP locally with qwen2.5-coder:7b + qwen3:4b |

---

## Phase 2 — Zero-waste refactor  ·  branch `refactor/zero-waste`

| id | phase | task | deps | status | attempts | verify | evidence | notes |
|----|-------|------|------|--------|----------|--------|----------|-------|
| 2.1 | 2 | Characterization tests for every uncovered path about to be touched | 1.* | todo | 0 | new suites green before any edit | | Non-negotiable: pin behaviour first |
| 2.2 | 2 | Remove 37 × F401 unused imports | 2.1 | todo | 0 | `ruff check backend --select F401` → 0 | | Each removal grep-evidenced in the commit body |
| 2.3 | 2 | Remove 15 × F841 unused variables | 2.1 | todo | 0 | `ruff check backend --select F841` → 0 | | Read each first — one may mask a dropped assignment |
| 2.4 | 2 | Fix 10 × F541 f-strings with no placeholder | 2.1 | todo | 0 | `ruff check backend --select F541` → 0 | | |
| 2.5 | 2 | Audit 24 × ARG001 individually | 2.1 | todo | 0 | whitelist file has one reason per survivor | | Many are FastAPI DI or monkeypatch signatures — keep |
| 2.6 | 2 | Whitelist file for justified survivors | 2.2–2.5 | todo | 0 | every entry has exactly one line of reason | | Registries, flags, fallbacks, error paths |
| 2.7 | 2 | Stage A split: `routes/codegen.py` pure helpers → `backend/codegen/` | 2.1 | todo | 0 | suite green; import paths unchanged | | 6 new modules; no name any test patches |
| 2.8 | 2 | Stage A split: `routes/tools.py` pure helpers → `backend/tools_lib/` | 2.1 | todo | 0 | suite green; import paths unchanged | | 5 new modules; no name any test patches |
| 2.9 | 2 | Comment hygiene: delete comments describing removed code | 2.7,2.8 | todo | 0 | manual diff re-read | | Comments must describe what the code does *now* |
| 2.10 | 2 | Smoke: multi-agent CodeGen `idle → completed`, both gates, PATCH honoured, no start from `executing` | 2.7 | todo | 0 | scripted API drive against a fixture project | | Mongo is UP locally |
| 2.11 | 2 | Smoke: Transformer with toolchain present, and with binary off PATH | 2.8 | todo | 0 | expect real compiler output, then `compilation_ready:false` | | mvn/gradle/javac/node all present |
| 2.12 | 2 | Stage B orchestrator split | DEC-3 | blocked(dep) | 0 | — | | Awaiting DEC-3. Stage A lands regardless |

---

## Phase 3 — Codebase Map  ·  branch `feat/codebase-map`

| id | phase | task | deps | status | attempts | verify | evidence | notes |
|----|-------|------|------|--------|----------|--------|----------|-------|
| 3.1 | 3 | Add `line` to extractor entity output (5 languages) | 2.* | todo | 0 | golden fixture asserts line numbers | | No extractor records line today |
| 3.2 | 3 | Add EXTENDS/IMPLEMENTS/OVERRIDES/INJECTS to `_VALID_EDGE_TYPES` + lift in `build_kb_graph` | 3.1 | todo | 0 | golden edge set matches **exactly** on both fixtures | | Data already exists in kb_entities; graph drops it |
| 3.3 | 3 | Author `tests/fixtures/helidon-sample/` + `springboot-sample/` + golden edge sets | — | todo | 0 | fixtures parse; golden sets committed | | Neither exists |
| 3.4 | 3 | tree-sitter call-edge extractor (Java + JS/TS) | 3.3 | todo | 0 | ≥95% precision on an audited 100-edge sample | | Only for CALLS. Unresolved → `external`, never guessed |
| 3.5 | 3 | Source↔Target view from existing traceability data | 3.2 | todo | 0 | envelope→task→target_path mapping renders | | Reuse codegen_envelopes/codegen_tasks; do not re-infer |
| 3.6 | 3 | Backend routes `/api/codebase-map/{pid}/*` | 3.2,3.4 | todo | 0 | registered in server.py; added to lib/api.js | | New router; scope by tenant |
| 3.7 | 3 | `elkjs` layout, one geometry for screen + PNG + drawio | 3.6 | todo | 0 | layout test: zero overlaps, bendpoints present | | `yarn add elkjs` — never npm |
| 3.8 | 3 | `.drawio` mxGraph XML writer with ELK geometry | 3.7 | todo | 0 | round-trip: counts + coords within 1px | | Native mxCell, individually editable |
| 3.9 | 3 | Mermaid emitter reusing `sanitizeMermaid` | 3.7 | todo | 0 | `mermaid-cli` parses without error | | Reuse Architecture.jsx's sanitiser, do not write a third |
| 3.10 | 3 | PNG/SVG rasteriser at 2× | 3.7 | todo | 0 | PNG dims == viewBox × 2 | | Embedded fonts, opaque background |
| 3.11 | 3 | `CodebaseMap.jsx` page + route + sidebar + palette | 3.6 | todo | 0 | `yarn build`; data-testids stable | | Reuse ERDiagram.jsx zoom/pan patterns |
| 3.12 | 3 | Collapse-by-default above ~150 visible nodes | 3.11 | todo | 0 | largest fixture stays usable | | Never render a hairball |
| 3.13 | 3 | Playwright E2E: 4 views, drill-down, 4 downloads | 3.11 | todo | 0 | `yarn playwright test codebase-map` | | Playwright not installed — setup is part of this row |
| 3.14 | 3 | `docs/codebase-map/ACCURACY.md` | 3.4 | todo | 0 | precision + recall per edge type, both fixtures | | |

---

## Final

| id | phase | task | deps | status | attempts | verify | evidence | notes |
|----|-------|------|------|--------|----------|--------|----------|-------|
| F.1 | — | `FINAL_REPORT.md` | all | todo | 0 | every row `done` or `blocked` | | Lines removed / kept-with-reason / added; test count before+after; open decisions; reproduction commands |
