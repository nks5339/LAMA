#!/usr/bin/env bash
# Start the LAMA UI (React 19 + CRACO dev server) on :3000.
#   ./scripts/run-frontend.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../frontend"

command -v yarn >/dev/null 2>&1 || { echo "yarn missing — run: corepack enable"; exit 1; }
[ -d node_modules ] || yarn install

# Point the SPA at the local API. Use 127.0.0.1 consistently: browsers treat
# localhost and 127.0.0.1 as different origins and CORS will block the mix.
export REACT_APP_BACKEND_URL="${REACT_APP_BACKEND_URL:-http://127.0.0.1:8000}"
echo "UI  → http://localhost:3000"
echo "API → $REACT_APP_BACKEND_URL"
echo
exec yarn start
