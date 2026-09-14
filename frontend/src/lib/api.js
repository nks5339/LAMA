import axios from "axios";

// In the single-image deploy nginx serves the SPA and proxies /api → uvicorn
// on the same origin, so an empty BACKEND_URL (relative /api) is the correct
// default. REACT_APP_BACKEND_URL can override for split local dev (e.g.
// "http://127.0.0.1:8000"). Never let it be literally "undefined".
const RAW_BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const BACKEND_URL =
  RAW_BACKEND_URL && RAW_BACKEND_URL !== "undefined"
    ? RAW_BACKEND_URL.replace(/\/+$/, "")
    : "";
export const API = `${BACKEND_URL}/api`;

const api = axios.create({ baseURL: API, timeout: 600000 });

// iter-13.68 — Multi-tenant. Attach Bearer token (JWT) on every request,
// and on 401 drop the token + redirect to /login so the user re-auths.
const TOKEN_KEY = "lama:auth:token";
api.interceptors.request.use((config) => {
  try {
    const t = typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) : "";
    if (t) {
      config.headers = config.headers || {};
      config.headers.Authorization = `Bearer ${t}`;
    }
  } catch (_) { /* localStorage may be blocked */ }
  return config;
});
api.interceptors.response.use(
  (r) => r,
  (err) => {
    const status = err?.response?.status;
    if (status === 401 && typeof window !== "undefined") {
      try {
        window.localStorage.removeItem(TOKEN_KEY);
        window.localStorage.removeItem("lama:auth:user");
        window.localStorage.removeItem("lama:auth:tenant");
      } catch (_) { /* */ }
      // Avoid redirect loop on the login page itself.
      if (!window.location.pathname.startsWith("/login")) {
        window.location.replace("/login");
      }
    }
    return Promise.reject(err);
  },
);

// iter-13.68 — Auth helpers.
export const login = (username, password) =>
  api.post("/auth/login", { username, password }).then((r) => r.data);
export const me = () => api.get("/auth/me").then((r) => r.data);
export const logout = () => api.post("/auth/logout").then((r) => r.data);

// iter-13.68 — Admin (super-admin only).
export const adminListTenants = () => api.get("/admin/tenants").then((r) => r.data);
export const adminCreateTenant = (payload) => api.post("/admin/tenants", payload).then((r) => r.data);
export const adminUpdateTenant = (id, payload) => api.patch(`/admin/tenants/${id}`, payload).then((r) => r.data);
export const adminDeleteTenant = (id) => api.delete(`/admin/tenants/${id}`).then((r) => r.data);
export const adminListUsers = (tenantId) =>
  api.get("/admin/users", { params: tenantId ? { tenant_id: tenantId } : {} }).then((r) => r.data);
export const adminCreateUser = (payload) => api.post("/admin/users", payload).then((r) => r.data);
export const adminUpdateUser = (id, payload) => api.patch(`/admin/users/${id}`, payload).then((r) => r.data);
export const adminDeleteUser = (id) => api.delete(`/admin/users/${id}`).then((r) => r.data);
export const adminDashboard = () => api.get("/admin/dashboard").then((r) => r.data);

// Projects
export const listProjects = () => api.get("/projects").then((r) => r.data);
export const createProject = (payload) =>
  api.post("/projects", payload).then((r) => r.data);
// iter-13.88 — hard delete a project and every artefact referencing it.
export const deleteProject = (projectId) =>
  api.delete(`/projects/${projectId}`).then((r) => r.data);
export const getPipelineStatus = (projectId) =>
  api.get(`/projects/${projectId}/pipeline`).then((r) => r.data);

// iter-13.66 — Skip / unskip an intermediate pipeline stage.
// Allowed stages: DataModel, Architecture, Living. Discovery and CodeGen
// are NOT skippable on the backend (HTTP 400). When the stage is currently
// frozen, pass force=true to overwrite the frozen artifacts with the skip
// marker (backend returns 409 otherwise).
export const skipStage = (projectId, stage, { force = false } = {}) =>
  api.post(`/pipeline/${projectId}/skip/${stage}`, null, { params: force ? { force: true } : {} }).then((r) => r.data);
export const unskipStage = (projectId, stage) =>
  api.post(`/pipeline/${projectId}/unskip/${stage}`).then((r) => r.data);

// iter-13.31 — per-project settings (graph-KB toggle, …).
// `use_graph_kb`: null = use env default; true/false = explicit override.
export const getProjectSettings = (projectId) =>
  api.get(`/projects/${projectId}/settings`).then((r) => r.data);
export const updateProjectSettings = (projectId, patch) =>
  api.patch(`/projects/${projectId}/settings`, patch).then((r) => r.data);

// iter-14.25 — Journey-KB toggle (Phase 2 wiring).
// Mirrors the Graph-KB toggle pattern: null = env default; true/false = override.
// Endpoints live on the KB router (see backend/routes/kb.py journeys/*).
export const getJourneySettings = (projectId) =>
  api.get(`/kb/${projectId}/journeys/config`).then((r) => r.data);
export const updateJourneySettings = (projectId, patch) =>
  api.post(`/kb/${projectId}/journeys/toggle`, patch).then((r) => r.data);

// KB
// iter-13.83 — `replace` (default true) wipes the project's prior KB before
// ingesting so re-scanning a different folder doesn't accumulate stale
// files from earlier scans. Pass replace=false for additive workflows.
export const scanFolder = (projectId, folderPath, kind = "", replace = true) =>
  api
    .post("/kb/scan-folder", {
      project_id: projectId,
      folder_path: folderPath,
      kind,
      replace,
    })
    .then((r) => r.data);

// iter-13.63 — Git ingest + module-selection (Discovery → Source Files accordion)
// iter-13.83 — `replace` (default true) — see scanFolder above.
// iter-14.5 — `/clone-git` now returns 200 with `{status:"ingesting", source_id, ...}`
// after the *clone* itself succeeds. Ingest runs on the backend in an
// asyncio background task. Callers that need the final file counts must
// poll `cloneGitStatus(projectId, source_id)` until `status === "done"`
// (or `"failed"`). The convenience wrapper `cloneGitRepoAndWait` below
// does the polling so most call-sites stay one-liners.
export const cloneGitRepo = (projectId, payload, replace = true) =>
  api
    .post(`/kb/${projectId}/clone-git`, { ...(payload || {}), replace })
    .then((r) => r.data);
export const cloneGitStatus = (projectId, sourceId = "") =>
  api
    .get(`/kb/${projectId}/clone-git/status`, {
      params: sourceId ? { source_id: sourceId, _t: Date.now() } : { _t: Date.now() },
    })
    .then((r) => r.data);
