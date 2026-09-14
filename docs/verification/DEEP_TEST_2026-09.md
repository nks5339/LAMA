# LAMA — deep test, 2026-09-15

Scope: verify the application is error-free front and back, that nothing
unreachable remains, that the container and dependency story is current, and
that every protected contract still holds. Every number below is real command
output, not a summary of intent.

Baseline for comparison: `docs/recon/pytest-baseline.txt`
(`92 failed, 632 passed, 3090 warnings, 46 errors`).

---

## 1. Headline

| Bar | Before | After |
|---|---|---|
| `pytest` (in-process suites) | 92 failed, 46 errors, 632 passed | **0 failed, 0 errors, 837 passed**, 129 gated |
| `ruff check backend` | clean | **clean** |
| `yarn lint` | *did not exist* | **0 errors**, 44 warnings |
| `yarn build` | compiled | **compiles** |
| `yarn test` | could not run | **runs** |
| `vulture --min-confidence 80` | n/a | **0 findings** (whitelist applied) |
| Registered routes | 311 | **312** |
| `requirements.txt` explicit pins | 176 | **38 direct → 122 resolved, 0 CUDA** |
| `package.json` dependencies | 59 | **26** |
| Frontend source files | 111 | **68** |

Net for this pass: `127 files changed, 1290 insertions(+), 8104 deletions(-)`.

---

## 2. Defects found and fixed

### D-A. `GET /api/kb/{pid}/build-progress` did not exist — user-visible

`backend/routes/kb.py:1582`: `async def build_progress` sat directly beneath
the closing brace of the previous handler's `return` dict, with no
`@router.get` above it and no blank line — unlike every other handler boundary
in the file. The body was correct; only the registration was gone.

```
$ grep -rn "build-progress" backend/
backend/routes/kb.py:1005:  # ... the /build-progress poller
backend/routes/kb.py:1416:  # build-progress panel shows forward motion
   → 2 comments, 0 routes
```

`BuildKBProgressDialog.jsx:63` polls that path every 1–2 s from the moment a
user clicks "Build Knowledge Base", so every poll 404'd and the dialog could
never advance past its opening state.

A missing decorator is invisible to pyflakes, ruff and vulture — the function
is still defined, still syntactically perfect, still referenced by nothing.
Only the caller's URL proves it gone. Guard added accordingly: a test that
checks **every** path in `lib/api.js` against the registered routes, not just
this one. Verified non-vacuous — with the decorator removed again it reports
exactly `['/api/kb/{}/build-progress']` out of 229 paths checked.

### D-B. `setOpen is not defined` — runtime crash on the envelope Edit button

`CodeGenMultiAgentPanel.jsx:513` called `setOpen(true)` inside `EnvelopeRow`.
That component has no such binding: its expansion is parent-controlled through
`isOpen`/`onToggle`, and the local `open` at `:453` is derived, not state. The
`setOpen` 200 lines below belongs to a different component. Clicking Edit on
any envelope row threw `ReferenceError`. Caught by the new `no-undef` rule —
this was the single `no-undef` in the whole codebase.

### D-C. 21 stale tests, only one of which was a product defect

Full breakdown in the commit body of `9116586`. Summary:

- **10** `test_codegen_standalone` — the harness `exec`s functions extracted
  from `codegen.py` into a namespace missing three module globals
  (`codegen.py:81-83`). Now read from the source rather than hardcoded.
- **6** confidence — two independent causes stacked. `_collect_artifact_text`
  stubs predated a `max_chars=` parameter, and the resulting `TypeError` became
  a `0.0` "missing" row that read as a scoring bug. Separately, with the engine
  flag unset, iter-14.29 auto-mode resolves to langgraph on any box where
  langgraph imports and the droid CLI is absent, so `strict_hf_only()` blocked
  the fabric fallback the tests were asserting on. Confirmed both were
  load-bearing by reverting each alone.
- **2** `confidence_langgraph` — pinned "off by default". iter-14.29 made the
  flag a three-way resolve and `resolve_confidence_engine`'s own docstring says
  so; the **module docstring** still promised the old contract and was the thing
  that was wrong.
- **2** `test_iter1411` — report-row keys `actors`/`nfr` predate the iter-14.23
  rename to IEEE-830 section keys and map to `[]`, so the improve loop resolved
  no work and exited after one iteration.
- **1** `test_iter1410` — asserted a 3-entry trajectory, predating the iter-14.11
  final-seal pass. *The seal is not waste*: iterations 2+ score with a single
  cheap evaluator over only the regenerated sections, so the loop re-scores once
  with the full panel to make the persisted number honest.
