# RECON — Phase 0

Read-only reconnaissance of the LAMA repository, produced before any refactor or
feature work. Raw tool output lives in [`docs/recon/`](recon/).

Generated on branch `refactor/zero-waste` from `8c658c1`.

---

## 0.7 Baseline (measured, not estimated)

Every later claim of "no new failures" is measured against this.

| Metric | Value |
|---|---|
| Tests collected | 770 |
| Passed | 632 |
| Failed | 92 |
| Errors | 46 |
| Runtime | 33.15s |

```bash
.venv/bin/python -m pytest backend/tests/ -q -p no:randomly \
  --ignore=backend/tests/test_console.py
```

Full output: [`docs/recon/pytest-baseline.txt`](recon/pytest-baseline.txt).

`test_console.py` is excluded because it errors at **collection**, not at test time:
it reads `REACT_APP_BACKEND_URL` at module scope and calls `.rstrip()` on the `None`
that comes back. It is a live-server integration suite, as are `test_lama_v2.py`,
`test_lama_v4.py`, `test_migrationos.py` and `test_srs_streaming.py` — all of the 46
errors are `requests.exceptions.ConnectionError` from those suites expecting a server
on `REACT_APP_BACKEND_URL`. They are not unit tests and cannot pass without a running
stack.

**This baseline is red and stays red.** Fixing it is out of scope and is logged as a
decision. The working rule for every task is *zero new failures, zero new errors*.

### Tooling

| Tool | Status | Notes |
|---|---|---|
| `ruff` | present, 0.x system install | 88 backend findings, 252 test findings |
| `vulture` | **installed in Phase 0** (2.16) | 7 findings at `--min-confidence 80` |
| `pyflakes` | present in `.venv` (3.4.0) | the PRD's standing verification step |
| `mypy` | absent | no type checker is configured or enforced in this repo |
| `knip` / `tsc` | absent | frontend has no TS; ESLint is disabled at build time |
| `tree-sitter` | absent | needed for Phase 3 call edges only |
| `playwright` | absent | needed for Phase 3 E2E only |

There is **no project-wide formatter and no type checker**. `DISABLE_ESLINT_PLUGIN=true`
is set at build time on purpose. So "do not lower the configured strictness" means:
keep `ruff --select F,ARG` and `vulture --min-confidence 80` as the bar, and raise it
where cheap. It does not mean a type checker exists to satisfy.

---

## 0.2 Reachability results

### Symbol-level (ruff + vulture)

88 findings in `backend/` excluding tests:

| Rule | Count | Meaning |
|---|---|---|
| F401 | 37 | unused import |
| ARG001 | 24 | unused function argument |
| F841 | 15 | unused variable |
| F541 | 10 | f-string with no placeholder |
| F811 | 1 | redefined while unused |
| **F821** | **1** | **undefined name — a live defect, see below** |

Raw: [`ruff-backend.txt`](recon/ruff-backend.txt), [`ruff-tests.txt`](recon/ruff-tests.txt),
[`vulture.txt`](recon/vulture.txt).

### String-keyed registries

Symbol analysers cannot see string-keyed registries, so these were swept separately by
literal grep. Script and machine-readable output:
[`registry-reachability.json`](recon/registry-reachability.json).

| Registry | Keys | Unreachable |
|---|---|---|
| Seeded prompt keys (`seed.py`) | 68 | 1 — `test.jmeter.samplers` |
| `AGENT_COMPLEXITY` tiers | 52 | 0 |
| `LAMA_*` env vars | 137 | 1 — see below |

The first sweep flagged 29 env vars as unread. Each was verified by hand and 28 are
false positives in two classes: read through the `_f()` / `_i()` coercion helpers in
`confidence_langgraph.py`, or shell-protocol sentinels embedded in Factory CLI command
strings (`LAMA_MAT_CHUNK_`, `LAMA_WORKSPACE_READY`, `LAMA_EOF_`) which are not env vars
at all. Compose-only host-path vars (`LAMA_IMAGE`, `LAMA_DROID_BIN_HOST`,
`LAMA_EXPORT_HOST_DIR`, `LAMA_CORP_CA_HOST`, …) are correctly read by
`docker-compose.yml` and not by Python. All `flag-gated(keep)`.

---

## Defects found

Six. D1–D4 were found by reading the code. **D5 and D6 were found only by
running it** — driving a local model and a live Azure deployment through the real
fabric. Neither is visible to any static sweep, and D5 had been losing every
single verifier verdict.

