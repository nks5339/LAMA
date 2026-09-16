#!/usr/bin/env bash
# Start the LAMA API (FastAPI + uvicorn) with hot reload.
#   ./scripts/run-backend.sh [port]      default port: 8000
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PORT="${1:-8000}"

[ -x .venv/bin/uvicorn ] || { echo "No .venv found — run ./scripts/setup.sh first."; exit 1; }

if ! nc -z 127.0.0.1 27017 >/dev/null 2>&1; then
  echo "WARNING: MongoDB is not reachable on 27017."
  echo "         The app will start but every data route will fail."
  echo "         Start it with: brew services start mongodb-community@8.0"
  echo
fi

echo "API      → http://127.0.0.1:${PORT}/api/health"
echo "Docs     → http://127.0.0.1:${PORT}/docs"
echo "Providers→ http://127.0.0.1:${PORT}/api/health/providers"
echo
# server.py loads backend/.env itself, so run from backend/.
cd backend
exec ../.venv/bin/uvicorn server:app --reload --host 0.0.0.0 --port "$PORT"
