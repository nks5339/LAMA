"""Pydantic models for LAMA."""
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
import uuid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------- Project ----------
class ProjectCreate(BaseModel):
    name: str
    # iter-14.93 — project_type distinguishes the classical legacy-migration
    # 5-stage pipeline from the single-purpose tool project types (Gap
    # Analyzer, Technology Transformer). Determines stage_status layout
    # and the sidebar pipeline rendered for the project.
    # Values: "legacy_migration" | "gap_analysis" | "tech_transformer".
    project_type: Optional[str] = "legacy_migration"
    # iter-13.58 — `source_tech` is auto-detected by `routes/kb.py` during
    # Build KB (see tech_detector). Optional at create time so the UI form
    # only asks for the name; existing callers that still pass it are honoured.
    source_tech: Optional[str] = ""
    # iter-13.59 — `target_tech` is now chosen AFTER Build KB via the
    # TargetStackSuggester (top-3 recommendations + "Others" custom picker).
    # Optional at create time so the New Project form only asks for a name.
    target_tech: Optional[str] = ""
    description: Optional[str] = ""
    github_repo: Optional[str] = ""
    tenant_id: Optional[str] = ""  # required unless caller is super_admin creating a super_admin


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    name: str
    # iter-13.68 — multi-tenant. Every project belongs to a tenant.
    # Backfilled to "tenant_default" by seed_tenancy() for legacy rows.
    tenant_id: str = "tenant_default"
    # Default to "" — populated by tech_detector on Build KB. The Sidebar
    # tolerates empty source_tech and renders only the target until then.
    source_tech: str = ""
    # Default to "" — populated by TargetStackSuggester after Build KB.
    # The Sidebar tolerates empty target_tech and shows an italic hint until
    # the user picks (or assembles) a stack on the Discovery page.
    target_tech: str = ""
    description: str = ""
    github_repo: str = ""
    # iter-14.93 — project_type: "legacy_migration" (classical 5-stage
    # pipeline), "gap_analysis" (3-stage tool project), or
    # "tech_transformer" (3-stage tool project). The Sidebar renders the
    # pipeline layout based on this field.
    project_type: str = "legacy_migration"
    stage: str = "Discovery"  # Discovery, DataModel, Architecture, CodeGen, Living
    stage_status: Dict[str, str] = Field(default_factory=lambda: {
        "Discovery": "active",
        "DataModel": "locked",
        "Architecture": "locked",
        "CodeGen": "locked",
        "Living": "locked",
    })
    freeze_gates: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


# ---------- KB ----------
class KBFile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    filename: str
    filetype: str
    size: int
    chunk_count: int = 0
    entity_count: int = 0
    status: str = "uploaded"  # uploaded, processed
    # iter-13.69 — user-tagged source file role. See kb/file_kinds.py for
    # the full taxonomy. Drives SRS prompt's "SOURCE MATERIAL INVENTORY"
    # block AND CodeGen frontend prompt's `{theme_brief}` injection.
    kind: str = "legacy_code"
    kind_notes: str = ""
    created_at: str = Field(default_factory=_now_iso)


class KBChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    file_id: str
    chunk_index: int
    content: str
    created_at: str = Field(default_factory=_now_iso)


class KBStatus(BaseModel):
    project_id: str
    files: int
    chunks: int
    entities: int
    classes: int
    methods: int
    tables: int
    columns: int
    roles: int
    routes: int = 0  # iter-14.30 — expose route count in KB Health
    relationships: int
    toon_size: int
    modules: int = 0
    component_maps: int = 0


# ---------- Chat ----------
class ChatRequest(BaseModel):
    project_id: str
    message: str
    model: str = ""  # iter-13.30: empty = Console.routing[tier] picks per agent_key
    stage: str = "Discovery"
    conversation_id: Optional[str] = None
    edit_mode: bool = False
    selected_section: Optional[str] = None
    # iter-13.100 — optional rolling-memory session. When supplied, the chat
    # route routes through `llm.fabric_call_with_session` so the LLM sees
    # the session's rolling summary + hydrated refs + last-K verbatim
    # turns. Empty (default) → behaviour identical to pre-13.100 chat.
    session_id: Optional[str] = ""


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    conversation_id: str
    project_id: str
    role: str  # user / assistant / system
    content: str
    model: Optional[str] = None
    tokens: int = 0
    created_at: str = Field(default_factory=_now_iso)


