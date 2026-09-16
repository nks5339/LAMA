"""Generic AI-driven migration plugin — iter-21.

The catalogue in `dcte/stacks.py` offers far more stack pairs than there are
deterministic transformers, and the engine used to `raise` on any pair it
could not resolve:

    RuntimeError: No transformation plugin registered for jsp → react-19

So every stack added to the dropdown without a hand-written transformer
would have been a trap — selectable, and dead on start. This plugin is the
floor under the dropdown: it claims the pairs nobody else claims.

What it does deterministically is deliberately small, because for an
arbitrary pair there is no safe regex:

  * stage every source file the pair could plausibly care about into the
    destination tree, preserving relative layout,
  * mark each one `needs_manual` so the report never claims a file was
    migrated when only copied,
  * leave the actual conversion to the AI pass, which by this point has a
    brief built for exactly this pair (`prompt_builder`).

It therefore does nothing useful with `ai_refactor=False` — and says so, in
an analysis finding, rather than reporting a successful no-op migration.
The two deterministic plugins keep priority; `PluginRegistry.resolve` only
falls through to this one when no exact match exists.
"""
from __future__ import annotations

from pathlib import Path

from ...plugin_base import (
    TransformationPlugin, TransformContext,
    AnalysisResult, TransformResult, TransformedFile,
    ValidationResult, ReportBundle,
)
from ...stacks import get_stack, stack_label

# Extensions worth staging. Anything else (images, jars, lockfiles) is left
# where it is: copying a 40 MB binary into the output tree buys nothing and
# the AI pass would skip it anyway.
_SOURCE_SUFFIXES: frozenset[str] = frozenset({
    ".java", ".kt", ".scala", ".groovy",
    ".cs", ".vb", ".fs",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".rb", ".go", ".rs", ".php",
    ".jsp", ".jspx", ".tag", ".tld", ".ftl", ".vm",
    ".html", ".htm", ".css", ".scss", ".less",
    ".sql", ".pks", ".pkb", ".prc", ".fnc",
    ".xml", ".json", ".yml", ".yaml", ".properties", ".conf", ".ini", ".toml",
    ".gradle", ".kts", ".csproj", ".sln", ".md",
})

# Never walked: build output, VCS metadata, dependency caches. Mirrors the
# skip-patterns the KB folder scan uses (CLAUDE.md contract #8) — the order
# matters there and the same names matter here.
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "vendor", "__pycache__", "target", "build",
    "dist", "out", "bin", "obj", ".idea", ".vscode", ".mvn", ".gradle",
    ".venv", "venv", ".next", ".nuxt", "coverage",
})


def _is_skipped(p: Path, root: Path) -> bool:
    try:
        rel = p.relative_to(root)
    except ValueError:
        return True
    return any(part in _SKIP_DIRS for part in rel.parts)


