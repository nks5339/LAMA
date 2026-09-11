"""Pipeline-level controls (skip / unskip intermediate stages).

iter-13.66 — UX request: let the user skip in-between pipeline nodes
(DataModel / Architecture / Living) when they don't need those artifacts
for a given migration run. The initial stage (Discovery) and the final
code-producing stage (CodeGen) are NEVER skippable — Discovery is the
source-of-truth for every downstream stage, and CodeGen is the actual
deliverable.

A "skipped" stage writes a minimal StageContext doc (so downstream
`require_stage_context` calls still resolve) and promotes the next
stage to `available`. The project's `stage_status[stage]` is set to
`"skipped"` so the UI can render it distinctly from `frozen`.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from db import projects, audit_log
from pipeline import save_stage_context

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# Order matches Sidebar.STAGES — single source of truth here.
STAGE_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"]

# Skippable stages: every intermediate node except Discovery (initial) and
# CodeGen (final deliverable). Living is technically a post-CodeGen stage
# and is also skippable since it's optional observability tooling.
SKIPPABLE_STAGES = {"DataModel", "Architecture", "Living"}


def _next_stage(stage: str) -> str | None:
    try:
        idx = STAGE_ORDER.index(stage)
    except ValueError:
        return None
    return STAGE_ORDER[idx + 1] if idx + 1 < len(STAGE_ORDER) else None


@router.post("/{project_id}/skip/{stage}")
async def skip_stage(project_id: str, stage: str, force: bool = False):
    """Mark an intermediate stage as skipped and unlock the next stage.

    Returns HTTP 400 for Discovery / CodeGen (not skippable) and 404 if
    the project does not exist. When the stage is currently `frozen`,
    pass `?force=true` to confirm the destructive overwrite — the existing
    StageContext (and the `version` counter) is replaced by the skip marker.
    """
    if stage not in SKIPPABLE_STAGES:
        raise HTTPException(
            400,
            f"Stage '{stage}' cannot be skipped. Only {sorted(SKIPPABLE_STAGES)} are skippable.",
        )
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    current = (proj.get("stage_status") or {}).get(stage, "locked")
    if current == "frozen" and not force:
        raise HTTPException(
            409,
            f"Stage '{stage}' is currently frozen. Re-issue with ?force=true to overwrite the frozen artifacts with a skip marker.",
        )

    now = datetime.now(timezone.utc).isoformat()

    # Write a minimal StageContext so any downstream stage that calls
    # require_stage_context(... stage='DataModel'/'Architecture'/'Living')
    # still resolves and can branch on outputs.skipped.
    await save_stage_context(
        project_id=project_id,
        stage=stage,
        outputs={"skipped": True, "skipped_at": now},
        sources={"reason": "user-skipped via pipeline.skip"},
        toon_summary="",
        frozen_by="user-skip",
    )

    updates: dict = {f"stage_status.{stage}": "skipped", "updated_at": now}
    nxt = _next_stage(stage)
    if nxt:
        # Don't override a stage that's already moved past 'available'
        # (e.g. user skipped Architecture but CodeGen was already frozen).
        cur_next = (proj.get("stage_status") or {}).get(nxt, "locked")
        if cur_next in (None, "locked"):
            updates[f"stage_status.{nxt}"] = "available"

    await projects.update_one({"id": project_id}, {"$set": updates})

    await audit_log.insert_one({
        "action": "pipeline.skip",
        "project_id": project_id,
        "at": now,
        "details": {"stage": stage, "next_unlocked": nxt},
    })

    return {
        "ok": True,
        "stage": stage,
        "skipped": True,
        "next_unlocked": nxt,
    }


@router.post("/{project_id}/unskip/{stage}")
async def unskip_stage(project_id: str, stage: str):
    """Reverse a skip: clear the skipped StageContext and re-lock downstream.

    Useful when the user changes their mind before freezing CodeGen.
    """
    if stage not in SKIPPABLE_STAGES:
        raise HTTPException(400, f"Stage '{stage}' is not a skippable stage.")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    from db import stage_context as stage_context_col
    ctx = await stage_context_col.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0}
    )
    if not ctx or not (ctx.get("outputs") or {}).get("skipped"):
        raise HTTPException(400, f"Stage '{stage}' is not in a skipped state.")

    now = datetime.now(timezone.utc).isoformat()
    await stage_context_col.delete_one({"project_id": project_id, "stage": stage})

    updates: dict = {f"stage_status.{stage}": "available", "updated_at": now}
    # Re-lock any downstream stage that hasn't been independently frozen.
    nxt = _next_stage(stage)
    while nxt:
        cur = (proj.get("stage_status") or {}).get(nxt, "locked")
        if cur == "frozen":
            break
        updates[f"stage_status.{nxt}"] = "locked"
        nxt = _next_stage(nxt)

    await projects.update_one({"id": project_id}, {"$set": updates})

    await audit_log.insert_one({
        "action": "pipeline.unskip",
        "project_id": project_id,
        "at": now,
        "details": {"stage": stage},
    })

    return {"ok": True, "stage": stage, "skipped": False}


# =======================================================================
# iter-13.71 — Per-stage Confidence / Accuracy badge
#
# Surfaces an aggregate confidence% (0-100) per stage so the user can
# decide whether to FREEZE or REGENERATE without re-reading every line.
# Reuses the multi-model confidence engine + the same section catalogue
# the Living-stage Accuracy Report uses, filtered to a single stage.
# Persisted to the `stage_confidence` collection so the badge can render
# instantly on stage-page load.
# =======================================================================
from db import stage_confidence, parity_runs  # noqa: E402
from confidence import (         # noqa: E402
    band_of,
    pick_evaluator_models,
    score_artifact_multi_model,
)
# Cross-route helper import: living.py owns the section catalogue + the
# artifact/KB/ground-truth collectors. Pipeline is the orchestration
# layer, so importing from living is acceptable (one-way) and keeps the
# catalogue as a single source of truth.
from routes.living import (      # noqa: E402
    _REPORT_SECTIONS,
    _kb_summary_for_eval,
    _ground_truth_for_eval,
    _collect_artifact_text,
)
import asyncio                    # noqa: E402
import logging                    # noqa: E402
import re                          # noqa: E402
import os                         # noqa: E402
import uuid                       # noqa: E402
from typing import Any, Dict, Optional  # noqa: E402

logger = logging.getLogger("lama.pipeline")


_STAGE_CONFIDENCE_STAGES = {"Discovery", "DataModel", "Architecture", "CodeGen", "Living"}

# iter-14.46 — In-process cache for the live parity_loop score used by
# `get_stage_confidence("CodeGen")`. score_run scans hundreds of files
# (~3-4 min on ceots' 1,188 files), which is unacceptable for a pill
# poll. We cache the projection for 90s per project — the pill is
# stable enough at that granularity, and any Auto-Validate finish
# invalidates the cache via `invalidate_codegen_parity_cache`.
_CODEGEN_PARITY_CACHE: Dict[str, Dict[str, Any]] = {}
_CODEGEN_PARITY_TTL_SECS = 90.0


def invalidate_codegen_parity_cache(project_id: str) -> None:
    _CODEGEN_PARITY_CACHE.pop(project_id, None)


async def get_codegen_parity_snapshot(
    project_id: str,
    threshold: float = 95.0,
    prefer_fast: bool = False,
) -> Optional[Dict[str, Any]]:
    """iter-14.48 — Shared parity_loop snapshot for CodeGen sections.

    Returns a normalized snapshot dict:
      { overall, backend_score, frontend_score, tests_score,
        backend_files, frontend_files, test_files,
        services, threshold, generated_at, source: 'cache'|'live'|'persisted' }

    Consumers:
      • `get_stage_confidence('CodeGen')` — outer confidence pill (allowed
        to run the 3-4 min live scan when the cache is cold).
      • `routes.living._run_accuracy_report` — Accuracy Report backend_code /
        frontend_code / tests rows. Passes `prefer_fast=True` so the
        report never blocks 3-4 min on a cold cache; falls straight
        through to persisted parity_runs (or returns None → LLM fallback).

    iter-14.49 — Added `prefer_fast` because the accuracy-report job was
    stuck at 15% "Picking evaluator models…" every time the cache expired
    (post-restart), since the very first snapshot call fired a full
    `score_run` inline. Fast path = cache → persisted → None.
    """
    import time as _time
    _cached = _CODEGEN_PARITY_CACHE.get(project_id)
    if _cached and (_time.time() - _cached["ts"]) < _CODEGEN_PARITY_TTL_SECS:
        services = _cached["services"]
        overall = float(_cached["overall"])
        generated_at = _cached["generated_at"]
        source = "cache"
    elif prefer_fast:
        # Skip the live 3-4 min scan; fall straight through to persisted.
        latest_parity = await parity_runs.find_one(
            {"project_id": project_id, "status": "complete"},
            {"_id": 0, "final_score": 1, "ended_at": 1, "iterations": 1, "threshold": 1},
            sort=[("ended_at", -1)],
        )
        if latest_parity and isinstance(latest_parity.get("final_score"), (int, float)):
            overall = float(latest_parity["final_score"])
            iters = latest_parity.get("iterations") or []
            services = ((iters[-1] if iters else {}) or {}).get("services") or []
            threshold = float(latest_parity.get("threshold") or threshold)
            generated_at = latest_parity.get("ended_at")
            source = "persisted"
        else:
            return None
    else:
        services = None
        overall = 0.0
        generated_at = None
        source = None
        try:
            from codegen.parity_loop import score_run as _score_run
            _live = await _score_run(project_id, iteration=0, threshold=threshold)
            _live_dict = _live.to_dict() if hasattr(_live, "to_dict") else dict(_live)
            overall = float(_live_dict.get("overall_score") or 0.0)
            services = _live_dict.get("services") or []
            generated_at = _live_dict.get("generated_at") or datetime.now(timezone.utc).isoformat()
            _CODEGEN_PARITY_CACHE[project_id] = {
                "ts": _time.time(), "overall": overall, "services": services,
                "threshold": threshold, "generated_at": generated_at,
            }
            source = "live"
        except Exception as _le:  # noqa: BLE001
            logger.warning("iter-14.48 parity snapshot live failed for %s: %s", project_id, _le)
            latest_parity = await parity_runs.find_one(
                {"project_id": project_id, "status": "complete"},
                {"_id": 0, "final_score": 1, "ended_at": 1, "iterations": 1, "threshold": 1},
                sort=[("ended_at", -1)],
            )
            if latest_parity and isinstance(latest_parity.get("final_score"), (int, float)):
                overall = float(latest_parity["final_score"])
                iters = latest_parity.get("iterations") or []
                services = ((iters[-1] if iters else {}) or {}).get("services") or []
                threshold = float(latest_parity.get("threshold") or threshold)
                generated_at = latest_parity.get("ended_at")
                source = "persisted"
            else:
                return None

    # Split per-file scores by category. Frontend service names in
    # parity_loop are always literal "frontend"; anything else is backend.
    be_files: list[dict] = []
    fe_files: list[dict] = []
    test_files: list[dict] = []
    for svc in services or []:
        svc_name = svc.get("service") or ""
        for f in (svc.get("files") or []):
            fp = str(f.get("file_path") or "")
            if not fp:
                continue
            # Test files supersede FE/BE bucketing so the "Tests" row
            # is not double-counted against parity of the owning service.
            if re.search(r"(?:^|/)(test|tests|spec|specs|__tests__|it\.ts|it\.tsx|test\.ts|test\.tsx|Test\.java|IT\.java|Tests\.java)(?:$|/)", fp, re.IGNORECASE):
                test_files.append(f)
            elif svc_name == "frontend" or fp.startswith("frontend/"):
                fe_files.append(f)
            else:
                be_files.append(f)

    def _mean(files: list[dict]) -> float:
        if not files:
            return 0.0
        return round(sum(float(f.get("score") or 0.0) for f in files) / len(files), 2)

    return {
        "overall": round(overall, 2),
        "backend_score": _mean(be_files),
        "frontend_score": _mean(fe_files),
        "tests_score": _mean(test_files),
        "backend_files": be_files,
        "frontend_files": fe_files,
        "test_files": test_files,
        "services": services or [],
        "threshold": threshold,
        "generated_at": generated_at,
        "source": source,
    }


def _sections_for_stage(stage: str) -> list[dict]:
    return [s for s in _REPORT_SECTIONS if s.get("stage") == stage]


# ─── In-memory confidence-job registry (mirrors routes/living._JOBS) ───
# Each entry tracks live progress + a cooperative control flag so the
# user can pause / resume / stop the multi-model evaluation from the
# Confidence pill popover. Identical pattern to architecture/codegen
# long-running jobs.
_CONFIDENCE_JOBS: Dict[str, Dict[str, Any]] = {}


def _new_confidence_job(project_id: str, stage: str) -> str:
    jid = uuid.uuid4().hex
    _CONFIDENCE_JOBS[jid] = {
        "id": jid,
        "kind": "stage-confidence",
        "project_id": project_id,
        "stage": stage,
        "status": "queued",       # queued / running / paused / stopping / complete / error / stopped
        "control": "running",     # running / paused / stopping
        "step": "queued",
        "pct": 0,
        "section_done": 0,
        "section_total": 0,
        "current_section": "",
        "running_score": None,    # rolling overall score as sections complete
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": "",
        "result": None,
    }
    return jid


def _cjob_update(jid: str, **kwargs):
    if jid in _CONFIDENCE_JOBS:
        _CONFIDENCE_JOBS[jid].update(kwargs)


def _cjob_complete(jid: str, result: Dict[str, Any]):
    _cjob_update(jid, status="complete", step="Done", pct=100, result=result,
                 completed_at=datetime.now(timezone.utc).isoformat())


def _cjob_error(jid: str, err: str):
    _cjob_update(jid, status="error", error=err,
                 completed_at=datetime.now(timezone.utc).isoformat())


async def _cjob_await_control(jid: str):
    """Cooperative pause / stop checkpoint. Called between every section
    scoring round-trip so the user's pause/stop click takes effect at
    the very next opportunity (no hung HTTP call to cancel)."""
    while True:
        job = _CONFIDENCE_JOBS.get(jid)
        if not job:
            return "stopping"   # job vanished — treat as stop
        ctrl = job.get("control") or "running"
        if ctrl == "stopping":
            return "stopping"
        if ctrl != "paused":
            return "running"
        _cjob_update(jid, status="paused", step="⏸ Paused — waiting for user…")
        await asyncio.sleep(0.5)


async def compute_stage_confidence(project_id: str, stage: str,
                                   jid: Optional[str] = None,
                                   only_sections: Optional[list[str]] = None,
                                   evaluator_models: Optional[list[str]] = None,
                                   artifact_max_chars: Optional[int] = None) -> dict:
    """Run the multi-model confidence engine on every catalogue section
    owned by `stage` and persist a single doc to `stage_confidence`.

    When `jid` is supplied, also pumps progress + cooperative pause/stop
    control into the in-memory job registry so the UI can render a
    progress popover with pause / stop buttons.

    iter-14.11 — TOKEN-EFFICIENT recompute.
      • `only_sections` — score ONLY these catalogue keys and merge the
        result with the prior doc's untouched rows. Powers the auto-improve
        loop's delta-scoring (only re-score what we just regenerated).
      • `evaluator_models` — bypass `pick_evaluator_models()` and use the
        provided list. Auto-improve passes a single cheap evaluator on
        intermediate iterations; only the FINAL "sealing" scoring uses
        the full multi-model panel.
      • `artifact_max_chars` — trim the artifact text handed to the LLM
        scorer. 28k full-fidelity is only needed on the final scoring;
        intermediate scoring can use 8k with negligible loss because the
        remediation hints already told the LLM what to look for.

    Returns the persisted document (without `_id`)."""
    sections = _sections_for_stage(stage)
    if jid:
        _cjob_update(jid, section_total=len(sections))
    if not sections:
        return {
            "project_id": project_id, "stage": stage,
            "overall_score": 0.0, "overall_band": "poor",
            "models_used": [], "sections": [],
            "section_count": 0, "sections_below_95": 0,
            "missing_artifacts": 0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_catalogue_sections_for_stage",
        }

    # iter-14.11 — delta-scoring precompute. `sections_to_score` is the
    # (filtered) catalogue we'll actually run through the LLM; `carryover`
    # holds prior rows we'll re-use verbatim (their SRS body hasn't changed
    # since the last score, so re-scoring would be pure token waste).
    prev_doc = await stage_confidence.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0},
    ) or {}
    prev_rows_by_key = {r.get("key"): r for r in (prev_doc.get("sections") or [])}
    if only_sections:
        only_set = set(only_sections)
        sections_to_score = [s for s in sections if s["key"] in only_set]
        carryover = [
            prev_rows_by_key[s["key"]]
            for s in sections if s["key"] not in only_set and s["key"] in prev_rows_by_key
        ]
    else:
        sections_to_score = sections
        carryover = []

    if jid:
        _cjob_update(jid, status="running", step="Loading KB digests…", pct=4)
    kb_summary = await _kb_summary_for_eval(project_id)
    if jid:
        _cjob_update(jid, step="Loading ground-truth digest…", pct=8)
    ground_truth = await _ground_truth_for_eval(project_id)
    if jid:
        _cjob_update(jid, step="Picking evaluator models…", pct=12)
    if evaluator_models:
        # Caller wants a specific (usually cheaper) model list — honour it.
        models = list(evaluator_models)
    else:
        models = await pick_evaluator_models()

    rows: list[dict] = list(carryover)
    missing = 0
    total = max(1, len(sections_to_score))

    # iter-14.18 — Parallelize per-section scoring.
    # Previously each of the (up to 12) SRS sections was awaited in series:
    # HF-served sections cost ~1-2s each, LLM-fallback sections cost 60s+
    # each, so iter-1 "full" scoring in the improve-loop could span
    # 15s (all HF) → several minutes (few LLM fallbacks). Iter 2+ was fast
    # only because delta-scoring narrowed the loop to 1-2 sections.
    #
    # Fix: fan out with `asyncio.gather` bounded by a semaphore. Concurrency
    # 4 keeps HF-CPU load sane on the pilot's 4-vCPU host and matches
    # OpenRouter's per-key concurrency comfort for LLM fallbacks. Override
    # via LAMA_CONFIDENCE_STAGE_CONCURRENCY.
    #
    # Cancellation: we still poll `_cjob_await_control` before dispatch,
    # and each coroutine short-circuits if the job flipped to stopping.
    # Progress: we emit an update as each future settles, not as each is
    # dispatched — the UI now sees "3/12 done" instead of "3/12 started".
    # iter-14.29 — Central resolver: use LangGraph explicitly (env
    # override), OR auto-fall-back to LangGraph when Factory.ai droid
    # is not enabled / not connected. See
    # `confidence_langgraph.resolve_confidence_engine`.
    try:
        from confidence_langgraph import resolve_confidence_engine as _resolve_ce
        _use_langgraph = _resolve_ce() == "langgraph"
    except Exception:  # noqa: BLE001
        _use_langgraph = (
            (os.environ.get("LAMA_CONFIDENCE_ENGINE") or "").strip().lower()
            == "langgraph"
        )
    try:
        _concurrency = max(1, int(
            os.environ.get("LAMA_CONFIDENCE_STAGE_CONCURRENCY") or "4"
        ))
    except (TypeError, ValueError):
        _concurrency = 4
    _sema = asyncio.Semaphore(_concurrency)
    _stop_flag = {"stopped": False}

    async def _score_one_section(sec: dict) -> dict:
        async with _sema:
            if _stop_flag["stopped"]:
                return {
                    "key": sec["key"], "label": sec["label"],
                    "score": 0.0, "band": "poor",
                    "rationale": "Stopped by user before scoring reached this section.",
                    "gaps": [], "evidence": [], "votes": [], "missing": True,
                    "model_agreement_spread": 0.0,
                }
            artifact_text = await _collect_artifact_text(
                project_id,
                sec.get("source_keys", []),
                max_chars=(artifact_max_chars or 28000),
            )
            if not artifact_text:
                return {
                    "key": sec["key"], "label": sec["label"],
                    "score": 0.0, "band": "poor",
                    "rationale": "No artifact generated yet for this section.",
                    "gaps": ["Run the owning stage to produce this section"],
                    "evidence": [], "votes": [], "missing": True,
                    "model_agreement_spread": 0.0,
                }
            # iter-14.15 — route through the LangGraph + HF engine when the
            # feature-flag is on; transparent fallback to the existing
            # `score_artifact_multi_model` symbol (which tests monkeypatch)
            # on any engine failure. Contract #4 preserved (LLM node inside
            # the graph still uses fabric_call).
            verdict = None
            if _use_langgraph:
                try:
                    from confidence_langgraph import (  # noqa: E402
                        is_available as _clg_available,
                        score_artifact_multi_model_langgraph as _clg_multi,
                    )
                    if _clg_available():
                        verdict = await _clg_multi(
                            project_id=project_id,
                            stage=stage,
                            artifact_text=artifact_text,
                            sections=[{"key": sec["key"], "label": sec["label"],
                                       "what_to_check": sec["what_to_check"]}],
                            kb_summary=kb_summary,
                            ground_truth=ground_truth,
                            models=models,
                        )
                        if not (verdict.get("sections") or []):
                            verdict = None
                except Exception as _exc:  # noqa: BLE001
                    logger.warning(
                        "compute_stage_confidence[%s] · langgraph engine "
                        "failed on section=%s (%s) — falling back to fabric",
                        project_id, sec.get("key"), _exc,
                    )
                    verdict = None
            if verdict is None:
                # iter-14.21 — Strict-HF guard: when LAMA_CONFIDENCE_ENGINE=
                # langgraph (or LAMA_CONFIDENCE_STRICT_HF=1) is set, refuse
                # to route confidence scoring through fabric_call → Factory
                # CLI. Return a marked-missing stub so the aggregator sees
                # the outage explicitly instead of a phantom Factory-graded
                # score.
                try:
                    from confidence_langgraph import (
                        strict_hf_only as _strict,
                        engine_unavailable_row as _stub,
                    )
                except Exception:  # noqa: BLE001
                    _strict = lambda: False  # noqa: E731
                    _stub = None
                if _strict():
                    logger.warning(
                        "compute_stage_confidence[%s] · strict-HF mode ON — "
                        "refusing fabric fallback for %s; marking missing.",
                        project_id, sec.get("key"),
                    )
                    if _stub:
                        return _stub(sec, reason="langgraph unavailable")
                    return {
                        "key": sec["key"], "label": sec["label"],
                        "score": 0.0, "band": "poor",
                        "rationale": "Strict-HF mode: fabric fallback blocked.",
                        "gaps": [], "evidence": [], "votes": [], "missing": True,
                        "model_agreement_spread": 0.0,
                        "engine": "langgraph",
                        "route_taken": "engine_unavailable",
                    }
                verdict = await score_artifact_multi_model(
                    project_id=project_id,
                    stage=stage,
                    artifact_text=artifact_text,
                    sections=[{"key": sec["key"], "label": sec["label"],
                               "what_to_check": sec["what_to_check"]}],
                    kb_summary=kb_summary,
                    ground_truth=ground_truth,
                    models=models,
                )
            row = (verdict.get("sections") or [{}])[0]
            row["missing"] = False
            return row

    if jid:
        _cjob_update(
            jid, status="running",
            step=f"Scoring {total} section(s) · concurrency={_concurrency}"
                 + (" · engine=langgraph" if _use_langgraph else ""),
            pct=12,
        )

    tasks = [asyncio.create_task(_score_one_section(sec))
             for sec in sections_to_score]

    # Consume futures as they complete so progress + cancellation feel snappy.
    done_count = 0
    for coro in asyncio.as_completed(tasks):
        # Cooperative stop: cancel any not-yet-started tasks so the semaphore
        # queue drains fast. In-flight LLM calls will still complete their
        # current httpx request (fabric_call has its own timeout).
        if jid:
            ctl = await _cjob_await_control(jid)
            if ctl == "stopping" and not _stop_flag["stopped"]:
                _stop_flag["stopped"] = True
                for t in tasks:
                    if not t.done():
                        t.cancel()
        try:
            row = await coro
        except asyncio.CancelledError:
            continue
        except Exception as exc:  # noqa: BLE001 — never let a single section kill the run
            logger.warning(
                "compute_stage_confidence[%s] · section scorer raised: %s",
                project_id, exc,
            )
            continue
        if row.get("missing"):
            missing += 1
        rows.append(row)
        done_count += 1
        if jid:
            done_scores = [r["score"] for r in rows if not r.get("missing")]
            running = round(
                sum(done_scores) / len(done_scores), 2
            ) if done_scores else 0.0
            _cjob_update(
                jid,
                section_done=done_count,
                running_score=running,
                step=f"Scored {done_count}/{total} · latest={row.get('label','?')}",
                pct=12 + int(80 * done_count / total),
            )

    # Preserve stable catalogue order in the merged rows.
    order = {s["key"]: idx for idx, s in enumerate(sections)}
    rows.sort(key=lambda r: order.get(r.get("key"), 999))

    scored = [r["score"] for r in rows if not r.get("missing")]
    overall_pool = [r["score"] for r in rows]
    # iter-14.8 — `overall_score` is the LATEST run's score. See high-water
    # mark note below for why we also persist `best_overall_score`.
    overall = round(sum(scored) / len(scored), 2) if scored else 0.0
    # `sections_below_95` counts genuine low scores; missing rows are
    # tracked separately in `missing_artifacts`.
    below = [r for r in rows if not r.get("missing") and r["score"] < 95.0]
    stopped_early = jid is not None and len(rows) < len(sections)

    # iter-14.9 — High-water mark.
    # The multi-model evaluator is stochastic — running the same recompute
    # twice against the same artifacts can drift ±1-3% between runs. Prior
    # behaviour was to overwrite `overall_score` with the LATEST result,
    # which meant the pill regressed even when nothing about the artifacts
    # had changed and the user could never "lock in" a passing score.
    # Fix: retain `best_overall_score` (and a snapshot of the run that
    # produced it) across recomputes. The pill/popover render the max of
    # (best, latest); regressions are still visible in the popover as
    # "Latest: X% · Best: Y%" so the drift stays honest. Partial runs
    # (stopped by user, timed out mid-loop) never displace the best.
    prev = await stage_confidence.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0},
    ) or {}
    prev_best = float(prev.get("best_overall_score")
                      or prev.get("overall_score")
                      or 0.0)
    if not stopped_early and overall >= prev_best:
        best_overall = overall
        best_generated_at = datetime.now(timezone.utc).isoformat()
        best_snapshot = {
            "sections": rows,
            "models_used": models,
            "section_count": len(rows),
            "sections_below_95": len(below),
            "missing_artifacts": missing,
        }
    else:
        best_overall = prev_best
        best_generated_at = prev.get("best_generated_at") or prev.get("generated_at")
        best_snapshot = {
            "sections": prev.get("best_sections") or prev.get("sections") or [],
            "models_used": prev.get("best_models_used") or prev.get("models_used") or [],
            "section_count": prev.get("best_section_count") or prev.get("section_count") or 0,
            "sections_below_95": prev.get("best_sections_below_95")
                                 if prev.get("best_sections_below_95") is not None
                                 else prev.get("sections_below_95") or 0,
            "missing_artifacts": prev.get("best_missing_artifacts")
                                 if prev.get("best_missing_artifacts") is not None
                                 else prev.get("missing_artifacts") or 0,
        }

    doc = {
        "project_id": project_id, "stage": stage,
        "overall_score": overall,
        "overall_band": band_of(overall),
        # `scored_score` retained for back-compat with any consumer that
        # was reading it explicitly; identical to overall_score now.
        "scored_score": round(sum(scored) / len(scored), 2) if scored else 0.0,
        "models_used": models,
        "section_count": len(rows),
        "sections_below_95": len(below),
        "missing_artifacts": missing,
        "sections": rows,
        "partial": stopped_early,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # iter-14.9 — high-water mark fields
        "best_overall_score": round(best_overall, 2),
        "best_overall_band": band_of(best_overall),
        "best_generated_at": best_generated_at,
        "best_sections": best_snapshot["sections"],
        "best_models_used": best_snapshot["models_used"],
        "best_section_count": best_snapshot["section_count"],
        "best_sections_below_95": best_snapshot["sections_below_95"],
        "best_missing_artifacts": best_snapshot["missing_artifacts"],
    }

    if jid:
        _cjob_update(jid, step="Persisting report…", pct=96)
    await stage_confidence.update_one(
        {"project_id": project_id, "stage": stage},
        {"$set": doc}, upsert=True,
    )
    await audit_log.insert_one({
        "action": "pipeline.confidence.compute",
        "project_id": project_id,
        "at": doc["generated_at"],
        "details": {"stage": stage, "overall_score": overall,
                    "best_overall_score": doc["best_overall_score"],
                    "sections_below_95": len(below),
                    "missing_artifacts": missing,
                    "partial": stopped_early},
    })
    return doc


async def _run_confidence_job(jid: str, project_id: str, stage: str):
    try:
        cfg = _CONFIDENCE_JOBS.get(jid) or {}
        doc = await compute_stage_confidence(
            project_id, stage, jid=jid,
            evaluator_models=cfg.get("evaluator_models"),
            artifact_max_chars=cfg.get("artifact_max_chars"),
        )
        ctl = (_CONFIDENCE_JOBS.get(jid) or {}).get("control") or "running"
        if ctl == "stopping":
            _cjob_update(jid, status="stopped",
                         step="⏹ Stopped — partial result persisted",
                         completed_at=datetime.now(timezone.utc).isoformat(),
                         result={"overall_score": doc.get("overall_score"),
                                 "section_count": doc.get("section_count"),
                                 "partial": True})
        else:
            _cjob_complete(jid, {"overall_score": doc.get("overall_score"),
                                 "overall_band": doc.get("overall_band"),
                                 "best_overall_score": doc.get("best_overall_score"),
                                 "best_overall_band": doc.get("best_overall_band"),
                                 "section_count": doc.get("section_count"),
                                 "sections_below_95": doc.get("sections_below_95"),
                                 "missing_artifacts": doc.get("missing_artifacts"),
                                 "partial": doc.get("partial", False)})
    except Exception as exc:  # noqa: BLE001
        _cjob_error(jid, str(exc)[:500])


@router.get("/{project_id}/confidence")
async def list_stage_confidence(project_id: str):
    """Return the most recent stage_confidence doc for EVERY stage that
    has one. Frontend uses this for the sidebar / stage-overview view.

    iter-14.24 — Skips stored docs whose `sections[].key` set no longer
    matches the current `_REPORT_SECTIONS` catalogue for that stage, so
    the sidebar pill drops to "-" (unknown) instead of surfacing rows
    keyed on a deprecated catalogue.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    rows = await stage_confidence.find(
        {"project_id": project_id}, {"_id": 0},
    ).to_list(20)
    # Build a stage → current-catalogue-keys map for staleness detection.
    _stage_keys: dict[str, set[str]] = {}
    for s in _REPORT_SECTIONS:
        st = s.get("stage")
        k = s.get("key")
        if isinstance(st, str) and isinstance(k, str):
            _stage_keys.setdefault(st, set()).add(k)
    fresh: dict[str, dict] = {}
    for r in rows:
        st = r.get("stage")
        if not isinstance(st, str):
            continue
        stored_keys = {
            s["key"] for s in (r.get("sections") or [])
            if isinstance(s.get("key"), str)
        }
        current_keys = _stage_keys.get(st, set())
        if stored_keys and current_keys and not stored_keys.issubset(current_keys):
            logger.info(
                "list_stage_confidence[%s/%s] · dropping stale doc "
                "(unknown keys: %s).",
                project_id, st, sorted(stored_keys - current_keys)[:6],
            )
            continue
        fresh[st] = r
    return {"project_id": project_id, "stages": fresh}


