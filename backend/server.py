"""LAMA — Legacy Application Modernisation AI Studio — FastAPI entrypoint."""
from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from pathlib import Path
import os
import asyncio
import logging

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# Import after dotenv is loaded
from db import client, ensure_indexes  # noqa: E402
from routes.projects import router as projects_router  # noqa: E402
from routes.kb import router as kb_router  # noqa: E402
from routes.db_ingest import router as db_ingest_router  # noqa: E402  (iter 13.8)
from routes.chat import router as chat_router  # noqa: E402
from routes.srs import router as srs_router  # noqa: E402
from routes.prompts import router as prompts_router  # noqa: E402
from routes.github import router as github_router  # noqa: E402
from routes.audit import router as audit_router  # noqa: E402
from routes.datamodel import router as datamodel_router, factory_router as factory_router  # noqa: E402
from routes.architecture import router as architecture_router  # noqa: E402
from routes.codegen import router as codegen_router  # noqa: E402
from routes.living import router as living_router  # noqa: E402
from routes.console import router as console_router  # noqa: E402
from routes.integrations import router as integrations_router  # noqa: E402  (iter-13.60)
from routes.pipeline import router as pipeline_router  # noqa: E402  (iter-13.66 — skip stages)
from routes.auth import router as auth_router  # noqa: E402  (iter-13.68 — multi-tenant)
from routes.admin import router as admin_router  # noqa: E402  (iter-13.68 — super-admin)
from routes.context import router as context_router  # noqa: E402  (iter-13.91.16 — host-anchored context bundler)
from routes.sessions import router as sessions_router  # noqa: E402  (iter-13.100 — rolling-memory agent sessions)
from routes.tools import router as tools_router  # noqa: E402  (Tools — Gap Analyzer + Transformer)
from routes.dcte import router as dcte_router  # noqa: E402  (Tools — Direct Transform / DCTE)
from seed import run_seed  # noqa: E402
from log_tail import install_log_tail_handler  # noqa: E402  (iter-14.17 — live log tail)

# iter-14.17 — install the ring-buffer handler on the ROOT logger as
# early as possible so every subsequent import can contribute records.
# Idempotent + does NOT displace existing handlers (uvicorn / supervisord
# stdout logging still works).
install_log_tail_handler()


app = FastAPI(title="LAMA API", version="0.2.0")
api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "LAMA", "status": "ok"}


@api_router.get("/health")
async def health():
    # `srs_version` is the iteration marker. Bump it whenever shipping a new
    # SRS pipeline change so users can verify which code is actually running
    # in their container. Visible at `GET /api/health` or `GET /health`.
    return {
        "ok": True,
        "srs_version": "iter-13.16.1",
        "features": {
            "background_job": True,
            "auto_resume_on_mount": True,
            "heartbeat_data_events": True,
            "listener_cleanup_on_disconnect": True,
            "section_repair_pass": True,
            "re_module_imported": True,  # iter-13.16.1 — the _re NameError fix
        },
    }


@api_router.get("/health/providers")
async def health_providers(project_id: str = ""):
    """iter-14.32 — Health-probe every generation provider in priority
    order (Factory.ai → Anthropic → OpenAI → Groq → Ollama → env-Ollama)
    and return the same report `fabric_call` uses at request time.

    Use this from the Console page to see WHY a generation call is
    failing without shelling into the container. `project_id` is
    optional; without it Factory.ai enablement is judged from the
    global env only.
    """
    from llm import probe_generation_providers
    from confidence_langgraph import resolve_confidence_engine, is_available as _hf_ok
    _report = await probe_generation_providers(project_id)
    return {
        "generation": _report,
        "confidence": {
            "engine": resolve_confidence_engine(),
            "langgraph_hf_ready": bool(_hf_ok()),
        },
    }