# ---------- SRS ----------
class SRSSectionUpdate(BaseModel):
    section: str
    content: str


class SRSDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    sections: Dict[str, str] = Field(default_factory=dict)
    # iter-14.11 — per-section retry telemetry. Populated by the SRS
    # score-gated retry loop in `routes/srs.py::_run_one_section`.
    # Shape per section key:
    #   {
    #     "attempts": int (1..=1+MAX_AUTO_RETRIES),
    #     "initial_score": float, "final_score": float,
    #     "band": "excellent|good|fair|poor",
    #     "plateaued": bool, "delta": float,
    #     "last_evaluated_at": ISO,
    #     "generated_at": ISO,
    #   }
    sections_meta: Dict[str, Dict] = Field(default_factory=dict)
    frozen: bool = False
    frozen_at: Optional[str] = None
    frozen_by: Optional[str] = None
    version: int = 1
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


# ---------- Prompts ----------
class PromptUpdate(BaseModel):
    template: str
    description: Optional[str] = None


class Prompt(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    stage: str
    template: str
    description: str = ""
    version: int = 1
    updated_at: str = Field(default_factory=_now_iso)


class ProjectPrompt(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    key: str
    template: str
    description: str = ""
    version: int = 1
    updated_at: str = Field(default_factory=_now_iso)


# ---------- GitHub ----------
class GithubPushRequest(BaseModel):
    project_id: str
    repo_url: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    branch: str = "main"


# ---------- Pipeline / Stage handoff ----------
class StageContext(BaseModel):
    """Persisted snapshot of everything a stage produced.
    Loaded by the NEXT stage as its primary input context.
    This is the pipeline handoff mechanism between all 5 stages."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    stage: str  # Discovery | DataModel | Architecture | CodeGen | Living
    frozen_at: str = ""
    frozen_by: str = "system"
    version: int = 1
    outputs: Dict[str, Any] = Field(default_factory=dict)
    toon_summary: str = ""
    sources: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)



# ---------- Data Model (Stage 2) ----------
class DataModelArtifact(BaseModel):
    """A generated DDL / migration script / bus matrix artifact for Stage 2."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    type: str  # oltp_ddl | olap_ddl | bus_matrix | migrate_old_to_oltp | migrate_oltp_to_olap | test_migration
    content: str = ""
    version: int = 1
    generated_by: str = ""  # model id used
    tracability: Dict[str, Any] = Field(default_factory=dict)
    frozen: bool = False
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


# ---------- Architecture (Stage 3) ----------
class ArchDocument(BaseModel):
    """One architecture artifact — HLD, LLD, API contract, sequence diagrams, service map, ADR."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    type: str
    content: str = ""
    version: int = 1
    frozen: bool = False
    frozen_at: Optional[str] = None
    generated_by: str = ""
    tracability: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class ServiceDefinition(BaseModel):
    """One microservice / module boundary recommended by Stage 3."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    name: str
    display_name: str
    pattern: str = "microservice"
    backend_lang: str = "nodejs"
    frontend: bool = False
    tables: List[str] = Field(default_factory=list)
    api_endpoints: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    events_published: List[str] = Field(default_factory=list)
    events_consumed: List[str] = Field(default_factory=list)
    status: str = "pending"
    codegen_status: str = "pending"
    source_module: str = ""
    responsibility: str = ""
    estimated_loc: int = 0
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


# ---------- Code Generation (Stage 4) ----------
class CodegenFile(BaseModel):
    """One generated file inside a service."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    service_id: str = ""
    service_name: str = ""
    file_path: str
    content: str = ""
    language: str = "text"
    file_type: str = "other"
    version: int = 1
    edited: bool = False
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class CodegenRun(BaseModel):
    """Tracks one full code generation run."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    status: str = "running"
    services_total: int = 0
    services_done: int = 0
    files_total: int = 0
    files_done: int = 0
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    github_commit: str = ""
    started_at: str = Field(default_factory=_now_iso)
    completed_at: Optional[str] = None


# ---------- Console: Model Fabric / Agent Fabric / Token Usage ----------
class ModelProvider(BaseModel):
    """One configured LLM provider (OpenRouter, Anthropic, OpenAI, Groq, Ollama, custom)."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    name: str
    provider_type: str  # "openrouter"|"anthropic"|"openai"|"groq"|"ollama"|"custom"
    base_url: str
    api_key: str
    is_default: bool = False
    is_active: bool = True
    # iter-14.34 — user-controlled toggle for whether the stored api_key
    # is actually sent on outbound calls. Lets the operator disable
    # Ollama-cloud-key-driven routing (and Bearer headers) without losing
    # the stored key. Default True = backward-compatible with existing rows.
    key_enabled: bool = True
    detected_from_key: str = ""
    models: List[Dict[str, Any]] = Field(default_factory=list)
    routing: Dict[str, str] = Field(default_factory=lambda: {"low": "", "medium": "", "high": ""})
    # iter-13.112 — Stage-wise model routing with generate/regenerate modes (just like Factory)
    # Each stage gets TWO model picks: one for first-time generation, one for regeneration.
    # Format: {"Discovery.generate": "model-id", "Discovery.regenerate": "model-id", ...}
    stage_routing_generate: Dict[str, str] = Field(default_factory=lambda: {
        "Discovery": "", "DataModel": "", "Architecture": "", "CodeGen": "", "Living": ""
    })
    stage_routing_regenerate: Dict[str, str] = Field(default_factory=lambda: {
        "Discovery": "", "DataModel": "", "Architecture": "", "CodeGen": "", "Living": ""
    })
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class AgentConfig(BaseModel):
    """Configuration for one task agent or stage orchestrator."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    key: str
    agent_type: str  # "orchestrator"|"task"
    stage: str  # "Discovery"|"DataModel"|"Architecture"|"CodeGen"|"Living"
    label: str
    description: str = ""
    complexity: str = "medium"  # "low"|"medium"|"high"
    model_override: str = ""
    provider_id: str = ""
    max_tokens: int = 4096
    temperature: float = 0.3
    status: str = "enabled"  # "enabled"|"disabled"|"replaced"|"wrapped"
    wrap_prefix: str = ""
    wrap_suffix: str = ""
    replaced_template: str = ""
    chain_to: str = ""
    chain_condition: str = "always"
    token_budget_total: int = 0
    tokens_used_last_run: int = 0
    tokens_used_all_time: int = 0
    last_run_at: str = ""
    last_run_model: str = ""
    last_run_input_tokens: int = 0
    last_run_output_tokens: int = 0
    last_run_cost_usd: float = 0.0
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class TokenUsageLog(BaseModel):
    """One LLM call record for reporting."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str = ""
    agent_key: str
    stage: str = ""
    model: str = ""
    provider_type: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = "success"
    error: str = ""
    created_at: str = Field(default_factory=_now_iso)


# ---------- Multi-tenancy (iter-13.68) ----------
class Tenant(BaseModel):
    """A LAMA tenant (organisation/customer). Every Project belongs to one."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    name: str
    slug: str = ""           # short URL-safe identifier (lowercased, deduped on create)
    description: str = ""
    is_active: bool = True
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class TenantCreate(BaseModel):
    name: str
    slug: Optional[str] = ""
    description: Optional[str] = ""


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class User(BaseModel):
    """A LAMA user. role ∈ {super_admin, tenant_admin, tenant_user}.
    For super_admin, `tenant_id == "*"` (visible across every tenant).
    Password is stored as a bcrypt hash in `password_hash`; never returned
    by the API."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    username: str
    full_name: str = ""
    email: str = ""
    role: str = "tenant_user"   # super_admin | tenant_admin | tenant_user
    tenant_id: str = ""          # "*" for super_admin
    is_active: bool = True
    password_hash: str = ""
    last_login_at: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class UserPublic(BaseModel):
    """Same as User but without `password_hash` — what the API returns."""
    id: str
    username: str
    full_name: str = ""
    email: str = ""
    role: str
    tenant_id: str
    is_active: bool = True
    last_login_at: Optional[str] = None
    created_at: str
    updated_at: str


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = ""
    email: Optional[str] = ""
    role: str = "tenant_user"
    tenant_id: Optional[str] = ""   # required unless caller is super_admin creating a super_admin


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None  # plaintext; will be bcrypt-hashed before persist


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: str
    user: UserPublic
    tenant: Optional[Tenant] = None


# ---------- Rolling-memory agent sessions (iter-13.100) ----------
#
# These models back the `agent_sessions` collection. They give every
# LLM call that opts in a durable, droid-handoff-aware conversational
# context that survives browser refresh, factory swaps, and LLM
# context-window overflow.
#
# Design rules (see CLAUDE.md §4 + the iter-13.100 plan):
#   1. `summary` is a COMPRESSED form of older turns; it's a HINT, not
#      authoritative truth. Never put facts here that aren't also
#      anchored via a `ContextRef` to a stage_context / kb doc.
#   2. `summary_sig` is an HMAC of `summary || refs || project_id ||
#      stage || agent_key` using `LAMA_SESSION_SIGNING_KEY`. Any read
#      that fails signature validation MUST refuse to trust the summary
#      (we re-hydrate from `refs` and start a fresh summary).
#   3. `live_turns` keeps the last K turns verbatim (per-agent override
#      via `AGENT_MEMORY[agent_key].keep_last_k`, default 8).
#   4. `version` increments on every rollover so the Console UI can show
#      "rollover #N happened at <ts>".
class ContextRef(BaseModel):
    """A pointer to an authoritative artifact stored elsewhere in LAMA.

    Used inside `AgentSession.refs` so the rolling-memory layer
    references frozen stage_context / KB docs by ID instead of inlining
    their (potentially huge) content into the summary. The server
    re-hydrates `refs` on every fabric_call so the LLM always sees the
    latest version of the artifact, and a tampered summary cannot
    silently override a frozen fact.
    """
    model_config = ConfigDict(extra="ignore")
    collection: str           # "stage_context" | "kb_files" | "srs_documents" | …
    doc_id: str               # primary key of the doc inside `collection`
    label: str = ""           # human-readable hint (e.g. "Architecture frozen v3")
    hash: str = ""            # sha256 of the doc snapshot at attach time (drift detection)
    attached_at: str = Field(default_factory=_now_iso)


class AgentTurn(BaseModel):
    """One verbatim turn kept in the live tail of a rolling session."""
    model_config = ConfigDict(extra="ignore")
    role: str                  # "system" | "user" | "assistant"
    content: str
    token_count: int = 0       # chars/4 estimate at write time
    created_at: str = Field(default_factory=_now_iso)


class AgentSession(BaseModel):
    """Durable conversational/working context for ONE (project, stage,
    agent_key) thread. Persisted in the `agent_sessions` collection.

    Lifecycle:
      • created on first `fabric_call(..., session_id=...)` for the triple
      • appended to on every subsequent turn
      • rolled over when accumulated token count exceeds `window_budget_pct`
        of the resolved model's `context_window` (older turns → summary;
        last K turns kept verbatim)
      • resumed by Droid 2 simply by passing the same `session_id`
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    tenant_id: str = "tenant_default"
    stage: str = "Discovery"
    agent_key: str = "srs.chat"
    # Rolling summary of everything OLDER than `live_turns`. Empty until
    # the first rollover. NEVER trust this without HMAC validation —
    # see `summary_sig` below.
    summary: str = ""
    summary_sig: str = ""             # HMAC-SHA256 hex digest
    summary_model: str = ""           # model that wrote the latest summary
    rollover_count: int = 0           # number of times we've rolled over
    # Authoritative refs (stage_context IDs, frozen artifact IDs, …).
    # The server re-hydrates these on every read so a tampered summary
    # cannot smuggle in a fact that isn't backed by a real doc.
    refs: List[ContextRef] = Field(default_factory=list)
    # Live verbatim tail (last K turns). Older turns get compressed into
    # `summary` on rollover. K is configured per agent_key in
    # `fabric.model_fabric.AGENT_MEMORY` (default 8).
    live_turns: List[AgentTurn] = Field(default_factory=list)
    # Rough token budget at last rollover decision — chars/4 estimate.
    token_count: int = 0
    # Effective context window for the resolved model at last call
    # (snapshotted so the Console can show "85% full" without re-resolving).
    window_budget: int = 0
    # Status: "active" | "archived" (frozen / superseded)
    status: str = "active"
    # Audit
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class AgentSessionCreate(BaseModel):
    """POST /api/sessions body."""
    project_id: str
    stage: str = "Discovery"
    agent_key: str = "srs.chat"
    refs: List[ContextRef] = Field(default_factory=list)
    seed_summary: str = ""           # optional — seed from a droid-handoff brief


# ---------- Multi-Agent Transformer (iter-16) ----------

class TransformerEnvelope(BaseModel):
    """API-to-DB envelope discovered by the Context Manager agent.
    Captures one vertical slice: controller → service → repository → DB table."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    transform_id: str
    envelope_id: str = ""
    status: str = "draft"  # draft | approved | in_progress | coded | verified | done

    # Endpoint details
    endpoint_method: str = ""  # GET | POST | PUT | DELETE
    endpoint_path: str = ""
    controller_class: str = ""
    controller_file: str = ""

    # Service layer
    service_class: str = ""
    service_file: str = ""
    service_method: str = ""
    business_logic_summary: str = ""

    # Repository / data layer
    repository_class: str = ""
    repository_file: str = ""
    db_tables: List[str] = Field(default_factory=list)
    db_operations: List[str] = Field(default_factory=list)  # SELECT, INSERT, UPDATE, DELETE

    # External calls
    external_calls: List[Dict[str, Any]] = Field(default_factory=list)

    # Migration metadata
    files_affected: List[str] = Field(default_factory=list)
    action: str = "TRANSFORM"  # NO_CHANGE | TRANSFORM | REWRITE | DELETE | NEW
    risk_level: str = "low"  # low | medium | high | critical
    layer: str = ""  # controller | service | repository | entity | config | filter | util

    # Acceptance criteria
    acceptance_criteria: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)


class TransformerTask(BaseModel):
    """One atomic migration task created by the Planner agent."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    transform_id: str
    task_id: str = ""  # e.g. TASK-001
    envelope_id: str = ""

    # Task metadata
    title: str = ""
    description: str = ""
    phase: str = "scaffold"  # scaffold | logic | harden
    layer: str = ""  # entity | repository | service | controller | config | filter | util | build
    action: str = "TRANSFORM"  # NO_CHANGE | TRANSFORM | REWRITE | DELETE | NEW
    wave: int = 1
    wave_name: str = ""

    # Files
    source_path: str = ""
    target_path: str = ""

    # Status
    status: str = "PENDING"  # PENDING | IN_PROGRESS | CODED | VERIFIED | TESTED | DONE | BLOCKED
    assigned_to: str = "coder"  # coder | verifier | reviewer | tester
    depends_on: List[str] = Field(default_factory=list)
    confidence: float = 0.0

    # Verifier output
    verifier_score: float = 0.0
    verifier_checks: Dict[str, Any] = Field(default_factory=dict)
    rejection_count: int = 0

    # Notes
    notes: str = ""
    error: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class TransformerAgentRun(BaseModel):
    """One agent execution step in the multi-agent pipeline."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    transform_id: str
    agent: str = ""  # super_agent | context_manager | planner | coder | verifier | tester
    phase: str = ""  # init | plan | execute | verify | test | finalize
    task_id: str = ""  # optional — which task this run is for

    status: str = "running"  # running | completed | failed
    input_summary: str = ""
    output_summary: str = ""
    score: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    created_at: str = Field(default_factory=_now_iso)


# ---------- Multi-Agent CodeGen (iter-17) ----------
# Mirrors iter-16 Transformer models but scoped to a legacy_migration project's
# Stage-4 (CodeGen) pipeline instead of a standalone Transformer job. Kept in
# distinct collections (`codegen_*`) so the two pipelines can evolve
# independently — see AGENTS.md Iter-17 for the full rationale.

class CodeGenEnvelope(BaseModel):
    """API-to-DB envelope discovered by the CodeGen Context Manager agent.
    Captures one vertical slice: controller → service → repository → DB table.
    Consumes the frozen Architecture StageContext + Discovery KB (NOT tools_kb).
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    stage: str = "CodeGen"  # future multi-stage reuse
    envelope_id: str = ""
    status: str = "draft"  # draft | approved | in_progress | coded | verified | done

    # Endpoint details
    endpoint_method: str = ""  # GET | POST | PUT | DELETE
    endpoint_path: str = ""
    controller_class: str = ""
    controller_file: str = ""

    # Service layer
    service_class: str = ""
    service_file: str = ""
    service_method: str = ""
    business_logic_summary: str = ""

    # Repository / data layer
    repository_class: str = ""
    repository_file: str = ""
    db_tables: List[str] = Field(default_factory=list)
    db_operations: List[str] = Field(default_factory=list)

    # External calls
    external_calls: List[Dict[str, Any]] = Field(default_factory=list)

    # Files
    files_affected: List[str] = Field(default_factory=list)
    action: str = "NEW"  # NO_CHANGE | TRANSFORM | REWRITE | DELETE | NEW
    risk_level: str = "low"  # low | medium | high | critical
    layer: str = ""  # controller | service | repository | entity | config | filter | util
    side: str = "backend"  # backend | frontend | shared — helps the Planner fan-out

    # Acceptance criteria
    acceptance_criteria: List[str] = Field(default_factory=list)
    br_ids: List[str] = Field(default_factory=list)  # cited BR-* identifiers for traceability
    created_at: str = Field(default_factory=_now_iso)


class CodeGenTask(BaseModel):
    """One atomic code-generation task produced by the CodeGen Planner agent.

    `assigned_to` is one of `coder_be | coder_fe | verifier | reviewer | tester`.
    Planner suggests it; `routes.codegen._route_task_to_coder` deterministically
    overrides based on target_path so a frontend file NEVER lands on the BE
    coder and vice versa.
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    stage: str = "CodeGen"
    task_id: str = ""  # e.g. TASK-001
    envelope_id: str = ""

    # Task metadata
    title: str = ""
    description: str = ""
    phase: str = "scaffold"  # scaffold | logic | harden
    layer: str = ""  # BE: entity|repository|service|controller|config|filter|util|build
                     # FE: page|component|api_client|style|route|hook
    action: str = "NEW"  # NO_CHANGE | TRANSFORM | REWRITE | DELETE | NEW
    wave: int = 1
    wave_name: str = ""

    # Files
    source_path: str = ""
    target_path: str = ""

    # Status
    status: str = "PENDING"  # PENDING | APPROVED | IN_PROGRESS | CODED | VERIFIED | TESTED | DONE | BLOCKED
    assigned_to: str = "coder_be"  # coder_be | coder_fe | verifier | reviewer | tester
    depends_on: List[str] = Field(default_factory=list)
    confidence: float = 0.0

    # Verifier output
    verifier_score: float = 0.0
    verifier_checks: Dict[str, Any] = Field(default_factory=dict)
    rejection_count: int = 0

    # Notes
    notes: str = ""
    error: str = ""
    br_ids: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class CodeGenAgentRun(BaseModel):
    """One agent execution step in the multi-agent CodeGen pipeline.

    `agent` is one of:
      super_agent | context_manager | planner |
      coder_be | coder_fe |
      verifier | reviewer | tester |
      build_tool_selector | traceability_gate | finalizer
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_new_id)
    project_id: str
    stage: str = "CodeGen"
    agent: str = ""
    phase: str = ""  # init | context | plan | code | verify | review | test | traceability | finalize
    task_id: str = ""
    envelope_id: str = ""

    status: str = "running"  # running | completed | failed
    input_summary: str = ""
    output_summary: str = ""
    score: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    created_at: str = Field(default_factory=_now_iso)


class CodeGenPipelineState(BaseModel):
    """Singleton state document per project for the multi-agent CodeGen
    pipeline. One doc per project_id. Drives the FSM and stores between-
    phase metadata (build systems, error, timestamps).
    """
    model_config = ConfigDict(extra="ignore")
    project_id: str
    # idle | envelopes_pending | envelopes_confirmed | tasks_pending
    # | tasks_confirmed | executing | paused | traceability_gate
    # | completed | failed
    status: str = "idle"
    current_wave: int = 0
    build_system_be: str = ""
    build_system_fe: str = ""
    envelope_confirmed_at: Optional[str] = None
    tasks_confirmed_at: Optional[str] = None
    last_error: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # Traceability gate results
    br_coverage_pct: float = 0.0
    br_missing: List[str] = Field(default_factory=list)
    # Bookkeeping
    envelope_count: int = 0
    task_count: int = 0
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
