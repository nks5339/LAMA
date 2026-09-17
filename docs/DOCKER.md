# Running LAMA with Docker only

Everything below runs LAMA — **frontend, backend, MongoDB and Nginx together**
— using nothing but Docker Desktop. You do not need Python, Node, yarn, or
MongoDB installed on your machine.

macOS and Windows are covered separately because three things genuinely
differ: the build platform, the `HOME` variable, and line endings.

> Deploying to a **server** instead? See [`docker/README.md`](../docker/README.md)
> — Hostinger, build-and-push, and the published image.
> Want the local dev path with hot reload? See [`RUNNING.md`](RUNNING.md).

---

## What you get

One container, `lama`, running four processes under supervisord.

| | |
|---|---|
| URL | <http://localhost:8382> |
| Health | <http://localhost:8382/health> → `{"ok":true}` |
| Login | `superadmin` / `lama-admin-2026` (override with `LAMA_SUPERADMIN_USER` / `LAMA_SUPERADMIN_PASS`) |
| Processes | `mongodb`, `backend` (uvicorn), `nginx` |
| Architecture | `linux/amd64` |
| Not bundled | Qdrant — pass `QDRANT_URL` + `QDRANT_API_KEY` if you want semantic search |

---

## Read this first — the one step everyone misses

`docker-compose.yml` mounts your local UI bundle **over** the one baked into
the image:

```yaml
- ./frontend/build:/usr/share/nginx/html:ro
```

`frontend/build/` is gitignored, so on a fresh clone it does not exist.
Docker then creates it as an empty directory, Nginx serves nothing, and you
get a **blank page at :8382 with a healthy container** — which looks like a
broken app rather than a missing bundle.

So either build the bundle (§2 below, no Node required) or remove that line
from `docker-compose.yml` to use the image's own copy.

The same applies to three other mounts. A bind-mount whose source is missing
is created by Docker as a **directory**, even when a *file* was expected —
verified, not assumed:

| Mount source | In a fresh clone | Fix |
|---|---|---|
| `./frontend/build` | missing | build it (§2) or drop the mount |
| `./certs/corp-ca.pem` | missing | `touch` it (§1) — only a real cert if you are behind a TLS-intercepting proxy |
| `./hf_cache` | missing | `mkdir` it — seeds the offline confidence models; empty is fine |
| `${HOME}/.local/bin-linux/droid`, `${HOME}/.factory` | missing unless you use Factory/droid | leave them; Docker creates empty dirs and nothing reads them |

---

# macOS

## 0. Prerequisites

Docker Desktop, running. That is all.

```bash
docker version --format '{{.Server.Version}}'
```

**Apple Silicon (M1–M4):** the image is `linux/amd64` and runs under
emulation. That works. If you *build* it yourself you must say so explicitly
— see §3, and note the known arm64 failure there.

## 1. Clone and create the paths Compose expects

```bash
git clone <your-repo-url> lama
cd lama

mkdir -p certs hf_cache frontend/build
touch certs/corp-ca.pem
```

## 2. Build the UI bundle — inside Docker, no Node on your Mac

```bash
docker run --rm \
  -v "$(pwd)/frontend:/app" -w /app \
  -e CI=true -e DISABLE_ESLINT_PLUGIN=true \
  node:24-bookworm-slim \
  sh -c 'corepack enable && yarn install --frozen-lockfile && yarn build'
```

Writes `frontend/build/`. Takes a few minutes the first time (yarn install);
about 30 s after that, since `node_modules` persists in the mounted folder.

## 3. Get the image

The committed `.env` pins `LAMA_IMAGE=lama:local` and `LAMA_PULL_POLICY=never`,
so Compose expects an image **you built**. Pick one:

**A — pull the published image instead (fastest)**

```bash
LAMA_IMAGE=mishramesh/lama:latest LAMA_PULL_POLICY=always docker compose up -d
```

**B — build the thin local image** (adds `git` + `dulwich` on the published base)

```bash
docker build --platform linux/amd64 -f Dockerfile.local -t lama:local .
```

**C — build the full image from source**

```bash
docker build --platform linux/amd64 -t lama:local .
```

> `--platform linux/amd64` is **required on Apple Silicon**. A native arm64
> build dies at the MongoDB layer with
> `E: Unable to locate package mongodb-org-server`, because MongoDB ships no
> arm64 Debian packages on the `mongodb-org/7.0` channel. The error names the
> package, not the architecture, so it reads as a broken Dockerfile. Every
> other layer builds fine on arm64.

## 4. Configure the LLM provider

Edit `.env` in the repo root:

```bash
OPENROUTER_API_KEY=sk-or-...
LAMA_JWT_SECRET=<any random string, 32+ chars>
```

Using Ollama on your Mac instead of a cloud key? Leave the key empty and add
the provider from **Console → Model Fabric** once the app is up. Compose
already maps `host.docker.internal`, so the container can reach it.