// Poll every `intervalMs` (default 2s) until ingest resolves to
// "done" / "failed" / "unknown", or `timeoutMs` (default 30 min) elapses.
// `onTick(doc)` is called with each poll response so the UI can show
// live-ish progress ("still ingesting…", cancel button, etc.).
export const cloneGitRepoAndWait = async (
  projectId, payload, replace = true, { intervalMs = 2000, timeoutMs = 1800000, onTick } = {},
) => {
  const initial = await cloneGitRepo(projectId, payload, replace);
  if (!initial?.source_id || initial.status === "done") return initial;
  const deadline = Date.now() + timeoutMs;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (Date.now() > deadline) {
      throw new Error(
        `Ingest still running after ${Math.round(timeoutMs / 1000)}s — the ` +
        `clone itself succeeded (commit ${initial.commit?.slice(0, 8) || "?"}); ` +
        `check "Source Files" in a moment.`,
      );
    }
    await new Promise((res) => setTimeout(res, intervalMs));
    let doc;
    try {
      doc = await cloneGitStatus(projectId, initial.source_id);
    } catch (_e) {
      // Transient poll error — retry next tick.
      continue;
    }
    if (typeof onTick === "function") {
      try { onTick(doc); } catch (_) { /* onTick is best-effort */ }
    }
    if (doc.status === "done" || doc.status === "failed") {
      return { ...initial, ...doc };
    }
  }
};
export const getGitSource = (projectId) =>
  api.get(`/kb/${projectId}/git-source`).then((r) => r.data);
// iter-13.83 — `replace` defaults to FALSE for upload (additive workflow);
// pass true to wipe prior KB before this upload batch.
export const uploadKBFiles = (projectId, files, kind = "", replace = false) => {
  const fd = new FormData();
  fd.append("project_id", projectId);
  if (kind) fd.append("kind", kind);
  if (replace) fd.append("replace", "true");
  files.forEach((f) => fd.append("files", f));
  return api.post("/kb/upload", fd, { headers: { "Content-Type": "multipart/form-data" } }).then((r) => r.data);
};

// iter-13.69 — File-kind taxonomy + per-file tagging.
// Single source of truth is backend/kb/file_kinds.py — this list is kept
// in lock-step so the dropdown works offline / before /api/kb/kinds resolves.
export const FILE_KINDS = [
  { value: "legacy_code",    label: "Legacy code",
    hint: "PHP / Java / .NET / Python / JS / etc. (default)." },
  { value: "legacy_db",      label: "Legacy DB / schema",
    hint: "SQL DDL, schema dumps, stored-procedure scripts." },
  { value: "existing_srs",   label: "Existing SRS / BRD",
    hint: "Prior requirements doc — LAMA cites it in the new SRS." },
  { value: "business_doc",   label: "Business / process doc",
    hint: "Policy notes, SOPs, user manuals, training material." },
  { value: "figma_export",   label: "Figma export / design tokens",
    hint: "Figma JSON / design-token JSON — FE codegen follows this theme." },
  { value: "design_mockup",  label: "Design mockup / wireframe",
    hint: "Screen mockup PDFs / images — FE codegen mirrors the layout." },
  { value: "api_spec",       label: "API spec (OpenAPI / Postman)",
    hint: "OpenAPI / Swagger / Postman / WSDL — Stage 3 uses verbatim." },
  { value: "test_artifact",  label: "Test artifact / UAT script",
    hint: "Test plans / recorded scenarios — Stage 5 uses for parity." },
  { value: "other",          label: "Other",
    hint: "Anything else — kept for context but not specially treated." },
];

export const listKBFiles = (projectId) => api.get(`/kb/${projectId}/files`).then((r) => r.data);
export const deleteKBFile = (fileId) => api.delete(`/kb/files/${fileId}`).then((r) => r.data);
// `force=true` wipes prior extracted entities for the project so that
// re-extraction uses the *current* extractor (needed after extractor upgrades).
export const buildKB = (projectId, { force = false } = {}) =>
  api.post("/kb/build", { project_id: projectId, force }).then((r) => r.data);
export const kbStatus = (projectId) =>
  // Cache-bust so the Refresh button on the KB Health card always hits the
  // server — some proxies (and Service Workers) were serving 200s from cache
  // and the counters appeared "stuck". Iter 13.4.
  api.get(`/kb/${projectId}/status`, { params: { _t: Date.now() } }).then((r) => r.data);
// iter-13.33 — live KB-build phase tracker. Poll this every 1–2 s while
// buildKB() is in flight to know whether it is still running (running:true,
// phase ∈ {extracting, aggregating, tech_detect, toon_persist, graph_build,
// graphify, qdrant_indexing}) or finished (phase: "done"/"error" with an
// "error" message when failed).
export const kbBuildProgress = (projectId) =>
  api.get(`/kb/${projectId}/build-progress`, { params: { _t: Date.now() } }).then((r) => r.data);
// iter-13.17 — deep legacy-logic analysis (pre-SRS pass).
// POST runs the analyzer (default cache TTL 24h; pass {force: true} to
// re-run). GET returns the persisted analysis doc verbatim, or
// {exists:false} if it hasn't been built yet. The SRS generate route
// auto-triggers this if missing, so explicit calls from the UI are
// optional / power-user.


// ─── Target-stack suggestions (knowledge-graph driven) ──────────────────
// Surfaces top-N candidate modern stacks predicted from the legacy KB.
// User picks one → POST writes it into project.target_tech, where SRS,
// Architecture, CodeGen and Living pipelines already consume it.
export const getTargetStackSuggestions = (projectId, topN = 3) =>
  api
    .get(`/kb/${projectId}/target-stack/suggestions`, { params: { top_n: topN, _t: Date.now() } })
    .then((r) => r.data);
export const selectTargetStack = (projectId, payload) =>
  api.post(`/kb/${projectId}/target-stack/select`, payload).then((r) => r.data);

// ─── iter 13.8 — live DB / app-URL ingestion ──────────────────────────
// Connect to a live database (postgres/mysql/oracle/mssql/sqlite) and
// pull schema metadata directly into the KB so the SRS prompts have
// authoritative table+FK info. When the driver isn't installed or
// credentials fail, the backend stores a descriptor-only fallback.
export const dbConnect = (projectId, payload) =>
  api.post(`/kb/${projectId}/db-connect`, payload, { timeout: 60000 }).then((r) => r.data);
export const registerAppUrl = (projectId, application_url, notes = "") =>
  api.post(`/kb/${projectId}/app-url`, { application_url, notes }, { timeout: 15000 }).then((r) => r.data);
export const listDataSources = (projectId) =>
  api.get(`/kb/${projectId}/data-sources`, { params: { _t: Date.now() } }).then((r) => r.data);

// Chat
export const listModels = () => api.get("/chat/models").then((r) => r.data);
export const sendMessage = (payload) => api.post("/chat", payload).then((r) => r.data);

// iter-13.100 — Rolling-memory agent sessions (droid-handoff aware).
// One session per (project, stage, agent_key) gives the LLM an
// "infinite conversation" that survives browser refresh, context-window
// overflow (rollover summarises older turns) and droid swap (Droid 2
// resumes by passing the same session_id).
export const createSession = (payload) =>
  api.post("/sessions", payload).then((r) => r.data);