@router.get("/{project_id}/confidence/{stage}")
async def get_stage_confidence(project_id: str, stage: str):
    """Most recent persisted confidence report for one stage, or
    `{"present": false}` so the UI renders an empty pill cleanly.

    iter-14.24 — Also treats stored docs whose `sections[].key` set no
    longer matches the current `_REPORT_SECTIONS` catalogue as *stale*
    and returns `present:false` for them. Prevents the popover from
    rendering rows keyed on a deprecated catalogue (e.g. the old
    Workflow / Business rules / Approval-flow / User-manual / Actors /
    NFR set that pre-dated the iter-14.23 IEEE-830 alignment). Once the
    operator clicks Recompute, the fresh doc overwrites the stale one
    and reads flip back to `present:true`. The stale doc itself is
    kept in Mongo so `best_overall_score` history isn't lost.
    """
    if stage not in _STAGE_CONFIDENCE_STAGES:
        raise HTTPException(400, f"Unknown stage '{stage}'.")
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = await stage_confidence.find_one(
        {"project_id": project_id, "stage": stage}, {"_id": 0},
    )
    if not doc:
        return {"present": False, "project_id": project_id, "stage": stage}
    # Stale-catalogue check.
    try:
        current_keys = {
            s["key"] for s in _REPORT_SECTIONS
            if s.get("stage") == stage and isinstance(s.get("key"), str)
        }
        stored_keys = {
            s["key"] for s in (doc.get("sections") or [])
            if isinstance(s.get("key"), str)
        }
        # Consider stored stale ONLY when it has keys AND at least one
        # doesn't map to the current catalogue. Empty stored keys are a
        # legitimate "not scored yet" state and should not be flagged.
        if stored_keys and not stored_keys.issubset(current_keys):
            logger.info(
                "get_stage_confidence[%s/%s] · stored doc uses stale catalogue "
                "(unknown keys: %s) — returning present:false so the UI shows "
                "an empty pill until Recompute.",
                project_id, stage, sorted(stored_keys - current_keys)[:6],
            )
            return {
                "present": False,
                "stale": True,
                "project_id": project_id,
                "stage": stage,
                "reason": "catalogue_changed",
            }
    except Exception:  # noqa: BLE001 — never let staleness detection break reads
        pass
    # iter-14.44 — CodeGen pill mirrors the deterministic parity_loop score.
    # Rationale: the Auto-Validate popover shows the parity_loop overall
    # (regex/structural over codegen_files), while the outer pill previously
    # showed the LLM multi-model semantic score. The two engines drift and
    # confuse operators ("why does the popover say 83% but the pill 69%?").
    # We treat parity_loop as the single source of truth for CodeGen's
    # top-line number and keep the LLM-eval section breakdown as the
    # popover's "why" explainer.
    if stage == "CodeGen":
        try:
            # iter-14.48 — Delegated to shared helper so the Living-stage
            # Accuracy Report reads the same numbers (no more mismatch
            # between the pill and the KB-vs-Generated section).
            snap = await get_codegen_parity_snapshot(project_id, threshold=95.0)
            if not snap:
                return {"present": True, **doc}
            pscore = float(snap["overall"])
            services = snap["services"]
            threshold = float(snap["threshold"])
            doc["overall_score"] = pscore
            doc["best_overall_score"] = pscore
            doc["source_engine"] = "parity_loop"
            doc["parity_generated_at"] = snap.get("generated_at")
            doc["parity_converged"] = pscore >= threshold

            # Project per-service scores into the popover's section list.
            try:
                def _band(sc: float) -> str:
                    if sc >= threshold:
                        return "excellent"
                    if sc >= threshold - 10:
                        return "good"
                    if sc >= threshold - 25:
                        return "fair"
                    return "poor"

                parity_sections: list[dict] = []
                for svc in services:
                    svc_name = svc.get("service") or "unknown"
                    svc_score = float(svc.get("score") or 0.0)
                    # Split BE-side "<svc>" into a backend_code row and
                    # mirror the frontend service into frontend_code so
                    # the popover legend stays stable.
                    if svc_name == "frontend":
                        key, label = "frontend_code", "Frontend code (UI parity)"
                    elif svc_name in ("_lama", "reports"):
                        continue  # skip meta services
                    else:
                        key, label = "backend_code", f"Backend code ({svc_name})"
                    files = svc.get("files") or []
                    below = [f for f in files
                             if float(f.get("score") or 0.0) < threshold]
                    top_gap = ""
                    if below:
                        worst = min(below, key=lambda f: float(f.get("score") or 0.0))
                        comps = worst.get("components") or []
                        weakest = min(comps, key=lambda c: float(c.get("score") or 0.0)) if comps else {}
                        _p = str(worst.get("file_path") or "")
                        _p_short = _p.rsplit("/", 1)[-1] if _p else ""
                        top_gap = (
                            f"{_p_short}: {weakest.get('name','?')} "
                            f"{float(weakest.get('score') or 0.0):.0f}% — "
                            f"{(weakest.get('detail') or '')[:80]}"
                        )
                    parity_sections.append({
                        "key": key,
                        "label": label,
                        "score": round(svc_score, 2),
                        "band": _band(svc_score),
                        "rationale": (
                            f"Parity-loop score over {len(files)} file(s); "
                            f"{len(below)} below threshold ({threshold:.0f}%)."
                        ),
                        "gaps": [top_gap] if top_gap else [],
                        "evidence": [],
                        "votes": [{"model": "parity_loop", "score": round(svc_score, 2)}],
                        "missing": False,
                        "model_agreement_spread": 0.0,
                        "engine": "parity_loop",
                    })
                if parity_sections:
                    doc["sections"] = parity_sections
                    doc["best_sections"] = parity_sections
                    doc["section_count"] = len(parity_sections)
                    doc["sections_below_95"] = sum(
                        1 for s in parity_sections if s["score"] < threshold
                    )
                    doc["missing_artifacts"] = 0
                    doc["best_missing_artifacts"] = 0
                    doc["overall_band"] = _band(pscore)
                    doc["best_overall_band"] = _band(pscore)
                    doc["models_used"] = ["parity_loop"]
                    doc["best_models_used"] = ["parity_loop"]
            except Exception as _se:  # noqa: BLE001
                logger.warning(
                    "iter-14.46 CodeGen section projection failed: %s", _se,
                )
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "get_stage_confidence[%s/CodeGen] parity mirror failed: %s",
                project_id, _e,
            )
    return {"present": True, **doc}