## 5. Start

```bash
docker compose up -d
docker compose logs -f lama          # Ctrl-C to stop following
curl http://127.0.0.1:8382/health
open http://localhost:8382
```

First boot takes ~40 s: it seeds Mongo, the prompt library and the agent
config. The healthcheck allows for that (`start_period: 40s`).

---

# Windows

## 0. Prerequisites

Docker Desktop with the **WSL 2 backend** (Settings → General → *Use the WSL 2
based engine*). Commands below are **PowerShell**.

```powershell
docker version --format '{{.Server.Version}}'
```

## 1. Clone with LF line endings

This matters. Compose bind-mounts a shell script into the container and runs
it with `/bin/sh`:

```yaml
- ./docker/entrypoint-with-wheels.sh:/entrypoint-with-wheels.sh:ro
```

With Git's Windows default (`core.autocrlf=true`) that file lands as CRLF,
the shebang becomes `#!/bin/sh\r`, and the container fails with:

```
exec /entrypoint-with-wheels.sh: no such file or directory
```

— which names the script, not the carriage return. The repo now ships a
`.gitattributes` that forces LF, so a fresh clone is correct. If you cloned
**before** that existed, renormalise:

```powershell
git rm --cached -r .
git reset --hard
```

Then:

```powershell
git clone <your-repo-url> lama
cd lama

New-Item -ItemType Directory -Force -Path certs, hf_cache, frontend\build | Out-Null
New-Item -ItemType File -Force -Path certs\corp-ca.pem | Out-Null
```

## 2. Set `HOME` — Compose needs it and Windows does not have it

`docker-compose.yml` mounts `${HOME}/.local/bin-linux/droid` and
`${HOME}/.factory`. Windows sets `USERPROFILE`, not `HOME`, so Compose warns
*"The HOME variable is not set. Defaulting to a blank string"* and resolves
those to `/.local/bin-linux/droid` — the root of your drive. Verified.

Add this to `.env` in the repo root:

```ini
HOME=C:/Users/<you>
```

Forward slashes, no trailing slash, no quotes. Or, if you do not use
Factory/droid at all, pin the three paths somewhere harmless instead:

```ini
LAMA_DROID_BIN_HOST=./certs/corp-ca.pem
LAMA_DROID_BIN_HOST_DIR=./certs
LAMA_FACTORY_CONFIG_HOST=./certs
```

## 3. Build the UI bundle — inside Docker, no Node on Windows

```powershell
docker run --rm -v "${PWD}/frontend:/app" -w /app -e CI=true -e DISABLE_ESLINT_PLUGIN=true node:24-bookworm-slim sh -c 'corepack enable && yarn install --frozen-lockfile && yarn build'
```

Keep the inner command in **single** quotes — PowerShell passes it through
literally, which is what `sh -c` needs.

## 4. Get the image

The committed `.env` pins `LAMA_IMAGE=lama:local` / `LAMA_PULL_POLICY=never`.

**A — pull the published image (fastest)**

```powershell
$env:LAMA_IMAGE="mishramesh/lama:latest"; $env:LAMA_PULL_POLICY="always"; docker compose up -d
```

**B or C — build locally**

```powershell
docker build -f Dockerfile.local -t lama:local .    # thin
docker build -t lama:local .                        # full
```

Windows hosts are `amd64`, so no `--platform` flag is needed.

## 5. Configure and start

Edit `.env`:

```ini
OPENROUTER_API_KEY=sk-or-...
LAMA_JWT_SECRET=<any random string, 32+ chars>
```

```powershell
docker compose up -d
docker compose logs -f lama
curl.exe http://127.0.0.1:8382/health
Start-Process http://localhost:8382
```

> Use `curl.exe`, not `curl` — in PowerShell, `curl` is an alias for
> `Invoke-WebRequest`, which takes different flags.

---

# Day-to-day — both platforms

## Lifecycle

```bash
docker compose up -d           # start (or apply .env / compose changes)
docker compose stop            # stop, keep containers and data
docker compose start           # start again
docker compose restart lama    # restart in place
docker compose down            # remove the container, KEEP the volumes
docker compose ps              # what is running, and health
```

## Logs

```bash
docker compose logs -f lama                  # everything, following
docker compose logs --tail=200 lama          # last 200 lines
docker compose logs --since=10m lama         # last 10 minutes

# per-process, inside the container (paths are from docker/supervisord.conf)
docker compose exec lama tail -f /var/log/backend.out.log
docker compose exec lama tail -f /var/log/backend.err.log
docker compose exec lama tail -f /var/log/mongodb.err.log
docker compose exec lama tail -f /var/log/nginx.err.log
```

## Applying code changes

`./backend` and `./frontend/build` are bind-mounted, so most edits need no
rebuild:

```bash
# after editing Python under backend/
docker compose restart lama

# after editing React — rebuild the bundle, then restart nginx
docker run --rm -v "$(pwd)/frontend:/app" -w /app -e CI=true -e DISABLE_ESLINT_PLUGIN=true \
  node:24-bookworm-slim sh -c 'corepack enable && yarn build'
docker compose restart lama
```

Only a change to `requirements.txt`, the `Dockerfile`, or the bundled
toolchains needs a real image rebuild.

## Inside the container

```bash
docker compose exec lama bash                     # shell
docker compose exec lama supervisorctl status     # process states
docker compose exec lama supervisorctl restart backend
docker compose exec lama mongosh lama             # the Mongo shell

# which toolchains are present (the compile/verify agents use these)
docker compose exec lama sh -c 'javac -version; dotnet --version; node -v; mvn -v | head -1; go version'
```

The image ships **Temurin 25** and **.NET SDK 10** because
`backend/dcte/stacks.py` pins `spring-boot-4` to Java 25 and `dotnet-10` to
`net10.0`. An older toolchain does not fail cleanly — it turns valid
generated code into syntax errors, which reads as a bad migration rather than
a missing dependency.

## Running the tests in the container

```bash
docker compose exec lama python -m pytest /app/backend/tests/ -q
```

Offline and reproducible: no Mongo, no network, same counts every run. Two
groups are opt-in — `--run-integration` (needs the API on :8382) and
`--run-network` (queries Maven Central).

## Data and volumes

| Volume | Holds | Survives |
|---|---|---|
| `lama_mongo_data` | all projects, KBs, SRS, generated code | `down`, image upgrades |
| `lama_hf_cache` | offline HuggingFace confidence models | `down` |
| `lama_wheels` | LangGraph wheels seeded on first boot | `down` |

```bash
docker volume ls | grep lama
docker compose down -v            # DESTROYS all three — every project is gone
docker volume rm lama_hf_cache    # drop just the model cache; re-seeds on next boot
```

Back up your work before an image upgrade:

```bash
docker compose exec lama mongodump --db lama --archive=/tmp/lama.archive
docker compose cp lama:/tmp/lama.archive ./lama-backup.archive
```

Restore:

```bash
docker compose cp ./lama-backup.archive lama:/tmp/lama.archive
docker compose exec lama mongorestore --archive=/tmp/lama.archive --drop
```

## Direct Transform — let the picker see your code

Direct Transform's folder picker browses the **container's** filesystem, and
`LAMA_DCTE_WORKSPACE` defaults to `$HOME` inside the image. Point it at
something bind-mounted or it will never show your project. Compose already
maps the parent of this repo to `/lama-export`:

```ini
# .env
LAMA_DCTE_WORKSPACE=/lama-export
```

---

# Troubleshooting

**Blank page at :8382, container healthy.** `frontend/build/` is empty. Build
the bundle (§2) and `docker compose restart lama`.

**`pull access denied for lama` / `repository does not exist`.** `.env` pins
`LAMA_IMAGE=lama:local` and you have not built it. Build it, or override with
`LAMA_IMAGE=mishramesh/lama:latest LAMA_PULL_POLICY=always`.

**`E: Unable to locate package mongodb-org-server` during build.** You are
building natively on Apple Silicon. Add `--platform linux/amd64`.

**`exec /entrypoint-with-wheels.sh: no such file or directory`.** CRLF line
endings on a Windows checkout — see Windows §1.

**`The "HOME" variable is not set`.** Windows. Set `HOME` in `.env` — see
Windows §2.

**Port 8382 already in use.** Change the host side only:

```yaml
ports:
  - "9382:8382"
```

**`CERTIFICATE_VERIFY_FAILED` behind a corporate proxy.** Drop your root CA at
`certs/corp-ca.pem` (it is already bind-mounted and trusted by Node). For the
Python side set `LAMA_CA_BUNDLE`, or `LAMA_DISABLE_SSL_VERIFY=true` as a
development-only last resort.

**Everything looks healthy but no LLM call works.** No provider is configured.
Check **Console → Model Fabric**; there are no hard-coded vendor defaults
anywhere, so an unconfigured install raises rather than silently guessing a
model.

**Start over completely.**

```bash
docker compose down -v
docker rmi lama:local
```

---

## Verified

The commands here were run on macOS with Docker 29.7.2, except the Windows
PowerShell syntax, which is derived from the same Compose behaviour reproduced
with `HOME` unset. Specifically confirmed: the Docker-only frontend build
(exit 0, 174 files emitted); that a missing bind-mount source is created as a
*directory*; that an unset `HOME` resolves the droid mounts to `/.local/...`;
that `temurin-25-jdk`, `dotnet-sdk-10.0`, `mongodb-org-server` and
`mongodb-mongosh` all resolve on `linux/amd64`; and that a native arm64 build
fails only at the MongoDB layer.
