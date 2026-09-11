#!/bin/sh
# iter-14.26 / 14.27 — Compose-level entrypoint wrapper.
#
# Two jobs:
#   1. Seed the named volumes lama_hf_cache and lama_wheels from the
#      read-only host bind-mounts at /seed/hf_cache and /seed/wheels
#      (only when the volumes are empty — first boot after a fresh
#      `docker volume rm` or a brand-new install). Subsequent restarts
#      re-use whatever's already in the volume, so container recreates
#      no longer need any host filesystem interaction to hydrate HF
#      models or the langgraph wheels.
#
#   2. `pip install --no-index --find-links=/wheels langgraph
#      langchain-core` if the modules aren't importable in the current
#      site-packages. Needed because the baked runtime image predates
#      the requirements.txt pin for langgraph==0.2.60 (iter-14.14/15),
#      and container recreates wipe any manual `pip install`.
#
# Once the image is rebuilt from the current requirements.txt AND
# ships the HF encoders in /root/.cache/huggingface, both jobs become
# no-ops and this wrapper can be dropped from docker-compose.yml.
#
# The final `exec /entrypoint.sh` hands off to the baked LAMA
# entrypoint which starts supervisord (mongod + nginx + backend).
set -e

# --- Job 1a: seed HF cache from host (first-boot only) --------------
if [ -d /root/.cache/huggingface ] && [ -d /seed/hf_cache ]; then
    if [ -z "$(ls -A /root/.cache/huggingface 2>/dev/null)" ]; then
        echo "[lama-boot] seeding lama_hf_cache from /seed/hf_cache …"
        cp -a /seed/hf_cache/. /root/.cache/huggingface/ 2>&1 | tail -3 || {
            echo "[lama-boot] WARNING: HF cache seed failed — first HF call may hit the network."
        }
        echo "[lama-boot] HF cache seed complete ($(du -sh /root/.cache/huggingface 2>/dev/null | cut -f1))."
    else
        echo "[lama-boot] lama_hf_cache already populated — skipping seed."
    fi
fi

# --- Job 1b: seed wheels volume from host (first-boot only) ---------
if [ -d /wheels ] && [ -d /seed/wheels ]; then
    if [ -z "$(ls -A /wheels 2>/dev/null)" ]; then
        echo "[lama-boot] seeding lama_wheels from /seed/wheels …"
        cp -a /seed/wheels/. /wheels/ 2>&1 | tail -3 || {
            echo "[lama-boot] WARNING: wheels seed failed — langgraph install will be skipped."
        }
        echo "[lama-boot] wheels seed complete ($(ls /wheels 2>/dev/null | wc -l) files)."
    else
        echo "[lama-boot] lama_wheels already populated — skipping seed."
    fi
fi

# --- Job 2: install langgraph from /wheels if missing ---------------
if [ -d /wheels ] && ls /wheels/langgraph-*.whl >/dev/null 2>&1; then
    if ! python -c 'import langgraph, langchain_core' >/dev/null 2>&1; then
        echo "[lama-boot] installing langgraph + langchain-core from /wheels…"
        pip install --no-cache-dir --no-index --find-links=/wheels \
            langgraph langchain-core 2>&1 | tail -3 || {
                echo "[lama-boot] WARNING: wheel install failed — confidence engine may fall back to strict-HF stub."
            }
    else
        echo "[lama-boot] langgraph already importable — skipping wheel install."
    fi
else
    echo "[lama-boot] /wheels not populated — skipping wheel install."
fi

# ---- iter-14.28 — Build /tmp/full-ca.pem so httpx trusts the corp CA.
# The baked /entrypoint.sh in older images is missing this logic, so
# REQUESTS_CA_BUNDLE=/tmp/full-ca.pem ends up pointing at a missing file
# and every https://ollama.com / https://api.anthropic.com call fails
# with CERTIFICATE_VERIFY_FAILED behind a Zscaler / Netskope MITM.
CA_BUNDLE_OUT="/tmp/full-ca.pem"
if [ ! -s "$CA_BUNDLE_OUT" ]; then
    [ -f /etc/ssl/certs/ca-certificates.crt ] && cat /etc/ssl/certs/ca-certificates.crt > "$CA_BUNDLE_OUT"
    for corp in /usr/local/share/ca-certificates/corp-ca.crt \
                /usr/local/share/ca-certificates/*.crt; do
        [ -f "$corp" ] && cat "$corp" >> "$CA_BUNDLE_OUT" 2>/dev/null || true
    done
fi
echo "[lama-boot] CA bundle at $CA_BUNDLE_OUT ($(wc -c < "$CA_BUNDLE_OUT" 2>/dev/null || echo 0) bytes)"
# Point every Python HTTP client + LAMA's _http_verify() at the combined
# bundle so no client falls back to the system store without corp-ca.
export LAMA_CA_BUNDLE="$CA_BUNDLE_OUT"
export REQUESTS_CA_BUNDLE="$CA_BUNDLE_OUT"
export SSL_CERT_FILE="$CA_BUNDLE_OUT"
export CURL_CA_BUNDLE="$CA_BUNDLE_OUT"

# ---- iter-14.56 — Ensure OLTP DB drivers are importable BEFORE handing
# control to the baked /entrypoint.sh. The pre-built `mishramesh/lama:latest`
# image was cut before docker/entrypoint.sh grew the `ensure_pkg` block for
# oracledb / psycopg2 / pymysql / pymssql (iter 13.8.1), so those drivers
# never actually land in the container on first boot. Without them the
# live-DB re-ingest endpoint silently returns `entities_ingested: 0` with
# `last_extract_error: "Driver not installed: ..."`, which then breaks the
# deterministic OLTP / OLAP / Migration paths. Self-heal here so operators
# don't need to `docker exec ... pip install` manually every rebuild.
# Opt out with LAMA_SKIP_DB_DRIVER_INSTALL=1.
if [ "${LAMA_SKIP_DB_DRIVER_INSTALL:-0}" != "1" ]; then
    for pair in \
        "oracledb=oracledb==2.4.1" \
        "psycopg2=psycopg2-binary==2.9.9" \
        "pymysql=pymysql==1.1.1" \
        "pymssql=pymssql==2.3.0"; do
        mod="${pair%%=*}"
        spec="${pair#*=}"
        if ! python -c "import $mod" >/dev/null 2>&1; then
            echo "[lama-boot] installing missing DB driver: $spec"
            pip install --no-cache-dir --quiet "$spec" 2>&1 | tail -2 || \
                echo "[lama-boot] WARN: pip install $spec failed — live-DB re-ingest for $mod will report Driver-not-installed."
        fi
    done
fi

exec /entrypoint.sh "$@"