# Register sub-routers under /api
api_router.include_router(projects_router)
api_router.include_router(kb_router)
api_router.include_router(db_ingest_router)   # iter 13.8 — DB + app-URL ingestion
api_router.include_router(chat_router)
api_router.include_router(srs_router)
api_router.include_router(prompts_router)
api_router.include_router(github_router)
api_router.include_router(audit_router)
api_router.include_router(datamodel_router)
api_router.include_router(factory_router)
api_router.include_router(architecture_router)
api_router.include_router(codegen_router)
api_router.include_router(living_router)
api_router.include_router(console_router)
api_router.include_router(integrations_router)
api_router.include_router(pipeline_router)   # iter-13.66 — skip / unskip intermediate stages
api_router.include_router(auth_router)        # iter-13.68 — login / me / logout
api_router.include_router(admin_router)       # iter-13.68 — super-admin tenants/users/dashboard
api_router.include_router(context_router)     # iter-13.91.16 — host-anchored context bundle preview
api_router.include_router(sessions_router)    # iter-13.100 — rolling-memory agent sessions
api_router.include_router(tools_router)       # Tools — Gap Analyzer + Transformer (bypass pipeline)
api_router.include_router(dcte_router)        # Tools — Direct Transform / DCTE (bypass pipeline)

app.include_router(api_router)

# iter-16.x — CORS. Two subtle rules of the CORS spec bit us in prod:
#   1. `Access-Control-Allow-Origin: *` is invalid when
#      `Access-Control-Allow-Credentials: true`. Starlette detects this
#      and silently drops the ACAO header entirely, producing exactly the
#      "No 'Access-Control-Allow-Origin' header is present" error the
#      browser reports — even though the operator "set CORS_ORIGINS=*".
#   2. Browsers treat `http://localhost:8382` and `http://127.0.0.1:8382`
#      as DIFFERENT origins, so an absolute `REACT_APP_BACKEND_URL`
#      pointing at 127.0.0.1 while the SPA is loaded from localhost (or
#      vice-versa) will be preflight-blocked. The FE now defaults to
#      relative `/api` (same-origin), so this only matters for split
#      dev / cross-host deploys — but when it DOES matter, "*" needs to
#      keep working. Use `allow_origin_regex=".*"` (with credentials
#      still on) so every requesting origin is echoed back into ACAO
#      individually, which the browser accepts. Explicit env-var
#      overrides still win.
_configured_origins = os.environ.get("CORS_ORIGINS", "*").split(",")
_configured_origins = [o.strip() for o in _configured_origins if o.strip()]
_wildcard_cors = (not _configured_origins) or ("*" in _configured_origins)
if _wildcard_cors:
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origin_regex=".*",
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origins=_configured_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("lama")


