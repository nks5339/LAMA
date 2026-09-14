# ---------------------------------------------------------------
# LAMA — Legacy Application Modernisation AI Studio
# Single-image bundle (frontend + backend + MongoDB + Nginx).
#
# Public port: 8382 (configurable at runtime by remapping)
# Persisted state: /data/db   (mount a host volume for MongoDB)
# External services: Qdrant (HTTP)  — pass QDRANT_URL/QDRANT_API_KEY env-vars
# Push: docker push mishramesh/lama:latest
# ---------------------------------------------------------------

# ===============================================================
# Stage 1 — build React frontend
# ===============================================================
FROM node:24-bookworm-slim AS frontend-build
WORKDIR /build
# Do NOT set NODE_ENV=production here — it would make yarn install skip
# devDependencies (craco, eslint, etc.) and yarn build would then fail with
# "craco: not found". craco/CRA set NODE_ENV=production internally at build time.
ENV DISABLE_ESLINT_PLUGIN=true \
    GENERATE_SOURCEMAP=false \
    CI=false \
    REACT_APP_BACKEND_URL=""

# Cache yarn install layer.
# Note: yarn.lock is optional — the wildcard makes the COPY succeed even
# if the lockfile isn't tracked in git. Commit yarn.lock for reproducible
# CI builds.
COPY frontend/package.json frontend/yarn.lock* ./
RUN corepack enable && \
    if [ -f yarn.lock ]; then \
        yarn install --frozen-lockfile --network-timeout 600000; \
    else \
        echo "[lama] yarn.lock not found — falling back to fresh resolve"; \
        yarn install --network-timeout 600000; \
    fi

# Build (craco internally sets NODE_ENV=production)
COPY frontend/ ./
RUN yarn build

