#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# LAMA — one-shot local setup (macOS / Linux)
#
# Creates .venv, installs backend + frontend dependencies, and writes a
# starter backend/.env. Safe to re-run: it never overwrites an existing
# backend/.env and reuses an existing .venv.
#
#   ./scripts/setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'; GREEN=$'\033[32m'; BLUE=$'\033[34m'
step() { printf "\n${BOLD}${BLUE}==>${RESET} ${BOLD}%s${RESET}\n" "$1"; }
info() { printf "    ${DIM}%s${RESET}\n" "$1"; }

printf "${BOLD}LAMA local setup${RESET}  ${DIM}%s${RESET}\n" "$ROOT"

# ── 1. Python virtual environment ──────────────────────────────────────────
step "Creating .venv"
if [ -x ".venv/bin/python" ]; then
  info "reusing existing .venv ($(.venv/bin/python --version 2>&1))"
else
  python3 -m venv .venv
  info "created with $(.venv/bin/python --version 2>&1)"
fi
.venv/bin/python -m pip install --quiet --upgrade pip

# ── 2. Backend dependencies ────────────────────────────────────────────────
# requirements.txt is a pip-freeze from a Linux CUDA box (nvidia-*/cuda-*
# pins have no macOS wheels). requirements-dev-macos.txt is the portable
# local set — same versions wherever Python 3.14 allows.
step "Installing backend dependencies"
REQ="backend/requirements-dev-macos.txt"
[ -f "$REQ" ] || REQ="backend/requirements.txt"
info "from $REQ"
.venv/bin/python -m pip install --quiet -r "$REQ"
info "done"

# ── 3. backend/.env ────────────────────────────────────────────────────────
step "Configuring backend/.env"
if [ -f backend/.env ]; then
  info "already exists — left untouched"
else
  SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > backend/.env <<EOF
# Local dev environment (loaded by server.py). GITIGNORED.
MONGO_URL=mongodb://127.0.0.1:27017
DB_NAME=lama

LAMA_JWT_SECRET=$SECRET

# --- LLM ---------------------------------------------------------------
# Local Ollama is the default. Configure per-tier models in the UI under
# Console -> Models. Leave LAMA_DEFAULT_MODEL EMPTY so per-agent tier
# routing applies (a value here overrides routing for EVERY call).
LAMA_OLLAMA_BASE_URL=http://localhost:11434/v1
LAMA_DEFAULT_MODEL=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Pin the confidence engine explicitly: auto-mode would switch to LangGraph
# merely because the package is importable.
LAMA_CONFIDENCE_ENGINE=fabric

# --- Vector store ------------------------------------------------------
# Offline by default: embedded Qdrant on disk, no server, no Docker.
# Set QDRANT_URL to use a real server instead (URL wins over PATH).
QDRANT_PATH=./.qdrant_local
QDRANT_URL=
QDRANT_API_KEY=
LAMA_EMBED_BACKEND=ollama
LAMA_OLLAMA_EMBED_MODEL=nomic-embed-text
EOF
  info "written with a freshly generated LAMA_JWT_SECRET"
fi

# ── 4. Frontend ────────────────────────────────────────────────────────────
step "Installing frontend dependencies"
if ! command -v yarn >/dev/null 2>&1; then
  info "yarn missing — enabling via corepack (package.json pins yarn 1.22.22)"
  corepack enable 2>/dev/null || corepack enable --install-directory "$HOME/.local/bin" yarn 2>/dev/null || true
fi
if command -v yarn >/dev/null 2>&1; then
  (cd frontend && yarn install --silent)
  info "done ($(yarn --version 2>/dev/null | tail -1))"
else
  info "SKIPPED — could not provision yarn. Run 'corepack enable' then 'cd frontend && yarn install'."
  info "Never use 'npm install' here; the lockfile is yarn's."
fi

# ── 5. Verify ──────────────────────────────────────────────────────────────
step "Verifying the backend imports"
( cd backend && MONGO_URL="mongodb://127.0.0.1:27017" DB_NAME=lama ../.venv/bin/python -c "
import server
n = len([p for p in server.app.openapi()['paths'] if p.startswith('/api')])
print(f'    API routes registered: {n}')
" )

printf "\n${GREEN}${BOLD}Setup complete.${RESET}\n"
printf "  1. ${BOLD}./scripts/doctor.sh${RESET}      check MongoDB / Ollama / toolchains\n"
printf "  2. ${BOLD}./scripts/run-backend.sh${RESET} start the API   → http://127.0.0.1:8000/api/health\n"
printf "  3. ${BOLD}./scripts/run-frontend.sh${RESET} start the UI   → http://localhost:3000\n\n"
printf "  Full guide: ${BOLD}docs/RUNNING.md${RESET}\n\n"
