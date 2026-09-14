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

---

### DEC-6 — Synthetic filenames never reached tech detection

**Raised:** Phase 2, while auditing unused variables.

`routes/kb.py` builds a `synthetic_files` list (filenames recovered from
archive members that have no real file row), then had:

```python
# Merge: real files first (they own the chunks for content scan),
# synthetic files only contribute filenames for the language tally.
files_for_detect = files + synthetic_files
...
tech = detect_tech_stack(files, chunks_by_file)     # <- `files`, not the merge
```

The merged list was assigned and never used. The comment describes an intent
that the code does not carry out: those synthetic filenames have never
contributed to the language tally.

This is a dropped assignment, not dead weight — the variable is waste, but the
*absence* of its use is a latent bug. Wiring it in is a one-word change:

```python
tech = detect_tech_stack(files_for_detect, chunks_by_file)
```

I did **not** make that change. It alters tech-detection results on every
existing project — a project whose language was detected as one thing could
flip to another, which changes target-stack suggestions and every prompt that
carries `source_tech`. That is a behaviour change, and the refactor rules
forbid making one. The dead variable is removed and a comment now records the
discrepancy at the site.

**What I need from you — one of:**

1. **Wire it in.** Pass `files + synthetic_files` to `detect_tech_stack`, and
   accept that existing projects may re-detect differently on their next KB
   build. Best done with a before/after comparison on a real pilot project.
2. **Leave it.** Detection uses real files only, deliberately. I delete the
   synthetic-file construction too, since nothing else consumes it.

*Recommendation: 1, but measured first.* The comment suggests someone
concluded the tally was incomplete without them. Worth confirming on the PMIS
pilot before flipping it for everyone.

---

### DEC-7 — 300+ routes accept requests with no bearer token

**Raised:** deep test, live against a running backend. **This is the most
serious finding in the audit.** It needs a decision because the fix is small
but its blast radius is not.

Only two routers enforce authentication. Every other data-bearing endpoint
answers an anonymous caller:

```
GET /api/projects                              401   <- enforced
GET /api/kb/{pid}/status                       200
GET /api/codegen/{pid}/multi-agent/state       200
GET /api/codegen/{pid}/files                   200   <- generated source code
GET /api/tools/transformer                     200
GET /api/console/providers                     200   <- provider configuration
GET /api/audit?project_id=...                  200
GET /api/prompts                               200
```

Auth-dependency references per router:

| Router | Routes | Auth refs |
|---|---|---|
| `admin` | 9 | 11 |
| `auth` | 4 | 4 |
| `projects` | 7 | 8 |
| **every other router** | **~287** | **0** |

`CLAUDE.md` contract #3 states the intent plainly: *"New routes that read
project data must scope by tenant, not just `project_id`."* The intent is not
implemented outside those three routers. In a multi-tenant deployment reachable
beyond localhost, any caller who knows or guesses a `project_id` can read
another tenant's knowledge base, generated source, audit log and provider
configuration. `CORS_ORIGINS=*` means a browser on any origin can do it too.

**The fix is small, and the client is already ready for it.** `lib/api.js`
attaches a bearer token to every request through an axios interceptor
(line 19), and the two XHR upload paths set the header explicitly. So a global
dependency would not break the UI:

```python
# server.py
from auth import get_current_user

_OPEN_PATHS = {"/api/health", "/api/health/providers",
               "/api/auth/login", "/api/"}

api_router = APIRouter(
    prefix="/api",
    dependencies=[Depends(get_current_user)],   # <- the whole change
)
# with the open paths mounted on a separate un-gated router
```

**Why I did not just apply it.** It flips ~287 endpoints from open to closed in
one commit. Anything that calls LAMA without a token stops working: the testing
agent's flows, any curl scripts or dashboards you have, and the five
live-server test suites. That is a behaviour change with real operational
blast radius, and which of those matter is something only you know.

**What I need from you — one of:**

1. **Apply it globally now.** I add the dependency, exempt health and login,
   and re-drive the full pipeline to prove nothing broke.
2. **Apply it behind `LAMA_REQUIRE_AUTH`,** defaulting ON, so you can switch
   it off for a single environment while migrating tooling.
3. **Gate only the sensitive readers first** — `console/*` (provider
   configuration), `codegen/*/files` (generated source), `audit/*` — and leave
   the rest for a follow-up.

*Recommendation: 2.* It closes the hole by default and gives you one lever if
something you own turns out to call LAMA unauthenticated.

**Already fixed, separately, because it was unambiguous:** the same endpoint
was emitting 12 characters of the real 32-character Azure key —
`api_key` gave `yuATf0...p6pG` and `detected_from_key` gave a completely
unmasked `yuATf0sx...`. Masking now reveals a 4-character trailing fragment at
most, and `detected_from_key` is masked on the way out so existing rows are
covered. That reduces the severity of DEC-7 but does not remove it: the
endpoint still should not answer an anonymous caller at all.

---

## DEC-8 — The Discovery model selector is not in the UI

**Status:** open. Not applied, because restoring it is a product decision.

**What I found.** `frontend/src/components/ChatPanel.jsx` (475 LOC) held the
only `data-testid="model-selector"` in the codebase. `DiscoveryV2.jsx:20`
imported it — and never rendered it. A repo-wide grep for `<ChatPanel` returns
zero hits. The live chat is `FloatingChat.jsx`, which *consumes* a `model`
prop (sends it at line 223) but has no picker of its own and is never passed
an `onModelChange`.

So the chain CLAUDE.md contract #11 describes:

> "The model picked in ChatPanel (lifted into Discovery state, persisted to
> `localStorage["lama:chat:model"]`) is forwarded into the
> `/api/srs/generate/stream` POST body"

is broken at the first link. `DiscoveryV2.chatModel` is read once from
`localStorage` on mount and can never change from inside the app. Its setter
and `handleModelChange` were both dead. SRS generation therefore always runs
with whatever model was last written to that key — or the backend default if
it was never written.

**What I did.** Removed the dead code (ChatPanel.jsx, the unused setter and
handler) and corrected CLAUDE.md contracts #5, #6 and #11 so they describe
what the code actually does. The backend side is untouched and still works:
`/api/srs/generate/stream` still accepts and honours a `model` in the body.

**What I need from you — one of:**

1. **Restore the picker.** Give `FloatingChat` a model dropdown, pass
   `onModelChange` from `DiscoveryV2`, and re-add the `model-selector` testid.
   This is what the contract says should exist. `git show 9f39a02^:frontend/src/components/ChatPanel.jsx`
   has the original selector to lift from.
2. **Drop the contract.** Accept that the model is chosen in Console (tier
   routing) rather than per-conversation, and delete contract #11.
3. **Leave it.** The key is still read on mount, so an operator can set
   `localStorage["lama:chat:model"]` by hand.

*Recommendation: 1 if per-conversation model choice is a feature you want;
otherwise 2, because a documented contract that no code implements is worse
than no contract.*

**Two related testids in the same contract also do not exist:**
`owl-export-btn` and `refresh-kb-health` have zero occurrences anywhere in
`frontend/src`, and the third stage-badge variant the code emits is
`stage-{key}-badge-skipped`, not `-locked`. The `/api/kb/{pid}/owl-export`
route behind the first one is alive and working — only the button is gone.
I corrected the doc rather than inventing buttons.
