# LAMA — Legacy Application Modernization & Alignment

An AI-assisted **legacy → cloud-native migration studio**. Point it at a folder of
legacy code (PHP/CodeIgniter, JSP, .NET, Python, JS…) and it walks the application
through a deterministic **5-stage pipeline**, producing freezable, GitHub-pushable
artifacts at each stage.

| # | Stage | Output |
|---|---|---|
| 1 | **Discovery** | Knowledge Base, IEEE-830 SRS, Business Ontology |
| 2 | **DataModel** | OLTP + OLAP DDL, Bus Matrix, migration scripts |
| 3 | **Architecture** | Service Map, HLD, LLD, API contracts, sequence diagrams |
| 4 | **CodeGen** | Per-service source tree, Dockerfiles, ZIP, GitHub push |
| 5 | **Living** | Selenium tests, SRS-drift detection, observability |

Stages are strictly sequential — each refuses to run until the previous one is frozen.

---

## Quick start

```bash
git clone <your-repo-url> lama
cd lama

./scripts/setup.sh        # creates .venv, installs everything, writes backend/.env
./scripts/doctor.sh       # verifies MongoDB, Ollama, build toolchains
./scripts/run-backend.sh  # API → http://127.0.0.1:8000/api/health
./scripts/run-frontend.sh # UI  → http://localhost:3000
```

Prefer Docker? Everything runs in one container on `:8382`:

```bash
(cd frontend && yarn build)      # compose bind-mounts ./frontend/build
docker compose up -d
curl http://127.0.0.1:8382/health
```

> **Before the Docker path, back up `backend/.env`** — the container's entrypoint
> writes through the bind-mount and replaces it:
> `cp backend/.env backend/.env.mine`

### 📘 Full guides

**[docs/DOCKER.md](docs/DOCKER.md)** — **Docker-only** setup, macOS and Windows
separately. No Python, Node or MongoDB needed on your machine; includes the
one-liner that builds the UI bundle in a container.

**[docs/RUNNING.md](docs/RUNNING.md)** — every command, both paths, troubleshooting.
Also available as a formatted PDF: **[docs/LAMA-Setup-Guide.pdf](docs/LAMA-Setup-Guide.pdf)**
(rebuild it with `./scripts/build-docs-pdf.sh`).

---

## Requirements at a glance

| Required | Optional but recommended |
|---|---|
| Python 3.11+ (3.14 works) | Maven · Gradle · Go — CodeGen / Direct Transform compile agents |
| MongoDB 7/8 — system of record | **JDK 25** — the `spring-boot-4` target emits Java 25; JDK 17 cannot compile it (image ships Temurin 25) |
| Node 20+ with yarn 1.22 (`corepack enable`) — image builds on Node 24 LTS | **.NET SDK 10** — the `dotnet-10` target emits `net10.0`; SDK 8 cannot build it |
| An LLM provider — Ollama or an OpenRouter key | Qdrant — semantic search (embedded mode needs no server) · Docker Desktop — for the container path |

Only the compile/verify agents need the build toolchains; the pipeline itself
runs without them. `doctor.sh` checks the JDK and .NET **versions**, not just
their presence, because a too-old toolchain fails in a way that looks like a
bad migration rather than a missing dependency.

`./scripts/doctor.sh` tells you exactly which of these are missing and whether it
actually blocks you.

---

## Manifest

| Path | What |
|---|---|
| `backend/` | FastAPI app — 20 routers, 59 collections, `server.py` is the entrypoint |
| `frontend/` | React 19 + CRACO + Tailwind + shadcn/ui |
| `scripts/` | `setup.sh` · `doctor.sh` · `run-backend.sh` · `run-frontend.sh` · `build-docs-pdf.sh` |
| `docs/` | `DOCKER.md` (Docker-only, Mac + Windows), `RUNNING.md`, the setup PDF, `ARCHITECTURE.md`, `USER_MANUAL.md` |
| `backend/requirements.txt` | **Container** dependency set — direct deps only, pinned for Linux / Python 3.11 |
| `backend/requirements-dev-macos.txt` | **Local** dependency set — portable, no GPU/ML stack |
| `CLAUDE.md` / `AGENTS.md` | Architectural contracts and rules of engagement |
| `memory/PRD.md` | Append-only iteration log — the "why" behind every decision |

## Tests

```bash
./.venv/bin/python -m pytest backend/tests/ -q
```

A bare run is **fully offline and reproducible** — no Mongo, no Qdrant, no
network, and the same pass/skip counts on every run. Two opt-in groups are
skipped by default, with the reason printed by `-rs`:

```bash
# the eight legacy suites that drive the API over HTTP (start uvicorn first)
./.venv/bin/python -m pytest backend/tests/ --run-integration

# the two tests that query Maven Central over the public internet
./.venv/bin/python -m pytest backend/tests/ --run-network
```

Both are real coverage and are gated, not deleted — see the reasoning in
`backend/tests/conftest.py`. The network pair can fail when Maven Central is
slow; that is a property of the endpoint, which is why it is not in the
default run.
