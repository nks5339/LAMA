"""Plugin interface for DCTE transformations."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class TransformContext:
    job_id: str
    service_id: str
    source_path: Path
    module_path: Path | None
    destination_path: Path
    source_stack: str
    target_stack: str
    output_root: Path | None = None  # iter-18.4 — set by engine; plugins skip walking here
    options: dict[str, Any] = field(default_factory=dict)
    emit: Callable[..., None] = field(default=lambda *_a, **_k: None)

    def under_output_root(self, p: Path) -> bool:
        """True if `p` is inside output_root (used to skip re-walking own output)."""
        if not self.output_root:
            return False
        try:
            p.resolve().relative_to(self.output_root.resolve())
            return True
        except (ValueError, OSError):
            return False


@dataclass
class AnalysisResult:
    files_scanned: int = 0
    matched_files: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    complexity_score: float = 0.0
    risk_level: str = "low"
    effort_hours: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TransformedFile:
    source: str
    target: str
    kind: str
    status: str = "success"
    lines_changed: int = 0
    notes: str | None = None
    diff_preview: str | None = None


@dataclass
class TransformResult:
    files: list[TransformedFile] = field(default_factory=list)
    generated_files: list[str] = field(default_factory=list)
    manual_intervention: list[dict[str, Any]] = field(default_factory=list)
    dependency_changes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    compilation_ok: bool = True
    dependency_resolution_ok: bool = True
    api_compatibility_ok: bool = True
    syntax_ok: bool = True
    config_consistency_ok: bool = True
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all([
            self.compilation_ok,
            self.dependency_resolution_ok,
            self.api_compatibility_ok,
            self.syntax_ok,
            self.config_consistency_ok,
        ])


@dataclass
class ReportBundle:
    summary_md: str = ""
    api_change_md: str = ""
    database_md: str = ""
    quality_md: str = ""
    traceability_rows: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class TransformationPlugin(ABC):
    id: str = "abstract"
    display_name: str = "Abstract"
    source_stack: str = ""
    target_stack: str = ""
    version: str = "0.1.0"
    # iter-18.4 — where under output_root/ this plugin's produced tree lives.
    # Engine sets ctx.destination_path = output_root / destination_dir_name / <svc.name>.
    destination_dir_name: str = "converted-source"

    @abstractmethod
    def analyze(self, ctx: TransformContext) -> AnalysisResult: ...

    @abstractmethod
    def transform(self, ctx: TransformContext, analysis: AnalysisResult) -> TransformResult: ...

    @abstractmethod
    def validate(self, ctx: TransformContext, result: TransformResult) -> ValidationResult: ...

    @abstractmethod
    def generate_report(
        self,
        ctx: TransformContext,
        analysis: AnalysisResult,
        result: TransformResult,
        validation: ValidationResult,
    ) -> ReportBundle: ...

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source_stack": self.source_stack,
            "target_stack": self.target_stack,
            "version": self.version,
        }