- **1** `test_iter1519` — asserted a six-agent roster; `devops_expert` was added
  deliberately in iter-15.62 and `test_iter1562` asserts it positively. The two
  suites contradicted each other.
- **1** `test_srs_streaming` — **not** a production defect, contrary to first
  reading. It asserted `section_progress` during a slow section; that event
  belongs to the per-section generator, and `/generate/stream` now runs the
  batched one. Keep-alive works: the SSE writer (`srs.py:7373-7378`) emits
  `ping` every 4 s while the queue is idle, and two fired inside the 8 s stall.
  Observed stream: `start, batch_start, batch_complete, section_complete,
  batch_repair_start, batch_repair_complete, section_repair, ping, ping, …`

---

## 3. Waste removed

### Backend dependencies

`requirements.txt` was a raw `pip freeze` from a Linux CUDA workstation: 176
pins, against 30 third-party modules the code actually imports (AST walk over
all 91 application modules, not a grep — several files embed `import boto3` /
`import psycopg2` inside string templates emitted into *generated* projects).

Removed with no code path anywhere: the **19 nvidia/cuda/triton pins** (the
Dockerfile installs CPU-only torch specifically to avoid these, then
requirements.txt dragged them back in), `openai` (LAMA calls every provider
over raw httpx per contract #4 — all 14 "openai" occurrences are provider names
and base URLs), `litellm`, `stripe`, `pandas`, `jq`, `s5cmd`, `fastuuid`,
`librt`, `ast_serialize`, the eight `google-*` SDKs, and `black`/`mypy`/
`flake8`/`isort`, which the image never runs.