export const getSession = (sessionId) =>
  api.get(`/sessions/${sessionId}`).then((r) => r.data);
export const listSessions = (projectId, { stage, agentKey, includeArchived } = {}) =>
  api.get("/sessions", {
    params: {
      project_id: projectId,
      ...(stage ? { stage } : {}),
      ...(agentKey ? { agent_key: agentKey } : {}),
      ...(includeArchived ? { include_archived: true } : {}),
    },
  }).then((r) => r.data);
export const archiveSession = (sessionId, reason = "") =>
  api.post(`/sessions/${sessionId}/archive`, { reason }).then((r) => r.data);

// SRS
// iter-13.35 — cache-bust on every read so a freshly-regenerated section
// (in particular the storytelling Use Cases) never shows stale content
// from a CDN / proxy / browser cache after page reload.
export const getSRS = (projectId) =>
  api.get(`/srs/${projectId}`, { params: { _t: Date.now() } }).then((r) => r.data);
export const updateSRSSection = (projectId, section, content) =>
  api.put(`/srs/${projectId}/section`, { section, content }).then((r) => r.data);
export const freezeSRS = (projectId, user, override) =>
  api.post("/srs/freeze", { project_id: projectId, user, ...(override ? { override } : {}) }).then((r) => r.data);
export const unfreezeSRS = (projectId) => api.post("/srs/unfreeze", { project_id: projectId }).then((r) => r.data);

// iter-14.25.9 — Reset the entire SRS document + downstream artefacts and
// return Discovery to a clean slate. Typed confirmation ("RESET") required.
// KB, chat, analysis_digest, journeys, and Qdrant vectors are preserved.
export const resetSRS = (projectId, confirm = "RESET") =>
  api.post(`/srs/${projectId}/reset`, { confirm }).then((r) => r.data);
export const srsPdfUrl = (projectId) => `${API}/srs/${projectId}/export.pdf`;
// Polling fallback for the long-running SRS background job. Used by SRSPanel
// to recover gracefully when the SSE connection drops mid-stream — the
// backend keeps generating even when the client connection is gone, so we
// just need to wait for the job to finish and then GET /srs/{id}.
export const getSRSGenerateStatus = (projectId) =>
  api.get(`/srs/${projectId}/generate/status`).then((r) => r.data);
// iter-13.35 — live-job controls + per-section regenerate.
export const pauseSRSGeneration = (projectId) =>
  api.post(`/srs/${projectId}/generate/pause`).then((r) => r.data);
export const resumeSRSGeneration = (projectId) =>
  api.post(`/srs/${projectId}/generate/resume`).then((r) => r.data);
export const cancelSRSGeneration = (projectId) =>
  api.post(`/srs/${projectId}/generate/cancel`).then((r) => r.data);
export const regenerateSRSSection = (projectId, sectionKey, { model = "", conversationId = "" } = {}) =>
  api.post(`/srs/${projectId}/section/${sectionKey}/regenerate`, {
    model,
    conversation_id: conversationId,
  }).then((r) => r.data);