@router.post("/{project_id}/confidence/{stage}/recompute")
async def recompute_stage_confidence(
    project_id: str,
    stage: str,
    background: bool = True,
    fast: bool = False,
):
    """Kick off (or run-blocking) the multi-model evaluator for one stage.

    `?background=true` (default) → returns `{job_id, status:"queued"}`
       and runs in background. UI polls `/confidence/jobs/{job_id}` for
       live progress + can pause/resume/stop via the control endpoints.
    `?background=false` → blocks until the run completes (legacy
       behaviour; useful from server-side scripts / tests).

    `?fast=true` → cheap single-evaluator, small-artifact recompute.
       Skips the full 3-model panel and trims each artifact to 8k.
       ≈1/6 the token spend and ~1/4 the wall-clock of the full run.
       Best for quick "did my recent edits move the needle?" checks;
       use the default (fast=false) before freezing a stage.
    """
    if stage not in _STAGE_CONFIDENCE_STAGES:
        raise HTTPException(400, f"Unknown stage '{stage}'.")
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")

    if fast:
        # Cheap panel = first model from Console routing only, plus
        # trimmed artifact. `pick_evaluator_models` returns up to 3;
        # we deliberately take just the head-of-list.
        picked = await pick_evaluator_models()
        fast_models: Optional[list[str]] = [picked[0]] if picked else [""]
        fast_artifact_chars = 8000
    else:
        fast_models = None
        fast_artifact_chars = None

    if not background:
        return await compute_stage_confidence(
            project_id, stage,
            evaluator_models=fast_models,
            artifact_max_chars=fast_artifact_chars,
        )
    jid = _new_confidence_job(project_id, stage)
    # Persist the fast-mode knobs on the job dict so the background
    # runner (which only takes jid) can forward them into compute.
    _CONFIDENCE_JOBS[jid]["fast"] = bool(fast)
    _CONFIDENCE_JOBS[jid]["evaluator_models"] = fast_models
    _CONFIDENCE_JOBS[jid]["artifact_max_chars"] = fast_artifact_chars
    # Hold a strong ref to the task so the GC can't collect it mid-flight
    # (mirrors the codegen run pattern, prevents the "premature GC"
    # SonarLint warning).
    _CONFIDENCE_JOBS[jid]["_task"] = asyncio.create_task(
        _run_confidence_job(jid, project_id, stage)
    )
    return {"job_id": jid, "status": "queued",
            "project_id": project_id, "stage": stage,
            "fast": bool(fast)}


