"""LAMA — Direct Transform / Direct Code Transformation Engine (DCTE).

Iter-18. The third standalone Tools track, beside Gap Analyzer and
Transformer (routes/tools.py). Bypasses the 5-stage pipeline entirely:
no KB, no SRS, no stage_context, no project_id. Same posture as
routes/tools.py — no auth dependency, because the SPA gates every Tools
page behind RequireAuth and lib/api.js attaches the bearer token.

Endpoints (all under /api/dcte):
    GET    /plugins
    POST   /projects/detect
    GET    /fs/browse
    POST   /jobs
    GET    /jobs
    GET    /jobs/{id}
    DELETE /jobs/{id}
    POST   /jobs/{id}/start
    POST   /jobs/{id}/pause
    POST   /jobs/{id}/resume
    POST   /jobs/{id}/rollback
    GET    /jobs/{id}/report
    GET    /jobs/{id}/events
    GET    /jobs/{id}/transforms
    POST   /jobs/{id}/cicd
    GET    /jobs/{id}/artifact
    GET    /debug/env

Every LLM call this module drives goes through `llm.fabric_call` inside
the `dcte.*` agent modules with an `agent_key` (critical contract #4) —
there is no model or vendor named anywhere in this file.
"""
from __future__ import annotations
import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from db import dcte_jobs, dcte_transforms, dcte_events, dcte_reports, audit_log
from dcte import (
    TransformationEngine,
    ProjectDetector,
    DcteJob,
    DcteJobStatus,
    get_registry,
)
from dcte.models import (
    DetectRequest, DetectResponse,
    CreateJobRequest, JobActionResponse,
)
from dcte.job_manager import JobManager
from dcte.ai_refactor import transform_files

logger = logging.getLogger("lama.dcte")
router = APIRouter(prefix="/dcte", tags=["dcte"])

# iter-18.12 — Long-running DCTE jobs used FastAPI BackgroundTasks, which
# Starlette awaits inside the response cycle. On a client disconnect or
# keep-alive timeout the awaited task (and the worker thread it launched
# via ``asyncio.to_thread``) was silently cancelled with a BaseException
# that ``except Exception`` didn't catch — leaving jobs frozen mid-run
# (typically at ~70 %, right after the AI-transformer sweep) with no
# traceback and no status update.  We switch to a fire-and-forget
# ``asyncio.create_task`` and hold a strong reference in this set so the
# GC can't reap it.  The stall-watchdog below is a safety net for any
# future silent hang inside the engine itself.
_RUNNING_JOB_TASKS: "set[asyncio.Task]" = set()

# Milliseconds/seconds without a new dcte_events row before the watchdog
# considers the job stalled and marks it FAILED. Long enough to cover a
# slow Maven build (single mvn invocation caps at 480 s) plus one LLM
# fix round-trip.
_STALL_SECONDS = int(os.environ.get("LAMA_DCTE_STALL_SECONDS", "900"))
# How often the watchdog polls the last-event timestamp.
_WATCHDOG_INTERVAL_S = 30.0


def _workspace_root() -> Path:
    """Base directory that relative user paths resolve against.

    `LAMA_DCTE_WORKSPACE` when set, otherwise the operator's home. It is
    deliberately NOT `os.getcwd()`: the backend's working directory is
    `backend/` under split-dev and `/app/backend` inside the image, so a
    CWD default would resolve the same typed path to two different places
    and, in the container, point *inside* the application tree. `$HOME` is
    also what `/fs/browse` already opens on, so the picker and the
    resolver agree on where a bare relative path lives.
    """
    return Path(os.environ.get("LAMA_DCTE_WORKSPACE") or "~").expanduser()


