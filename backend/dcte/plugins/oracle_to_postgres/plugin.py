"""Oracle → PostgreSQL plugin."""
from __future__ import annotations
from pathlib import Path

from ...plugin_base import (
    TransformationPlugin, TransformContext,
    AnalysisResult, TransformResult, TransformedFile,
    ValidationResult, ReportBundle,
)
from ...validator import BasicValidator
from .sql_translator import SqlTranslator


_ORACLE_MARKERS = ("VARCHAR2", "NUMBER(", "SYSDATE", "NVL(", "DECODE(", "DUAL", "ROWNUM")


class OracleToPostgresPlugin(TransformationPlugin):
    id = "oracle-to-postgres"
    display_name = "Oracle → PostgreSQL"
    source_stack = "oracle"
    target_stack = "postgres-15"
    version = "0.1.0"
    destination_dir_name = "database-scripts"  # iter-18.4

    def analyze(self, ctx: TransformContext) -> AnalysisResult:
        r = AnalysisResult()
        src = ctx.module_path or ctx.source_path
        sql_files = [
            f for f in (list(src.rglob("*.sql")) + list(src.rglob("*.pkb")) + list(src.rglob("*.pks")))
            if not ctx.under_output_root(f)  # iter-18.4 — never re-walk own output
        ]
        r.files_scanned = len(sql_files)
        for f in sql_files:
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore").upper()
            except Exception:
                continue
            if any(mk in txt for mk in _ORACLE_MARKERS):
                r.matched_files.append(str(f))
            if "CREATE OR REPLACE PACKAGE" in txt:
                r.unsupported.append(str(f))
                r.findings.append({
                    "level": "warn", "file": str(f),
                    "message": "Oracle PACKAGE has no direct PG analogue — will split into schemas + functions (manual review required).",
                })
            if "PRAGMA AUTONOMOUS_TRANSACTION" in txt:
                r.unsupported.append(str(f))
                r.findings.append({
                    "level": "warn", "file": str(f),
                    "message": "PRAGMA AUTONOMOUS_TRANSACTION — needs dblink-based rewrite in PG.",
                })

        if len(r.unsupported) >= 3:
            r.risk_level = "high"
        elif len(r.unsupported) >= 1:
            r.risk_level = "medium"
        else:
            r.risk_level = "low"
        r.effort_hours = len(r.matched_files) * 0.3 + len(r.unsupported) * 2.0
        r.complexity_score = min(100.0, len(r.matched_files) * 1.5 + len(r.unsupported) * 6)
        return r

    def transform(self, ctx: TransformContext, analysis: AnalysisResult) -> TransformResult:
        result = TransformResult()
        src_root = ctx.module_path or ctx.source_path
        dest = ctx.destination_path
        translator = SqlTranslator()

        for fpath in analysis.matched_files:
            src = Path(fpath)
            rel = src.relative_to(src_root)
            # Land all translated SQL under database-scripts/, preserving relative paths.
            tgt = dest / rel
            tgt = tgt.with_suffix(".sql")  # normalise .pkb/.pks → .sql
            try:
                content = src.read_text(encoding="utf-8", errors="ignore")
                translated, meta = translator.translate(content)
                tgt.parent.mkdir(parents=True, exist_ok=True)
                # header
                header = (
                    "-- Translated from Oracle by DCTE (oracle-to-postgres).\n"
                    f"-- Source: {src}\n"
                    f"-- Types replaced: {meta.get('types_replaced')}, "
                    f"funcs: {meta.get('funcs_replaced')}, "
                    f"sequences: {meta.get('sequences_touched')}, "
                    f"decode: {meta.get('decode_expanded')}\n\n"
                )
                tgt.write_text(header + translated, encoding="utf-8")
                needs_manual = bool(meta.get("unsupported"))
                result.files.append(TransformedFile(
                    source=str(src), target=str(tgt),
                    kind="sql", status="needs_manual" if needs_manual else "success",
                    lines_changed=(meta.get("types_replaced", 0)
                                   + meta.get("funcs_replaced", 0)
                                   + meta.get("sequences_touched", 0)
                                   + meta.get("decode_expanded", 0)),
                    notes=(", ".join(meta.get("unsupported", [])) if needs_manual
                           else "clean translation"),
                ))
                if needs_manual:
                    result.manual_intervention.append({
                        "file": str(tgt), "reasons": meta.get("unsupported"),
                    })
            except Exception as e:
                result.files.append(TransformedFile(
                    source=str(src), target=str(tgt),
                    kind="sql", status="failed",
                    notes=f"translate error: {e}",
                ))
        ctx.emit("info", "transform", f"OracleToPostgres translated {len(result.files)} SQL file(s)")
        return result

    def validate(self, _ctx: TransformContext, result: TransformResult) -> ValidationResult:
        v = ValidationResult()
        bv = BasicValidator()
        for tf in result.files:
            if tf.status not in ("success", "needs_manual"):
                continue
            diags = bv.check_sql_semicolons(Path(tf.target))
            if diags:
                v.syntax_ok = False
                v.diagnostics.extend(diags)
        return v

    def generate_report(
        self, _ctx: TransformContext, analysis: AnalysisResult,
        result: TransformResult, validation: ValidationResult,
    ) -> ReportBundle:
        b = ReportBundle()
        b.summary_md = (
            "# Database Conversion Summary — Oracle → PostgreSQL\n"
            f"\n- Files scanned: **{analysis.files_scanned}**\n"
            f"- Files translated: **{sum(1 for f in result.files if f.status == 'success')}**\n"
            f"- Files needing manual review: **{sum(1 for f in result.files if f.status == 'needs_manual')}**\n"
            f"- Files failed: **{sum(1 for f in result.files if f.status == 'failed')}**\n"
            f"- Risk level: **{analysis.risk_level}**\n"
            f"- Estimated effort: **{analysis.effort_hours:.1f} h**\n"
        )
        b.database_md = "# Database Conversion Report\n\n" + "\n".join(
            f"- `{tf.source}` → `{tf.target}` — status=**{tf.status}** — {tf.notes or ''}"
            for tf in result.files
        )
        b.quality_md = "# SQL Quality Report\n\n" + "\n".join(
            f"- **{d.get('level','')}** — {d.get('file','')}: {d.get('message','')}"
            for d in validation.diagnostics[:200]
        ) or "# SQL Quality Report\n\nNo diagnostics reported."
        b.metrics = {
            "risk_level": analysis.risk_level,
            "translated": sum(1 for f in result.files if f.status == "success"),
            "needs_manual": sum(1 for f in result.files if f.status == "needs_manual"),
            "failed": sum(1 for f in result.files if f.status == "failed"),
        }
        return b