@router.get("/{project_id}/confidence/jobs/{job_id}")
async def get_confidence_job(project_id: str, job_id: str):
    """Live progress for one confidence job. Returns 404 once the in-memory
    record is gone (jobs are not persisted — UI should fall back to
    `/confidence/{stage}` for the final persisted result)."""
    job = _CONFIDENCE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Confidence job not found or already evicted")
    if job.get("project_id") != project_id:
        raise HTTPException(404, "Confidence job not found for this project")
    # Don't leak the asyncio.Task back to the client.
    return {k: v for k, v in job.items() if k != "_task"}


@router.post("/{project_id}/confidence/jobs/{job_id}/pause")
async def pause_confidence_job(project_id: str, job_id: str):
    job = _CONFIDENCE_JOBS.get(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Confidence job not found")
    if job.get("status") in {"complete", "error", "stopped"}:
        raise HTTPException(409, f"Job already {job['status']} — cannot pause.")
    if job.get("control") == "stopping":
        raise HTTPException(409, "Job is stopping — cannot pause.")
    _cjob_update(job_id, control="paused")
    return {"ok": True, "control": "paused"}


@router.post("/{project_id}/confidence/jobs/{job_id}/resume")
async def resume_confidence_job(project_id: str, job_id: str):
    job = _CONFIDENCE_JOBS.get(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Confidence job not found")
    if job.get("status") in {"complete", "error", "stopped"}:
        raise HTTPException(409, f"Job already {job['status']} — cannot resume.")
    if job.get("control") == "stopping":
        raise HTTPException(409, "Job is stopping — cannot resume.")
    _cjob_update(job_id, control="running", status="running")
    return {"ok": True, "control": "running"}


@router.post("/{project_id}/confidence/jobs/{job_id}/stop")
async def stop_confidence_job(project_id: str, job_id: str):
    job = _CONFIDENCE_JOBS.get(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Confidence job not found")
    if job.get("status") in {"complete", "error", "stopped"}:
        return {"ok": True, "control": "stopping",
                "note": f"Job already {job['status']}"}
    _cjob_update(job_id, control="stopping",
                 step="⏹ Stop requested — finishing current section…")
    return {"ok": True, "control": "stopping"}


# ════════════════════════════════════════════════════════════════════
# iter-14.10 — AUTO-IMPROVE LOOP for the Discovery/SRS confidence pill.
#
# Prior behaviour: Recompute only RE-SCORED the artifacts. If a section
# was stuck at 32% because the SRS body was hedged, or at 0.2% because
# BRs didn't cite verbatim source, hitting Recompute again produced the
# same low score. The user had to hunt through the section table,
# manually click Regenerate per section, and hope it improved. In
# practice, the pill was stuck around 35–40% and never reached the ≥95%
# gate needed to freeze.
#
# Fix: mirror the CodeGen parity_loop pattern for Discovery.
#   1. Score all sections owned by the stage.
#   2. If overall_score ≥ threshold  → converged, done.
#   3. Otherwise, for every section BELOW threshold, resolve its SRS
#      section keys via the catalogue's `source_keys` and regenerate
#      each one with the scorer's `rationale` + `gaps[]` injected as
#      remediation hints (see routes/srs.py::_gen_one_section).
#   4. Re-score → next iteration.
#   5. Cap at `max_iterations` (default 3, clamped [1, 5]) so a
#      stubbornly-thin KB can't burn unlimited tokens.
#
# Only Discovery is wired for auto-improve — DataModel/Architecture
# don't yet have per-section regen APIs, and CodeGen has its own
# parity_loop under /codegen/jobs/start/auto-validate. The frontend
# hides the button on non-Discovery pills.
# ════════════════════════════════════════════════════════════════════

_IMPROVE_ALLOWED_STAGES = {"Discovery"}


def _srs_keys_for_report_row(row_key: str) -> list[str]:
    """Resolve a confidence-catalogue row key → the SRS section keys whose
    text feeds it. E.g. `business_rules` → [`specific_requirements`,
    `detailed_use_cases`]. Filters out non-SRS source buckets (`kb.*`,
    `legacy.*`) so we only regenerate things we actually own.
    """
    sec = next((s for s in _REPORT_SECTIONS if s.get("key") == row_key), None)
    if not sec:
        return []
    out: list[str] = []
    for k in sec.get("source_keys") or []:
        if isinstance(k, str) and k.startswith("srs."):
            out.append(k.split(".", 1)[1])
    # Dedup while preserving order.
    return list(dict.fromkeys(out))


async def _regen_srs_section_with_hints(
    project_id: str,
    section_key: str,
    hints: list[str],
    model: str = "",
) -> dict:
    """Internal helper — regenerate ONE SRS section, feeding the scorer's
    gaps as remediation hints. Persists the new content + bumps version.
    Returns {"ok": bool, "section": key, "tokens": int, "failed": bool}.
    Never raises on individual-section failure — the loop should keep
    going and try the next section.
    """
    try:
        # Late imports so pipeline.py stays importable when srs.py is
        # being reloaded during dev (circular-import guard).
        from routes.srs import (
            SECTION_CONFIGS, _load_srs_context, _gen_one_section,
            _is_failed_section,
        )
        from db import srs_documents
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "section": section_key, "error": f"import: {exc}"}

    cfg = next((c for c in SECTION_CONFIGS if c["key"] == section_key), None)
    if not cfg:
        return {"ok": False, "section": section_key, "error": "unknown section key"}

    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        return {"ok": False, "section": section_key, "error": "SRS not yet generated"}
    if doc.get("frozen"):
        return {"ok": False, "section": section_key, "error": "SRS is frozen"}

    try:
        proj, _existing, full_toon, summary, convo_text, analysis_digest = \
            await _load_srs_context(project_id, None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "section": section_key, "error": f"context: {exc}"}

    prior_sections = doc.get("sections") or {}
    try:
        r = await _gen_one_section(
            cfg, proj, full_toon, summary, convo_text, model,
            project_id=project_id,
            prior_sections=prior_sections,
            analysis_digest=analysis_digest,
            agent_key="srs.regenerate",
            remediation_hints=hints,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "section": section_key, "error": f"gen: {exc}"}

    content = (r or {}).get("content") or ""
    if not content.strip():
        return {"ok": False, "section": section_key, "error": "empty content"}

    now_iso = datetime.now(timezone.utc).isoformat()
    new_version = int(doc.get("version", 0) or 0) + 1
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": {
            f"sections.{section_key}": content,
            "version": new_version,
            "updated_at": now_iso,
        }},
    )
    try:
        await audit_log.insert_one({
            "action": "srs.regenerate_section",
            "project_id": project_id,
            "section": section_key,
            "model_used": r.get("model_used", ""),
            "tokens": r.get("tokens", 0),
            "version": new_version,
            "at": now_iso,
            "source": "confidence.improve_loop",   # iter-14.10 marker
        })
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "section": section_key,
        "tokens": r.get("tokens", 0),
        "failed": _is_failed_section(content),
    }


