# Deep test — is it actually implemented properly?

A layered verification of the whole application, not just the changes made in
this session. Static bars, the full suite, a live backend, and the real
pipelines driven end to end against live providers.

**Headline: seven more defects found, every one by running the code rather than
reading it, and seven guards restored that had gone stale and were protecting
nothing. One finding needs your decision before it can be fixed.**

---

## Layer 1 — Static bars

| Check | Result |
|---|---|
| `ruff check backend` (E9, F, ARG) | **All checks passed** |
| `vulture --min-confidence 80` + whitelist | **clean** |
| `pyflakes` undefined names, all backend source | **0** |
| `compileall backend` | **all compile** |
| App boot | **311 routes across 21 routers** |
| `yarn build` | **Compiled successfully** |

## Layer 2 — Test suite

| | Failed | Passed | Errors |
|---|---|---|---|
| Baseline (`8c658c1`) | 92 | 632 | 46 |
| Now | **85** | **797** | 46 |

Zero new failures at every commit, verified by diffing failure identifiers
rather than comparing counts. Seven pre-existing failures fixed. All 46 errors
and most remaining failures are five live-server integration suites that need
a running HTTP server.

## Layer 3 — Live API

Backend booted, authenticated as super-admin, every major read endpoint
exercised. `/api/projects` returns 401 without a bearer token, which is the
multi-tenant auth behaving correctly rather than a fault.

Registries cross-checked directly against Mongo: 63 agent configs, 60 seeded
prompts, no duplicate keys, none disabled.

## Layer 4 — Contracts the operating rules protect

Every one driven live, not asserted from source.

| Contract | Result |
|---|---|
| Stage gates refuse when upstream is not frozen | **HTTP 400** on multi-agent start, single-shot generate, auto-validate, and DataModel OLTP |
| A run cannot start unless idle | **HTTP 409** with the current status named |
| Gate ordering cannot be skipped | confirming envelopes from `tasks_pending` → **HTTP 400** |
| Human gate 1 (`envelopes_pending`) | operator confirm advances to `tasks_pending` |
| Human gate 2 (`tasks_pending`) | operator confirm advances to `executing` |
| Envelope `PATCH` is honoured | edited summary, tables and BR ids all persisted |
| Deterministic envelopes are LLM-free | 4 envelopes from 4 endpoints, **LLM fallback not invoked** |
| LLM fallback fires below three envelopes | on an empty fixture: "Deterministic pass yielded only 0 envelope(s); falling back" |
| Deterministic BE/FE routing | see **D8** — was broken, now fixed and verified |
| Toolchain present → real compiler output | real `mvn` ran; a deliberately broken Java file produced genuine `[ERROR]` lines parsed into one fix group at the right line |
| Toolchain absent → `compilation_ready: false` | `status: failed`, reason `toolchain missing on PATH: mvn`. **Never a silent skip** |
| DevOps Expert escalation, no iteration cap | fires correctly: `['coder', 'devops_expert']`, green build, `escalated_to_devops: True` |

---

## Defects found by this deep test

### D8 — the deterministic BE/FE router was inert

`_BE_PATH_PREFIXES` contains `services/`, and the planner writes frontend files
as `services/web/src/pages/panels.tsx`. That matches the backend rule on its
prefix **and** the frontend rule on its extension, so it was classed
"ambiguous" and deferred to whatever the Planner suggested.

Every frontend file in a real run was routed by the Planner alone. A `PATCH`
forcing `assigned_to: coder_be` onto a `.tsx` file was accepted and persisted.
The guarantee that "a frontend file never reaches the backend coder" was
guaranteeing nothing.

An earlier reading of the same run showed "0 routing violations". That was a
false negative: the Planner happened to assign correctly, so the broken guard
was never exercised. Only forcing the hostile case exposed it.

**Fixed.** A decisive extension now outranks a directory prefix. `.ts` and
`.js` stay ambiguous on purpose, because a Node backend uses them too.

### D9 / D10 — every service's code was written into the first service

All eight files for `claims-service` landed under `services/panel-service/`
with package `com.lama.panelservice`. Two independent causes:

1. The planner resolved **one** `be_service_name` (the first backend service
   found) and used it for every envelope in the project.
2. The envelope's owning service never reached the planner anyway. The builder
   records it as `_service_name`, but persistence writes an explicit
   projection of ~25 named keys and that was not among them.

This silently undoes the decomposition Stage 3 exists to produce, which is the
product's whole proposition, and the output still compiles so nothing
downstream notices. The build manifest had the same shape of bug: one
`pom.xml`, for the first service, leaving every other service with source
files and no build file.

**Fixed**, and verified live on the same fixture:

```
before   ENV-0003 /api/claims/{id} -> ['panel-service']
         manifests: services/panel-service/pom.xml

after    ENV-0001 /api/panels/{id} -> ['panel-service']
         ENV-0003 /api/claims/{id} -> ['claims-service']
         ENV-0004 /panels          -> ['web-ui']
         manifests: services/claims-service/pom.xml
                    services/panel-service/pom.xml
                    services/web-ui/package.json
```

### D11 — a transient 429 permanently demoted agents to another provider

The most consequential finding. Two agents ended a live run silently pinned to
local Ollama:

```
codegen.coder_fe  -> Ollama (local)
codegen.verifier  -> Ollama (local)
```

`token_usage_log` shows why:

```
codegen.verifier  azure  gpt-5.1  error ::
  HTTP 429: {"statusCode": 429,
             "message": "Rate limit is exceeded. Try again in 67 seconds."}
```

`_BILLING_MARKERS` contained `"rate limit"`, so a 429 was classified as a
billing failure. That routed a transient throttle into the billing-failover
path, which on success **leaves the agent pinned** to whichever provider
answered — permanently, silently, across restarts.

The verifier is the quality gate for every generated file. It was demoted from
a frontier model to a local 4B one with nothing said to the operator, and it
would never have recovered. With the wave fan-out issuing six coder calls at
once, a 429 on an enterprise deployment is the expected case. This directly
defeats "keep Azure as primary".

A 429 is also the one failure that states its own remedy. Waiting is correct;
changing vendor forever is not.

**Fixed.** Rate limits are classified separately from billing; `fabric_chat`
waits the delay the provider names and retries a bounded number of times; and
if a failover still happens the pin is released so the next call returns to the
primary. `"quota exceeded"` stays on the billing path, because a hard cap does
not heal by waiting. The two stale pins this bug had already written were
released.

### D12 — the multi-agent audit trail was invisible

`_audit_multi_agent` wrote the project id as `entity_id` and never set
`project_id`. `routes/audit.py::list_audit` filters on `project_id`. So every
state change the pipeline recorded could not be retrieved by the one consumer
that reads it.

```
audit_log rows total              117
rows WITH project_id               87
rows invisible to the Audit page   30
   29x codegen_multi_agent      <- the entire pipeline trail
    1x auth.login               <- correctly has no project
```

The helper's own docstring states the goal: *"so admins can reconstruct the
pipeline from the audit log alone"*. They could not. The rows existed, the
iter-17 contract was satisfied on paper, and none of it was reachable.

This is the shape of bug that static analysis and unit tests both miss: the
writer works, the reader works, and they disagree about one field name. It only
appears when you query the way the product does.

**Fixed**, and the 29 rows already written were backfilled from their own
`entity_id`, so the accumulated trail becomes visible rather than staying a
blind spot.

### D13 — provider endpoint emitted 12 characters of the API key

`GET /api/console/providers` returned, for a real 32-character Azure key:

```
api_key            'yuATf0...p6pG'     6 leading + 4 trailing
detected_from_key  'yuATf0sx...'       8 leading, not masked at all
```

Twelve of 32 characters, next to the full endpoint URL and deployment name.
`_serialize_provider` masked `api_key` and forgot `detected_from_key`, which
`setup_default_provider` populates as `api_key[:8]`.

Twelve characters will not let anyone brute-force the key, but it is enough to
confirm a key someone already holds, it survives into logs and screenshots, and
no caller needs it.

**Fixed.** Masking reveals a 4-character trailing fragment at most, and
`detected_from_key` is masked at serialization so rows already in Mongo are
covered without a migration.