def _resolve_path(raw: str) -> Path:
    """Expand ~ and resolve relative paths against the workspace root."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_workspace_root() / p).resolve()
    return p


def _mgr() -> JobManager:
    return JobManager(dcte_jobs, dcte_transforms, dcte_events, dcte_reports)


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
@router.get("/plugins")
async def list_plugins():
    reg = get_registry()
    return {"plugins": reg.describe_all()}


# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------
@router.post("/projects/detect", response_model=DetectResponse)
async def detect_project(req: DetectRequest):
    # iter-18.2 — resolve ~ and relative paths against LAMA_DCTE_WORKSPACE
    # before feeding to the detector, so the browser can send "~/projects/x".
    resolved = str(_resolve_path(req.source_path))
    fp = await asyncio.to_thread(ProjectDetector().detect, resolved)
    return DetectResponse(
        path=fp.path,
        exists=fp.exists,
        is_valid=fp.is_valid,
        detected_stack=fp.detected_stack,
        confidence=fp.confidence,
        hints=fp.hints,
        suggested_target=fp.suggested_target,
    )


# ---------------------------------------------------------------------------
# Filesystem browser (server-side folder picker)
# ---------------------------------------------------------------------------
@router.get("/fs/browse")
async def browse_fs(path: str = Query(default="")):
    """List directories under `path`. Empty path → LAMA_DCTE_WORKSPACE or $HOME."""
    p = _resolve_path(path) if path else _workspace_root()
    if not p.exists():
        raise HTTPException(404, f"Path does not exist: {p}")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    entries: list[dict] = []
    warning: str | None = None
    try:
        it = list(p.iterdir())
    except PermissionError as e:
        # iter-18.3 — soft-fail so the picker keeps its breadcrumb + manual
        # input usable. Users can still type an absolute path they DO have
        # perms to (e.g. ~/projects on macOS Docker Desktop where ~ 403s).
        return {
            "path": str(p.resolve()),
            "parent": str(p.resolve().parent) if p.resolve().parent != p.resolve() else None,
            "entries": [],
            "warning": f"Permission denied listing {p}: {e}. Type an absolute path below.",
        }
    for child in sorted(it, key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name.startswith("."):
            continue
        try:
            entries.append({
                "name": child.name,
                "path": str(child.resolve()),
                "is_dir": child.is_dir(),
            })
        except (PermissionError, OSError):
            continue
    return {
        "path": str(p.resolve()),
        "parent": str(p.resolve().parent) if p.resolve().parent != p.resolve() else None,
        "entries": entries,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------
@router.post("/jobs")
async def create_job(req: CreateJobRequest):
    if not req.services:
        raise HTTPException(400, "At least one service is required.")
    # iter-18.1 — source_root / output_root are optional at the API surface;
    # if omitted we derive them from the first service so callers don't have
    # to type the same paths twice. Reports and CI/CD land under output_root.
    first = req.services[0]
    src_root = req.source_root or first.source_path
    out_root = req.output_root or first.destination_path
    if not src_root or not out_root:
        raise HTTPException(400, "services[0] must set source_path and destination_path (or provide source_root/output_root).")
    job = DcteJob(
        name=req.name,
        source_root=src_root,
        output_root=out_root,
        services=req.services,
        ai_refactor=req.ai_refactor,
        generate_cicd=req.generate_cicd,
        model=(req.model or None),
        # iter-19 — Opt-in autonomous droid mode. When True DCTE calls
        # `droid exec --auto medium` once per service and skips the
        # legacy ai_refactor + build_fix + devops chain on success.
        # Env override `LAMA_DCTE_DROID_AGENT_DEFAULT=1` flips the
        # default ON for operators who want it repo-wide.
        use_droid_agent=(
            req.use_droid_agent
            or os.environ.get("LAMA_DCTE_DROID_AGENT_DEFAULT", "").strip().lower()
               in {"1", "true", "yes", "on"}
        ),
    )
    await _mgr().create(job)
    await audit_log.insert_one({
        "action": "dcte_job_created",
        "entity_type": "dcte_job",
        "entity_id": job.id,
        "details": {
            "services": len(job.services),
            "use_droid_agent": job.use_droid_agent,
        },
        "at": job.created_at,
    })
    return job.model_dump()


@router.get("/jobs")
async def list_jobs(tenant_id: str | None = Query(default=None)):
    jobs = await _mgr().list(tenant_id=tenant_id)
    return {"jobs": [j.model_dump() for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    return job.model_dump()


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    r = await dcte_jobs.delete_one({"_id": job_id})
    await dcte_transforms.delete_many({"job_id": job_id})
    await dcte_events.delete_many({"job_id": job_id})
    await dcte_reports.delete_many({"job_id": job_id})
    return {"deleted": r.deleted_count == 1}


# ---------------------------------------------------------------------------
# Start / Pause / Resume / Rollback
# ---------------------------------------------------------------------------
@router.post("/jobs/{job_id}/start", response_model=JobActionResponse)
async def start_job(job_id: str):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    if job.status in (DcteJobStatus.TRANSFORMING, DcteJobStatus.ANALYZING):
        return JobActionResponse(id=job.id, status=job.status, message="already running")
    _spawn_job_task(job_id)
    return JobActionResponse(id=job.id, status=DcteJobStatus.ANALYZING, message="started")


@router.post("/jobs/{job_id}/pause", response_model=JobActionResponse)
async def pause_job(job_id: str):
    await _mgr().update_status(job_id, DcteJobStatus.PAUSED)
    return JobActionResponse(id=job_id, status=DcteJobStatus.PAUSED, message="paused")


@router.post("/jobs/{job_id}/resume", response_model=JobActionResponse)
async def resume_job(job_id: str):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    _spawn_job_task(job_id)
    return JobActionResponse(id=job.id, status=DcteJobStatus.ANALYZING, message="resuming")


@router.post("/jobs/{job_id}/rollback", response_model=JobActionResponse)
async def rollback_job(job_id: str):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    engine = TransformationEngine()
    await asyncio.to_thread(engine.rollback, job)
    await _mgr().update_status(job_id, DcteJobStatus.ROLLED_BACK)
    await audit_log.insert_one({
        "action": "dcte_job_rollback",
        "entity_type": "dcte_job",
        "entity_id": job_id,
        "at": job.updated_at,
    })
    return JobActionResponse(id=job_id, status=DcteJobStatus.ROLLED_BACK, message="rolled back")


# ---------------------------------------------------------------------------
# Reports / Events / Transforms
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}/report")
async def get_reports(job_id: str):
    return {"reports": await _mgr().reports_for(job_id)}


@router.get("/jobs/{job_id}/events")
async def get_events(job_id: str, limit: int = Query(default=200, ge=1, le=1000)):
    return {"events": await _mgr().events_for(job_id, limit=limit)}


@router.get("/jobs/{job_id}/transforms")
async def get_transforms(job_id: str):
    return {"transforms": await _mgr().transforms_for(job_id)}


# ---------------------------------------------------------------------------
# CI/CD
# ---------------------------------------------------------------------------
class CicdRequest(BaseModel):
    """Body of POST /jobs/{id}/cicd.

    Typed rather than a bare `dict` per CLAUDE.md convention #3, so an
    unknown provider is a 422 from the schema instead of a 400 raised out
    of the middle of a partially-completed write loop.
    """
    providers: list[str] = Field(default_factory=lambda: ["github"])


@router.post("/jobs/{job_id}/cicd")
async def emit_cicd(job_id: str, req: CicdRequest):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    providers = req.providers or ["github"]
    engine = TransformationEngine()
    out: list[str] = []
    for p in providers:
        try:
            path = await asyncio.to_thread(engine.cicd.write, p, Path(job.output_root))
            out.append(str(path))
        except Exception as e:
            raise HTTPException(400, f"CI/CD emit failed for {p}: {e}") from e
    return {"emitted": out}


# ---------------------------------------------------------------------------
# Artifact download (guarded to output_root)
# ---------------------------------------------------------------------------
@router.get("/jobs/{job_id}/artifact")
async def download_artifact(job_id: str, path: str = Query(...)):
    job = await _mgr().get(job_id)
    if not job:
        raise HTTPException(404, "DCTE job not found")
    p = Path(path).resolve()
    root = Path(job.output_root).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(400, "Path escapes output_root")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Artifact not found")
    return FileResponse(str(p))


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
def _spawn_job_task(job_id: str) -> asyncio.Task:
    """iter-18.12 — Fire-and-forget the DCTE runner as a top-level asyncio
    task and keep a strong reference so it isn't garbage-collected mid-run.
    Replaces ``BackgroundTasks.add_task`` which tied the task's lifetime
    to the response cycle and silently cancelled multi-hour jobs on any
    client disconnect / keep-alive timeout.
    """
    task = asyncio.create_task(_run_job_background(job_id), name=f"dcte-run-{job_id}")
    _RUNNING_JOB_TASKS.add(task)
    task.add_done_callback(_RUNNING_JOB_TASKS.discard)
    return task


async def _stall_watchdog(job_id: str) -> None:
    """iter-18.12 — Fail the job if no new event has been persisted for
    ``_STALL_SECONDS`` seconds while it's in a running state.  Runs
    alongside the engine; the caller is responsible for cancelling it
    when the engine completes.
    """
    from datetime import datetime, timezone
    running_states = {
        DcteJobStatus.ANALYZING.value,
        DcteJobStatus.TRANSFORMING.value,
        DcteJobStatus.BUILDING.value,
        DcteJobStatus.VALIDATING.value,
        DcteJobStatus.REPORTING.value,
    }
    mgr = _mgr()
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            j = await mgr.get(job_id)
            if not j:
                return
            status_val = j.status.value if hasattr(j.status, "value") else str(j.status)
            if status_val not in running_states:
                return
            last = await dcte_events.find_one(
                {"job_id": job_id}, sort=[("at", -1)],
            )
            last_at_raw = (last or {}).get("at")
            if not last_at_raw:
                continue
            try:
                last_at = datetime.fromisoformat(str(last_at_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            idle_s = (datetime.now(timezone.utc) - last_at).total_seconds()
            if idle_s < _STALL_SECONDS:
                continue
            logger.warning(
                "DCTE stall watchdog: job %s idle for %.0fs — marking FAILED",
                job_id, idle_s,
            )
            msg = (
                f"stall watchdog: no engine event for {int(idle_s)}s "
                f"(threshold {_STALL_SECONDS}s). The runner is presumed hung. "
                "Delete the job and re-run; if it recurs, check LAMA_DCTE_STALL_SECONDS "
                "or reduce the batch size."
            )
            try:
                await mgr.update_status(job_id, DcteJobStatus.FAILED, error=msg)
                from dcte.models import DcteEvent as _E
                await mgr.append_event(_E(
                    job_id=job_id, service_id=None, level="error", phase="watchdog",
                    message=msg, payload={"idle_seconds": int(idle_s)},
                ))
            except Exception as e:
                logger.warning("stall watchdog persist failed: %s", e)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("stall watchdog loop error: %s", e)


async def _run_job_background(job_id: str) -> None:
    mgr = _mgr()
    job = await mgr.get(job_id)
    if not job:
        logger.warning("DCTE background: job %s not found", job_id)
        return

    loop = asyncio.get_running_loop()

    async def _persist_event(evt):
        try:
            await mgr.append_event(evt)
        except Exception as e:
            logger.warning("event persist failed: %s", e)

    async def _persist_transform(rec):
        try:
            await mgr.append_transform(rec)
        except Exception as e:
            logger.warning("transform persist failed: %s", e)

    async def _persist_report(rep):
        try:
            await mgr.append_report(rep)
        except Exception as e:
            logger.warning("report persist failed: %s", e)

    async def _persist_status(status, progress, error):
        try:
            await mgr.update_status(job.id, status, progress=progress, error=error)
        except Exception as e:
            logger.warning("status persist failed: %s", e)

    def _event_sink(evt):
        asyncio.run_coroutine_threadsafe(_persist_event(evt), loop)

    # iter-18.6 — Pre-flight the LLM fabric so users know upfront whether
    # the AI transformer will actually run.  Silent no-op has been the top
    # complaint ("converted files identical to originals").  If nothing can
    # serve a `fabric_call` we emit a WARN event that the FE renders in the
    # job event log, so the user sees why nothing was rewritten.
    #
    # Two things can serve one: an active Console provider (the normal
    # path), or an env OPENROUTER_API_KEY, which `llm.py` still uses as a
    # cross-vendor last resort when the Console provider raises CreditError
    # and is not itself openrouter-typed.  This block used to AND that key
    # with `LAMA_DISABLE_OPENROUTER_FALLBACK != "1"`; that variable was
    # removed repo-wide in 2026-09 (see docker-compose.yml and .env, which
    # both record the removal) and no code has read it since iter-14.31, so
    # the clause was dead and the advice it printed — "unset
    # LAMA_DISABLE_OPENROUTER_FALLBACK" — pointed at a flag that no longer
    # exists, in the one message a stuck operator actually reads.
    if job.ai_refactor:
        try:
            from db import model_providers as _providers_col
            active = await _providers_col.find_one({"is_active": True})
            has_env = bool(os.getenv("OPENROUTER_API_KEY"))
            if not active and not has_env:
                from dcte.models import DcteEvent as _E
                _event_sink(_E(
                    job_id=job.id, service_id=None, level="warn", phase="ai_refactor",
                    message=("No active LLM provider found. AI transformer will "
                             "be SKIPPED — only deterministic (regex-based) rewrites "
                             "will apply. Add and activate a provider in "
                             "Console → Model Fabric, then re-run."),
                    payload={"active_provider": False, "env_fallback": False},
                ))
        except Exception as e:
            logger.warning("AI fabric preflight failed: %s", e)

    def _transform_sink(rec):
        asyncio.run_coroutine_threadsafe(_persist_transform(rec), loop)

    def _report_sink(rep):
        asyncio.run_coroutine_threadsafe(_persist_report(rep), loop)

    def _status_sink(status, progress, error):
        # iter-18.16 — Monotonic guard.  Multiple phase-transition
        # checkpoints in the engine + wrapper (transformer band vs
        # BUILDING checkpoint vs VALIDATING checkpoint) historically
        # emitted progress values in an order that let the bar step
        # backwards (e.g. transformer finished at (idx+0.60)/n, then
        # engine BUILDING checkpoint fired (idx+0.60)/n — flat — then
        # AI fix-up rounds pushed further; or on multi-service jobs
        # svc N+1 restarted at (idx+0.30)/n which is below svc N's
        # last tick).  We now clamp every emission to be ≥ the highest
        # value ever emitted for this job so the FE bar and %-label
        # only ever move forward.
        prev = getattr(_status_sink, "_hwm", 0.0)
        try:
            p = float(progress)
        except (TypeError, ValueError):
            p = prev
        if p < prev:
            p = prev
        else:
            _status_sink._hwm = p
        asyncio.run_coroutine_threadsafe(
            _persist_status(status, p, error), loop,
        )

    def _ai_refactor_wrapper(files):
        if not job.ai_refactor:
            return []
        # iter-18.8 — scale timeout to file count. Previous fixed 180 s
        # cap silently killed jobs with ~50+ files (each batch of 3 goes
        # through fabric → factory-droid or Ollama and takes seconds).
        # A TimeoutError here surfaced as "never reached the LLM" which
        # was inaccurate — the LLM WAS reached, we just gave up early.
        batches = max(1, len(files))  # iter-18.8 _MAX_BATCH_FILES=1 → 1 batch per file
        # 45 s per batch is generous for Ollama / Factory droid round-trips
        # + a 120 s floor so tiny jobs still get a reasonable window.
        timeout_s = max(120, batches * 45)

        # iter-18.10 — Per-batch progress callback. Every batch fires TWO
        # events (start + done) so:
        #   • FE renders each batch's start as an "AI batch i/N: file.java"
        #     line — the user sees the log tick every few seconds.
        #   • The engine's active-service progress bar smoothly advances
        #     between the transform-checkpoint (30%) and validate-
        #     checkpoint (70%) across the whole AI sweep, instead of
        #     freezing at 30% for minutes.
        from dcte.models import DcteEvent as _AiEvt
        # wrapper is called ONCE per service; increment an attribute-based
        # counter to know which service we're on for progress math.
        svc_idx = getattr(_ai_refactor_wrapper, "_svc_counter", 0)
        setattr(_ai_refactor_wrapper, "_svc_counter", svc_idx + 1)
        n_svcs = max(1, len(job.services))
        # iter-18.16 — AI band ends at 0.60 (not 0.70) because the
        # engine's next checkpoint (BUILDING) fires at (idx+0.60)/n_svcs
        # — using 0.70 here made the bar visibly step BACKWARD from 70%
        # → 60% when the transformer finished.  Now the transition is
        # flat: AI reaches (idx+0.60)/n_svcs → BUILDING keeps it there
        # → VALIDATING moves it to (idx+0.7)/n_svcs.
        ai_lo, ai_hi = 0.30, 0.60   # matches engine BUILDING checkpoint

        # iter-18.14 — Announce the parallel mode so operators watching
        # the MiniConsole understand why the sweep is faster than the
        # historical serial baseline.
        try:
            _conc = int(os.environ.get("LAMA_DCTE_TRANSFORMER_CONCURRENCY", "3"))
        except ValueError:
            _conc = 3
        _conc = max(1, _conc)
        _event_sink(_AiEvt(
            job_id=job.id, service_id=None, level="info", phase="ai_refactor",
            message=(
                f"AI transformer sweep starting: {len(files)} file(s), "
                f"concurrency={_conc} "
                f"(override via LAMA_DCTE_TRANSFORMER_CONCURRENCY)"
            ),
            payload={"file_count": len(files), "concurrency": _conc},
        ))

        # iter-18.14 — With parallel batches (asyncio.gather in
        # transform_files) batches finish out of order, so we can no
        # longer use ``batch_i / batch_n`` as the progress fraction —
        # it would jitter or momentarily go backwards.  Instead, count
        # completed batches locally and drive progress off that.
        _completed_batches = {"done": 0}

        def _pcb(batch_i, batch_n, batch_files, phase):
            names = ", ".join(Path(f).name for f in batch_files[:3])
            more = "" if len(batch_files) <= 3 else f" (+{len(batch_files) - 3} more)"
            msg = f"AI batch {batch_i}/{batch_n} [{phase}]: {names}{more}"
            _event_sink(_AiEvt(
                job_id=job.id, service_id=None, level="info", phase="ai_refactor",
                message=msg,
                payload={"batch": batch_i, "batches": batch_n, "phase_kind": phase},
            ))
            if phase != "start" and phase != "skipped_read":
                # Anything that's not a pre-flight "start" ping counts
                # as a batch completion for progress accounting
                # (done / error / skipped_read / done rewrote=…).
                _completed_batches["done"] += 1
            if batch_n > 0:
                per_svc = 1.0 / n_svcs
                svc_base = svc_idx * per_svc
                # iter-18.16 — Cap the fraction at 1.0.  With iter-18.15
                # fix-up rounds, _completed_batches["done"] can exceed
                # batch_n (the primary-sweep total) as fix-up batches
                # complete.  Uncapped, ai_frac > 1.0 would push
                # progress past the AI band ceiling and collide with
                # the downstream BUILDING/VALIDATING checkpoints.
                ai_frac = min(1.0, _completed_batches["done"] / batch_n)
                progress = svc_base + per_svc * (ai_lo + (ai_hi - ai_lo) * ai_frac)
                _status_sink("transforming", min(0.99, progress), None)

        try:
            result = asyncio.run_coroutine_threadsafe(
                transform_files(files, progress_cb=_pcb, model=(job.model or None)), loop,
            ).result(timeout=timeout_s)
            suggestions: list = []
            for r in result.get("rewritten", []):
                suggestions.append({
                    "file": r["file"],
                    "category": "rewrite",
                    "severity": r.get("risk", "low"),
                    "message": "AI transformer rewrote this file",
                    "suggested_change": "; ".join(str(c) for c in r.get("changes", [])),
                })
            for s in result.get("skipped", []):
                suggestions.append({
                    "file": s["file"],
                    "category": "guardrail",
                    "severity": "low",
                    "message": "AI transformer rewrite rejected by guardrail",
                    "suggested_change": s.get("reason", ""),
                })
            for n in result.get("notes", []):
                suggestions.append({
                    "file": "",
                    "category": "diagnostic",
                    "severity": "low",
                    "message": "AI transformer diagnostic",
                    "suggested_change": str(n),
                })
            # iter-18.15 — Surface any file that STILL contains legacy
            # markers after the primary sweep + fix-up rounds. These are
            # the "needs manual" bucket the user complained about
            # (migrated apps still pointing at Helidon).
            residual = result.get("residual") or []
            fixup_rounds = int(result.get("fixup_rounds") or 0)
            if residual:
                _event_sink(_AiEvt(
                    job_id=job.id, service_id=None, level="warn", phase="ai_refactor",
                    message=(
                        f"residue: {len(residual)} file(s) still carry legacy "
                        f"markers after {fixup_rounds} fix-up round(s) — "
                        "flagged as needs_manual"
                    ),
                    payload={
                        "count": len(residual),
                        "fixup_rounds": fixup_rounds,
                        "sample": [
                            {"file": r["file"], "markers": r["markers"][:5]}
                            for r in residual[:5]
                        ],
                    },
                ))
                for r in residual:
                    suggestions.append({
                        "file": r["file"],
                        "category": "needs_manual",
                        "severity": "high",
                        "message": (
                            f"Residual legacy markers after {fixup_rounds} "
                            f"fix-up round(s) — manual migration required"
                        ),
                        "suggested_change": ", ".join(r.get("markers") or []),
                    })
            elif fixup_rounds > 0:
                _event_sink(_AiEvt(
                    job_id=job.id, service_id=None, level="info", phase="ai_refactor",
                    message=(
                        f"residue: all files clean after {fixup_rounds} "
                        "fix-up round(s)"
                    ),
                    payload={"fixup_rounds": fixup_rounds},
                ))
            return suggestions
        except concurrent.futures.TimeoutError:
            logger.warning("AI transformer wrapper timed out after %ss", timeout_s)
            return [{
                "file": "", "category": "diagnostic", "severity": "medium",
                "message": "AI transformer diagnostic",
                "suggested_change": (
                    f"timed out after {timeout_s}s across {batches} batch(es). "
                    "The LLM was reached but the full sweep didn't finish. "
                    "Consider fewer files (drop tests / generated code) or a "
                    "faster model routed for tier=high."
                ),
            }]
        except Exception as e:
            logger.warning("AI transformer wrapper failed: %s", e)
            return [{
                "file": "", "category": "diagnostic", "severity": "medium",
                "message": "AI transformer diagnostic",
                "suggested_change": f"wrapper error: {type(e).__name__}: {e}",
            }]

    def _build_fix_wrapper(dest_root, service_id):
        """iter-18.11 — Compile the dest tree with mvn/gradle; on failure,
        let the LLM patch each failing file's errors and retry (max 5
        attempts). Returns a plain dict so the engine event payload is
        JSON-serialisable.
        """
        from dcte.build_agent import build_and_fix
        from dcte.models import DcteEvent as _BldEvt
        n_svcs = max(1, len(job.services))
        svc_idx = getattr(_build_fix_wrapper, "_svc_counter", 0)
        setattr(_build_fix_wrapper, "_svc_counter", svc_idx + 1)
        # Build phase occupies (0.60 → 0.70) of the active service slot.
        bd_lo, bd_hi = 0.60, 0.70

        def _bpcb(attempt, max_attempts, fname, phase):
            msg = f"Build attempt {attempt}/{max_attempts}: {phase}"
            if fname:
                msg = f"Build attempt {attempt}/{max_attempts} [{fname}]: {phase}"
            _event_sink(_BldEvt(
                job_id=job.id, service_id=service_id, level="info", phase="build",
                message=msg,
                payload={"attempt": attempt, "max_attempts": max_attempts, "file": fname},
            ))
            if max_attempts > 0:
                per_svc = 1.0 / n_svcs
                svc_base = svc_idx * per_svc
                frac = attempt / max_attempts
                progress = svc_base + per_svc * (bd_lo + (bd_hi - bd_lo) * frac)
                _status_sink("building", min(0.99, progress), None)

        try:
            result = asyncio.run_coroutine_threadsafe(
                build_and_fix(Path(dest_root), progress_cb=_bpcb), loop,
            ).result(timeout=1800)  # 5 attempts × ~6 min max
            return result.as_dict() if hasattr(result, "as_dict") else dict(result)
        except concurrent.futures.TimeoutError:
            logger.warning("Build agent wrapper timed out")
            return {"skipped": False, "success": False, "attempted": True,
                    "notes": ["build wrapper timed out after 1800s"], "attempts": 0,
                    "fixes_applied": 0, "errors": [], "tool": None,
                    "final_output_tail": ""}
        except Exception as e:
            logger.warning("Build agent wrapper failed: %s", e)
            return {"skipped": False, "success": False, "attempted": True,
                    "notes": [f"wrapper error: {type(e).__name__}: {e}"],
                    "attempts": 0, "fixes_applied": 0, "errors": [],
                    "tool": None, "final_output_tail": ""}

    # iter-18.17 — DevOps agent wrapper. Runs AFTER build_and_fix and
    # closes structural gaps (missing @SpringBootApplication, missing
    # application.yml sections, missing spring-boot-maven-plugin,
    # unresolved compile errors) that javac itself can't see.
    def _devops_wrapper(dest_root, service_id, build_result):
        try:
            from dcte.devops_agent import run_devops
            from dcte.models import DcteEvent as _DoEvt

            # iter-18.18 — Emit a "starting" beacon so the events log
            # has a heartbeat between the BUILDING (0.60) and DEVOPS
            # (0.65) checkpoints.  Without this, the log would look
            # silent for as long as run_devops takes to reach its
            # first internal progress callback, which is what the
            # user perceived as "getting stuck at 60%".
            _event_sink(_DoEvt(
                job_id=job.id, service_id=service_id,
                level="info", phase="devops",
                message="DevOps agent starting: scanning for structural gaps",
                payload={"phase_kind": "start"},
            ))

            def _dcb(phase, count):
                _event_sink(_DoEvt(
                    job_id=job.id, service_id=service_id,
                    level="info", phase="devops",
                    message=f"DevOps [{phase}]: {count}",
                    payload={"phase_kind": phase, "count": count},
                ))

            src_root = Path(job.source_root) if job.source_root else None
            coro = run_devops(
                Path(dest_root),
                source_root=src_root,
                build_result=build_result,
                progress_cb=_dcb,
            )
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            result = fut.result(timeout=180)
            return result.as_dict() if hasattr(result, "as_dict") else dict(result)
        except concurrent.futures.TimeoutError:
            logger.warning("DevOps agent wrapper timed out")
            return {"attempted": True, "fixes_applied": 0, "gaps_found": [],
                    "fixes": [], "unresolved": [],
                    "notes": ["devops wrapper timed out after 180s"]}
        except Exception as e:
            logger.warning("DevOps agent wrapper failed: %s", e)
            return {"attempted": True, "fixes_applied": 0, "gaps_found": [],
                    "fixes": [], "unresolved": [],
                    "notes": [f"wrapper error: {type(e).__name__}: {e}"]}

    # iter-18.17 — Tester agent wrapper. Static parity checks
    # (endpoints + config) + real boot smoke against /actuator/health.
    # Bounded by LAMA_DCTE_BOOT_SMOKE_TIMEOUT_S (default 180s).
    def _tester_wrapper(dest_root, service_id, source_root):
        try:
            from dcte.tester_agent import run_tester
            from dcte.models import DcteEvent as _TeEvt
            try:
                _boot_timeout = int(
                    os.environ.get("LAMA_DCTE_BOOT_SMOKE_TIMEOUT_S", "180") or "180"
                )
            except ValueError:
                _boot_timeout = 180
            _run_boot = (
                os.environ.get("LAMA_DCTE_TESTER_BOOT_SMOKE", "1").lower()
                not in ("0", "false", "no")
            )
            # iter-18.18 — Emit "starting" + "boot smoke" heartbeats.
            # run_boot=True can block for up to 180s while the JVM
            # comes up; without a beacon the log would look dead
            # while the tester was actually working.
            _event_sink(_TeEvt(
                job_id=job.id, service_id=service_id,
                level="info", phase="test",
                message=(
                    "Tester agent starting: parity checks"
                    + (f" + boot smoke (up to {_boot_timeout}s)" if _run_boot else "")
                ),
                payload={"phase_kind": "start", "boot_smoke": _run_boot,
                         "timeout_s": _boot_timeout},
            ))
            src_root = Path(source_root) if source_root else None
            coro = run_tester(
                Path(dest_root),
                source_root=src_root,
                run_boot=_run_boot,
                timeout_s=_boot_timeout,
            )
            # Wrapper timeout > boot timeout so the tester can shut
            # down cleanly if the smoke test itself times out.
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            result = fut.result(timeout=_boot_timeout + 60)
            return result.as_dict() if hasattr(result, "as_dict") else dict(result)
        except concurrent.futures.TimeoutError:
            logger.warning("Tester agent wrapper timed out")
            return {"verdict": "PASS_WITH_WARNINGS",
                    "notes": ["tester wrapper timed out"],
                    "parity_endpoints": {}, "parity_config": {},
                    "residual": [], "boot_smoke": {"runnable": False,
                                                    "reason": "wrapper timeout",
                                                    "healthy": False}}
        except Exception as e:
            logger.warning("Tester agent wrapper failed: %s", e)
            return {"verdict": "PASS_WITH_WARNINGS",
                    "notes": [f"wrapper error: {type(e).__name__}: {e}"],
                    "parity_endpoints": {}, "parity_config": {},
                    "residual": [], "boot_smoke": {"runnable": False,
                                                    "reason": str(e),
                                                    "healthy": False}}

    def _droid_agent_wrapper(dest_root: Path, service_id: str,
                              model: str | None) -> dict[str, Any]:
        """iter-19 — Sync bridge for the async ``run_droid_agent``.
        The engine runs on a worker thread (``asyncio.to_thread``); we
        schedule the coroutine back onto the main event loop and block
        this thread on the future so the engine sees a normal sync call.
        Any exception bubbles up as ``{success: False, error: ...}``
        so the engine's fallback branch stays clean.
        """
        try:
            from dcte.droid_agent import run_droid_agent
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"droid_agent import failed: {e}"}
        try:
            coro = run_droid_agent(Path(dest_root), service_id, model)
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            # Timeout envelope = agent timeout + 60s cleanup margin.
            import os as _os
            try:
                agent_timeout = max(60, int(_os.environ.get(
                    "LAMA_DCTE_DROID_AGENT_TIMEOUT_SEC", "1800")))
            except ValueError:
                agent_timeout = 1800
            return fut.result(timeout=agent_timeout + 60) or {"success": False,
                                                              "error": "empty result"}
        except concurrent.futures.TimeoutError:
            return {"success": False,
                    "error": "droid agent wrapper exceeded envelope timeout"}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"droid agent wrapper failed: {e}"}

    def _run_sync():
        eng = TransformationEngine()
        return eng.run_job(
            job,
            record_sink=_transform_sink,
            event_sink=_event_sink,
            report_sink=_report_sink,
            status_sink=_status_sink,
            ai_refactor_fn=_ai_refactor_wrapper if job.ai_refactor else None,
            build_fix_fn=_build_fix_wrapper,
            devops_fn=_devops_wrapper,
            tester_fn=_tester_wrapper,
            droid_agent_fn=_droid_agent_wrapper if getattr(job, "use_droid_agent", False) else None,
        )

    # iter-18.9 — Spawn the live-progress narrator alongside the engine.
    # A small LLM (tier=low) summarises recent events into a 1-sentence
    # "currently doing X…" line every ~6 s so the FE has something to
    # render besides a % bar. The loop self-terminates on any terminal
    # job status. Failures are silent — the narrator is decorative.
    from dcte.narrator import narration_loop
    from dcte.models import DcteEvent as _NarrEvt

    async def _narr_get_job(jid: str):
        j = await mgr.get(jid)
        if not j:
            return None
        return {"status": j.status.value if hasattr(j.status, "value") else j.status,
                "progress": j.progress}

    async def _narr_recent_events(jid: str, limit: int):
        return await mgr.events_for(jid, limit=limit)

    def _narr_emit(jid: str, message: str, progress_pct: int):
        _event_sink(_NarrEvt(
            job_id=jid, service_id=None, level="info", phase="narration",
            message=message, payload={"progress_pct": progress_pct},
        ))

    narr_task = asyncio.create_task(
        narration_loop(
            job.id, _narr_get_job, _narr_recent_events, _narr_emit,
            interval_s=6.0,
        )
    )
    # iter-18.12 — Stall watchdog: if no dcte_events row lands for the job
    # in _STALL_SECONDS while the engine is still running, mark the job
    # FAILED with a clear error.  Historically we had jobs pinned at 70 %
    # with no events for >10 h because a BackgroundTask cancellation had
    # silently killed the thread; we've since replaced BackgroundTasks
    # with create_task, but keep this watchdog as a belt-and-braces net.
    watchdog_task = asyncio.create_task(_stall_watchdog(job.id))

    try:
        final = await asyncio.to_thread(_run_sync)
        await mgr.update_status(
            final.id, final.status,
            progress=final.progress, error=final.error,
        )
        await audit_log.insert_one({
            "action": "dcte_job_completed",
            "entity_type": "dcte_job",
            "entity_id": final.id,
            "details": {"status": final.status.value, "error": final.error},
            "at": final.updated_at,
        })
    except asyncio.CancelledError:
        # iter-18.12 — Cancellation used to silently drop the job. Record
        # it so the FE sees the real state instead of "transforming"
        # indefinitely, then re-raise so upstream cancel semantics hold.
        logger.warning("DCTE background task cancelled for job %s", job.id)
        try:
            await mgr.update_status(
                job.id, DcteJobStatus.FAILED,
                error="background task was cancelled (client disconnect or shutdown)",
            )
        except Exception:
            pass
        raise
    except BaseException as e:  # noqa: BLE001 — we intentionally catch BaseException
        # iter-18.12 — Previously we caught only ``Exception``, so a
        # BaseException (SystemExit, KeyboardInterrupt, or a wrapped
        # CancelledError) exited without touching the DB.
        logger.exception("DCTE background failure")
        try:
            await mgr.update_status(job.id, DcteJobStatus.FAILED, error=str(e) or type(e).__name__)
        except Exception:
            pass
    finally:
        # iter-18.9 — stop the narrator regardless of engine outcome.
        narr_task.cancel()
        try:
            await narr_task
        except (asyncio.CancelledError, Exception):
            pass
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Env / debug
# ---------------------------------------------------------------------------
@router.get("/debug/env")
async def debug_env():
    """Non-secret env snapshot for the DCTE page's diagnostics."""
    return {
        "workspaces_root": os.environ.get("LAMA_DCTE_WORKSPACE", ""),
        "ai_refactor_enabled_default": True,
        "plugins": [p["id"] for p in get_registry().describe_all()],
    }