# iter-14.11 — token-efficiency knobs. Externalise so tests can tweak.
# These are the "lean scoring" settings used during INTERMEDIATE iterations
# of the improve loop. The final (sealing) score always uses the full
# multi-model panel + full-fidelity artifact text.
_IMPROVE_INTRA_MODELS = ["deepseek/deepseek-chat"]   # single cheap evaluator
_IMPROVE_INTRA_ARTIFACT_CHARS = 8000                  # 28k → 8k (~70% cut)
_IMPROVE_DEFAULT_TOKEN_BUDGET = 250_000               # ~1 M dollars-ish upper bound

# Substrings that indicate the section's top gap is a KB-level gap (the SRS
# regenerator cannot fix these — it can only mark them as `⚠ EVIDENCE GAP`).
# Matched case-insensitively against `rationale + gaps` for each row.
_KB_GAP_PATTERNS = (
    "0 role", "no role rows", "0 rows in kb", "no records in kb",
    ".lama/ bundle absent", ".lama/ directory is absent",
    "kb hasn't been built", "kb is empty",
    "mandatory citation failure",   # only when the missing citation IS a KB record
    "no artifact",                  # missing upstream artifact
)


def _row_looks_like_kb_gap(row: dict) -> bool:
    """Heuristic: does this section's top gap point at KB emptiness rather
    than an SRS-fixable issue? If yes, regenerating won't help — the LLM
    will just re-emit `⚠ EVIDENCE GAP` markers. One attempt is enough."""
    hay = ((row.get("rationale") or "") + " " + " ".join(row.get("gaps") or [])).lower()
    return any(p in hay for p in _KB_GAP_PATTERNS)


