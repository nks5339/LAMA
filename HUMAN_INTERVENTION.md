# HUMAN_INTERVENTION

Two sections. **Blocked** holds tasks that failed three times with three different
hypotheses. **Decisions** holds things that are not failures but need a product call.

---

## Blocked

*(none yet)*

---

## Decisions

### DEC-1 — `tools.transformer.validator` is registered but never invoked

**Raised:** Phase 0 recon.

The agent is fully wired as a first-class component and is never called:

| Where | What |
|---|---|
| `backend/seed.py:6143` | full prompt template, `force_update: True` |
| `backend/seed.py:8299` | `agent_configs` row, `complexity: medium`, `max_tokens: 6000` |
| `backend/fabric/model_fabric.py:223` | `AGENT_COMPLEXITY` tier = `medium` |
| `frontend/src/lib/agentLabels.js:37` | UI label "Validator", colour `#84CC16` |

The iter-16 multi-agent Transformer driver never references it. Its role — syntax and
semantic-equivalence validation of transformed code — is now covered by
`_structural_check` plus the real subprocess compiler added in iter-15.44, which is
strictly stronger evidence than an LLM opinion.

Not a deletion candidate per the ground rules: registered is not dead.

**What I need from you — one of:**

1. **Retire it.** Delete the prompt, the `agent_configs` row, the tier entry and the UI
   label in one commit. The compiler already does this job better.
2. **Wire it up.** Invoke it between Coder and Verifier as a cheap pre-compile syntax
   gate, so obvious breakage is caught before the expensive compile.
3. **Leave it.** Keep it dormant as a deliberate extension point and add a comment at
   each of the four sites saying so, so the next audit does not re-raise this.

*Recommendation: 3 for now, 1 later.* It costs nothing dormant, and option 2 spends
tokens duplicating a subprocess compiler that already returns ground truth.

---

### DEC-2 — `test.jmeter.samplers` prompt key has no consumer

**Raised:** Phase 0 recon (registry sweep).

Seeded at `backend/seed.py:5310`. No code loads this key. Its sibling `test.jmeter`
(5028) *is* loaded by `routes/living.py`.

Same class as DEC-1 but lower stakes. Likely a split of the JMeter prompt that was
started and not finished.

**What I need from you:** retire, or wire into the Living stage's JMeter generation?

---

### DEC-3 — Stage B of the big-file split

**Raised:** Phase 0 recon (monkeypatch seam map). **Blocks nothing** — Stage A lands
regardless and does most of the line-count work.

`routes/tools.py` is patched by 15 test files on 24 internal function names that call
each other. Moving those functions to submodules breaks `monkeypatch.setattr` silently:
the test passes, the fake is never called, the real compiler runs.

Two ways forward, both honest:

**Option A — late-binding call convention.** The extracted module does
`from routes import tools as _host` inside the function body and calls
`_host._run_compiler(...)`. The patched attribute resolves at call time so all 15 test
files stay byte-identical. Cost: a deliberate circular-import indirection that a future
reader must understand, and a convention that is easy to violate accidentally.

**Option B — update the test patch targets.** Move the functions cleanly and repoint
each `monkeypatch.setattr` at the new module. Cost: 15 test files change in the same
commit as the refactor, which the operating prompt's "tests do not change" line
discourages.

*Recommendation: B.* The indirection in A is a trap. A patch target is not an import
path, so B does not actually violate the intent of the rule. But this changes test
files during a refactor, so it is your call.

---

### DEC-4 — The pre-existing red test baseline

**Raised:** Phase 0 recon.

Baseline is **92 failed, 632 passed, 46 errors**. All 46 errors and most failures come
from five live-server integration suites (`test_lama_v2`, `test_lama_v4`,
`test_migrationos`, `test_srs_streaming`, `test_console`) that call a real HTTP server
at `REACT_APP_BACKEND_URL` and get `ConnectionError`.

`test_console.py` is worse: it errors at *collection* because it calls `.rstrip()` on
the `None` returned by `os.environ.get("REACT_APP_BACKEND_URL")`, which aborts the
whole pytest run unless ignored.

Per your decision the working rule is *no new failures*. But two cheap improvements are
available and I would rather ask than assume:

1. Guard `test_console.py` with `pytest.skip(allow_module_level=True)` when the env var
   is unset, so a plain `pytest backend/tests/` stops aborting at collection.
2. Mark all five suites `@pytest.mark.integration` and add an `addopts` default that
   deselects them, so the default run is the honest unit suite.

Both are small, both change test files, neither fixes an underlying product bug.

**What I need from you:** do 1, do 1 and 2, or leave the baseline exactly as-is?

---

### DEC-5 — Credential rotation

**Raised:** Phase 1.2 (Azure provider work).

Four API keys were pasted into the conversation transcript: an Azure key, a Gemini key,
a Groq key, and a second commented-out Azure key. They are being written to
`backend/.env`, which is gitignored, and nowhere else. No key is committed and none
appears in any file I have created.

A conversation transcript is not a secret store. **Treat all four as exposed and rotate
them.** This is not something I can or should do for you.

Also note `backend/.env` is destroyed by `docker compose up` — the entrypoint writes a
runtime `.env` through the bind-mount. Keep `backend/.env.mine` as a copy.
