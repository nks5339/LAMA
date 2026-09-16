"""TransformationEngine — the DCTE orchestrator."""
from __future__ import annotations
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import (
    DcteJob, DcteJobStatus, ServiceConfig,
    DcteTransformRecord, DcteEvent, DcteReportDoc, DcteReportKind,
)
from .plugin_base import TransformContext
from .plugin_registry import get_registry
from .project_detector import ProjectDetector
from .cicd_generator import CicdGenerator
from .report_generator import ReportGenerator
from .impact_analyzer import ImpactAnalyzer

logger = logging.getLogger("lama.dcte.engine")

EmitFn = Callable[..., None]


class TransformationEngine:
    def __init__(self, emit: EmitFn | None = None) -> None:
        self.registry = get_registry()
        self.detector = ProjectDetector()
        self.cicd = CicdGenerator()
        self.reports = ReportGenerator()
        self.impact = ImpactAnalyzer()
        self._emit_ext = emit or (lambda *_a, **_k: None)

    def run_job(
        self,
        job: DcteJob,
        record_sink: Callable[[DcteTransformRecord], None] | None = None,
        event_sink: Callable[[DcteEvent], None] | None = None,
        report_sink: Callable[[DcteReportDoc], None] | None = None,
        status_sink: Callable[[str, float, str | None], None] | None = None,
        ai_refactor_fn: Callable[[list[Path]], list[dict[str, Any]]] | None = None,
        build_fix_fn: Callable[[Path, str], dict[str, Any]] | None = None,
        devops_fn: Callable[[Path, str, dict[str, Any] | None], dict[str, Any]] | None = None,
        tester_fn: Callable[[Path, str, str | None], dict[str, Any]] | None = None,
        # iter-19 — Optional autonomous droid callable. Signature:
        #   droid_agent_fn(dest_root: Path, service_id: str, model: str | None) -> dict
        # Only invoked when ``job.use_droid_agent`` is True. On success we
        # SKIP the ai_refactor + build_fix + devops chain for that service
        # (droid did the whole thing itself). On failure we fall through.
        droid_agent_fn: Callable[[Path, str, str | None], dict[str, Any]] | None = None,
    ) -> DcteJob:
        record_sink = record_sink or (lambda *_: None)
        event_sink = event_sink or (lambda *_: None)
        report_sink = report_sink or (lambda *_: None)
        status_sink = status_sink or (lambda *_: None)

        def emit(level: str, phase: str, message: str, **payload):
            evt = DcteEvent(
                job_id=job.id,
                service_id=payload.pop("service_id", None),
                level=level, phase=phase, message=message,
                payload=payload or None,
            )
            event_sink(evt)
            self._emit_ext(evt)

        try:
            self._prep_output_root(job.output_root)
            job.status = DcteJobStatus.ANALYZING
            job.started_at = _now_iso()
            status_sink(job.status.value, 0.0, None)
            emit("info", "start",
                 f"DCTE job {job.id} starting — {len(job.services)} service(s)")

            all_traceability: list[dict[str, Any]] = []
            aggregate_metrics: dict[str, Any] = {
                "files_transformed": 0,
                "files_needs_manual": 0,
                "files_failed": 0,
                "services": [],
            }

            for idx, svc in enumerate(job.services):
                job.current_service_id = svc.id
                status_sink(job.status.value, idx / max(1, len(job.services)), None)

                plugin = self.registry.resolve(svc.source_stack, svc.target_stack)
                if plugin is None:
                    emit("error", "resolve",
                         f"No plugin for {svc.source_stack} → {svc.target_stack}",
                         service_id=svc.id)
                    raise RuntimeError(
                        f"No transformation plugin registered for "
                        f"{svc.source_stack} → {svc.target_stack}"
                    )

                ctx = self._make_ctx(job, svc, emit, plugin.destination_dir_name)
                emit("info", "analyze",
                     f"Analyzing service {svc.name} via {plugin.id}",
                     service_id=svc.id)

                analysis = plugin.analyze(ctx)
                emit("info", "analyze",
                     f"{plugin.id}: {analysis.files_scanned} scanned, "
                     f"{len(analysis.matched_files)} candidate(s), risk={analysis.risk_level}",
                     service_id=svc.id, findings=analysis.findings[:20])

                job.status = DcteJobStatus.TRANSFORMING
                status_sink(job.status.value, (idx + 0.3) / max(1, len(job.services)), None)

                result = plugin.transform(ctx, analysis)
                for tf in result.files:
                    rec = DcteTransformRecord(
                        job_id=job.id, service_id=svc.id, plugin_id=plugin.id,
                        source_file=tf.source, target_file=tf.target,
                        kind=tf.kind, status=tf.status,
                        lines_changed=tf.lines_changed, notes=tf.notes,
                    )
                    record_sink(rec)
                    all_traceability.append({
                        "service": svc.name, "plugin": plugin.id,
                        "source": tf.source, "target": tf.target,
                        "kind": tf.kind, "status": tf.status,
                        "lines_changed": tf.lines_changed,
                    })

                emit("info", "transform",
                     f"Transformed {len(result.files)} file(s)",
                     service_id=svc.id,
                     generated=len(result.generated_files),
                     needs_manual=len(result.manual_intervention))

                # iter-19 — Autonomous droid branch. When enabled AND
                # available, droid takes over the whole "make it a working
                # Spring Boot service" job — migration, build, self-fix
                # loop — inside its own agentic session. Success snapshots
                # the tree diff as transform records and SKIPS the
                # legacy ai_refactor + build_fix + devops chain. Failure
                # (missing binary, timeout, non-green build) falls
                # through to the legacy chain so a job never dead-ends.
                droid_took_over = False
                if job.use_droid_agent and droid_agent_fn is not None:
                    job.status = DcteJobStatus.TRANSFORMING
                    status_sink(job.status.value,
                                (idx + 0.35) / max(1, len(job.services)), None)
                    emit("info", "droid_agent",
                         "Autonomous droid mode engaged — agent taking over service",
                         service_id=svc.id)
                    try:
                        da = droid_agent_fn(Path(ctx.destination_path), svc.id, job.model) or {}
                    except Exception as e:  # noqa: BLE001
                        emit("warn", "droid_agent",
                             f"Droid agent errored: {e} — falling back to legacy chain",
                             service_id=svc.id)
                        da = {"success": False, "error": str(e)}

                    if da.get("success"):
                        added = da.get("added_files", []) or []
                        changed = da.get("changed_files", []) or []
                        removed = da.get("removed_files", []) or []
                        emit("info", "droid_agent",
                             f"Droid agent succeeded in {da.get('elapsed_ms', 0) / 1000:.1f}s "
                             f"— {len(changed)} changed, {len(added)} added, "
                             f"{len(removed)} removed",
                             service_id=svc.id,
                             droid={
                                 k: da.get(k) for k in (
                                     "elapsed_ms", "auto_level", "timeout_s",
                                     "brief_rev", "log_tail",
                                 )
                             },
                             added_ct=len(added), changed_ct=len(changed),
                             removed_ct=len(removed))
                        for f in changed:
                            rec = DcteTransformRecord(
                                job_id=job.id, service_id=svc.id,
                                plugin_id="droid-agent",
                                source_file=f, target_file=f,
                                kind="rewrite", status="success",
                                lines_changed=0,
                                notes="droid-agent autonomous rewrite",
                            )
                            record_sink(rec)
                            all_traceability.append({
                                "service": svc.name, "plugin": "droid-agent",
                                "source": f, "target": f,
                                "kind": "rewrite", "status": "success",
                                "lines_changed": 0,
                            })
                        for f in added:
                            rec = DcteTransformRecord(
                                job_id=job.id, service_id=svc.id,
                                plugin_id="droid-agent",
                                source_file="", target_file=f,
                                kind="add", status="success",
                                lines_changed=0,
                                notes="droid-agent added file",
                            )
                            record_sink(rec)
                            all_traceability.append({
                                "service": svc.name, "plugin": "droid-agent",
                                "source": "", "target": f,
                                "kind": "add", "status": "success",
                                "lines_changed": 0,
                            })
                        droid_took_over = True
                    else:
                        emit("warn", "droid_agent",
                             f"Droid agent did not succeed: "
                             f"{(da.get('error') or 'unknown')[:200]} "
                             "— falling back to legacy per-file chain",
                             service_id=svc.id,
                             droid={
                                 k: da.get(k) for k in (
                                     "elapsed_ms", "auto_level",
                                     "timeout_s", "error", "log_tail",
                                 )
                             })

                if job.ai_refactor and ai_refactor_fn and not droid_took_over:
                    try:
                        # iter-18.6 — feed every .java + .sql the plugin
                        # emitted into dest so the LLM sees the full picture
                        # (Helidon SE files that had no MP markers were
                        # invisible to the pre-18.6 handoff).
                        dest_root = Path(ctx.destination_path)
                        ai_targets: list[Path] = []
                        if dest_root.exists():
                            for suf in (".java", ".sql"):
                                ai_targets.extend(dest_root.rglob(f"*{suf}"))
                        # de-dup while preserving order
                        seen: set[str] = set()
                        deduped: list[Path] = []
                        for p in ai_targets:
                            k = str(p)
                            if k not in seen:
                                seen.add(k)
                                deduped.append(p)
                        suggestions = ai_refactor_fn(deduped)
                        rewritten_ct = sum(1 for s in suggestions if s.get("category") == "rewrite")
                        skipped_ct   = sum(1 for s in suggestions if s.get("category") == "guardrail")
                        diag_ct      = sum(1 for s in suggestions if s.get("category") == "diagnostic")
                        # iter-18.15 — files still carrying legacy markers
                        # after the primary + fix-up passes.
                        needs_manual_ct = sum(1 for s in suggestions if s.get("category") == "needs_manual")
                        # iter-18.8 — pull the actual reason strings out of
                        # the diagnostic entries so the terminal message
                        # names the real cause instead of guessing.
                        diag_reasons = [
                            (s.get("suggested_change") or "").strip()
                            for s in suggestions
                            if s.get("category") == "diagnostic"
                        ]
                        diag_reasons = [r for r in diag_reasons if r]
                        if rewritten_ct == 0 and skipped_ct == 0:
                            if diag_ct == 0:
                                msg = ("AI transformer never reached the LLM. "
                                       "No active model provider is configured. "
                                       "Add one in Console → Model Fabric.")
                            else:
                                # Surface up to 3 real reasons + a fix hint.
                                head = "; ".join(diag_reasons[:3])
                                msg = (f"AI transformer reached the LLM but "
                                       f"produced no rewrites. Reason(s): {head}. "
                                       "If the reason is 'timed out': the sweep "
                                       "was killed mid-way — re-run with fewer "
                                       "files. If it's 'non-JSON response': "
                                       "route tier=high to a stronger coder "
                                       "model in Console → Model Fabric.")
                            emit("warn", "ai_refactor", msg,
                                 service_id=svc.id, files_seen=len(deduped),
                                 diagnostics=diag_ct, diag_reasons=diag_reasons[:5])
                        else:
                            emit("info", "ai_refactor",
                                 f"AI rewrote {rewritten_ct} file(s), skipped {skipped_ct} by guardrail, {diag_ct} diagnostic(s), {needs_manual_ct} needs_manual",
                                 service_id=svc.id, suggestions=suggestions[:10],
                                 files_seen=len(deduped),
                                 needs_manual=needs_manual_ct,
                                 diag_reasons=diag_reasons[:5])
                    except Exception as e:
                        emit("warn", "ai_refactor",
                             f"AI refactor skipped: {e}", service_id=svc.id)

                # iter-18.11 — Build-agent gate. After the AI has rewritten
                # the source we compile it; if the build fails, the agent
                # feeds each failing file's javac errors back to the LLM
                # and asks for a targeted fix, then rebuilds. Loops up to
                # 5 attempts. Non-blocking: a missing mvn / no pom.xml
                # returns skipped=True so downstream stages continue.
                if build_fix_fn is not None and not droid_took_over:
                    job.status = DcteJobStatus.BUILDING
                    status_sink(job.status.value,
                                (idx + 0.60) / max(1, len(job.services)), None)
                    try:
                        emit("info", "build",
                             "Compile-and-fix agent starting", service_id=svc.id)
                        br = build_fix_fn(ctx.dest_root, svc.id) or {}
                        if br.get("skipped"):
                            emit("warn", "build",
                                 f"Build skipped: {'; '.join(br.get('notes', [])[-2:])}",
                                 service_id=svc.id, build=br)
                        elif br.get("success"):
                            emit("info", "build",
                                 f"Build succeeded on attempt {br.get('attempts')} "
                                 f"(fixes applied: {br.get('fixes_applied', 0)})",
                                 service_id=svc.id, build=br)
                        else:
                            tail = (br.get('final_output_tail') or '')[-500:]
                            emit("warn", "build",
                                 f"Build did NOT go green after {br.get('attempts')} attempt(s); "
                                 f"{br.get('fixes_applied', 0)} file(s) auto-fixed. "
                                 f"Manual intervention required.",
                                 service_id=svc.id, build=br, output_tail=tail)
                    except Exception as e:
                        emit("warn", "build",
                             f"Build agent errored: {e}", service_id=svc.id)

                # iter-18.17 — DevOps agent phase: patches structural
                # gaps (missing @SpringBootApplication, missing
                # application.yml sections, missing spring-boot-maven-
                # plugin, unresolved compile errors) that javac itself
                # can't see. Non-blocking.
                dv: dict[str, Any] = {}
                if devops_fn is not None and not droid_took_over:
                    job.status = DcteJobStatus.DEVOPS
                    status_sink(job.status.value,
                                (idx + 0.65) / max(1, len(job.services)), None)
                    try:
                        emit("info", "devops",
                             "DevOps agent scanning for structural gaps",
                             service_id=svc.id)
                        dv = devops_fn(ctx.dest_root, svc.id,
                                       br if build_fix_fn is not None else None) or {}
                        emit("info", "devops",
                             f"DevOps: {dv.get('fixes_applied', 0)} gap(s) fixed, "
                             f"{len(dv.get('unresolved') or [])} unresolved",
                             service_id=svc.id, devops=dv)
                    except Exception as e:
                        emit("warn", "devops",
                             f"DevOps agent errored: {e}", service_id=svc.id)

                # iter-18.17 — Tester agent phase: static endpoint +
                # config parity + real boot smoke against
                # /actuator/health. Emits a PASS / PASS_WITH_WARNINGS /
                # FAIL verdict.
                tv: dict[str, Any] = {}
                if tester_fn is not None:
                    job.status = DcteJobStatus.TESTING
                    status_sink(job.status.value,
                                (idx + 0.72) / max(1, len(job.services)), None)
                    try:
                        emit("info", "test",
                             "Tester agent running parity + boot smoke",
                             service_id=svc.id)
                        src_root_str = (
                            svc.source_root if hasattr(svc, "source_root")
                            else job.source_root
                        )
                        tv = tester_fn(ctx.dest_root, svc.id, src_root_str) or {}
                        emit(
                            "info" if tv.get("verdict") == "PASS" else "warn",
                            "test",
                            f"Tester verdict: {tv.get('verdict')} — "
                            + "; ".join((tv.get("notes") or [])[:4]),
                            service_id=svc.id, tester=tv,
                        )
                    except Exception as e:
                        emit("warn", "test",
                             f"Tester agent errored: {e}", service_id=svc.id)

                job.status = DcteJobStatus.VALIDATING
                status_sink(job.status.value, (idx + 0.85) / max(1, len(job.services)), None)
                validation = plugin.validate(ctx, result)
                emit("info", "validate",
                     f"Validation {'OK' if validation.all_ok else 'issues found'}",
                     service_id=svc.id, diagnostics=validation.diagnostics[:20])

                bundle = plugin.generate_report(ctx, analysis, result, validation)

                out_reports = Path(job.output_root) / "reports" / svc.name
                out_reports.mkdir(parents=True, exist_ok=True)
                for kind, body, md_name in (
                    (DcteReportKind.SUMMARY, bundle.summary_md, "summary.md"),
                    (DcteReportKind.API_CHANGE, bundle.api_change_md, "api_change.md"),
                    (DcteReportKind.DATABASE, bundle.database_md, "database.md"),
                    (DcteReportKind.QUALITY, bundle.quality_md, "quality.md"),
                ):
                    if not body:
                        continue
                    p = out_reports / md_name
                    p.write_text(body, encoding="utf-8")
                    report_sink(DcteReportDoc(
                        job_id=job.id, kind=kind,
                        path=str(p), summary=bundle.metrics,
                    ))

                aggregate_metrics["services"].append({
                    "name": svc.name, "plugin": plugin.id,
                    **bundle.metrics,
                })
                aggregate_metrics["files_transformed"] += sum(
                    1 for f in result.files if f.status == "success"
                )
                aggregate_metrics["files_needs_manual"] += sum(
                    1 for f in result.files if f.status == "needs_manual"
                )
                aggregate_metrics["files_failed"] += sum(
                    1 for f in result.files if f.status == "failed"
                )

            job.status = DcteJobStatus.REPORTING
            status_sink(job.status.value, 0.95, None)
            trace_path = self.reports.write_traceability(job.output_root, all_traceability)
            report_sink(DcteReportDoc(
                job_id=job.id, kind=DcteReportKind.TRACEABILITY,
                path=str(trace_path), summary={"rows": len(all_traceability)},
            ))
            impact = self.impact.compute(job, aggregate_metrics)
            impact_path = self.reports.write_impact(job.output_root, impact)
            report_sink(DcteReportDoc(
                job_id=job.id, kind=DcteReportKind.IMPACT,
                path=str(impact_path), summary=impact,
            ))

            for provider in job.generate_cicd:
                p = self.cicd.write(provider, Path(job.output_root))
                emit("info", "cicd", f"Generated {provider} pipeline → {p}")

            job.status = DcteJobStatus.COMPLETED
            job.progress = 1.0
            job.completed_at = _now_iso()
            status_sink(job.status.value, 1.0, None)
            emit("info", "done", f"DCTE job {job.id} completed")
            return job

        except Exception as e:
            logger.exception("DCTE job failed")
            job.status = DcteJobStatus.FAILED
            job.error = str(e)
            job.updated_at = _now_iso()
            status_sink(job.status.value, job.progress, str(e))
            emit("error", "failed", f"DCTE job failed: {e}")
            return job

    def rollback(self, job: DcteJob) -> DcteJob:
        out = Path(job.output_root)
        if out.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup = out.with_name(out.name + f".rollback.{ts}")
            shutil.move(str(out), str(backup))
        job.status = DcteJobStatus.ROLLED_BACK
        job.updated_at = _now_iso()
        return job

    def _prep_output_root(self, output_root: str) -> None:
        root = Path(output_root).expanduser()
        (root / "converted-source").mkdir(parents=True, exist_ok=True)
        (root / "database-scripts").mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir(parents=True, exist_ok=True)
        (root / "documentation").mkdir(parents=True, exist_ok=True)
        (root / "cicd").mkdir(parents=True, exist_ok=True)

    def _make_ctx(self, job: DcteJob, svc: ServiceConfig, emit: EmitFn,
                  destination_dir_name: str = "converted-source") -> TransformContext:
        # iter-18.4 — Compute a spec-compliant destination:
        #   output_root/
        #     converted-source/<svc.name>/    <- Java plugins land here
        #     database-scripts/<svc.name>/    <- SQL plugins land here
        #     reports/, cicd/, documentation/ <- managed by engine
        # svc.destination_path is treated as ADVISORY only (kept in the record
        # for traceability). This prevents the nested out/out/out/ recursion
        # that happens when the user pointed destination_path INSIDE
        # source_path.
        source = Path(svc.source_path).expanduser().resolve()
        module = Path(svc.module_path).expanduser().resolve() if svc.module_path else None
        output_root = Path(job.output_root).expanduser().resolve()
        dest = output_root / destination_dir_name / svc.name
        dest.mkdir(parents=True, exist_ok=True)
        return TransformContext(
            job_id=job.id,
            service_id=svc.id,
            source_path=source,
            module_path=module,
            destination_path=dest,
            output_root=output_root,
            source_stack=svc.source_stack,
            target_stack=svc.target_stack,
            options=svc.options,
            emit=lambda level, phase, msg, **kw: emit(
                level, phase, msg, service_id=svc.id, **kw
            ),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