class GenericAiPlugin(TransformationPlugin):
    """Fallback transformer for any pair without a deterministic plugin."""

    id = "generic-ai"
    display_name = "Generic (AI-driven)"
    # Sentinels: `PluginRegistry.resolve` never matches this plugin on a
    # stack pair, it selects it explicitly as the fallback. Real values would
    # make it claim a pair it has no special knowledge of.
    source_stack = "*"
    target_stack = "*"
    version = "1.0.0"
    destination_dir_name = "converted-source"

    # ── analyze ──────────────────────────────────────────────────────
    def analyze(self, ctx: TransformContext) -> AnalysisResult:
        r = AnalysisResult()
        src = ctx.module_path or ctx.source_path
        src_label = stack_label(ctx.source_stack)
        tgt_label = stack_label(ctx.target_stack)

        if not src.exists():
            r.findings.append({
                "level": "error", "file": str(src),
                "message": f"Source path does not exist: {src}",
            })
            r.risk_level = "high"
            return r

        candidates = [
            f for f in src.rglob("*")
            if f.is_file()
            and f.suffix.lower() in _SOURCE_SUFFIXES
            and not _is_skipped(f, src)
            and not ctx.under_output_root(f)
        ]
        r.files_scanned = len(candidates)
        r.matched_files = [str(f) for f in candidates]

        # An unknown stack is not an error — the brief falls back to "follow
        # that stack's mainstream conventions" — but the operator should know
        # the guidance is thinner for it.
        for role, sid in (("source", ctx.source_stack), ("target", ctx.target_stack)):
            if get_stack(sid) is None:
                r.findings.append({
                    "level": "warn", "file": "",
                    "message": (f"{role.capitalize()} stack '{sid}' is not in the "
                                "catalogue, so the migration brief carries no "
                                "stack-specific conventions for it."),
                })

        r.findings.append({
            "level": "info", "file": "",
            "message": (f"No deterministic transformer exists for {src_label} → "
                        f"{tgt_label}. Files are staged verbatim and the "
                        "conversion is performed by the AI pass."),
        })
        # Stated here rather than discovered later: with the AI pass off this
        # plugin produces a copy, and a copy that reports success is a lie.
        if not ctx.options.get("ai_refactor", True):
            r.findings.append({
                "level": "warn", "file": "",
                "message": ("AI-assisted refactor is OFF, so this job will copy "
                            "the tree without converting it. Enable it, or pick a "
                            "pair with a deterministic transformer."),
            })

        n = len(candidates)
        r.complexity_score = min(100.0, n * 1.2)
        r.effort_hours = round(n * 0.4, 1)
        r.risk_level = "high" if n > 120 else "medium" if n > 25 else "low"
        return r

    # ── transform ────────────────────────────────────────────────────
    def transform(self, ctx: TransformContext, analysis: AnalysisResult) -> TransformResult:
        result = TransformResult()
        src = ctx.module_path or ctx.source_path
        dest_root = ctx.destination_path
        dest_root.mkdir(parents=True, exist_ok=True)

        for raw in analysis.matched_files:
            f = Path(raw)
            try:
                rel = f.relative_to(src)
            except ValueError:
                rel = Path(f.name)
            target = dest_root / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                body = f.read_text(encoding="utf-8", errors="ignore")
                target.write_text(body, encoding="utf-8")
            except Exception as e:
                result.files.append(TransformedFile(
                    source=str(f), target=str(target), kind="stage",
                    status="failed", notes=f"copy failed: {e}",
                ))
                continue
            result.files.append(TransformedFile(
                source=str(f), target=str(target), kind="stage",
                # Never "success": nothing has been converted yet. The AI pass
                # rewrites these in place and the engine re-records what it
                # actually changed.
                status="needs_manual",
                lines_changed=0,
                notes="staged verbatim; conversion performed by the AI pass",
            ))

        result.manual_intervention.append({
            "file": "", "severity": "medium",
            "message": (f"{stack_label(ctx.source_stack)} → "
                        f"{stack_label(ctx.target_stack)} has no deterministic "
                        "transformer; review the AI-converted output before use."),
        })
        return result

    # ── validate ─────────────────────────────────────────────────────
    def validate(self, _ctx: TransformContext, result: TransformResult) -> ValidationResult:
        v = ValidationResult()
        failed = [f for f in result.files if f.status == "failed"]
        if failed:
            v.syntax_ok = False
            v.diagnostics.extend({
                "file": f.source, "level": "error",
                "message": f.notes or "staging failed",
            } for f in failed)
        # Compilation is the build agent's verdict, not ours: with no
        # deterministic rewrite there is nothing here we could check that
        # the compiler will not check better.
        return v

    # ── report ───────────────────────────────────────────────────────
    def generate_report(
        self, ctx: TransformContext, analysis: AnalysisResult,
        result: TransformResult, _validation: ValidationResult,
    ) -> ReportBundle:
        b = ReportBundle()
        src_label = stack_label(ctx.source_stack)
        tgt_label = stack_label(ctx.target_stack)
        staged = sum(1 for f in result.files if f.status != "failed")
        failed = sum(1 for f in result.files if f.status == "failed")

        b.summary_md = (
            f"# Migration Summary — {src_label} → {tgt_label}\n"
            "\n"
            "Performed by the **generic AI-driven plugin**: this stack pair has "
            "no hand-written deterministic transformer, so files were staged "
            "verbatim and converted by the AI pass using a brief built for this "
            "pair.\n"
            "\n"
            f"- Files scanned: **{analysis.files_scanned}**\n"
            f"- Files staged: **{staged}**\n"
            f"- Files that could not be staged: **{failed}**\n"
            f"- Risk: **{analysis.risk_level}**\n"
            f"- Estimated review effort: **{analysis.effort_hours} h**\n"
            "\n"
            "> Every staged file is recorded as `needs_manual` rather than "
            "`success`. Review the converted output before shipping it.\n"
        )
        b.metrics = {
            "files_scanned": analysis.files_scanned,
            "files_staged": staged,
            "files_failed": failed,
            "deterministic": False,
            "source_stack": ctx.source_stack,
            "target_stack": ctx.target_stack,
        }
        return b