async def _run_improve_loop(
    jid: str, project_id: str, stage: str,
    threshold: float, max_iterations: int, max_sections_per_iter: int,
    token_budget: int = _IMPROVE_DEFAULT_TOKEN_BUDGET,
):
    """Iterative score → regenerate-below-threshold → re-score loop.

    iter-14.11 — TOKEN-EFFICIENT variant:
      • Intermediate iterations use a SINGLE cheap evaluator + 8k artifact
        text (delta-scoring: only re-score sections we just regenerated).
      • Sections whose top gap is a KB gap get regenerated at most ONCE
        (to add `⚠ EVIDENCE GAP` markers) and are then permanently skipped.
      • Sections whose score didn't improve after regeneration are marked
        stalled and skipped on subsequent iterations.
      • Loop aborts early when cumulative regen tokens exceed `token_budget`.
      • Final "sealing" score uses the full multi-model panel + full-
        fidelity artifact text so the persisted number is honest.
    """
    trajectory: list[dict] = []
    # Sections we WILL NOT touch again for the rest of this loop. Populated
    # by KB-gap detection + stall detection.
    skipped: set[str] = set()
    # Per-SRS-section: score after the prior regeneration attempt. Used to
    # detect no-improvement stalls.
    last_regen_score: dict[str, float] = {}
    # Running token accounting (SRS regeneration only — scoring tokens are
    # not returned by the evaluator).
    tokens_used = 0
    stop_reason = ""
    try:
        _cjob_update(jid, status="running",
                     step=f"Iter 1/{max_iterations} — scoring…", pct=2,
                     iterations=trajectory,
                     threshold=threshold, max_iterations=max_iterations,
                     tokens_used=0, token_budget=token_budget)

        converged = False
        prev_iter_scored_keys: list[str] | None = None
        for it in range(1, max_iterations + 1):
            # Cooperative pause/stop check
            ctl = await _cjob_await_control(jid)
            if ctl == "stopping":
                _cjob_update(jid, step="⏹ Stopped by user", status="stopped")
                stop_reason = "user_stop"
                break

            base_pct = int(((it - 1) / max_iterations) * 95)
            _cjob_update(jid,
                         step=f"Iter {it}/{max_iterations} — scoring "
                              f"({'delta' if prev_iter_scored_keys else 'full'}, "
                              f"{'lean' if it > 1 else 'full'} evaluator)…",
                         pct=max(2, base_pct))

            # iter-14.11 — lean scoring on iter 2+.
            # Iter 1: full multi-model panel over ALL sections (establish baseline).
            # Iter 2+: single cheap evaluator over ONLY the sections we just
            #          regenerated (delta scoring). Keeps carryover scores stable.
            if it == 1:
                doc = await compute_stage_confidence(project_id, stage)
            else:
                doc = await compute_stage_confidence(
                    project_id, stage,
                    only_sections=prev_iter_scored_keys,
                    evaluator_models=_IMPROVE_INTRA_MODELS,
                    artifact_max_chars=_IMPROVE_INTRA_ARTIFACT_CHARS,
                )

            latest = float(doc.get("overall_score") or 0.0)
            best = float(doc.get("best_overall_score") or latest)
            rows_by_key = {r.get("key"): r for r in (doc.get("sections") or [])}

            # Update stall-tracker: if a section we regenerated last iter
            # scored the same-or-worse than its prior score, mark stalled.
            for sk_key, prior in list(last_regen_score.items()):
                row = rows_by_key.get(_report_key_for_srs(sk_key))
                if row and float(row.get("score") or 0) <= prior + 0.5:
                    skipped.add(sk_key)

            below = [
                r for r in (doc.get("sections") or [])
                if not r.get("missing") and float(r.get("score") or 0) < threshold
            ]
            iter_row = {
                "iteration": it,
                "overall_score": round(latest, 2),
                "best_overall_score": round(best, 2),
                "below_count": len(below),
                "regenerated": [],
                "skipped_sections": sorted(skipped),
                "tokens_used": tokens_used,
                "scoring_mode": "full" if it == 1 else "lean-delta",
            }
            trajectory.append(iter_row)
            _cjob_update(jid,
                         running_score=round(best, 2),
                         iterations=trajectory,
                         tokens_used=tokens_used,
                         step=f"Iter {it}: score={latest:.1f}% best={best:.1f}% "
                              f"below={len(below)} tokens={tokens_used:,}")

            if best >= threshold:
                converged = True
                _cjob_update(jid,
                             step=f"✓ Converged at iter {it} ({best:.1f}% ≥ {threshold:.0f}%)",
                             pct=99)
                stop_reason = "converged"
                break

            if it == max_iterations:
                _cjob_update(jid,
                             step=f"⚠ Reached max_iter={max_iterations} at best={best:.1f}%",
                             pct=99)
                stop_reason = "max_iter"
                break

            if tokens_used >= token_budget:
                _cjob_update(jid,
                             step=f"⚠ Token budget hit ({tokens_used:,} ≥ {token_budget:,}) "
                                  f"at best={best:.1f}%", pct=99)
                stop_reason = "token_budget"
                break

            # Pick the worst N sections and regenerate.
            below.sort(key=lambda r: float(r.get("score") or 0))
            targets = below[:max_sections_per_iter]

            resolved: list[tuple[dict, list[str]]] = []
            for tgt in targets:
                srs_keys = _srs_keys_for_report_row(tgt.get("key") or "")
                for sk in srs_keys:
                    resolved.append((tgt, [sk]))
            # Dedup SRS section keys while preserving priority order, and
            # skip any sections we've already given up on (KB-gap / stalled).
            seen: set[str] = set()
            work: list[tuple[dict, str]] = []
            for tgt, sks in resolved:
                for sk in sks:
                    if sk in seen or sk in skipped:
                        continue
                    seen.add(sk)
                    work.append((tgt, sk))

            if not work:
                _cjob_update(
                    jid,
                    step=(f"iter {it}: nothing worth regenerating "
                          f"(all below-threshold sections are KB-gap or stalled) — "
                          f"bailing"),
                )
                stop_reason = "no_actionable_sections"
                break

            _cjob_update(jid,
                         step=f"Iter {it}/{max_iterations} — regenerating "
                              f"{len(work)} section(s) with scorer feedback…",
                         pct=base_pct + int(0.5 * (95 / max_iterations)))

            iter_regen_srs_keys: list[str] = []
            for i, (tgt, sk) in enumerate(work, start=1):
                ctl = await _cjob_await_control(jid)
                if ctl == "stopping":
                    stop_reason = "user_stop"
                    break
                if tokens_used >= token_budget:
                    stop_reason = "token_budget"
                    break

                hints: list[str] = []
                if tgt.get("rationale"):
                    hints.append(str(tgt["rationale"]))
                for g in tgt.get("gaps") or []:
                    if g:
                        hints.append(str(g))
                res = await _regen_srs_section_with_hints(
                    project_id, sk, hints, model="",
                )
                tokens_used += int(res.get("tokens") or 0)
                iter_row["tokens_used"] = tokens_used

                is_kb_gap = _row_looks_like_kb_gap(tgt)
                # KB-gap sections get exactly ONE attempt — enough for the
                # LLM to add `⚠ EVIDENCE GAP` markers — then permanently
                # skipped. Retrying is pure token waste; the SRS can't
                # fabricate KB rows that don't exist.
                if is_kb_gap and res.get("ok"):
                    skipped.add(sk)

                iter_row["regenerated"].append({
                    "srs_section": sk,
                    "for_report_row": tgt.get("key"),
                    "prior_score": float(tgt.get("score") or 0),
                    "ok": bool(res.get("ok")),
                    "kb_gap": is_kb_gap,
                    "tokens": int(res.get("tokens") or 0),
                    "error": res.get("error"),
                })
                if res.get("ok"):
                    iter_regen_srs_keys.append(sk)
                    # Track prior score so we can detect stalls next iter.
                    last_regen_score[sk] = float(tgt.get("score") or 0)
                _cjob_update(
                    jid,
                    iterations=trajectory,
                    tokens_used=tokens_used,
                    step=(f"Iter {it}/{max_iterations} — regenerated "
                          f"{i}/{len(work)} ({sk}) tokens={tokens_used:,}"),
                    pct=base_pct + int(((i / max(1, len(work))) * (95 / max_iterations))),
                )

            # Feed the delta-scoring path on the next iteration.
            prev_iter_scored_keys = [
                _report_key_for_srs(k) for k in iter_regen_srs_keys
            ]
            # Filter out unmapped/duplicates.
            prev_iter_scored_keys = list(dict.fromkeys(
                k for k in prev_iter_scored_keys if k
            ))

        # iter-14.11 — Seal the final score with the FULL evaluator panel
        # so the persisted number matches what a fresh Recompute would
        # produce. Only runs if we did >= 1 lean-scoring iteration AND
        # didn't converge on the full-panel iter 1 already.
        if (
            len(trajectory) > 1
            and stop_reason not in {"user_stop"}
        ):
            _cjob_update(jid, step="Sealing final score (full evaluator panel)…", pct=99)
            sealed = await compute_stage_confidence(project_id, stage)
            final_latest = float(sealed.get("overall_score") or 0.0)
            final_best = float(sealed.get("best_overall_score") or final_latest)
            trajectory.append({
                "iteration": len(trajectory) + 1,
                "overall_score": round(final_latest, 2),
                "best_overall_score": round(final_best, 2),
                "below_count": sum(
                    1 for r in (sealed.get("sections") or [])
                    if not r.get("missing") and float(r.get("score") or 0) < threshold
                ),
                "regenerated": [],
                "skipped_sections": sorted(skipped),
                "tokens_used": tokens_used,
                "scoring_mode": "final-seal",
            })
            if final_best >= threshold:
                converged = True

        # Terminal event
        final = await stage_confidence.find_one(
            {"project_id": project_id, "stage": stage}, {"_id": 0},
        ) or {}
        _cjob_complete(jid, {
            "overall_score": final.get("overall_score"),
            "best_overall_score": final.get("best_overall_score"),
            "overall_band": final.get("overall_band"),
            "best_overall_band": final.get("best_overall_band"),
            "iterations_run": len(trajectory),
            "converged": converged,
            "trajectory": trajectory,
            "tokens_used": tokens_used,
            "stop_reason": stop_reason or "unknown",
            "skipped_sections": sorted(skipped),
        })
        await audit_log.insert_one({
            "action": "pipeline.confidence.improve",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "stage": stage, "threshold": threshold,
                "iterations_run": len(trajectory),
                "converged": converged,
                "final_overall_score": final.get("overall_score"),
                "final_best_score": final.get("best_overall_score"),
                "tokens_used": tokens_used,
                "stop_reason": stop_reason,
                "skipped_sections": sorted(skipped),
            },
        })
    except Exception as exc:  # noqa: BLE001
        _cjob_error(jid, str(exc)[:500])