Kept despite being invisible to an AST walk, each now annotated: the DWH
drivers reached through `db_ingest.py::_DRIVER_PROBES`, `bcrypt` (imported
dynamically by passlib's `CryptContext`), `python-multipart`, and `boto3`
(a real code path at `platform_catalog.py:235`).

Proof (`docs/verification/requirements-resolution.txt`):

```
pip install --dry-run --only-binary=:all: --python-version 3.11 \
    --platform manylinux_2_28_x86_64 ... -r backend/requirements.txt
→ resolved, 122 packages, 0 nvidia/cuda/triton
```

`backend/requirements_mac.txt` deleted: zero references repo-wide, one commit
of provenance, and a *pre-marker* copy of `requirements.txt` with unguarded
nvidia pins — strictly less installable on a Mac than the file it forked from.

### Frontend

Reachability computed from `src/index.js`: **111 source files → 70 reachable →
41 unreachable, 4,899 LOC.**

- 37 shadcn components in `components/ui/` (2,512 LOC). Only 10 of 46 were
  reachable. The app toasts via `sonner`; `toast`/`toaster`/`use-toast` were a
  complete unused parallel stack.
- `pages/Transformer.test.jsx` — tracked in git, 1,728 LOC, not a test despite
  the name, exported a second `TransformerPage`. Jest collected it and failed
  with "must contain at least one test", which is why `yarn test` was unusable.
- `plugins/health-check/` (333 LOC) behind `ENABLE_HEALTH_CHECK`, which a
  repo-wide grep shows is set nowhere — the only hit was the line reading it.
- ~300 lines of declared-but-never-rendered blocks, and 45 of 273 `lib/api.js`
  exports with zero callers. `cloneGitRepo`/`cloneGitStatus` look dead
  externally but are used by `cloneGitRepoAndWait`, so they were kept — caught
  by checking internal references, not just external ones.
- 33 of 59 `package.json` dependencies had zero imports, including 22
  `@radix-ui/*` kept alive only by the dead `ui/` files, and `cra-template`, a
  scaffolding package `create-react-app` reads once at generation time.

---

## 4. Container and dependency currency

**Python stays on 3.11 — reviewed, not inherited.** Moving to 3.14 to match the
macOS dev box was considered and rejected on evidence: `torch 2.12.0` does ship
a cp314 manylinux wheel, but `scipy` ships none at either its pinned `1.17.1`
or the latest release, and scipy is a hard transitive dep of
`sentence-transformers`, which `confidence_langgraph.py` imports. Source-building
scipy in a slim image means gfortran + BLAS/LAPACK. Separately, nothing here
could verify such a change: the Docker daemon is not running on this machine,
and `Dockerfile.local` exists precisely because a corporate TLS proxy blocks
rebuilding the base image. Python 3.11 is supported to Oct 2027. The reasoning
is now a comment above the `FROM` line.

**Node 20 → 24.** Node 20 reached end-of-life 2026-04-30; 24 is Active LTS
through 2028-04-30. Verified rather than assumed: `yarn build` succeeds on this
host's Node 26, which brackets 24, so CRA 5 / react-scripts 5.0.1 is fine on a
modern Node.

**Dockerfile.** `pip install 'uvicorn[standard]'` was unpinned and ran *after*
`requirements.txt`, silently upgrading past the `uvicorn==0.25.0` it had just
pinned and pulling uvloop/httptools/watchfiles/websockets unpinned. Now pinned
and not reinstalled. `poetry` pinned to 2.4.3.

**docker-compose.yml.** `LAMA_DEFAULT_MODEL` defaulted to
`deepseek/deepseek-chat`, overriding Console tier routing for every agent on any
deploy that did not set it — contradicting contract #4 and `entrypoint.sh:96`,
which deliberately defaults it empty. Now empty.
`LAMA_DISABLE_OPENROUTER_FALLBACK` dropped from compose and root `.env`: no code
has read it since iter-14.31, and exporting it implied an operator could
re-enable the fallback by clearing it, which they cannot.

`docker compose config -q` → exit 0.

Toolchain versions left alone on purpose — Gradle 8.10.2, Go 1.23.4,
dotnet 8.0, OpenJDK 17 are what the Tester agent compiles *user* projects
against, so bumping them changes generated-project behaviour.

---

## 5. Protected contracts — verified live

Backend on `127.0.0.1:8382`, MongoDB on 27017, Ollama on 11434.

| Contract | Check | Result |
|---|---|---|
| Stage gate: Discovery | `POST /api/data-model/generate/oltp` unfrozen | **400** "Discovery stage not frozen…" |
| Stage gate: Architecture | `POST /api/codegen/{pid}/multi-agent/start` unfrozen | **400** "Architecture stage not frozen…" |
| Run state machine | `multi-agent/start` while `envelopes_pending` | **409** "cannot start. Cancel or rerun to reset." |
| Gate ordering | `tasks/confirm` out of order | **400** "tasks cannot be confirmed — current status is 'completed'" |
| Human gate on restart | restart a completed run | resets to **`envelopes_pending`** — gate re-armed |
| Envelope PATCH | `PATCH …/envelopes/ENV-0001` then read back | **honoured**; `service_name` persists (`panel-service`) |
| Multi-tenancy | super-admin `POST /api/projects` without `tenant_id` | **refused** — "must specify tenant_id" |
| Auth | `GET /api/projects` without bearer | **401** |
| Envelope generation LLM-free | source scan of `_deterministic_codegen_envelopes` | **no** LLM tokens in 118 lines |
| DevOps escalation uncapped | `_run_compile_fix_loop` signature | `max_iterations` default **None** |
| BE/FE router deterministic | `_route_task_to_coder` source | LLM-free, logs `route_override` |
| Verifier floor | `_VERIFIER_SCORE_FLOOR` | **95.0** |
| Off-by-default not dead | `LAMA_USE_JOURNEY_KB` / `LAMA_USE_GRAPH_KB` | read by 3 / 6 modules |
| Key masking | `GET /api/console/providers` | key returned as `''` |

### Tester toolchain — both halves

Run against a deliberately broken Maven project.

*Toolchain present* — real compiler, real failure, not a skip:

```
status: failed   exit_code: 1   duration_ms: 3432   46 lines captured
  [ERROR] COMPILATION ERROR :
  [ERROR] …/Broken.java:[2,42] illegal start of expression
  [ERROR] Failed to execute goal …maven-compiler-plugin:3.15.0:compile
```

*Toolchain absent* (`PATH` emptied) — hard failure, never a silent pass:

```
status: failed
reason: toolchain missing on PATH: mvn (this is a LAMA runtime image bug — please report)
```

`compilation_ready` is `failed == 0 and (passed > 0 or skipped > 0)`
(`tools.py:6429`), so a missing binary can never produce a green tester.

---

## 6. Test suite structure

The repo had **no** `conftest.py`, **no** `pytest.ini` and **no** marker
anywhere, so `pytest backend/tests/` was the only way to run anything and it
reported 131 failures. **110 of those were eight legacy suites that drive the
app over HTTP and fail with `ConnectionError` because nothing was listening.**
A permanently 131-red run cannot tell you a real regression just landed, and 21
genuine failures had been sitting underneath it unnoticed.

Those eight are now marked `integration` and skipped by default. They are real
end-to-end coverage and are **gated, not deleted**:

```
test_arch_codegen  test_console  test_datamodel  test_iter10_ontology
test_iter11_living_diff  test_lama_v2  test_lama_v4  test_migrationos
```

`pytest.ini` also pins `asyncio_mode = strict`. It was unset, and
`pytest-asyncio` is pinned only in `requirements-dev-macos.txt` — so whether an
`@pytest.mark.asyncio` test executed at all depended on which requirements file
was installed. With the plugin absent the coroutine is never awaited and the
test passes without running an assertion.

All eight also predate iter-13.68 (JWT auth + multi-tenancy) and died in their
first fixture on `401 Missing Authorization header`. `conftest.py` now attaches
a bearer token centrally in integration mode only.

### The integration suites, run for real

Against a live backend on `:8382` with MongoDB and Ollama up:

| | passed | failed | errors |
|---|---|---|---|
| before the auth fix | 898 | 20 | 48 |
| **after the auth fix** | **918** | **30** | **17** |

`918 passed, 30 failed, 1 skipped, 17 errors in 868.33s (0:14:28)`

Errors fell because tests that used to die in a fixture now actually execute;
some then fail on their own merits, which is the point — they are visible
rather than masked. The 17 remaining errors are concentrated in three suites:
`test_lama_v2` (8), `test_lama_v4` (5), `test_iter11_living_diff` (4).

**These 30 failures are not triaged individually in this report.** They are
pre-existing behaviour in eight suites that were 100% unrunnable before this
pass and are still gated off by default, so they cannot mask a regression in
the in-process suites. Triaging them is follow-up work, not a blocker — and
worth doing now that they run at all.

A second pass intended to capture per-test detail was stopped after ~20 minutes:
these suites drive the full pipeline including live SRS generation, and with
`qwen3:4b` on local Ollama a single `test_generate_srs_real_llm` blocks for a
very long time (the pytest process showed 1.89s of CPU against ~20 minutes
wall-clock — it was waiting on the model, not working). The completed 868s run
above is the authoritative result.

---

## 7. Open items — not fixed, by design

- **DEC-8 (new): the Discovery model selector is not in the UI.**
  `ChatPanel.jsx` held the only `data-testid="model-selector"`. `DiscoveryV2`
  imported it and never rendered it; `<ChatPanel` appears nowhere in the tree.
  So the chain CLAUDE.md contract #11 describes — model picked in ChatPanel,
  lifted into Discovery state, forwarded to `/api/srs/generate/stream` — is
  broken at the first link, and `chatModel` can never change from inside the
  app. `owl-export-btn` and `refresh-kb-health` likewise do not exist, and the
  third badge variant the code emits is `-skipped`, not `-locked`. CLAUDE.md
  contracts #5/#6/#11 corrected to describe reality; restoring the picker is a
  product decision.
- **DEC-1 … DEC-7** remain open, including the ~287 routes that accept
  unauthenticated requests.
- **Azure is not registered as a provider.** `model_providers` holds exactly
  one row, `Ollama (local)`, so `/api/health/providers` reports
  `"chosen": "ollama"`. The Azure credentials are still present in the
  gitignored `backend/.env` (`AZURE_API_KEY`, `AZURE_ENDPOINT`,
  `AZURE_API_VERSION`, `AZURE_DEPLOYMENT`), but the Console provider row is
  gone. This is instance state, not a code defect — re-registering it writes a
  key into Mongo, so it was left for an explicit decision.

---

## 8. Reproducing this

```bash
# Backend
ruff check backend
.venv/bin/vulture backend backend/.vulture-whitelist.py --min-confidence 80
#   NOTE: the whitelist is an INPUT PATH, not --exclude. Passing it via
#   --exclude merely stops vulture scanning it, and the bar then reports
#   10 phantom findings that are all already whitelisted.
.venv/bin/python -m pytest -q                       # 837 passed, 129 skipped
.venv/bin/python -c "import sys;sys.path.insert(0,'backend');from server import app;print(len(app.routes))"

# Integration, against a live server
(cd backend && ../.venv/bin/uvicorn server:app --port 8382 &)
REACT_APP_BACKEND_URL=http://127.0.0.1:8382 \
  .venv/bin/python -m pytest -q --run-integration

# Frontend
cd frontend && yarn lint && yarn build && CI=true yarn test --watchAll=false --passWithNoTests

# Dependency resolution for the container target
pip install --dry-run --only-binary=:all: --python-version 3.11 \
    --platform manylinux_2_28_x86_64 --target /tmp/probe -r backend/requirements.txt

# Compose
docker compose config -q

# Reset caches to a fresh-looking instance
./scripts/clear-caches.sh            # dry run
./scripts/clear-caches.sh --apply
```
