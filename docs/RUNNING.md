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
| **MongoDB 7/8** | system of record — **required** | `brew tap mongodb/brew && brew install mongodb-community` |
| **Node 18+** | frontend | `brew install node` |
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
> `requirements.txt` is a `pip freeze` taken on a Linux CUDA machine: it pins
> 19 `nvidia-*` / `cuda-*` packages that have no macOS wheels, so it cannot
> install on a Mac. The `-dev-macos` file installs the same application at the
> same versions wherever Python 3.14 allows. **`requirements.txt` remains the
> source of truth for the Docker image** — do not point the container at the
> dev file.

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
brew services start mongodb-community     # persistent
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
REACT_APP_BACKEND_URL=http://127.0.0.1:8000 yarn start
```

Or: **`./scripts/run-backend.sh`** and **`./scripts/run-frontend.sh`**.

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
# LAMA_DISABLE_OPENROUTER_FALLBACK has no effect -- no code reads it.
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

```bash
./.venv/bin/python -m pytest backend/tests/ -q
./.venv/bin/python -m pytest backend/tests/test_lama_v4.py -k test_chat -x   # one test
./.venv/bin/python -m pyflakes backend/routes/codegen.py                      # lint
```

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