### D14 — the verifier could not see what it was gating against

The most consequential quality finding, and it was exposed by an earlier fix
rather than found directly.

The `codegen.verifier` rubric defines four of its nine checks in terms of the
envelope:

```
3_contract_preservation  "Compare with the envelope. Every path MUST match exactly."
5_business_logic_check   "Compare method body against the envelope's
                          business_logic_summary"
6_data_integrity         "column names match the OLTP DDL"
9_completeness           "All methods from the envelope exist in the file"
```

The call site sent the file content and a task id. Nothing else. Four of the
nine checks were structurally impossible.

Before the `UNVERIFIABLE` verdict was added, the verifier had no legal way to
report that, so it guessed — and reading well-formed code, it mostly guessed
`ACCEPT`. **The quality gate for every generated file was a rubber stamp.**

On the live run nine files came back `UNVERIFIABLE` citing *"without the
TASK-XXXX envelope or contract details"*. That is the verifier correctly
reporting it had been asked to do an impossible job. The prompt hardening did
not make it strict; it stopped it bluffing and surfaced the real defect
underneath.

**Fixed.** The user prompt now carries the task and the envelope: endpoint,
service, business-logic summary, db tables and operations, BR ids, acceptance
criteria. When the envelope lookup fails the prompt says so explicitly, so the
model marks the dependent checks N/A rather than guessing — the same failure
mode this fix exists to remove.

**Breakdown of the 26 verification failures on the live fixture run**, which
is how the split between fixture artefact and real fault was established:

| Cause | Count |
|---|---|
| Cite the missing DataModel / DDL | 16 |
| Cite the missing envelope (**D14**) | 9 |
| Genuine code faults | 3 |

The 16 are a fixture artefact: that fixture was built with a frozen
Architecture and no DataModel stage, which a real run cannot do — the pipeline
requires DataModel frozen before Architecture. The 9 were the defect above. The
3 were real, and are listed in the quality-chain section.

---

---

## Needs your decision — DEC-7

**~287 routes accept requests with no bearer token.** Only the `admin`, `auth`
and `projects` routers enforce authentication. Everything else answers an
anonymous caller:

```
GET /api/projects                         401   <- enforced
GET /api/kb/{pid}/status                  200
GET /api/codegen/{pid}/files              200   <- generated source code
GET /api/console/providers                200   <- provider configuration
GET /api/audit?project_id=...             200
```

`CLAUDE.md` contract #3 states the intent: *"New routes that read project data
must scope by tenant, not just `project_id`."* Outside those three routers it
is not implemented. With `CORS_ORIGINS=*`, a browser on any origin can read it.

The fix is a global dependency and `lib/api.js` already attaches a bearer token
to every request, so the UI would not break. I did not apply it: it flips ~287
endpoints from open to closed in one commit, and anything of yours that calls
LAMA without a token stops working. Options are costed in
`HUMAN_INTERVENTION.md` DEC-7.

---

## Guards restored

Seven tests were red at `8c658c1`, verified in a worktree of that commit. In
every case the **product was correct and the test had drifted** — which is
worse than a plain failure, because each protects a contract the operating
rules call load-bearing, and a red test protects nothing.

| Suite | Why it was red |
|---|---|
| `test_iter1562_fix_loop_iterations` | the stub never took the `on_stage` parameter `_coder_apply_fix` had gained, so the call raised `TypeError` inside the loop's own try/except and the escalation looked dead |
| `test_iter1393_workspace_block` (×3) | iter-13.99 deliberately moved the pseudo-root to `/lama-workspaces/` with a double-underscore separator; one assertion also had a case mismatch that never matched |
| `test_iter1391_factory_isolation` (×3) | iter-13.91.8 collapsed the nested workspace path to one level, and iter-13.91.12 replaced "empty when the env-var is absent" with an explicit `host_anchored` flag |

Each now pins the current, deliberately-chosen, documented contract. Two pin
strictly more than before, and one new test was added for an uncovered path.
No assertion was loosened to make anything pass.

---

## Generated code, inspected