# ===============================================================
# Stage 2 — runtime image (Python + Mongo + Nginx + supervisord)
# ===============================================================
# Python 3.11 is a DELIBERATE choice, not drift. Reviewed 2026-09 against
# moving to 3.14 to match the common macOS dev setup, and rejected:
# `scipy` publishes no cp314 manylinux wheel at either its pinned version
# or the latest, and scipy is a hard transitive dependency of
# sentence-transformers, which confidence_langgraph.py imports. Building
# it from source in a slim image means gfortran + BLAS/LAPACK. Everything
# else in the set resolves for both. 3.11 is supported until Oct 2027.
# Dev/prod parity is instead handled by requirements-dev-macos.txt, which
# documents the four pins that differ and why.
FROM python:3.11-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# Install MongoDB 7, Nginx, supervisord, curl, gnupg
#
# iter 13.8.1 — also pull `freetds-dev` + `libssl-dev` so that pip can
# build `pymssql` from sdist on platforms where a manylinux wheel isn't
# available (the live-DB extractor for MS SQL Server). `oracledb` and
# `psycopg2-binary` + `pymysql` ship pure-python or pre-built wheels so
# no extra system libs are required for those.
#
# iter-13.90 — `git` + `openssh-client` are required by the
# /api/kb/{pid}/clone-git endpoint (the user-facing "git URL" ingest
# path in Discovery → Source Files). If you slim the image and drop
# them, kb.py's `_git_clone()` now transparently falls back to the
# pure-python `dulwich` library (also pinned in requirements.txt) so
# clones still work — just slower and without shell-side credential
# helpers.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release \
        nginx supervisor tzdata \
        freetds-dev libssl-dev gcc \
        git openssh-client \
        unzip apt-transport-https; \
    rm -rf /var/lib/apt/lists/*

# ── Real build toolchains for the CodeGen "Tester" agent ────────────
#
# iter-15.6x — The Tester agent (`_run_compiler` in routes/tools.py)
# materialises the transformed tree into a scratch workspace and
# invokes the SAME build tool the operator picked at step 1 (Maven,
# Gradle, npm/yarn/pnpm, pip/poetry, dotnet, go). Before this change
# NONE of these toolchains existed in the runtime image, so every
# native compile silently fell into the "toolchain missing on PATH"
# skip branch — which `compilation_ready` treated as a pass. That is
# exactly why the in-app Tester showed green while a real
# `mvn clean install` on the downloaded project failed: the Tester was
# never actually compiling anything for Java/.NET/Go targets.
#
# This block installs a real toolchain for every entry in
# `BUILD_TOOL_NATIVE_SUPPORT` so every combination the operator can
# pick actually runs a real build:
#   - Java/Maven/Gradle → default-jdk (OpenJDK 17) + maven + Gradle
#     (official binary distribution — bookworm's apt Gradle is stale)
#   - Node/npm/yarn/pnpm → NodeSource Node 24 (matches frontend-build
#     stage) + corepack (ships yarn/pnpm without extra global installs).
#     Node 20 reached end-of-life on 2026-04-30; 24 is the Active LTS
#     line until 2026-10-20 and is supported to 2028-04-30.
#   - Python/pip/poetry → poetry via pip (python3 is already the base image)
#   - .NET → Microsoft's apt feed, dotnet-sdk-8.0
#   - Go → official upstream tarball (bookworm's golang-go is too old
#     for modern go.mod toolchain directives)
#
# This intentionally trades image size for correctness — see
# memory/PRD.md iter-15.6x for the size delta and rationale.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends default-jdk maven; \
    rm -rf /var/lib/apt/lists/*; \
    mvn -v

ENV GRADLE_VERSION=8.10.2 \
    GRADLE_HOME=/opt/gradle
RUN set -eux; \
    curl -fsSL -o /tmp/gradle.zip "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip"; \
    unzip -q /tmp/gradle.zip -d /opt; \
    ln -s "/opt/gradle-${GRADLE_VERSION}" "$GRADLE_HOME"; \
    rm -f /tmp/gradle.zip
ENV PATH="${GRADLE_HOME}/bin:${PATH}"
RUN gradle -v

RUN set -eux; \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    corepack enable; \
    rm -rf /var/lib/apt/lists/*; \
    node -v && npm -v

RUN set -eux; \
    curl -fsSL -o /tmp/packages-microsoft-prod.deb \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb; \
    dpkg -i /tmp/packages-microsoft-prod.deb; \
    rm -f /tmp/packages-microsoft-prod.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends dotnet-sdk-8.0; \
    rm -rf /var/lib/apt/lists/*; \
    dotnet --version

ENV GO_VERSION=1.23.4
RUN set -eux; \
    curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-$(dpkg --print-architecture).tar.gz"; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm -f /tmp/go.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}"
RUN go version

# ── OPTIONAL: Oracle Instant Client (Thick mode) ────────────────────
# The pure-python `oracledb` driver (pulled by requirements.txt) speaks
# Oracle 12c+ in "Thin" mode out of the box — covers the vast majority
# of live-DB schema-extraction users. If you need:
#   • Oracle 11g (Thin mode requires 12.1+ wire protocol),
#   • Oracle Wallet (mTLS to ATP / Autonomous DB),
#   • OCI net-services (TNS_ADMIN, ezconnect with SCAN),
#   • TLS / SQL*Net advanced security features,
# then uncomment this block to install the free Instant Client. The
# backend calls `oracledb.init_oracle_client()` automatically when it
# detects /opt/oracle/instantclient* on the LD_LIBRARY_PATH.
#
# RUN set -eux; \
#     apt-get update; \
#     apt-get install -y --no-install-recommends libaio1 unzip; \
#     mkdir -p /opt/oracle; \
#     cd /opt/oracle; \
#     curl -fsSLO https://download.oracle.com/otn_software/linux/instantclient/2370000/instantclient-basiclite-linux.x64-23.7.0.25.01.zip; \
#     unzip -q instantclient-basiclite-linux.x64-23.7.0.25.01.zip; \
#     rm instantclient-basiclite-linux.x64-23.7.0.25.01.zip; \
#     ln -sfn /opt/oracle/instantclient_23_7 /opt/oracle/instantclient; \
#     echo /opt/oracle/instantclient > /etc/ld.so.conf.d/oracle-instantclient.conf; \
#     ldconfig; \
#     rm -rf /var/lib/apt/lists/*
# ENV LD_LIBRARY_PATH=/opt/oracle/instantclient:${LD_LIBRARY_PATH}

# MongoDB 7 GA from official repo (Debian 12 / bookworm)
RUN set -eux; \
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends mongodb-org-server mongodb-mongosh; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /data/db /var/log/mongodb /var/log/supervisor; \
    id mongodb >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin mongodb; \
    chown -R mongodb:mongodb /data/db /var/log/mongodb

# ---------- Python backend ----------
WORKDIR /app/backend
COPY backend/requirements.txt ./

# Install CPU-only PyTorch first (saves ~700 MB vs the default CUDA build,
# and drops the triton GPU dep entirely). When pip later processes
# requirements.txt, the existing torch==2.12.0 install satisfies the pin and
# the CUDA wheel is NOT re-pulled.
# `uvicorn[standard]` is now pinned in requirements.txt, so it is NOT
# reinstalled here. It used to be a trailing unpinned `pip install`, which
# silently upgraded past the uvicorn==0.25.0 that requirements.txt had just
# pinned and pulled uvloop/httptools/watchfiles/websockets unpinned too.
#
# poetry is pinned because it is a build tool for the CodeGen Tester agent
# (python/poetry targets), not a LAMA dependency -- it deliberately stays
# out of requirements.txt so it cannot enter the app's resolution graph.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch==2.12.0 && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir 'poetry==2.4.3' && \
    poetry --version && \
    python -c "import uvicorn, fastapi, motor, qdrant_client; print('import smoke ok')"

COPY backend/ /app/backend/

# Drop the dev .env (real values come from -e flags at runtime via entrypoint.sh)
RUN rm -f /app/backend/.env

# ---------- React build output served by nginx ----------
COPY --from=frontend-build /build/build/ /usr/share/nginx/html/

# ---------- Nginx + supervisord configs ----------
COPY docker/nginx.conf      /etc/nginx/nginx.conf
COPY docker/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/entrypoint.sh    /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf || true

# Persist MongoDB data on a host volume
VOLUME ["/data/db"]

# Public port: 8382 (the only port exposed by the image)
EXPOSE 8382

# Healthcheck — passes once Nginx is up and /api/health returns 200
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8382/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
