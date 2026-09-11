#!/usr/bin/env bash
# Container entrypoint for LAMA single-image bundle.
# - Ensures /data/db exists (MongoDB needs it as a volume)
# - Writes a runtime backend .env from env-vars passed at `docker run -e ...`
# - Hands control to supervisord which runs mongo + backend + nginx

set -e

DATA_DIR="${MONGO_DATA_DIR:-/data/db}"
mkdir -p "$DATA_DIR" /var/log/supervisor
chown -R mongodb:mongodb "$DATA_DIR" || true

# ---- iter 13.8.1 / iter-13.95 — self-heal live-DB + DWH drivers ----
# `mishramesh/lama:latest` images built before iter 13.8.1 didn't bake in
# the enterprise DB drivers. iter-13.95 widens the self-heal to also cover
# the common data-warehouse targets users land on after the OLTP discovery
# stage (Snowflake / Redshift / BigQuery / ClickHouse). Pip is a no-op
# when the package is already present, so the cost on subsequent boots is
# essentially zero (just import checks).
#
# Opt out per-driver by setting LAMA_SKIP_DB_DRIVER_INSTALL=1 (skips all)
# or by removing the ensure_pkg line. Failures degrade gracefully — the
# /api/kb/{pid}/db-connect endpoint surfaces a remediation message
# including the exact pip command to run inside the container.
ensure_pkg() {
    python -c "import $1" 2>/dev/null && return 0
    echo "[lama] installing missing DB driver: $2"
    pip install --no-cache-dir --quiet "$2" || \
        echo "[lama] WARN: pip install $2 failed (live-DB extraction for this driver will fall back to descriptor-only)"
}
if [ "${LAMA_SKIP_DB_DRIVER_INSTALL:-0}" != "1" ]; then
    # ── OLTP / classic enterprise (iter 13.8.1) ──────────────────────
    ensure_pkg oracledb       "oracledb==2.4.1"
    ensure_pkg psycopg2       "psycopg2-binary==2.9.9"
    ensure_pkg pymysql        "pymysql==1.1.1"
    ensure_pkg pymssql        "pymssql==2.3.0"
    # ── Data-warehouse / OLAP (iter-13.95) ───────────────────────────
    # All pure-python wheels except clickhouse-driver (has manylinux
    # wheels for x86_64+arm64, no system deps). google-cloud-bigquery
    # is heavy (~30 MB) — gate behind env-var.
    ensure_pkg snowflake.connector  "snowflake-connector-python>=4.6.0"
    ensure_pkg clickhouse_driver    "clickhouse-driver==0.2.9"
    ensure_pkg redshift_connector   "redshift_connector==2.1.5"
    ensure_pkg ibm_db               "ibm_db==3.2.4"  || true  # IBM Db2 — needs CLI driver on some hosts; optional
    if [ "${LAMA_INSTALL_BIGQUERY:-0}" = "1" ]; then
        ensure_pkg google.cloud.bigquery "google-cloud-bigquery==3.27.0"
    fi
fi

# ---- iter-14.17.2 — LangGraph + HuggingFace confidence engine deps ----
# The pre-baked `mishramesh/lama:latest` image (built before iter-14.14)
# doesn't include langgraph / sentence-transformers. Install them here
# so `LAMA_CONFIDENCE_ENGINE=langgraph` actually reaches the LangGraph
# code path instead of silently falling back to the legacy fabric
# evaluator (which then hangs on the Factory-CLI droid for 60-120s
# per section and caps confidence at 90%).
#
# Opt out with LAMA_SKIP_LANGGRAPH_INSTALL=1 if you rebuild the image
# with these already baked in.
if [ "${LAMA_SKIP_LANGGRAPH_INSTALL:-0}" != "1" ]; then
    ensure_pkg langgraph              "langgraph==0.2.60"
    ensure_pkg langchain_core         "langchain-core==0.3.29"
    ensure_pkg sentence_transformers  "sentence-transformers"
fi

# ---- iter-14.17.2 — Corporate-CA SSL bundle for HuggingFace Hub ----
# `httpx` (used by huggingface_hub) honours SSL_CERT_FILE; requests
# honours REQUESTS_CA_BUNDLE. Point both at a combined bundle so
# first-time HF model downloads succeed inside corporate networks.
# If no corp CA file exists, the bundle is just the system CAs — this
# is a no-op on cloud hosts. Overridden by explicit SSL_CERT_FILE env.
CA_BUNDLE_OUT="/tmp/full-ca.pem"
if [ ! -f "$CA_BUNDLE_OUT" ]; then
    if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
        cat /etc/ssl/certs/ca-certificates.crt > "$CA_BUNDLE_OUT"
    fi
    for corp in /usr/local/share/ca-certificates/corp-ca.crt \
                /usr/local/share/ca-certificates/*.crt; do
        [ -f "$corp" ] && cat "$corp" >> "$CA_BUNDLE_OUT" 2>/dev/null || true
    done
fi
echo "[lama] CA bundle at $CA_BUNDLE_OUT ($(wc -c < "$CA_BUNDLE_OUT" 2>/dev/null || echo 0) bytes)"

# ---- Runtime .env for the FastAPI backend ----
cat > /app/backend/.env <<EOF
MONGO_URL=${MONGO_URL:-mongodb://127.0.0.1:27017}
DB_NAME=${DB_NAME:-lama}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
OPENROUTER_BASE_URL=${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}
QDRANT_URL=${QDRANT_URL:-}
QDRANT_API_KEY=${QDRANT_API_KEY:-}
# iter-13.30 — No hard-coded vendor default. When unset, Console-configured
# providers + AGENT_COMPLEXITY drive model selection per agent_key. Set this
# env-var only if you want a legacy-OpenRouter fallback when Console has no
# active providers (e.g. air-gapped boot before the operator logs in).
LAMA_DEFAULT_MODEL=${LAMA_DEFAULT_MODEL:-}
EOF

echo "[lama] starting bundle (mongo + backend + nginx) on port 8382"
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