Fifteen files produced by the multi-agent coders against live Azure `gpt-5.1`,
3k–11k characters each, correct Spring Boot idiom for the declared target
stack: `jakarta.persistence`, `@Entity`, `@Table`, `@RestControllerAdvice`.
Zero placeholder rejections.

The prompt hardening is visibly working. Rather than inventing, the model
writes what it does not know:

```java
// TRACEABILITY-GAP: Legacy error codes for ApiClaimsId exceptions are not
// specified in the envelope. Using best-effort placeholder codes ...
// replace with exact legacy codes when available.
```

That is the "legal way to be uncertain" the hardened prompts added, showing up
in real output.

---

## The quality chain, observed working end to end

The most useful evidence came from watching the agents disagree with each
other on a live run against Azure `gpt-5.1`.

**The verifier caught real compile errors, with evidence, and the gate acted
on them.** Two files were rejected at confidence 98:

```
TASK-0019  dto/ApiClaimsIdDtos.java
  [BLOCKER] File defines multiple public top-level records and none of them
            matches the filename
  evidence: 'public record CreateRequest(...)\n...\npublic record UpdateRequest(...)'

TASK-0020  exception/ApiClaimsIdException.java
  [BLOCKER] Global exception handler is defined as an inner static class
            inside a non-component class
  evidence: '@RestControllerAdvice\n    public static class GlobalExceptionHandler {'
```

Both are genuine faults. A Java file may hold only one public top-level type
and it must match the filename, so the first would not compile at all. The
second would compile but Spring would never register the handler.

Before the verifier-gate fix, a `REJECT` at confidence 98 was recorded as
`VERIFIED`, because the code read the verdict into a local and then decided on
the score alone. Both of these files would have shipped.

**The `UNVERIFIABLE` verdict earned its place immediately.** Fourteen files
came back unverifiable rather than accepted or rejected:

```
[BLOCKER] Cannot verify that the JPA mappings (table/columns, nullability,
          lengths) match ...
evidence: '// TRACEABILITY-GAP: Original DDL not present in KB; mapping based
           on best-effort ...'
```

That is correct. The fixture was built with a frozen Architecture and no
DataModel, so there is no OLTP DDL to check column mappings against. The
verifier said so and cited the coder's own gap comment as the evidence, rather
than guessing an `ACCEPT` or inventing a `REJECT`. The gate fails closed on
`UNVERIFIABLE`, so nothing unverifiable is silently approved.

Both hardened prompts are visibly working together here: the coder writes
`TRACEABILITY-GAP` where it lacks input instead of fabricating, and the
verifier reads that marker as evidence that it cannot complete its check.

---

## Known, not fixed

- **A partial `srs.generate` row** in `agent_configs` carries only `key`,
  `max_tokens` and `status` — no `stage`, no `complexity`. Routing is
  unaffected (`resolve_model` falls back to `AGENT_COMPLEXITY`), and the seed
  does not repair it. Cosmetic: `token_usage_log.stage` is blank for that
  agent.
- **Eight agents have no prompt template.** Five are orchestrators; the rest
  (`srs.regenerate`, `codegen.regenerate`, `srs.diff`) are tier-routing keys
  that deliberately reuse another agent's prompt, per the iter-13.76 split.
- **The 46 test errors and most of the 85 failures** are five live-server
  integration suites. See `HUMAN_INTERVENTION.md` DEC-4.

---

## Reproducing

```bash
# bars
(cd backend && ruff check .)
.venv/bin/vulture backend backend/.vulture-whitelist.py --min-confidence 80 --exclude backend/tests

# suite
MONGO_URL=mongodb://127.0.0.1:27017 DB_NAME=lama_test \
  .venv/bin/python -m pytest backend/tests/ -q -p no:randomly \
  --ignore=backend/tests/test_console.py

# the contracts this test added
.venv/bin/python -m pytest \
  backend/tests/test_route_task_to_coder.py \
  backend/tests/test_multiservice_attribution.py \
  backend/tests/test_rate_limit_handling.py -q

# live
cd backend && ../.venv/bin/uvicorn server:app --port 8090
curl -s localhost:8090/api/health/providers | jq .
```