// iter-13.37 — SSE variant. Plain POST above can blow the 60 s ingress
// timeout (504) because _gen_one_section may run for 2–4 minutes. The
// streaming variant emits a `ping` event every 4 s so every reverse
// proxy on the path keeps the connection alive, and a final `complete`
// event with the section content. Caller-supplied callbacks let the UI
// surface progress without polling.
//   onEvent(evt)  — fires for every SSE event: { type, ... }
// Returns a Promise that resolves to the final `complete` payload
// (or rejects if the backend emits an `error` event or the network dies).
export const regenerateSRSSectionStream = async (
  projectId,
  sectionKey,
  { model = "", conversationId = "" } = {},
  onEvent = () => {},
) => {
  const res = await fetch(`${API}/srs/${projectId}/section/${sectionKey}/regenerate/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ model, conversation_id: conversationId }),
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let final = null;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const events = buf.split("\n\n");
    buf = events.pop() || "";
    for (const evt of events) {
      const line = evt.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let data;
      try { data = JSON.parse(line.slice(5).trim()); } catch { continue; }
      try { onEvent(data); } catch (_) { /* swallow listener errors */ }
      if (data.type === "complete") {
        final = data;
      } else if (data.type === "error") {
        throw new Error(data.message || "Section regenerate failed");
      }
    }
    if (final) break;
  }
  if (!final) {
    throw new Error("Stream ended without a complete event");
  }
  return final;
};

// Prompts
export const listPrompts = () => api.get("/prompts").then((r) => r.data);
export const updatePrompt = (key, payload) => api.put(`/prompts/${key}`, payload).then((r) => r.data);
export const listProjectPrompts = (projectId) => api.get(`/prompts/project/${projectId}`).then((r) => r.data);
export const updateProjectPrompt = (projectId, key, payload) =>
  api.put(`/prompts/project/${projectId}/${key}`, payload).then((r) => r.data);

// Audit
export const listAudit = (projectId) =>
  api.get(`/audit`, { params: { project_id: projectId } }).then((r) => r.data);

// iter-13.101 — Detail Log Trace for a single LLM call.
// Returns the full {request_prompt, response_prompt, request_time,
// response_time, elapsed_ms, stage, agent_key, status, error_reason, …}
// envelope captured by llm.py::fabric_call.
export const getAuditTrace = (traceId) =>
  api.get(`/audit/trace/${traceId}`).then((r) => r.data);

// Data Model — Stage 2
// Job-based (recommended for OLTP + OLAP + scripts — bypasses ~60s ingress timeout)
export const startOLTPJob = (projectId, model) =>
  api.post("/data-model/jobs/start/oltp", { project_id: projectId, model }).then((r) => r.data);
export const startOLAPJob = (projectId, model) =>
  api.post("/data-model/jobs/start/olap", { project_id: projectId, model }).then((r) => r.data);
export const startScriptsJob = (projectId, model) =>
  api.post("/data-model/jobs/start/scripts", { project_id: projectId, model }).then((r) => r.data);
export const getDataModelJob = (jobId) =>
  api.get(`/data-model/jobs/${jobId}`).then((r) => r.data);
export const generateBusMatrix = (projectId, model) =>
  api.post("/data-model/generate/bus-matrix", { project_id: projectId, model }).then((r) => r.data);
export const generateEntityGraph = (projectId) =>
  api.post("/data-model/generate/entity-graph", { project_id: projectId }).then((r) => r.data);
export const getDataModelArtifacts = (projectId) =>
  api.get(`/data-model/${projectId}/artifacts`).then((r) => r.data);
export const getArtifact = (projectId, artifactId) =>
  api.get(`/data-model/${projectId}/artifact/${artifactId}`).then((r) => r.data);
export const updateArtifact = (projectId, artifactId, content) =>
  api.put(`/data-model/${projectId}/artifact/${artifactId}`, { content }).then((r) => r.data);
export const freezeArtifact = (projectId, artifactId) =>
  api.post(`/data-model/${projectId}/artifact/${artifactId}/freeze`).then((r) => r.data);
export const downloadArtifactUrl = (projectId, artifactId) =>
  `${API}/data-model/${projectId}/artifact/${artifactId}/download`;
export const factoryReset = (projectId) =>
  api.post(`/projects/${projectId}/factory-reset`).then((r) => r.data);
export const resetStage2 = (projectId) =>
  api.post(`/data-model/${projectId}/reset`).then((r) => r.data);

// Architecture — Stage 3
export const startArchRecommend = (projectId, model, message) =>
  api.post("/architecture/jobs/start/recommend", { project_id: projectId, model, message }).then((r) => r.data);
export const startArchHld = (projectId, model) =>
  api.post("/architecture/jobs/start/hld", { project_id: projectId, model }).then((r) => r.data);
export const startArchLld = (projectId, model) =>
  api.post("/architecture/jobs/start/lld", { project_id: projectId, model }).then((r) => r.data);
export const startArchSequence = (projectId, model) =>
  api.post("/architecture/jobs/start/sequence", { project_id: projectId, model }).then((r) => r.data);
export const startArchApiContracts = (projectId, model) =>
  api.post("/architecture/jobs/start/api_contracts", { project_id: projectId, model }).then((r) => r.data);
export const getArchJob = (jobId) =>
  api.get(`/architecture/jobs/${jobId}`).then((r) => r.data);
export const approveServiceMap = (projectId, approved = true, overrides = [], selectedServiceNames = null, selectedUtilities = null) =>
  api.post("/architecture/approve", {
    project_id: projectId,
    approved,
    overrides,
    // iter-13.57 — when null, backend keeps all services (legacy behaviour).
    // When an array, backend prunes arch_services to exactly this set so
    // every downstream job sees only the user-selected services.
    selected_service_names: selectedServiceNames === null ? undefined : selectedServiceNames,
    // iter-13.59 — per-utility selection: [{key, config}]. When null,
    // backend leaves the currently-persisted utility set untouched.
    selected_utilities:     selectedUtilities     === null ? undefined : selectedUtilities,
  }).then((r) => r.data);
// iter-13.59 — Cross-cutting utility catalog (audit-logger etc.).
export const listArchUtilities = (projectId) =>
  api.get(`/architecture/${projectId}/utilities`).then((r) => r.data);
export const sendArchChat = (payload) =>
  api.post("/architecture/chat", payload).then((r) => r.data);
export const applyArchChanges = (projectId, changes, conversationMessageId) =>
  api.post(`/architecture/${projectId}/apply-changes`, { changes, conversation_message_id: conversationMessageId }).then((r) => r.data);
export const getArchArtifacts = (projectId) =>
  api.get(`/architecture/${projectId}/artifacts`).then((r) => r.data);
export const updateArchArtifact = (projectId, artifactId, content) =>
  api.put(`/architecture/${projectId}/artifact/${artifactId}`, { content }).then((r) => r.data);
export const freezeArchArtifact = (projectId, artifactId) =>
  api.post(`/architecture/${projectId}/artifact/${artifactId}/freeze`).then((r) => r.data);
export const downloadArchArtifactUrl = (projectId, artifactId) =>
  `${API}/architecture/${projectId}/artifact/${artifactId}/download`;
// iter-14.25.12 — PDF variant (HLD / LLD / Sequence Diagrams / API
// Contracts / Service Map). Business reviewers download this; developers
// keep using the raw markdown / YAML / JSON URL above.
export const downloadArchArtifactPdfUrl = (projectId, artifactId) =>
  `${API}/architecture/${projectId}/artifact/${artifactId}/download.pdf`;
export const resetArch = (projectId) =>
  api.post(`/architecture/${projectId}/reset`).then((r) => r.data);
// iter-13.50 — Selective cleanup of artifacts whose body is just baked-in
// transport (DNS/connect) error messages from before the iter-13.50 fix.
export const purgeBrokenArch = (projectId, types) =>
  api.post(`/architecture/${projectId}/purge-broken`, types ? { types } : {}).then((r) => r.data);

// iter-13.81.13 — Merge N services into ONE combined service so CodeGen
// materialises a single source tree. mergedName must be lower-kebab-case.
export const mergeServices = (projectId, serviceNames, mergedName,
                              mergedDisplayName = "", mergedDescription = "") =>
  api.post(`/architecture/${projectId}/merge-services`, {
    service_names:        serviceNames,
    merged_name:          mergedName,
    merged_display_name:  mergedDisplayName || undefined,
    merged_description:   mergedDescription || undefined,
  }).then((r) => r.data);
export const unmergeService = (projectId, mergedName) =>
  api.post(`/architecture/${projectId}/unmerge-service`,
           { merged_name: mergedName }).then((r) => r.data);

// iter-14.33 — Apply MULTIPLE named merge-groups in one atomic-ish call.
// `groups` is an array of { merged_name, service_names, merged_display_name?,
// merged_description? }. Rejected all-or-nothing on validation; applied
// sequentially on write.
export const mergeServicesBatch = (projectId, groups) =>
  api.post(`/architecture/${projectId}/merge-services/batch`, { groups })
     .then((r) => r.data);

// CodeGen — Stage 4
// iter-13.120 — startCodegenJob accepts either a legacy scalar (string —
// back-compat with older callers / regen-single-service flow) OR an
// array of service names (new multi-select flow). null / undefined /
// empty array all mean "generate every service" (Generate All).
export const startCodegenJob = (projectId, model, serviceNameOrList) => {
  const body = { project_id: projectId, model };
  if (Array.isArray(serviceNameOrList)) {
    if (serviceNameOrList.length > 0) body.service_names = serviceNameOrList;
  } else if (serviceNameOrList) {
    body.service_name = serviceNameOrList;
  }
  return api.post("/codegen/jobs/start/generate", body).then((r) => r.data);
};
export const startGapRecoveryBackend = (projectId, model, serviceNameOrList) => {
  const body = { project_id: projectId, model };
  if (Array.isArray(serviceNameOrList)) {
    if (serviceNameOrList.length > 0) body.service_names = serviceNameOrList;
  } else if (serviceNameOrList) {
    body.service_name = serviceNameOrList;
  }
  return api.post("/codegen/jobs/start/gap-recovery-backend", body).then((r) => r.data);
};
export const startGapRecoveryFrontend = (projectId, model, serviceNameOrList) => {
  const body = { project_id: projectId, model };
  if (Array.isArray(serviceNameOrList)) {
    if (serviceNameOrList.length > 0) body.service_names = serviceNameOrList;
  } else if (serviceNameOrList) {
    body.service_name = serviceNameOrList;
  }
  return api.post("/codegen/jobs/start/gap-recovery-frontend", body).then((r) => r.data);
};
export const getCodegenJob = (jobId) =>
  api.get(`/codegen/jobs/${jobId}`).then((r) => r.data);
// iter-13.54 — Cooperative pause / resume / stop for any in-flight codegen
// or gap-recovery job. The backend flips a flag and per-file coroutines
// pick it up at their next checkpoint (~1s latency).
export const pauseCodegenJob = (jobId) =>
  api.post(`/codegen/jobs/${jobId}/pause`).then((r) => r.data);
export const resumeCodegenJob = (jobId) =>
  api.post(`/codegen/jobs/${jobId}/resume`).then((r) => r.data);
export const stopCodegenJob = (jobId) =>
  api.post(`/codegen/jobs/${jobId}/stop`).then((r) => r.data);
// iter-13.119 — Automated validation + improvement loop ("Auto-Validate
// & Improve"). Scores every generated source file across 6 axes, runs
// gap-recovery on the worst, loops until confidence ≥ threshold (default
// 95%) or max_iterations hit. Reuses the same job control APIs above.
export const startAutoValidate = (projectId, opts = {}) =>
  api.post("/codegen/jobs/start/auto-validate", {
    project_id: projectId,
    threshold: opts.threshold ?? 95,
    max_iterations: opts.maxIterations ?? 5,
    max_files_per_iter: opts.maxFilesPerIter ?? 20,
    service_name: opts.serviceName || undefined,
    model: opts.model || undefined,
  }).then((r) => r.data);
export const getParityReport = (projectId, runId) =>
  api.get(`/codegen/${projectId}/parity-report`, {
    params: runId ? { run_id: runId } : {},
  }).then((r) => r.data);
export const listCodegenFiles = (projectId) =>
  api.get(`/codegen/${projectId}/files`).then((r) => r.data);
// iter-13.122 — Legacy → New API mapping preview. Read-only, deterministic.
// Sourced from arch_services.routes_detail (legacy) + api_endpoints (new).
// Rendered on the CodeGen page BEFORE Generate so the user can validate
// coverage before committing to a generation run.
export const getCodegenApiMapping = (projectId) =>
  api.get(`/codegen/${projectId}/api-mapping`).then((r) => r.data);
export const getCodegenFile = (projectId, fileId) =>
  api.get(`/codegen/${projectId}/file/${fileId}`).then((r) => r.data);
export const updateCodegenFile = (projectId, fileId, content) =>
  api.put(`/codegen/${projectId}/file/${fileId}`, { content }).then((r) => r.data);
// iter-13.56 — right-click context menu in CodeGen file tree
export const deleteCodegenFile = (projectId, fileId) =>
  api.delete(`/codegen/${projectId}/file/${fileId}`).then((r) => r.data);
export const deleteCodegenPath = (projectId, pathPrefix, serviceName) =>
  api.post(`/codegen/${projectId}/delete-path`, {
    path_prefix: pathPrefix,
    service_name: serviceName || undefined,
  }).then((r) => r.data);
export const startCodegenZipDownload = (projectId) =>
  api.post(`/codegen/${projectId}/download-zip`, null, { responseType: "blob" }).then((r) => r.data);
// iter-13.110 — Export the generated frontend + backend project to a real
// folder on disk OUTSIDE the LAMA repo (default: <parent>/tanent-project-data/).
export const exportCodegenToDisk = (projectId) =>
  api.post(`/codegen/${projectId}/export-to-disk`).then((r) => r.data);
export const getCodegenExportRoot = (projectId) =>
  api.get(`/codegen/export-root`, { params: projectId ? { project_id: projectId } : {} }).then((r) => r.data);
export const startGithubPushJob = (projectId) =>
  api.post("/codegen/jobs/start/github-push", { project_id: projectId }).then((r) => r.data);
export const sendCodegenChat = (payload) =>
  api.post("/codegen/chat", payload).then((r) => r.data);
export const applyCodegenFileChange = (projectId, fileId, newContent, conversationMessageId) =>
  api.post(`/codegen/${projectId}/apply-file-change`, { file_id: fileId, new_content: newContent, conversation_message_id: conversationMessageId }).then((r) => r.data);
export const freezeCodegen = (projectId) =>
  api.post(`/codegen/${projectId}/freeze`).then((r) => r.data);
export const resetCodegen = (projectId) =>
  api.post(`/codegen/${projectId}/reset`).then((r) => r.data);


// Business-domain ontology (deterministic clusters + LLM enrichment)
export const getBusinessOntology = (projectId) =>
  api.get(`/kb/${projectId}/business-ontology`).then((r) => r.data);
export const startBusinessOntologyJob = (projectId, force = false) =>
  api.post(`/kb/${projectId}/business-ontology/jobs/start`, { force }).then((r) => r.data);
export const getBusinessOntologyJob = (projectId, jobId) =>
  api.get(`/kb/${projectId}/business-ontology/jobs/${jobId}`).then((r) => r.data);

// Living — Stage 5
export const startLivingJob = (kind, projectId, extra = {}) =>
  api.post(`/living/jobs/start/${kind}`, { project_id: projectId, ...extra }).then((r) => r.data);
export const getLivingJob = (jobId) =>
  api.get(`/living/jobs/${jobId}`).then((r) => r.data);
export const listLivingArtifacts = (projectId) =>
  api.get(`/living/${projectId}/artifacts`).then((r) => r.data);
export const getLivingArtifact = (projectId, artId) =>
  api.get(`/living/${projectId}/artifact/${artId}`).then((r) => r.data);
export const updateLivingArtifact = (projectId, artId, files) =>
  api.put(`/living/${projectId}/artifact/${artId}`, { files }).then((r) => r.data);
export const freezeLivingArtifact = (projectId, artId) =>
  api.post(`/living/${projectId}/artifact/${artId}/freeze`).then((r) => r.data);
export const downloadLivingArtifactUrl = (projectId, artId) =>
  `${API}/living/${projectId}/artifact/${artId}/download`;
export const freezeLiving = (projectId) =>
  api.post(`/living/${projectId}/freeze`).then((r) => r.data);
export const resetLiving = (projectId) =>
  api.post(`/living/${projectId}/reset`).then((r) => r.data);

// iter-13.70 — Accuracy Report (multi-model KB-vs-artifacts confidence).
export const startAccuracyReport = (projectId, sections = null) =>
  api
    .post(`/living/${projectId}/jobs/start/accuracy-report`, sections ? { sections } : {})
    .then((r) => r.data);
export const getLatestAccuracyReport = (projectId) =>
  api.get(`/living/${projectId}/accuracy-report/latest`).then((r) => r.data);

// iter-14.50 — Detailed Test-Case Matrix + Excel export
export const startTestCases = (projectId, model = "") =>
  api.post(`/living/jobs/start/test-cases`, { project_id: projectId, model }).then((r) => r.data);
export const cancelLivingJob = (jobId) =>
  api.post(`/living/jobs/${jobId}/cancel`).then((r) => r.data);
export const downloadTestCasesExcelUrl = (projectId, artId) =>
  `${API}/living/${projectId}/artifact/${artId}/excel`;

// Console — Model Fabric
export const setupProvider = (data) =>
  api.post("/console/providers/setup", data).then((r) => r.data);
export const listProviders = () =>
  api.get("/console/providers").then((r) => r.data);
export const updateProvider = (id, data) =>
  api.put(`/console/providers/${id}`, data).then((r) => r.data);
export const updateProviderKey = (id, apiKey) =>
  api.put(`/console/providers/${id}/key`, { api_key: apiKey }).then((r) => r.data);
export const deleteProvider = (id) =>
  api.delete(`/console/providers/${id}`).then((r) => r.data);
export const testProvider = (id) =>
  api.post(`/console/providers/${id}/test`).then((r) => r.data);
export const fetchProviderModels = (id) =>
  api.post(`/console/providers/${id}/fetch-models`).then((r) => r.data);
// iter-14.17 — live backend log tail (polls, no SSE).
// Returns { records: [{seq, ts, level, name, msg}], next_seq, dropped, capacity, size }
export const tailBackendLogs = ({ sinceSeq = 0, limit = 300, minLevel = "", contains = "" } = {}) =>
  api
    .get("/console/logs/tail", {
      params: {
        since_seq: sinceSeq,
        limit,
        min_level: minLevel || undefined,
        contains:  contains || undefined,
      },
    })
    .then((r) => r.data);
export const getFactoryOrchestratorConfig = (projectId) =>
  api.get("/console/factory-orchestrator/config", { params: { project_id: projectId } }).then((r) => r.data);
export const updateFactoryOrchestratorConfig = (payload) =>
  api.put("/console/factory-orchestrator/config", payload).then((r) => r.data);
export const testFactoryOrchestratorConfig = (projectId) =>
  api.get("/console/factory-orchestrator/test", { params: { project_id: projectId } }).then((r) => r.data);
// iter-13.125 — Standalone CLI probe so operators can validate a
// candidate `droid` binary path BEFORE persisting the config. Powers
// the "Test CLI" button in the Factory Orchestrator tab.
export const testFactoryOrchestratorCli = (cliBin) =>
  api.get("/console/factory-orchestrator/test-cli", { params: { cli_bin: cliBin || "" } }).then((r) => r.data);
export const deleteFactoryOrchestratorConfig = (projectId) =>
  api.delete("/console/factory-orchestrator/config", { params: { project_id: projectId } }).then((r) => r.data);
// iter-13.91.4 — explicit "create / refresh per-project workspace on the
// Droid filesystem" helper. Backend forces a fresh `mkdir -p` (skips the
// per-process cache) so the user sees real Droid state immediately.
export const ensureFactoryOrchestratorWorkspace = (projectId) =>
  api
    .post("/console/factory-orchestrator/workspace", { project_id: projectId })
    .then((r) => r.data);
// iter-13.91.4 — explicit "wake the Droid Computer now" helper. Sends a
// throttle-bypassed GET to /computers/{id} (Factory's auto-resume trigger)
// so the user can warm a cold Droid before starting a long-running stage.
export const wakeFactoryOrchestratorDroid = (projectId) =>
  api
    .post("/console/factory-orchestrator/wake", { project_id: projectId })
    .then((r) => r.data);
// iter-13.36 — manual re-trigger for the first-run Factory AI onboarding
// (KB build + top-3 target stack recommendation, DB pinned to legacy).
// The auto-fire on first activation is server-side; this is the explicit
// re-run / force-replay hook for the Console UI.

// Console — Agent Fabric
export const listAgents = () =>
  api.get("/console/agents").then((r) => r.data);
export const updateAgent = (key, data) =>
  api.put(`/console/agents/${encodeURIComponent(key)}`, data).then((r) => r.data);
export const resetAgentBudget = (key) =>
  api.post(`/console/agents/${encodeURIComponent(key)}/reset-budget`).then((r) => r.data);
export const testAgent = (key, projectId) =>
  api.post(`/console/agents/${encodeURIComponent(key)}/test`, { project_id: projectId }).then((r) => r.data);

// Console — Usage
export const getUsageSummary = (projectId, days = 7) =>
  api.get(`/console/usage/summary`, { params: { project_id: projectId || "", days } }).then((r) => r.data);

// Console — Prompt engineering
export const previewPrompt = (promptKey, projectId) =>
  api.post("/console/prompts/preview", { prompt_key: promptKey, project_id: projectId }).then((r) => r.data);
export const testPrompt = (promptKey, projectId, modelOverride) =>
  api.post("/console/prompts/test", {
    prompt_key: promptKey, project_id: projectId, model_override: modelOverride || "",
  }).then((r) => r.data);

// ─── Integrations — Stage-4 govt-service injector (iter-13.60) ──────
// Catalog is project-agnostic; selections + inject are per-project.
// Injected files land in codegen_files under `service_name=integrations`
// and are picked up by the existing ZIP / GitHub-push flow.
export const getProjectIntegrations = (projectId) =>
  api.get(`/integrations/${projectId}/selections`).then((r) => r.data);
export const setProjectIntegration = (projectId, integrationId, enabled, configOverrides = {}) =>
  api
    .put(`/integrations/${projectId}/selections/${integrationId}`, {
      enabled,
      config_overrides: configOverrides,
    })
    .then((r) => r.data);
export const injectIntegrations = (projectId, language = "") =>
  api
    .post(`/integrations/${projectId}/inject`, language ? { language } : {})
    .then((r) => r.data);

// ── iter-13.71 — per-stage Accuracy / Confidence badge ───────────────
// GET returns the latest persisted report (or { present:false }).
// POST recomputes — by default kicks off a background job and returns
// `{ job_id, status:"queued" }`; pass `{ background:false }` for the
// legacy blocking call. The job endpoints expose live progress + pause/
// resume/stop control so the UI can render a progress popover.
export const getStageConfidence = (projectId, stage) =>
  api.get(`/pipeline/${projectId}/confidence/${stage}`).then((r) => r.data);
export const recomputeStageConfidence = (
  projectId, stage, { background = true, fast = false } = {},
) =>
  api
    .post(`/pipeline/${projectId}/confidence/${stage}/recompute`, null, {
      params: { background, fast },
    })
    .then((r) => r.data);
export const getConfidenceJob = (projectId, jobId) =>
  api.get(`/pipeline/${projectId}/confidence/jobs/${jobId}`).then((r) => r.data);
export const pauseConfidenceJob = (projectId, jobId) =>
  api.post(`/pipeline/${projectId}/confidence/jobs/${jobId}/pause`).then((r) => r.data);
export const resumeConfidenceJob = (projectId, jobId) =>
  api.post(`/pipeline/${projectId}/confidence/jobs/${jobId}/resume`).then((r) => r.data);
export const stopConfidenceJob = (projectId, jobId) =>
  api.post(`/pipeline/${projectId}/confidence/jobs/${jobId}/stop`).then((r) => r.data);
// iter-14.10 — Auto-improve loop for the Discovery/SRS confidence pill.
// Kicks off score → regenerate-below-threshold → re-score, capped at
// max_iterations. Reuses the same /confidence/jobs/{id} progress
// polling + pause/resume/stop as `recomputeStageConfidence`.

// ════════════════════════════════════════════════════════════════════════════
// Tools — Standalone utilities (bypass pipeline)
// ════════════════════════════════════════════════════════════════════════════

// Gap Analyzer — Code vs SRS/FRS/User Manual
// onProgress: ({ phase, percent, message }) => void
//   phase: "uploading" | "processing" | "analyzing"
export const createGapAnalysis = (formData, { onProgress } = {}) =>
  new Promise((resolve, reject) => {
    const token = typeof window !== "undefined" ? window.localStorage.getItem("lama:auth:token") : "";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/tools/gap-analyzer/create`);
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.timeout = 1800000; // 30 minutes for large files

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        const pct = Math.round((e.loaded / e.total) * 100);
        onProgress({ phase: "uploading", percent: pct, message: `Uploading files... ${pct}%` });
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch { resolve(xhr.responseText); }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.detail || `HTTP ${xhr.status}`));
        } catch { reject(new Error(`HTTP ${xhr.status}: ${xhr.statusText}`)); }
      }
    };

    xhr.onerror = () => reject(new Error("Network error — check connection or file size"));
    xhr.ontimeout = () => reject(new Error("Request timed out — files may be too large"));

    if (onProgress) onProgress({ phase: "uploading", percent: 0, message: "Starting upload..." });
    xhr.send(formData);
  });
