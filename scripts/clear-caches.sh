#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# LAMA — clear caches and observability so a demo instance looks fresh
#
#   ./scripts/clear-caches.sh            # show what WOULD be removed
#   ./scripts/clear-caches.sh --apply    # actually remove it
#
# What this DOES remove
#   * Observability rows that accumulate on every run and are never pruned by
#     any existing reset path: llm_traces, token_usage_log, audit_log,
#     codegen_agent_runs, transformer_agent_runs. These carry rows whose
#     project_id is "", null, "smoke" or "TEST_*", so no per-project delete
#     can reach them -- they need an unfiltered delete_many({}).
#   * Local build/tool caches: __pycache__, .pytest_cache, .ruff_cache,
#     frontend/build, and the scratch dirs under /tmp.
#
# What this DELIBERATELY does NOT touch
#   * model_providers -- your Azure/Ollama credentials live here, it is NEVER
#     recreated by seed.py, and llm.py + fabric/model_fabric.py resolve every
#     LLM call through it. Wiping it leaves the app with no routing at all.
#   * projects and every stage artifact (KB, SRS, DataModel, Architecture,
#     CodeGen output). This script is a cache reset, not a factory reset.
#     For a single project use the Console's Factory Reset, or
#     POST /api/projects/{id}/factory-reset.
#   * prompts / agent_configs / tenants / users -- seed.py rebuilds prompts,
#     tenants and users idempotently on boot, but agent_configs is INSERT-ONLY,
#     so wiping it would silently discard Console customisations.
#   * backend/.qdrant_local -- it holds the vectors backing RAG for the
#     projects this script keeps.
#
# Mongo connection is read from backend/.env (MONGO_URL / DB_NAME).
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

if [ "$APPLY" = "0" ]; then
  echo "DRY RUN — nothing will be removed. Re-run with --apply to act."
  echo
fi

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# ── 1. Mongo: observability collections ────────────────────────────────────
"$PY" - "$APPLY" <<'PYEOF'
import os, sys, pathlib
apply = sys.argv[1] == "1"

env = pathlib.Path("backend/.env")
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

url = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")
name = os.environ.get("DB_NAME", "lama")

try:
    from pymongo import MongoClient
except ImportError:
    print("  ! pymongo not installed — skipping the Mongo half")
    sys.exit(0)

PURGE = [
    "llm_traces",
    "token_usage_log",
    "audit_log",
    "codegen_agent_runs",
    "transformer_agent_runs",
]
PRESERVE = ["model_providers", "projects", "prompts", "agent_configs", "tenants", "users"]

try:
    db = MongoClient(url, serverSelectionTimeoutMS=4000)[name]
    db.command("ping")
except Exception as exc:
    print(f"  ! MongoDB unreachable at {url} ({exc}) — skipping the Mongo half")
    sys.exit(0)

print(f"MongoDB {url} / {name}")
total = 0
for c in PURGE:
    n = db[c].count_documents({})
    total += n
    if apply and n:
        db[c].delete_many({})
    print(f"  {'purged ' if apply else 'would purge'} {n:6}  {c}")
print(f"  {'purged' if apply else 'would purge'} {total} observability rows in total")
print("  preserved: " + ", ".join(f"{c}={db[c].count_documents({})}" for c in PRESERVE))
PYEOF

# ── 2. On-disk caches ──────────────────────────────────────────────────────
echo
echo "On-disk caches"
purge_path() {
  local p="$1"
  [ -e "$p" ] || return 0
  local sz
  sz="$(du -sh "$p" 2>/dev/null | cut -f1)"
  if [ "$APPLY" = "1" ]; then
    rm -rf "$p"
    echo "  removed  ${sz:-?}  $p"
  else
    echo "  would remove  ${sz:-?}  $p"
  fi
}

while IFS= read -r d; do purge_path "$d"; done < <(
  find "$ROOT" -name __pycache__ -type d \
       -not -path "*/.venv/*" -not -path "*/node_modules/*" 2>/dev/null
)
purge_path "$ROOT/.pytest_cache"
purge_path "$ROOT/backend/.pytest_cache"
purge_path "$ROOT/.ruff_cache"
purge_path "$ROOT/backend/.ruff_cache"
purge_path "$ROOT/frontend/build"
purge_path "/tmp/lama-factory-cli"
purge_path "/tmp/droid-debug"
purge_path "/tmp/lama_boot.log"
purge_path "/tmp/lama_tok"
purge_path "$ROOT/backend/.env.mine.bak"

echo
echo "Browser state is per-viewer and cannot be cleared from here."
echo "In the running app, Sidebar's \"Refresh App\" clears every lama:* key"
echo "and sessionStorage -- but note it also calls factoryReset on the active"
echo "project, which this script deliberately does not do. To clear only the"
echo "browser half, run in the devtools console:"
echo "  Object.keys(localStorage).filter(k=>k.startsWith('lama:')).forEach(k=>localStorage.removeItem(k)); sessionStorage.clear()"

if [ "$APPLY" = "0" ]; then
  echo
  echo "DRY RUN complete. Re-run with --apply to remove the above."
fi
