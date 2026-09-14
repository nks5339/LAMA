#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# LAMA — environment doctor
#
# Reports what is present, what is missing, and whether each gap actually
# blocks you. Read-only: installs nothing, changes nothing.
#
#   ./scripts/doctor.sh
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'

fail=0
ok()   { printf "  ${GREEN}✓${RESET} %-22s %s\n" "$1" "${2:-}"; }
warn() { printf "  ${YELLOW}!${RESET} %-22s %s\n" "$1" "${2:-}"; }
bad()  { printf "  ${RED}✗${RESET} %-22s %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
head_() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

printf "${BOLD}${BLUE}LAMA environment doctor${RESET}  ${DIM}%s${RESET}\n" "$ROOT"

# ── Required to boot ───────────────────────────────────────────────────────
head_ "Required"

if command -v python3 >/dev/null 2>&1; then
  ok "python3" "$(python3 --version 2>&1)"
else
  bad "python3" "install Python 3.11+ (3.14 supported)"
fi

if [ -x "$ROOT/.venv/bin/python" ]; then
  ok ".venv" "$("$ROOT/.venv/bin/python" --version 2>&1)"
else
  bad ".venv" "missing — run ./scripts/setup.sh"
fi

if nc -z 127.0.0.1 27017 >/dev/null 2>&1; then
  ok "MongoDB" "reachable on 27017"
else
  bad "MongoDB" "NOT reachable on 27017 — brew services start mongodb-community"
fi

if [ -f "$ROOT/backend/.env" ]; then
  ok "backend/.env" "present"
else
  bad "backend/.env" "missing — see docs/RUNNING.md"
fi

if command -v node >/dev/null 2>&1; then
  ok "node" "$(node --version)"
else
  bad "node" "install Node 20+ (the image builds on Node 24 LTS)"
fi

if command -v yarn >/dev/null 2>&1; then
  ok "yarn" "$(yarn --version 2>/dev/null | tail -1)"
else
  bad "yarn" "run: corepack enable  (never use npm install here)"
fi

# ── LLM provider — app serves without one, but generates nothing ───────────
head_ "LLM provider ${DIM}(at least one needed for generation)${RESET}"

if nc -z 127.0.0.1 11434 >/dev/null 2>&1; then
  models=$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | paste -sd, - | cut -c1-52)
  ok "Ollama" "running · ${models:-no models pulled}"
else
  warn "Ollama" "not running — 'ollama serve', or set OPENROUTER_API_KEY"
fi

key=$(grep -E "^OPENROUTER_API_KEY=." "$ROOT/backend/.env" 2>/dev/null | head -1)
[ -n "$key" ] && ok "OpenRouter key" "set" || warn "OpenRouter key" "empty (fine if using Ollama)"

command -v droid >/dev/null 2>&1 \
  && ok "droid CLI" "$(droid --version 2>&1 | head -1)" \
  || warn "droid CLI" "absent — only needed when LAMA_FACTORY_MODE=cli"

# ── Build toolchains — a missing binary FAILS a compile job, never skips ───
head_ "Build toolchains ${DIM}(Tester/Compiler agent)${RESET}"
for pair in "mvn:Maven" "gradle:Gradle" "go:Go" "dotnet:.NET" "npm:npm" "yarn:yarn" "pnpm:pnpm" "poetry:Poetry"; do
  bin="${pair%%:*}"; label="${pair##*:}"
  command -v "$bin" >/dev/null 2>&1 && ok "$label" "$(command -v "$bin")" || warn "$label" "missing — see docs/RUNNING.md"
done
command -v java >/dev/null 2>&1 && ok "Java" "$(java -version 2>&1 | head -1)" || warn "Java" "missing (needed with Maven/Gradle)"

# ── Optional ───────────────────────────────────────────────────────────────
head_ "Optional"
qpath=$(grep -E "^QDRANT_PATH=." "$ROOT/backend/.env" 2>/dev/null | cut -d= -f2-)
qurl=$(grep -E "^QDRANT_URL=." "$ROOT/backend/.env" 2>/dev/null | cut -d= -f2-)
if [ -n "$qurl" ]; then ok "Qdrant" "online → $qurl"
elif [ -n "$qpath" ]; then ok "Qdrant" "offline (embedded) → $qpath"
else warn "Qdrant" "disabled — semantic search returns empty, no crash"; fi

docker info >/dev/null 2>&1 && ok "Docker" "daemon running" || warn "Docker" "daemon stopped (only needed for the Docker path)"

printf "\n"
if [ "$fail" -eq 0 ]; then
  printf "${GREEN}${BOLD}All required checks passed.${RESET}  Start with: ${BOLD}./scripts/run-backend.sh${RESET}\n\n"
else
  printf "${RED}${BOLD}%d required check(s) failed.${RESET}  See docs/RUNNING.md\n\n" "$fail"
  exit 1
fi