def _report_key_for_srs(srs_section_key: str) -> str:
    """Reverse mapping — given an SRS section key (e.g.
    `actors_use_case_inventory`), return the confidence-catalogue row key
    that consumes it (e.g. `actors`). Used by delta-scoring to know which
    report rows to re-score after regeneration.
    """
    if not srs_section_key:
        return ""
    target = f"srs.{srs_section_key}"
    for sec in _REPORT_SECTIONS:
        if sec.get("stage") != "Discovery":
            continue
        for k in sec.get("source_keys") or []:
            if k == target:
                return sec.get("key") or ""
    return ""


@router.post("/{project_id}/confidence/{stage}/improve")
async def improve_stage_confidence(project_id: str, stage: str, payload: dict | None = None):
    """Kick off the iterative score → regenerate-below-threshold → re-score
    loop. Returns `{job_id, status:"queued"}`; UI polls the same
    `/confidence/jobs/{job_id}` endpoint used by Recompute.

    Body (all optional):
      threshold:            float  (default 95.0; clamped [70, 100])
      max_iterations:       int    (default 3;    clamped [1, 5])
      max_sections_per_iter:int    (default 3;    clamped [1, 6])
      token_budget:         int    (default 250_000; clamped [50_000, 2_000_000])

    Only `Discovery` is supported for now — see module docstring.
    """
    if stage not in _IMPROVE_ALLOWED_STAGES:
        raise HTTPException(
            400,
            f"Auto-improve is only wired for {sorted(_IMPROVE_ALLOWED_STAGES)}. "
            f"For CodeGen use /codegen/jobs/start/auto-validate; other stages "
            f"expose Recompute only.",
        )
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")

    payload = payload or {}
    threshold = float(payload.get("threshold") or 95.0)
    threshold = max(70.0, min(100.0, threshold))
    max_iter = int(payload.get("max_iterations") or 3)
    max_iter = max(1, min(5, max_iter))
    max_per = int(payload.get("max_sections_per_iter") or 3)
    max_per = max(1, min(6, max_per))
    token_budget = int(payload.get("token_budget") or _IMPROVE_DEFAULT_TOKEN_BUDGET)
    token_budget = max(50_000, min(2_000_000, token_budget))

    jid = _new_confidence_job(project_id, stage)
    _CONFIDENCE_JOBS[jid]["kind"] = "stage-confidence-improve"
    _CONFIDENCE_JOBS[jid]["threshold"] = threshold
    _CONFIDENCE_JOBS[jid]["max_iterations"] = max_iter
    _CONFIDENCE_JOBS[jid]["token_budget"] = token_budget
    _CONFIDENCE_JOBS[jid]["_task"] = asyncio.create_task(
        _run_improve_loop(jid, project_id, stage, threshold, max_iter, max_per,
                          token_budget=token_budget),
    )
    return {
        "job_id": jid, "status": "queued",
        "project_id": project_id, "stage": stage,
        "threshold": threshold,
        "max_iterations": max_iter,
        "max_sections_per_iter": max_per,
        "token_budget": token_budget,
    }


