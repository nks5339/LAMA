# Running LAMA

Two supported ways to run the application:

| | **Path A — Terminal** | **Path B — Docker** |
|---|---|---|
| Best for | day-to-day development, hot reload | a production-like single container |
| You provide | MongoDB, Node, an LLM provider | Docker Desktop only |
| Runs on | API `:8000` · UI `:3000` | everything on `:8382` |
| Mongo | your local install | bundled inside the image |
| Rebuild after edits | automatic (hot reload) | restart the container |

Everything below is also scripted — see [Scripts](#scripts).

---

## Prerequisites

| Software | Needed for | Install |
|---|---|---|
| **Python 3.11+** (3.14 works) | backend | `brew install python@3.14` |
| **MongoDB 7/8** | system of record — **required** | `brew tap mongodb/brew && brew install mongodb-community@8.0` |
| **Node 20+** (image uses 24 LTS) | frontend | `brew install node` |
| **yarn 1.22** | frontend — *never use npm* | `corepack enable` |
| **An LLM provider** | all generation | Ollama (below) **or** an OpenRouter key |
| Maven · Gradle · Go · .NET | CodeGen compile/test agent | `brew install maven gradle go dotnet` |
| Poetry · pnpm | extra build tools | `brew install poetry` · `corepack enable pnpm` |
| Docker Desktop | Path B only | <https://docker.com/products/docker-desktop> |

> **A missing build tool is a hard failure, not a skip.** The Tester/Compiler
> agent returns `status:"failed"` when a binary is absent, by design — so a
> build can never report `compilation_ready: true` without really compiling.
> Install the toolchains for the languages you actually generate.

### LLM provider

**Option 1 — Ollama (local, free, offline).** Also supplies embeddings, so
you never need to download PyTorch.

```bash
brew install ollama
ollama serve                      # leave running, listens on :11434

ollama pull qwen3:4b              # 262K context — large prompts
ollama pull qwen2.5-coder:7b      # code generation
ollama pull qwen3:0.6b            # fast, cheap tier
ollama pull nomic-embed-text      # embeddings for semantic search
```

Then add the provider **once**, in the UI under **Console → Models → Add
Ollama**, and set the per-tier routing (`low` / `medium` / `high`). LAMA reads
providers from the database, not from env vars — without a provider row every
generation call fails.

> Models that "think" (the Qwen3 family) spend 300–1600 tokens reasoning
> before emitting anything. Give them a token budget of **2000+**, or a small
> budget returns empty content. `qwen2.5-coder:7b` does not think and is the
> safer pick for short, latency-sensitive calls.

**Option 2 — OpenRouter (cloud).** Put the key in `backend/.env`:

```bash
OPENROUTER_API_KEY=sk-or-...
```

---

## Path A — Terminal

### 1. First-time setup

```bash
git clone <your-repo-url> lama
cd lama

python3 -m venv .venv                                   # the venv lives at the repo root
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r backend/requirements-dev-macos.txt

corepack enable                                         # provides yarn 1.22.22
cd frontend && yarn install && cd ..
```

Or simply: **`./scripts/setup.sh`** — which does all of the above and writes a
starter `backend/.env` with a freshly generated JWT secret.

> **Why `requirements-dev-macos.txt` and not `requirements.txt`?**
> `requirements.txt` targets the container: Linux on **Python 3.11**. Four of
> its pins have no cp314 wheel, and it carries the HuggingFace/torch stack
> (~3 GB) that local dev does not need. The `-dev-macos` file installs the
> same application at the same versions wherever Python 3.14 allows, and
> documents each relaxed pin inline. **`requirements.txt` remains the source
> of truth for the Docker image** — do not point the container at the dev file.
>
> (It used to be a raw `pip freeze` from a Linux CUDA box pinning 19
> `nvidia-*`/`cuda-*` packages. It was pruned to direct dependencies in
> 2026-09 and no longer names a single CUDA package.)

### 2. Configure `backend/.env`

`server.py` loads `backend/.env` (not the repo-root `.env`, which belongs to
docker-compose). It is gitignored, so real secrets are safe here.

```bash
MONGO_URL=mongodb://127.0.0.1:27017     # required — hard KeyError if unset
DB_NAME=lama                            # required
LAMA_JWT_SECRET=<48+ random chars>      # else a shared dev secret is used

LAMA_OLLAMA_BASE_URL=http://localhost:11434/v1
LAMA_DEFAULT_MODEL=                     # keep EMPTY — see note
OPENROUTER_API_KEY=

LAMA_CONFIDENCE_ENGINE=fabric           # pin explicitly — see note

QDRANT_PATH=./.qdrant_local             # offline vector store, no server
QDRANT_URL=                             # set this to use a real Qdrant instead
LAMA_EMBED_BACKEND=ollama
LAMA_OLLAMA_EMBED_MODEL=nomic-embed-text
```

> **Keep `LAMA_DEFAULT_MODEL` empty.** Any value here overrides per-agent tier
> routing for *every* call, so the Console's low/medium/high routing stops
> working.
>
> **Pin `LAMA_CONFIDENCE_ENGINE`.** In `auto` mode the engine flips to
> LangGraph whenever the `droid` CLI is unreachable *and* `langgraph` happens
> to be importable — so merely installing a package would silently change how
> artifacts are scored. Setting it explicitly keeps scoring deterministic.

### 3. Start MongoDB

```bash
brew services start mongodb-community@8.0   # persistent — note the version
# The formula is versioned. `brew services start mongodb-community` fails with
# "Formula not installed"; `... stop mongodb-community` silently reports
# "not started" even while it is running. Always use the @8.0 suffix.
# or, foreground:  mongod --config /opt/homebrew/etc/mongod.conf
nc -z 127.0.0.1 27017 && echo "Mongo up"
```

### 4. Run it — two terminals

```bash
# Terminal 1 — API
cd backend
../.venv/bin/uvicorn server:app --reload --port 8000

# Terminal 2 — UI
cd frontend
yarn start
```

Or: **`./scripts/run-backend.sh`** and **`./scripts/run-frontend.sh`**.

Two ways to point the UI at the API, and they are not the same thing:

| | What happens | When to use |
|---|---|---|
| `yarn start` (default) | Browser calls relative `/api`; the CRA dev server proxies to `http://127.0.0.1:8000`. Same origin, no CORS. | Normal split dev |
| `REACT_APP_BACKEND_URL=http://127.0.0.1:8000 yarn start` | Browser calls the backend **directly**; the proxy is bypassed and CORS applies. | Backend on another host |

Set `REACT_APP_API_PROXY` instead if you only want to move the proxy target
without changing what the browser calls.

| URL | What |
|---|---|
| <http://localhost:3000> | the application |
| <http://127.0.0.1:8000/api/health> | `{"ok":true}` |
| <http://127.0.0.1:8000/api/health/providers> | which LLM provider is live |
| <http://127.0.0.1:8000/docs> | interactive API docs |

> Use `127.0.0.1` for the API *and* `localhost` for the UI consistently with
> whatever you put in `REACT_APP_BACKEND_URL` — browsers treat the two as
> different origins and mixing them triggers CORS preflight failures.

---

## Path B — Docker

Runs the whole stack — nginx, MongoDB, and the API — in one container on
**:8382**. MongoDB is bundled; you do **not** need a local install.

> **⚠️ Docker overwrites `backend/.env`.** `docker/entrypoint.sh` writes a
> runtime `.env` to `/app/backend/.env`, and compose bind-mounts `./backend`
> there — so the write lands on your **host** file and your local dev settings
> (JWT secret, Qdrant, Ollama) are replaced with container defaults. Before
> running the Docker path:
> ```bash
> cp backend/.env backend/.env.mine     # restore with: cp backend/.env.mine backend/.env
> ```
>
> **Apple Silicon:** the published image is `linux/amd64`, so it runs under
> emulation on M-series Macs. It works — verified — but is slower than native.

### 1. Build the UI bundle

Compose bind-mounts `./frontend/build` into nginx read-only, so it must exist
**before** the container starts:

```bash
cd frontend && yarn install && yarn build && cd ..
```

### 2. Choose the image

The repo-root `.env` pins `LAMA_IMAGE=lama:local` with `LAMA_PULL_POLICY=never`,
so compose will **not** pull. Pick one:

```bash
# Option 1 — build the local image (adds git, dulwich, JDK + Maven on top)
docker build -f Dockerfile.local -t lama:local .

# Option 2 — use the public image: comment out both lines in the root .env
#   LAMA_IMAGE=lama:local
#   LAMA_PULL_POLICY=never
docker pull mishramesh/lama:latest
```

### 3. Point it at an LLM provider

The root `.env` defaults to `LAMA_FACTORY_MODE=cli`, which requires the
**`droid`** CLI inside the container. If you do not have it, every LLM call
fails. Edit the repo-root `.env`:

```bash
LAMA_FACTORY_MODE=                 # blank — disable droid CLI mode
LAMA_DEFAULT_MODEL=                # blank — let Console routing decide
# LAMA_DISABLE_OPENROUTER_FALLBACK was removed in 2026-09 — no code has
# read it since iter-14.31 deleted the fallback outright.
# iter-14.31 removed the OpenRouter fallback for every generation path.
```

To reach an Ollama running on the **host**, use Docker's host alias:

```bash
LAMA_OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
```

### 4. Up

```bash
docker compose up -d
docker compose logs -f lama
curl http://127.0.0.1:8382/health          # {"ok":true}
```

Open <http://127.0.0.1:8382>.

### Everyday commands

```bash
docker compose restart lama                 # after backend/*.py edits (bind-mounted)
(cd frontend && yarn build) && docker compose restart lama   # after UI edits
docker compose down                         # stop, keep data volumes
docker compose down -v                      # stop and DELETE Mongo data
docker compose ps                           # status + health
docker compose exec lama bash               # shell inside
```

---

## Scripts

| Script | Does |
|---|---|
| `./scripts/setup.sh` | creates `.venv`, installs backend + frontend deps, writes `backend/.env` |
| `./scripts/doctor.sh` | checks Mongo, Ollama, toolchains, Qdrant, Docker — read-only |
| `./scripts/run-backend.sh [port]` | starts the API with hot reload (default 8000) |
| `./scripts/run-frontend.sh` | starts the UI on :3000 |

## Tests

### Backend — 1,055 tests

```bash
./.venv/bin/python -m pytest backend/tests/ -q
./.venv/bin/python -m pytest backend/tests/test_lama_v4.py -k test_chat -x   # one test
./.venv/bin/python -m pytest backend/tests/test_iter20_chat_stream.py -q      # streaming chat
./.venv/bin/python -m pyflakes backend/routes/codegen.py                      # lint
ruff check backend                                                            # the waste bar
```

### Frontend — 221 tests

Jest 27 (shipped with react-scripts) + React Testing Library 16. No live
server or Mongo needed; everything is stubbed.

```bash
cd frontend
yarn test:ci            # 221 tests, 12 suites — the CI gate
yarn test               # watch mode
yarn test:coverage      # same, with a coverage table
yarn typecheck          # tsc --noEmit — 0 errors expected
yarn lint               # 0 errors, 19 known warnings
```

Run one suite:

```bash
cd frontend
CI=true npx craco test --watchAll=false --testPathPattern="StageProgress"
```

> **`yarn test` needs no backend.** If a suite hangs, you are probably in
> watch mode — use `yarn test:ci`, which passes `--watchAll=false`.

Many suites call a live server and expect one at `REACT_APP_BACKEND_URL`:

```bash
REACT_APP_BACKEND_URL=http://127.0.0.1:8000 ./.venv/bin/python -m pytest backend/tests/ -q
```

Suites written before multi-tenant auth (iter-13.68) call the API without a
bearer token and fail with `401` — that is the **API behaving correctly**, not
a regression.

---

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `KeyError: 'MONGO_URL'` at startup | `backend/.env` missing or not loaded. It must sit next to `server.py`. |
| Server starts, then hangs | MongoDB unreachable — the startup seed is waiting. Start Mongo. |
| `no generation provider is healthy` | No LLM configured. Check `/api/health/providers`; add a provider in Console → Models. |
| LLM returns **empty** content | Thinking model ran out of budget. Raise `max_tokens` to 2000+, or use `qwen2.5-coder:7b`. |
| `401 Unauthorized` | Expected — log in. LAMA is multi-tenant with JWT auth. |
| `toolchain missing on PATH: mvn` | Install the toolchain; a missing binary fails the job by design. |
| CORS / no `Access-Control-Allow-Origin` | `localhost` vs `127.0.0.1` mismatch between UI and `REACT_APP_BACKEND_URL`. |
| Every `/api/*` call returns **504** in dev | The CRA proxy cannot reach the backend. Its default target is now `http://127.0.0.1:8000`; if uvicorn is on another port, set `REACT_APP_API_PROXY=http://127.0.0.1:<port>`. (Before 2026-09 the fallback was `:8382`, the *container* port, so a bare `yarn start` 504'd on every call.) |
| `Something is already running on port 3000` | A previous dev server survived. `lsof -nP -iTCP:3000 -sTCP:LISTEN` then `kill -9 <pid>`. |
| Jest: `Cannot find module 'react-router-dom'` | Stale Jest cache after a dependency change. `cd frontend && npx craco test --clearCache`. |
| `library load disallowed by system policy` | macOS quarantined a downloaded copy. `xattr -dr com.apple.quarantine .` then rebuild `.venv`. |
| `pull access denied for lama:local` | The image isn't built. See [Path B step 2](#2-choose-the-image). |
| Semantic search returns nothing | Qdrant off. Set `QDRANT_PATH` (offline) or `QDRANT_URL`. It degrades silently by design. |

---

## Architecture at a glance

```
                    Path A (dev)                    Path B (docker)
                    ────────────                    ───────────────
  Browser  ──▶  localhost:3000  (CRA dev server)      127.0.0.1:8382
                      │                                     │
                      ▼                                   nginx
               127.0.0.1:8000                         ┌──────┴──────┐
                 FastAPI API  ◀───────────────────▶   │  static UI  │
                      │                               │  FastAPI    │
        ┌─────────────┼─────────────┐                 │  MongoDB    │
        ▼             ▼             ▼                 └─────────────┘
    MongoDB       Ollama        Qdrant                  (one container)
     :27017       :11434     embedded / :6333
   system of      LLM +        semantic search
    record      embeddings      (optional)
```

The 5-stage pipeline — **Discovery → DataModel → Architecture → CodeGen →
Living** — is strictly sequential: each stage refuses to run until the one
before it is frozen.