export const pollGapAnalysisStatus = (analysisId) =>
  api.get(`/tools/gap-analyzer/${analysisId}/status`).then((r) => r.data);
export const runGapAnalysis = (analysisId, model = null) =>
  api.post(`/tools/gap-analyzer/${analysisId}/run`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const listGapAnalyses = () =>
  api.get("/tools/gap-analyzer").then((r) => r.data?.items || []);
export const getGapAnalysis = (analysisId) =>
  api.get(`/tools/gap-analyzer/${analysisId}`).then((r) => r.data);
export const deleteGapAnalysis = (analysisId) =>
  api.delete(`/tools/gap-analyzer/${analysisId}`).then((r) => r.data);
export const freezeGapAnalysis = (analysisId) =>
  api.post(`/tools/gap-analyzer/${analysisId}/freeze`, { confirm: "FREEZE" }).then((r) => r.data);
export const unfreezeGapAnalysis = (analysisId) =>
  api.post(`/tools/gap-analyzer/${analysisId}/unfreeze`, { confirm: "UNFREEZE" }).then((r) => r.data);
export const gapAnalysisExportUrl = (analysisId, format) =>
  `${API}/tools/gap-analyzer/${analysisId}/export/${format}`;
export const getGapAnalysisKB = (analysisId) =>
  api.get(`/tools/gap-analyzer/${analysisId}/kb`).then((r) => r.data);
export const getTransformerKB = (transformId) =>
  api.get(`/tools/transformer/${transformId}/kb`).then((r) => r.data);

// Transformer — Code Stack Transformation

// iter-15.40 — Fetch candidate build tools for the selected target-stack
// components. `params` = { backend, frontend, runtime, database }.
// Returns `{ suggestions: { component: [tools...] }, native_support: [...] }`.
export const suggestBuildTools = (params) =>
  api.get("/tools/transformer/build-tools", { params }).then((r) => r.data);

// onProgress: ({ phase, percent, message }) => void
//   phase: "uploading" | "processing" | "transforming"
export const createTransformation = (formData, { onProgress } = {}) =>
  new Promise((resolve, reject) => {
    const token = typeof window !== "undefined" ? window.localStorage.getItem("lama:auth:token") : "";
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/tools/transformer/create/v2`);
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.timeout = 1800000; // 30 minutes for large files

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        const pct = Math.round((e.loaded / e.total) * 100);
        onProgress({ phase: "uploading", percent: pct, message: `Uploading files... ${pct}%` });
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch { resolve(xhr.responseText); }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.detail || `HTTP ${xhr.status}`));
        } catch { reject(new Error(`HTTP ${xhr.status}: ${xhr.statusText}`)); }
      }
    };

    xhr.onerror = () => reject(new Error("Network error — check connection or file size"));
    xhr.ontimeout = () => reject(new Error("Request timed out — files may be too large"));

    if (onProgress) onProgress({ phase: "uploading", percent: 0, message: "Starting upload..." });
    xhr.send(formData);
  });
export const runTransformation = (transformId, model = null) =>
  api.post(`/tools/transformer/${transformId}/run`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const getTransformationStatus = (transformId) =>
  api.get(`/tools/transformer/${transformId}/status`).then((r) => r.data);
export const getGithubConfig = (projectId) =>
  api.get(`/github/config/${projectId}`).then((r) => r.data);
// iter-15.14 — Pause / Resume / Stop cooperative controls
export const getTransformationLogs = (transformId, { since = null, limit = 200 } = {}) =>
  api
    .get(`/tools/transformer/${transformId}/logs`, {
      params: { since: since || undefined, limit },
    })
    .then((r) => r.data);
export const pauseTransformation = (transformId) =>
  api.post(`/tools/transformer/${transformId}/pause`).then((r) => r.data);
export const resumeTransformation = (transformId) =>
  api.post(`/tools/transformer/${transformId}/resume`).then((r) => r.data);
export const stopTransformation = (transformId) =>
  api.post(`/tools/transformer/${transformId}/stop`).then((r) => r.data);
export const regenerateTransformationFile = (transformId, fileId, model = null) =>
  api.post(`/tools/transformer/${transformId}/files/${fileId}/regenerate`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const listTransformations = ({ projectId } = {}) =>
  api
    .get("/tools/transformer", { params: projectId ? { project_id: projectId } : {} })
    .then((r) => r.data?.items || []);
export const getTransformation = (transformId) =>
  api.get(`/tools/transformer/${transformId}`).then((r) => r.data);
export const getTransformationFiles = (transformId, fileType = "transformed") =>
  api.get(`/tools/transformer/${transformId}/files`, {
    params: { file_type: fileType },
  }).then((r) => r.data?.items || r.data || []);
export const getTransformationFile = (transformId, fileId) =>
  api.get(`/tools/transformer/${transformId}/files/${fileId}`).then((r) => r.data);
export const downloadTransformedCode = (transformId, scope = "code") =>
  `${API}/tools/transformer/${transformId}/download?scope=${encodeURIComponent(scope)}`;
export const downloadTransformedTests = (transformId) =>
  `${API}/tools/transformer/${transformId}/download?scope=tests`;
export const downloadTransformedBundle = (transformId) =>
  `${API}/tools/transformer/${transformId}/download?scope=all`;
export const deleteTransformation = (transformId) =>
  api.delete(`/tools/transformer/${transformId}`).then((r) => r.data);
export const pushTransformationToGithub = (transformId, formData) =>
  api.post(`/tools/transformer/${transformId}/push-github`, formData, {
    headers: { "Content-Type": "multipart/form-data" },
  }).then((r) => r.data);

// Stack Analyzer — Detect tech stack from source files
export const analyzeSourceStack = (formData) =>
  api.post("/tools/transformer/analyze-stack", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  }).then((r) => r.data);

// Multi-Agent Transformer Pipeline (iter-16)
export const runMultiAgentTransformation = (transformId, model = null) =>
  api.post(`/tools/transformer/${transformId}/run-multi-agent`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const confirmTransformationPlan = (transformId, model = null) =>
  api.post(`/tools/transformer/${transformId}/confirm-plan`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
// iter-15.28 — second human-in-the-loop gate: confirm the Planner's task
// list (waves + per-file actions) before Coder/Verifier/Tester run.
export const confirmTransformationTasks = (transformId, model = null) =>
  api.post(`/tools/transformer/${transformId}/tasks/confirm`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const getTransformationEnvelopes = (transformId) =>
  api.get(`/tools/transformer/${transformId}/envelopes`).then((r) => r.data);

// iter-15.41 — Mandatory traceability confirmation gate between Context
// Manager and Planner. `getTraceability` returns UI-ready hop chains
// (BE) or screen→API mappings (FE) plus a `mode` hint driving which
// panel to render. `confirmTraceability` stamps the doc and kicks off
// the Planner in the background — same effect as the legacy
// `/confirm-plan` endpoint, but semantically distinct so the audit log
// and the UI both record that traceability was explicitly reviewed.
export const getTransformationTraceability = (transformId) =>
  api.get(`/tools/transformer/${transformId}/traceability`).then((r) => r.data);
export const confirmTransformationTraceability = (transformId, model = null) =>
  api.post(`/tools/transformer/${transformId}/confirm-traceability`, null, {
    params: model ? { model } : {},
  }).then((r) => r.data);
export const getTransformationTasks = (transformId) =>
  api.get(`/tools/transformer/${transformId}/tasks`).then((r) => r.data);
export const getAgentTimeline = (transformId) =>
  api.get(`/tools/transformer/${transformId}/agent-timeline`).then((r) => r.data);
export const getCompilationResult = (transformId) =>
  api.get(`/tools/transformer/${transformId}/compilation`).then((r) => r.data);
export const runCompilationAnalysis = (transformId, model = null, opts = {}) =>
  api.post(`/tools/transformer/${transformId}/compile`, null, {
    params: {
      ...(model ? { model } : {}),
      // iter-15.58 — Compile-fix loop is ON by default. Pass
      // `{ autoFix: false }` from the caller for a single-shot compile.
      auto_fix: opts.autoFix === false ? false : true,
      ...(opts.maxIterations ? { max_iterations: opts.maxIterations } : {}),
    },
  }).then((r) => r.data);

// iter-15.19 — Per-transformation Agent Pipeline config panel: click an
// agent (SA/CM/Planner/Coder/Verifier/Tester) to review/edit its prompt +
// model for just this transformation, save, then rerun the pipeline.
export const listTransformerAgentConfigs = (transformId) =>
  api.get(`/tools/transformer/${transformId}/agents`).then((r) => r.data);
export const getTransformerAgentConfig = (transformId, agent) =>
  api.get(`/tools/transformer/${transformId}/agents/${agent}`).then((r) => r.data);
export const saveTransformerAgentConfig = (transformId, agent, payload) =>
  api.put(`/tools/transformer/${transformId}/agents/${agent}`, payload).then((r) => r.data);
export const rerunTransformerPipeline = (transformId) =>
  api.post(`/tools/transformer/${transformId}/rerun`).then((r) => r.data);

// iter-15.38 — chat assistant embedded in the Planner/Coder/Tester panels:
// "remove all test files from wave 2", "find files touching Order",
// "move the DAO classes to wave 3", "regenerate the failed controllers".
// Every turn is applied + persisted immediately server-side; the response
// carries the refreshed task list so the FE can just replace its state.
export const sendAgentPlanChat = (transformId, panel, message, model = null) =>
  api.post(`/tools/transformer/${transformId}/agent-chat`, { panel, message, model }).then((r) => r.data);

// iter-17 — Multi-Agent CodeGen Pipeline
export const startCodegenMultiAgent = (pid, model = null) =>
  api.post(`/codegen/${pid}/multi-agent/start`, null, { params: model ? { model } : {} });
export const getCodegenMultiAgentState = (pid) =>
  api.get(`/codegen/${pid}/multi-agent/state`);
export const listCodegenMultiAgentEnvelopes = (pid) =>
  api.get(`/codegen/${pid}/multi-agent/envelopes`);
export const updateCodegenMultiAgentEnvelope = (pid, envId, patch) =>
  api.patch(`/codegen/${pid}/multi-agent/envelopes/${envId}`, patch);
export const confirmCodegenMultiAgentEnvelopes = (pid, model = null) =>
  api.post(`/codegen/${pid}/multi-agent/envelopes/confirm`, null, { params: model ? { model } : {} });
export const setCodegenMultiAgentBuildSystem = (pid, be, fe) =>
  api.post(`/codegen/${pid}/multi-agent/build-system`, { be, fe });
export const listCodegenMultiAgentTasks = (pid, wave = null) =>
  api.get(`/codegen/${pid}/multi-agent/tasks`, { params: wave != null ? { wave } : {} });
export const updateCodegenMultiAgentTask = (pid, taskId, patch) =>
  api.patch(`/codegen/${pid}/multi-agent/tasks/${taskId}`, patch);
export const confirmCodegenMultiAgentTasks = (pid, model = null) =>
  api.post(`/codegen/${pid}/multi-agent/tasks/confirm`, null, { params: model ? { model } : {} });
export const listCodegenMultiAgentRuns = (pid, opts = {}) =>
  api.get(`/codegen/${pid}/multi-agent/agent-runs`, { params: opts });
export const rerunCodegenMultiAgent = (pid, model = null, opts = {}) => {
  // iter-17.14 — default wipe_files=true so a fresh rerun ALSO deletes
  // stale codegen_files (mixed .py + .java from previous runs). Pass
  // `{ wipeFiles: false }` to preserve existing files (matches
  // pre-17.14 behaviour for incremental patching).
  const params = { ...(model ? { model } : {}) };
  params.wipe_files = opts.wipeFiles === false ? "false" : "true";
  return api.post(`/codegen/${pid}/multi-agent/rerun`, null, { params });
};
// iter-17.4 — Recover from a Planner silent-failure without wiping envelopes.
export const retryCodegenMultiAgentPlanner = (pid, model = null) =>
  api.post(`/codegen/${pid}/multi-agent/retry-planner`, null, { params: model ? { model } : {} });
export const cancelCodegenMultiAgent = (pid) =>
  api.post(`/codegen/${pid}/multi-agent/cancel`);
export const getCodegenMultiAgentTraceability = (pid) =>
  api.get(`/codegen/${pid}/multi-agent/traceability`);

export default api;