### D1 — `response_format` never reaches any provider

**Severity: high. This is the largest single lever on output accuracy in the repo.**

Fifteen call sites pass `response_format={"type": "json_object"}` into `fabric_call`:

```
backend/routes/codegen.py   — verifier, reviewer, tester, traceability gate, finalizer
backend/routes/tools.py     — planner, verifier, tester, diagnosis
```

`fabric_call(**kwargs)` → `_fabric_call_impl(**kwargs)` → `fabric_chat(...)`. The
hand-off at [`llm.py:534-540`](../backend/llm.py#L534-L540) forwards exactly two
kwargs:

```python
result = await fabric_chat(
    ...
    max_tokens=kwargs.get("max_tokens", 0) or 0,
    temperature=kwargs.get("temperature", 0.3),
)
```

and `fabric_chat` builds its payload from a fixed four-key dict at
[`model_fabric.py:669`](../backend/fabric/model_fabric.py#L669). `response_format` is
dropped on the floor at all three forwarding sites (`llm.py` 534, 1586, 2010).

Every structural agent is asking for strict JSON, silently not getting it, and then
relying on `_extract_json_object` to scrape the outermost `{...}` out of whatever prose
the model returned. On a small local model that is precisely the failure mode that
produces a `verdict` of `REJECT` with `confidence: 0.0` for a perfectly good file.

Classification: `dead(safe)` for the kwarg as written — it reaches nothing. The fix is
to make it live, not to delete it.

### D2 — `_todo_hits` read before assignment

**Severity: high. A `NameError` on an error-recovery path.**

[`codegen.py:3751`](../backend/routes/codegen.py#L3751) formats `_todo_hits` into a
message. The name is not assigned until line 3784, in a *different* branch. The path
that reads it is the structural-validator final-attempt failure — reached exactly when
the LLM emitted structurally invalid code and the deterministic scaffold is the
recovery. So when CodeGen most needs to recover, it raises
`NameError: name '_todo_hits' is not defined` instead of returning the scaffold.

The message is also semantically wrong for its branch: this path fires on *missing
structural markers*, which are in the local `missing` list, not on TODO placeholders.

### D3 — `LAMA_DISABLE_OPENROUTER_FALLBACK` is a guardrail that does not exist

**Severity: medium. A cost control that silently does nothing.**

The variable is documented in six places and defaulted to `1`:

| Location | Claim |
|---|---|
| `CLAUDE.md:172, 274` | "disable that fallback with `LAMA_DISABLE_OPENROUTER_FALLBACK=1`" |
| `docker-compose.yml:183` | `${LAMA_DISABLE_OPENROUTER_FALLBACK:-1}` — on by default |
| `.env:18` | `LAMA_DISABLE_OPENROUTER_FALLBACK=1` |
| `docs/USER_MANUAL.md:695` | "Prevents silent expensive fallback…; default on" |
| `docs/ARCHITECTURE.md:3972` | "Kill-switch for silent env OpenRouter fallback" |
| `docs/RUNNING.md:211` | `=0 # allow the OpenRouter fallback` |

No Python file reads it. The only occurrence under `backend/` is a docstring in
`test_iter149_slim_cli_context.py`. `fabric_call`'s env-var OpenRouter retry is gated
only on `OPENROUTER_API_KEY` being non-empty
([`llm.py:1597`](../backend/llm.py#L1597)). Operators believe spend is capped; it is not.

Classification: the *documentation* is reachable, the *behaviour* is absent.

**Corrected on closer reading.** The original conclusion here — "implement the
read" — was wrong. iter-14.31 did not leave the fallback configurable, it removed
it: `_fabric_call_impl` now tries a Console-registered Ollama provider and then
raises. The guardrail is unconditional, which is *stronger* than the flag
promised. So the code is right and the documentation is the bug, and restoring
configurability would reintroduce the silent spend the hard kill exists to
prevent. The six docs were corrected instead.

### D5 — `_extract_json_object` could not read a fenced JSON block

**Severity: high. Found by measurement, not by reading.**

Not visible in Phase 0's static sweep — it is a logic error, not an
unreachable symbol. It surfaced when a local model was driven through the real
fabric while verifying the D1 fix.

`routes/codegen.py::_extract_json_object` stripped fences with
`s.split("```", 2)[-1]`. On a correctly fenced block that returns the empty
string following the **closing** fence:

```python
'```json\n{...}\n```'.split('```', 2)
# -> ['', 'json\n{...}\n', '']      and [-1] is ''
```

so the brace scan that followed had nothing to scan, and the single most common
shape an LLM emits parsed as `None`.

Measured impact on `codegen.verifier` with `qwen2.5-coder:7b`, 6 runs: the old
extractor parsed **0 of 6** replies. Each failure became `confidence 0.0`,
`verdict REJECT`, and a task marked `VERIFY_FAILED` — a good file reported as
bad. Full numbers in [`docs/quality/OLLAMA_BASELINE.md`](quality/OLLAMA_BASELINE.md).

`routes/tools.py` has a separate implementation that handles fences correctly,
so the Transformer pipeline was unaffected. That is the clearest argument yet
for the *Duplicates* catalogue below: the same helper, two implementations, one
silently broken for months.

### D6 — Azure's live API rejects what the fabric always sent

**Severity: high for the primary provider. Found only by calling it.**

Two hard 400s, neither inferable from the code:

1. `gpt-5.1` rejects `max_tokens` and requires `max_completion_tokens`. The
   fabric hardcoded `max_tokens` in both payload builders, so every call to a
   reasoning-class deployment failed outright. Applies to the whole o1/o3/o4 and
   gpt-5 family on any provider, not just Azure.
2. OpenAI-style `json_object` mode refuses to run unless the literal word
   "json" appears in the messages. Enabling JSON mode without a guard would
   have turned working calls into 400s for any agent whose prompt did not
   happen to say it.

Both are pinned by `backend/tests/test_provider_azure.py`.

### D4 — two dead frontend API helpers

`rebuildKbGraph` and `getKbGraph` at
[`api.js:265-269`](../frontend/src/lib/api.js#L265-L269) call `/kb/{pid}/rebuild-graph`
and `/kb/{pid}/graph`. Neither route is registered in `routes/kb.py`, and no component
imports either helper.

Classification: `dead(safe)`. Removal evidence is the route grep plus the caller grep.

---

## Structural observations that shape the work

### The Codebase Map is mostly a lifting job

`owl_extractor.py` already records inheritance and dependency injection for five
languages:

| Language | Records |
|---|---|
| Java | `extends`, `implements`, `CDI_INJECTION` (field-style `@Inject` **and** constructor-style `private final XService`) |
| PHP | `extends` |
| Python | `extends` (first base), `implements` (remaining bases) |
| .NET | `extends`, `implements` |
| JS | `extends`, `implements` |

But `_VALID_EDGE_TYPES` at [`kb_graph.py:616`](../backend/kb/kb_graph.py#L616) contains
only `HAS_METHOD, HAS_COLUMN, CALLS, READS, WRITES, EXPOSES, GUARDED_BY,
BELONGS_TO_MODULE, BELONGS_TO_ENTITY, REFERENCES_TABLE`. `build_kb_graph` reads each
`CLASS` entity, lifts its name, namespace, source and methods — and never touches
`extends`, `implements` or the `CDI_INJECTION` entities.

`CALLS` is a declared edge type but the deterministic pass never emits one. Its only
producer is the LLM graphify enrichment, which is off by default
(`LAMA_USE_GRAPH_KB`). So the call graph today is either empty or LLM-guessed.

Consequence: inheritance and injection edges need a lift, not a parser. Only `CALLS`
needs tree-sitter.

### Entities carry no line numbers

No extractor records a line number. The Codebase Map requires every node to carry
`file`, `line`, `language`, `layer`. `line` must be added to the extractors.

### The monkeypatch seam map governs the split

`routes/codegen.py` — patched by **4** test files on **10** names, all Mongo collection
handles plus `chat_completion`:

```
audit_log  chat_completion  codegen_agent_runs  codegen_envelopes  codegen_files
codegen_pipeline_state  codegen_tasks  projects  prompts_col  stage_context_col
```

`routes/tools.py` — patched by **15** test files on **24** names, mostly internal
orchestration functions that call each other:

```
_binary_on_path  _coder_apply_fix  _continue_multi_agent_after_confirm  _emit_log
_fetch_maven_versions  _get_effective_model  _get_effective_prompt  _log_agent_run
_parse_compile_errors  _planner_fix_tasks_from_errors  _project_id_for_transform
_resolve_failing_transform_file  _run_coder  _run_compiler
_run_multi_agent_transformation  _run_verifier  _update_agent_run
_write_transformed_workspace  audit_log  fabric_call  tempfile  transform_files
transformations  transformer_envelopes
```

`monkeypatch.setattr(tools_mod, "_run_compiler", fake)` sets an attribute on
`routes.tools`. If `_run_compile_fix_loop` moves to a submodule it resolves
`_run_compiler` from its **own** module globals, so the patch silently misses and the
test passes while exercising the real compiler. A re-export shim does not preserve
monkeypatch semantics. Splitting across those seams naively would make 15 test files
green and meaningless — strictly worse than not splitting.

Hence the two-stage split in the plan: Stage A moves only symbols no test patches,
Stage B is a costed decision.

---

## Duplicates (catalogued, not merged)

Per the ground rules the two pipelines share concepts, not code. Recorded here for a
later decision; **no action in this work**.

| Concept | CodeGen (`routes/codegen.py`) | Transformer (`routes/tools.py`) |
|---|---|---|
| JSON extraction | `_extract_json_object` (8896) | `_extract_json_object` (681) |
| Fence scrubbing | `_sanitize_llm_file` (9869) | `_scrub_code_fences` (5455), `_strip_llm_code_wrapper` (5399) |
| Placeholder / stub guard | `_looks_like_placeholder` (9950) | `_structural_check` (5755) |
| Agent run logging | `_log_codegen_agent_run` (8775) | `_log_agent_run` (4790) |
| Deterministic envelopes | `_deterministic_codegen_envelopes` (8927) | `_deterministic_envelopes_from_kb` (4825) |
| Deterministic task build | planner fan-out (9257+) | `_build_deterministic_tasks` (4323) |
| Bounded coder fan-out | `asyncio.Semaphore(LAMA_CODEGEN_PARALLELISM)` | `asyncio.Semaphore(LAMA_CODER_MAX_CONCURRENCY)` |
| Traceability gate | `_run_codegen_traceability_gate` (10337) | `get_transformation_traceability` (9735) |

The two envelope/planner/gate families are genuinely different in input and contract.
The four *utility* families (JSON extraction, fence scrubbing, stub guard, agent-run
logging) are near-identical and are the only honest consolidation candidates.

---

## Candidate list (0.3)

| Item | Class | Action |
|---|---|---|
| `_todo_hits` at codegen.py:3751 | **live defect** | fixed (P1.0) |
| `_extract_json_object` fence handling | **live defect (D5)** | fixed (P1.18) |
| `max_tokens` on reasoning models | **live defect (D6)** | fixed (P1.16) |
| `json_object` without the word "json" | **live defect (D6)** | fixed (P1.17) |
| `llm.chat_completion` | `dead(safe)` — unreachable since iter-14.31 | **Phase 2 decision** |
| `response_format` kwarg, 15 sites | **live defect** | make reachable (P1.1) |
| `LAMA_DISABLE_OPENROUTER_FALLBACK` | **live defect** | implement the read (P1.6) |
| `rebuildKbGraph`, `getKbGraph` | `dead(safe)` | remove with evidence (P1.5) |
| `test.jmeter.samplers` prompt key | `dead(registered-unused)` | **human decision** |
| `tools.transformer.validator` | `dead(registered-unused)` | **human decision** |
| 37 × F401 unused import | `dead(safe)` | remove (P2) |
| 15 × F841 unused variable | `dead(safe)` | remove (P2), each read first |
| 24 × ARG001 unused argument | `unknown(investigate)` | many are FastAPI/monkeypatch signatures — audit individually |
| 10 × F541 f-string no placeholder | `dead(safe)` | de-f (P2) |
| `LAMA_USE_JOURNEY_KB`, `LAMA_USE_GRAPH_KB` | `flag-gated(keep)` | keep, whitelist |
| Compose host-path env vars | `flag-gated(keep)` | keep, whitelist |
| `_try_ollama_fallback`, env OpenRouter retry | `fallback(keep)` | keep, whitelist |
| `JobStopped`, `CreditError`, `TransportError` | `error-path(keep)` | keep, whitelist |
| 252 test-file ruff findings | `flag-gated(keep)` | pytest fixture params; **do not touch** |

The 252 findings in `backend/tests/` are overwhelmingly `ARG001`/`ARG005` on pytest
fixture parameters and stub lambdas. Those arguments are required by the calling
convention. Removing them would break the fixtures. They are whitelisted wholesale.