@app.on_event("startup")
async def on_startup():
    # iter-13.99 — emit a hard-to-miss banner so the operator can
    # confirm from `docker compose logs lama` which sanitizer
    # generation is actually running. Without this banner, leak-fix
    # regressions reported by users were repeatedly caused by stale
    # containers running pre-fix code.
    logger.warning(
        "════════════════════════════════════════════════════════════════\n"
        "  LAMA backend booting — iter-13.99 ACTIVE\n"
        "  Project-isolated workspace:   /lama-workspaces/{tid}__{pid}/\n"
        "  Cross-project path guard:     HARD prefix enforcement + suffix\n"
        "  Output sanitizer location:    routes/srs.py::_gen_one_section\n"
        "  Verify per-call activity:     `docker logs lama | grep iter-13.99`\n"
        "════════════════════════════════════════════════════════════════"
    )
    try:
        await run_seed()
        logger.info("Seed complete")
    except Exception as e:
        logger.exception(f"Seed failed: {e}")

    # iter-13.120 — Create MongoDB indexes on hot-path collections.
    # Without these, every project_id filter (i.e. almost every query in
    # the app) does a full COLLSCAN. delete_project + project listing +
    # sidebar pipeline status were all bottlenecked on this.
    try:
        await ensure_indexes()
        logger.info("Index ensure complete")
    except Exception as e:
        logger.exception(f"Index ensure failed: {e}")

    # iter-14.8 — Enlarge the default ThreadPoolExecutor used by
    # `asyncio.to_thread`. The stdlib default is `min(32, cpu+4)`, which
    # on a small container is 5-8 threads. Our KB ingest fans out
    # `LAMA_INGEST_CONCURRENCY` (default 12) parallel workers, each of
    # which submits 3 to_thread calls per file (read/parse/chunk). Once
    # a couple of CPU-heavy files (large Java/SQL, minified JS) pin
    # threads, subsequent to_thread submissions queue forever and the
    # whole ingest stalls at ~80%. Set the pool to
    # `LAMA_THREADPOOL_WORKERS` (default 64) so we always have headroom.
    try:
        import concurrent.futures as _cf
        _n_workers = int(os.environ.get("LAMA_THREADPOOL_WORKERS", "64"))
        asyncio.get_running_loop().set_default_executor(
            _cf.ThreadPoolExecutor(
                max_workers=max(16, min(_n_workers, 256)),
                thread_name_prefix="lama-ingest",
            )
        )
        logger.info("Default thread pool sized to %d workers", _n_workers)
    except Exception as e:
        logger.exception(f"Thread pool sizing failed: {e}")


    # iter-15.15 — Startup sweeper for orphaned transformation workers.
    # `_run_transformation_background` runs as an in-process asyncio task
    # (BackgroundTasks) — a `docker compose restart lama` kills the loop
    # and leaves docs at status=running forever. Sweep them into a
    # paused_orphan state so the user can pick which ones to Resume via
    # the UI (avoids surprise parallel LLM calls).
    try:
        await _recover_orphaned_transformations()
    except Exception as e:
        logger.exception(f"Orphan sweep failed: {e}")


async def _recover_orphaned_transformations():
    """Sweep transformations left in status=running with a stale
    heartbeat (>60s) and mark them paused_orphan. The user resumes
    each explicitly via the UI Resume button (iter-15.14) which the
    /resume endpoint (iter-15.15) now relaunches with resume=True.
    """
    from datetime import datetime, timezone
    from db import transformations, audit_log
    now = datetime.now(timezone.utc)
    swept = 0
    async for doc in transformations.find({"status": "running"}):
        last_iso = doc.get("worker_heartbeat") or doc.get("updated_at")
        last_dt = None
        try:
            if last_iso:
                last_dt = datetime.fromisoformat(str(last_iso).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            last_dt = None
        if last_dt is not None and (now - last_dt).total_seconds() < 60:
            continue  # live worker
        tid = doc.get("_id")
        files_done = doc.get("files_done", 0)
        total = doc.get("files_total", 0)
        logger.warning(
            "[orphan-recovery] transform=%s name=%s files_done=%s/%s last_seen=%s — marking paused_orphan",
            tid, doc.get("name"), files_done, total, last_iso,
        )
        await transformations.update_one(
            {"_id": tid},
            {"$set": {
                "paused": True,
                "phase": "paused_orphan",
                "phase_label": "Interrupted by service restart — click Resume to continue",
                "worker_pid": None,
                "worker_heartbeat": None,
                "updated_at": now.isoformat(),
            }},
        )
        try:
            # iter-15.16 — seed the live-telemetry buffer so users landing
            # on the Transformer page immediately see WHY the run is idle.
            from routes.tools import _emit_log as _tx_emit_log
            _tx_emit_log(
                tid, "warn",
                f"Recovered as orphan at {files_done}/{total} — awaiting user Resume",
                last_seen=last_iso,
            )
        except Exception:
            pass
        try:
            await audit_log.insert_one({
                "action": "orphan_recovery",
                "entity_type": "transformation",
                "entity_id": tid,
                "details": {
                    "files_done": files_done,
                    "files_total": total,
                    "last_seen": last_iso,
                },
                "created_at": now.isoformat(),
            })
        except Exception as e:
            logger.warning(f"[orphan-recovery] audit insert failed for {tid}: {e}")
        swept += 1
    if swept:
        logger.warning("[orphan-recovery] swept %d orphaned transformation(s)", swept)


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
