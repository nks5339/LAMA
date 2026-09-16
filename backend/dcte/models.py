"""Pydantic v2 models for DCTE."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
import uuid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class DcteJobStatus(str, Enum):
    CREATED = "created"
    DETECTING = "detecting"
    ANALYZING = "analyzing"
    TRANSFORMING = "transforming"
    BUILDING = "building"
    # iter-18.17 — DevOps agent phase (structural gap patching) sits
    # between BUILDING and TESTING; TESTING replaces the historical
    # narrow "validating" as the post-conversion "good to go?" gate.
    DEVOPS = "devops"
    TESTING = "testing"
    VALIDATING = "validating"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    ROLLED_BACK = "rolled_back"


class DcteReportKind(str, Enum):
    SUMMARY = "migration_summary"
    TRACEABILITY = "traceability_matrix"
    API_CHANGE = "api_change"
    DATABASE = "database_conversion"
    QUALITY = "code_quality"
    IMPACT = "impact_analysis"


class ServiceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("svc"))
    name: str
    source_path: str
    module_path: str | None = None
    destination_path: str
    source_stack: str
    target_stack: str
    options: dict[str, Any] = Field(default_factory=dict)


class DcteJob(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("dcte"))
    name: str
    tenant_id: str = "tenant_default"
    created_by: str | None = None

    source_root: str
    output_root: str
    services: list[ServiceConfig] = Field(default_factory=list)

    status: DcteJobStatus = DcteJobStatus.CREATED
    progress: float = 0.0
    current_service_id: str | None = None
    error: str | None = None

    ai_refactor: bool = True
    generate_cicd: list[str] = Field(default_factory=list)
    # iter-18.13 — Optional Factory/Droid model hint. Empty string / "auto"
    # → let droid pick (unchanged legacy behaviour); any other value is
    # forwarded verbatim to ``fabric_call`` as ``model=...`` which becomes
    # the ``model_hint`` argument to ``route_via_factory_cli`` (see
    # backend/fabric/factory_cli.py::_run_droid_exec → ``-m <model>``).
    model: str | None = None
    # iter-19 — Autonomous droid mode. When True the engine invokes
    # ``droid exec --auto medium --cwd <service-dest>`` ONCE per service
    # (agentic — droid does the migration + mvn build-fix loop itself)
    # INSTEAD of running the regex + per-file ai_refactor + build_agent +
    # devops_agent chain. Falls back to the legacy chain if the droid
    # binary is unavailable or the agent errors out. Default OFF so
    # existing jobs keep the deterministic behaviour.
    use_droid_agent: bool = False

    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class DcteTransformRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("tx"))
    job_id: str
    service_id: str
    plugin_id: str
    source_file: str
    target_file: str
    kind: str
    status: str
    lines_changed: int = 0
    notes: str | None = None
    created_at: str = Field(default_factory=_now_iso)


class DcteEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("evt"))
    job_id: str
    service_id: str | None = None
    at: str = Field(default_factory=_now_iso)
    level: str = "info"
    phase: str
    message: str
    payload: dict[str, Any] | None = None


class DcteReportDoc(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("rep"))
    job_id: str
    kind: DcteReportKind
    format: str = "markdown"
    path: str
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)


class DetectRequest(BaseModel):
    source_path: str


class DetectResponse(BaseModel):
    path: str
    exists: bool
    is_valid: bool
    detected_stack: str
    confidence: float
    hints: list[str] = Field(default_factory=list)
    suggested_target: str | None = None


class CreateJobRequest(BaseModel):
    name: str
    source_root: str | None = None
    output_root: str | None = None
    services: list[ServiceConfig]
    ai_refactor: bool = True
    generate_cicd: list[str] = Field(default_factory=list)
    # iter-18.13 — Factory/Droid model hint. See DcteJob.model.
    model: str | None = None
    # iter-19 — Opt-in autonomous droid mode. See DcteJob.use_droid_agent.
    use_droid_agent: bool = False


class JobActionResponse(BaseModel):
    id: str
    status: DcteJobStatus
    message: str | None = None
