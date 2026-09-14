// iter-17 — Multi-Agent CodeGen Pipeline UI
// -----------------------------------------------------------------------
// Self-contained panel that drives the /api/codegen/{pid}/multi-agent/*
// contract. Rendered inside the existing Legacy Modernization CodeGen
// page when the mode toggle is set to "Multi-Agent Pipeline". Preserves
// the CodeGen page's visual language (EY yellow #FFE600 accent, slate
// text #2E2E38, off-white #F6F6FA background, hairline #E6E6E6 borders,
// rounded-sm, compact 10–12px type scale). Does NOT copy the Transformer
// page look.
//
// Layout (top → bottom, single scroll):
//   A. State header strip     — status badge, wave, BE/FE build sys, counts
//   B. Action bar             — Start / Confirm / Cancel / Rerun contextual
//   C. Envelopes section      — compact table + inline edit
//   D. Tasks section          — grouped by wave, inline edit
//   E. Traceability panel     — BR coverage %, missing list, per-envelope
//   F. Agent Runs timeline    — filter + Load More + expandable
//   G. Generated Code (Live)  — iter-17.10 file tree + Monaco editor,
//                               polls listCodegenFiles every 3s while the
//                               pipeline writes files so the operator sees
//                               each Coder-emitted file the moment it lands
//                               instead of waiting for the whole run.
//
// Polling cadence: state → 2s, agent-runs → 3s, only while status is a
// _pending or executing state; halts on terminal (completed/failed/idle).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2,
  Play,
  Check,
  X,
  RotateCcw,
  ChevronDown,
  ChevronRight as ChevronRightIcon,
  Sparkles,
  Wrench,
  Layers,
  ClipboardCopy,
  Target,
  AlertTriangle,
  Bot,
  Pencil,
  FileText,
  Wand2,
  FileCode,
  Folder,
  Maximize2,
  Minimize2,
  FolderOpen,
  Send,
  Code2,
} from "lucide-react";
import { toast } from "sonner";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import Editor from "@monaco-editor/react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import {
  startCodegenMultiAgent,
  getCodegenMultiAgentState,
  listCodegenMultiAgentEnvelopes,
  updateCodegenMultiAgentEnvelope,
  confirmCodegenMultiAgentEnvelopes,
  setCodegenMultiAgentBuildSystem,
  listCodegenMultiAgentTasks,
  updateCodegenMultiAgentTask,
  confirmCodegenMultiAgentTasks,
  listCodegenMultiAgentRuns,
  rerunCodegenMultiAgent,
  retryCodegenMultiAgentPlanner,
  cancelCodegenMultiAgent,
  getCodegenMultiAgentTraceability,
  listCodegenFiles,
  getCodegenFile,
  sendCodegenChat,
  applyCodegenFileChange,
} from "@/lib/api";

// -----------------------------------------------------------------------
// Constants — palette & lookup tables
// -----------------------------------------------------------------------
const TERMINAL_STATUSES = new Set(["idle", "completed", "failed"]);
const PENDING_STATUSES = new Set([
  "envelopes_pending",
  "envelopes_confirmed",
  "tasks_pending",
  "tasks_confirmed",
  "executing",
  "traceability_gate",
  "paused",
]);

const STATUS_BADGE = {
  idle:                { label: "Idle",              cls: "bg-slate-100 text-slate-700 border-slate-300" },
  envelopes_pending:   { label: "Envelopes Pending", cls: "bg-amber-100 text-amber-800 border-amber-300" },
  envelopes_confirmed: { label: "Envelopes ✓ Planning", cls: "bg-amber-100 text-amber-800 border-amber-300" },
  tasks_pending:       { label: "Tasks Pending",     cls: "bg-amber-100 text-amber-800 border-amber-300" },
  tasks_confirmed:     { label: "Tasks ✓ Queuing",   cls: "bg-amber-100 text-amber-800 border-amber-300" },
  executing:           { label: "Executing",         cls: "bg-sky-100 text-sky-800 border-sky-300" },
  paused:              { label: "Paused",            cls: "bg-yellow-100 text-yellow-900 border-yellow-300" },
  traceability_gate:   { label: "Traceability Gate", cls: "bg-purple-100 text-purple-800 border-purple-300" },
  completed:           { label: "Completed",         cls: "bg-emerald-100 text-emerald-800 border-emerald-300" },
  failed:              { label: "Failed",            cls: "bg-red-100 text-red-800 border-red-300" },
};

const SIDE_BADGE = {
  backend:  { label: "BE",     cls: "bg-slate-700 text-white" },
  frontend: { label: "FE",     cls: "bg-indigo-600 text-white" },
  shared:   { label: "Shared", cls: "bg-amber-600 text-white" },
};

const RISK_BADGE = {
  low:      "bg-emerald-50 text-emerald-700 border-emerald-200",
  medium:   "bg-amber-50 text-amber-800 border-amber-200",
  high:     "bg-red-50 text-red-700 border-red-200",
  critical: "bg-red-100 text-red-800 border-red-300",
};

const TASK_STATUS_BADGE = {
  PENDING:     "bg-slate-100 text-slate-700",
  APPROVED:    "bg-slate-200 text-slate-800",
  IN_PROGRESS: "bg-sky-100 text-sky-800 animate-pulse",
  CODED:       "bg-cyan-100 text-cyan-800",
  VERIFIED:    "bg-teal-100 text-teal-800",
  TESTED:      "bg-emerald-100 text-emerald-800",
  DONE:        "bg-green-100 text-green-800",
  BLOCKED:     "bg-red-100 text-red-800",
};

const ASSIGNED_BADGE = {
  coder_be: { label: "coder_be", cls: "bg-slate-700 text-white" },
  coder_fe: { label: "coder_fe", cls: "bg-indigo-600 text-white" },
};

const AGENT_OPTIONS = [
  "", "super_agent", "context_manager", "planner",
  "coder_be", "coder_fe", "verifier", "reviewer", "tester",
  "build_tool_selector", "traceability_gate", "finalizer",
];

const AGENT_ICON = {
  super_agent:         Sparkles,
  context_manager:     Layers,
  planner:             ClipboardCopy,
  coder_be:            Bot,
  coder_fe:            Bot,
  verifier:            Check,
  reviewer:            FileText,
  tester:              Target,
  build_tool_selector: Wrench,
  traceability_gate:   Target,
  finalizer:           Check,
};

// -----------------------------------------------------------------------
// Small helpers
// -----------------------------------------------------------------------
const cls = (...xs) => xs.filter(Boolean).join(" ");
const short = (s, n = 60) => (s && s.length > n ? s.slice(0, n) + "…" : s || "");
const fmtMs = (ms) => {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 1000)}s`;
};
const fmtTs = (ts) => {
  if (!ts) return "—";
  try {
    const d = typeof ts === "number" ? new Date(ts) : new Date(String(ts));
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString();
  } catch (_) { return String(ts); }
};
const errMsg = (e) =>
  e?.response?.data?.detail || e?.response?.data?.message || e?.message || String(e);

// -----------------------------------------------------------------------
// useCodegenMultiAgentState — one polling hook that owns state + envelopes
// + tasks + agent-runs + traceability. Halts polling on terminal statuses.
// -----------------------------------------------------------------------
function useCodegenMultiAgentState(projectId) {
  const [state, setState] = useState(null);
  const [envelopes, setEnvelopes] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [runs, setRuns] = useState([]);
  const [runsFilter, setRunsFilter] = useState("");
  const [runsLimit, setRunsLimit] = useState(20);
  const [runsHasMore, setRunsHasMore] = useState(false);
  const [traceability, setTraceability] = useState(null);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);
  const stateTimer = useRef(null);
  const runsTimer = useRef(null);

  const status = state?.status || "idle";
  const isTerminal = TERMINAL_STATUSES.has(status);
  const isPending = PENDING_STATUSES.has(status);

  const fetchState = useCallback(async () => {
    if (!projectId) return;
    try {
      const [sRes, eRes, tRes] = await Promise.all([
        getCodegenMultiAgentState(projectId),
        listCodegenMultiAgentEnvelopes(projectId),
        listCodegenMultiAgentTasks(projectId),
      ]);
      if (!mountedRef.current) return;
      setState(sRes.data || null);
      setEnvelopes(eRes.data?.envelopes || []);
      setTasks(tRes.data?.tasks || []);
    } catch (e) {
      if (mountedRef.current) {
        // Silent — polling errors shouldn't nag the user every 2s.
        // Surface only on manual actions.
        // eslint-disable-next-line no-console
        console.warn("codegen multi-agent state poll failed:", errMsg(e));
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [projectId]);

  const fetchRuns = useCallback(async (opts = {}) => {
    if (!projectId) return;
    const limit = opts.limit ?? runsLimit;
    const agent = opts.agent ?? runsFilter;
    try {
      const res = await listCodegenMultiAgentRuns(projectId, {
        limit,
        skip: 0,
        ...(agent ? { agent } : {}),
      });
      if (!mountedRef.current) return;
      const list = res.data?.runs || [];
      setRuns(list);
      setRunsHasMore(list.length >= limit);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("codegen multi-agent runs poll failed:", errMsg(e));
    }
  }, [projectId, runsLimit, runsFilter]);

  const fetchTraceability = useCallback(async () => {
    if (!projectId) return;
    try {
      const res = await getCodegenMultiAgentTraceability(projectId);
      if (mountedRef.current) setTraceability(res.data || null);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("codegen traceability fetch failed:", errMsg(e));
    }
  }, [projectId]);

  // Initial load + reset on projectId change.
  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);
    fetchState();
    fetchRuns({});
    return () => { mountedRef.current = false; };
  }, [projectId, fetchState, fetchRuns]);

  // State + envelopes + tasks polling @ 2s while non-terminal.
  useEffect(() => {
    if (stateTimer.current) { clearInterval(stateTimer.current); stateTimer.current = null; }
    if (!projectId) return;
    if (isTerminal) return;
    stateTimer.current = setInterval(fetchState, 2000);
    return () => { if (stateTimer.current) clearInterval(stateTimer.current); };
  }, [projectId, isTerminal, fetchState]);

  // Agent runs polling @ 3s while executing.
  useEffect(() => {
    if (runsTimer.current) { clearInterval(runsTimer.current); runsTimer.current = null; }
    if (!projectId) return;
    if (status !== "executing" && !isPending) return;
    runsTimer.current = setInterval(fetchRuns, 3000);
    return () => { if (runsTimer.current) clearInterval(runsTimer.current); };
  }, [projectId, status, isPending, fetchRuns]);

  // Auto-refresh traceability when we enter the gate or complete.
  useEffect(() => {
    if (status === "traceability_gate" || status === "completed") {
      fetchTraceability();
    }
  }, [status, fetchTraceability]);

  return {
    state,
    envelopes,
    tasks,
    runs,
    runsFilter,
    setRunsFilter,
    runsLimit,
    setRunsLimit,
    runsHasMore,
    traceability,
    loading,
    isTerminal,
    isPending,
    status,
    refetchState: fetchState,
    refetchRuns: fetchRuns,
    refetchTraceability: fetchTraceability,
  };
}

// -----------------------------------------------------------------------
// iter-17.10 — useLiveCodegenFiles: polls listCodegenFiles while the
// pipeline is writing files so the operator sees each Coder-emitted
// artefact the moment it lands, without leaving this panel. Falls
// silent (halts polling) on terminal statuses.
// -----------------------------------------------------------------------
function useLiveCodegenFiles(projectId, { isTerminal }) {
  const [services, setServices] = useState([]);   // [{ name, files: [...] }]
  const [totalFiles, setTotalFiles] = useState(0);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const mounted = useRef(true);
  const timer = useRef(null);

  const fetchFiles = useCallback(async () => {
    if (!projectId) return;
    try {
      const data = await listCodegenFiles(projectId);
      if (!mounted.current) return;
      setServices(Array.isArray(data?.services) ? data.services : []);
      setTotalFiles(Number(data?.total_files || 0));
    } catch (_) {
      // Silent — the pipeline may still be pre-Coder, in which case the
      // codegen_files collection is legitimately empty. Don't nag.
    } finally {
      if (mounted.current) setLoadedOnce(true);
    }
  }, [projectId]);

  useEffect(() => {
    mounted.current = true;
    setLoadedOnce(false);
    fetchFiles();
    return () => { mounted.current = false; };
  }, [projectId, fetchFiles]);

  useEffect(() => {
    if (timer.current) { clearInterval(timer.current); timer.current = null; }
    if (!projectId) return;
    if (isTerminal) return;
    timer.current = setInterval(fetchFiles, 3000);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [projectId, isTerminal, fetchFiles]);

  return { services, totalFiles, loadedOnce, refetch: fetchFiles };
}

// -----------------------------------------------------------------------
// iter-17.10 — Language hint for Monaco, mirrors CodeGen.jsx::pathLang
// but kept local so this panel stays self-contained.
// -----------------------------------------------------------------------
function guessLanguage(path = "") {
  const p = String(path).toLowerCase();
  if (p.endsWith(".py")) return "python";
  if (p.endsWith(".java")) return "java";
  if (p.endsWith(".kt") || p.endsWith(".kts")) return "kotlin";
  if (p.endsWith(".js") || p.endsWith(".jsx") || p.endsWith(".mjs") || p.endsWith(".cjs")) return "javascript";
  if (p.endsWith(".ts") || p.endsWith(".tsx")) return "typescript";
  if (p.endsWith(".go")) return "go";
  if (p.endsWith(".rs")) return "rust";
  if (p.endsWith(".cs")) return "csharp";
  if (p.endsWith(".sql")) return "sql";
  if (p.endsWith(".yaml") || p.endsWith(".yml")) return "yaml";
  if (p.endsWith(".json")) return "json";
  if (p.endsWith(".xml") || p.endsWith(".pom")) return "xml";
  if (p.endsWith(".md")) return "markdown";
  if (p.endsWith(".html") || p.endsWith(".htm")) return "html";
  if (p.endsWith(".css") || p.endsWith(".scss")) return "css";
  if (p.endsWith(".sh") || p.endsWith(".bash")) return "shell";
  if (p.endsWith("dockerfile") || p.endsWith(".dockerfile")) return "dockerfile";
  return "plaintext";
}

// -----------------------------------------------------------------------
// Typed-confirmation modal — mirrors freeze-gate UX. User must type
// exactly "RERUN" to enable the destructive button.
// -----------------------------------------------------------------------
function TypedConfirmModal({ open, onClose, onConfirm, word = "RERUN", title, warning, confirmLabel }) {
  const [typed, setTyped] = useState("");
  useEffect(() => { if (!open) setTyped(""); }, [open]);
  if (!open) return null;
  const enabled = typed === word;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      data-testid="codegen-ma-rerun-modal"
      onClick={onClose}
    >
      <div className="bg-white max-w-md w-full rounded-sm border-2 border-orange-500 p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="font-display font-bold text-lg text-orange-700 flex items-center gap-2">
          <RotateCcw className="w-4 h-4" /> {title}
        </h3>
        <p className="text-xs text-[#2E2E38] mt-2 leading-snug">{warning}</p>
        <p className="text-xs text-[#747480] mt-3">
          Type <code className="bg-[#F6F6FA] px-1">{word}</code> to confirm.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          data-testid="codegen-ma-rerun-input"
          className="mt-1 w-full border border-[#E6E6E6] focus:border-orange-500 outline-none px-2 py-1.5 text-sm rounded-sm"
        />
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA]"
          >
            Cancel
          </button>
          <button
            disabled={!enabled}
            onClick={onConfirm}
            data-testid="codegen-ma-rerun-confirm"
            className={cls(
              "text-xs px-3 py-1.5 rounded-sm font-bold text-white",
              enabled ? "bg-orange-600 hover:bg-orange-700" : "bg-orange-300 cursor-not-allowed",
            )}
          >
            {confirmLabel || "Rerun From Scratch"}
          </button>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------
// Envelope row (expandable)
// -----------------------------------------------------------------------
function EnvelopeRow({ envelope, projectId, onSaved, isOpen, onToggle }) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ac, setAc] = useState(envelope.acceptance_criteria || "");
  const [statusVal, setStatusVal] = useState(envelope.status || "draft");
  const open = !!isOpen;

  useEffect(() => {
    setAc(envelope.acceptance_criteria || "");
    setStatusVal(envelope.status || "draft");
  }, [envelope.acceptance_criteria, envelope.status]);

  const side = SIDE_BADGE[envelope.side] || { label: envelope.side || "?", cls: "bg-slate-300 text-slate-800" };
  const riskCls = RISK_BADGE[envelope.risk_level] || "bg-slate-50 text-slate-700 border-slate-200";
  const tables = Array.isArray(envelope.db_tables) ? envelope.db_tables : [];
  const brIds = Array.isArray(envelope.br_ids) ? envelope.br_ids : [];

  const save = async () => {
    setSaving(true);
    try {
      await updateCodegenMultiAgentEnvelope(projectId, envelope.envelope_id, {
        acceptance_criteria: ac,
        status: statusVal,
      });
      toast.success(`Envelope ${envelope.envelope_id} saved`);
      setEditing(false);
      onSaved?.();
    } catch (e) {
      toast.error("Save failed: " + errMsg(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <tr
        data-testid={`codegen-ma-envelope-row-${envelope.envelope_id}`}
        className="border-b border-[#F6F6FA] hover:bg-[#FAFAFC] cursor-pointer"
        onClick={() => onToggle?.(envelope.envelope_id)}
      >
        <td className="px-2 py-1.5 text-[11px] font-mono text-[#2E2E38]">
          <span className="inline-flex items-center gap-1">
            {open ? <ChevronDown className="w-3 h-3 text-[#747480]" /> : <ChevronRightIcon className="w-3 h-3 text-[#747480]" />}
            {envelope.envelope_id}
          </span>
        </td>
        <td className="px-2 py-1.5 text-[11px] font-mono text-[#2E2E38]">
          <span className="inline-block w-12 text-[9px] font-bold uppercase">{envelope.endpoint_method || "—"}</span>
          <span className="truncate">{envelope.endpoint_path || "—"}</span>
        </td>
        <td className="px-2 py-1.5 text-[10px]">
          <span className={cls("uppercase font-bold px-1.5 py-0.5 rounded-sm", side.cls)}>{side.label}</span>
        </td>
        <td className="px-2 py-1.5 text-[10px]">
          <span className={cls("uppercase font-semibold px-1.5 py-0.5 rounded-sm border", riskCls)}>
            {envelope.risk_level || "—"}
          </span>
        </td>
        <td className="px-2 py-1.5 text-[10px] text-[#747480]">{envelope.layer || "—"}</td>
        <td className="px-2 py-1.5 text-[10px] text-[#747480]">{tables.length}</td>
        <td className="px-2 py-1.5 text-[10px] text-[#747480]">{envelope.status || "draft"}</td>
        <td className="px-2 py-1.5 text-[10px] text-right">
          <button
            data-testid={`codegen-ma-envelope-edit-${envelope.envelope_id}`}
            // `setOpen` does not exist in this component -- EnvelopeRow's
            // expansion is parent-controlled via isOpen/onToggle (the local
            // `open` at line 453 is derived, not state). Clicking Edit threw
            // ReferenceError: setOpen is not defined. Expand through the
            // parent's toggle, and only when the row is not already open so
            // the toggle cannot collapse it.
            onClick={(e) => {
              e.stopPropagation();
              if (!open) onToggle?.(envelope.envelope_id);
              setEditing(true);
            }}
            className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA]"
          >
            <Pencil className="w-3 h-3" /> Edit
          </button>
        </td>
      </tr>
      {open && (
        <tr className="bg-[#FAFAFC]">
          <td colSpan={8} className="px-3 py-2 border-b border-[#E6E6E6]">
            <div className="grid grid-cols-2 gap-3 text-[10px]">
              <div>
                <div className="text-[9px] uppercase text-[#747480] font-semibold">Controller</div>
                <div className="font-mono text-[#2E2E38]">{envelope.controller_class || "—"}</div>
                <div className="font-mono text-[#747480] truncate">{envelope.controller_file || ""}</div>
              </div>
              <div>
                <div className="text-[9px] uppercase text-[#747480] font-semibold">Service</div>
                <div className="font-mono text-[#2E2E38]">{envelope.service_class || "—"} <span className="text-[#747480]">·{envelope.service_method || "—"}</span></div>
                <div className="font-mono text-[#747480] truncate">{envelope.service_file || ""}</div>
              </div>
              <div>
                <div className="text-[9px] uppercase text-[#747480] font-semibold">Repository</div>
                <div className="font-mono text-[#2E2E38]">{envelope.repository_class || "—"}</div>
                <div className="font-mono text-[#747480] truncate">{envelope.repository_file || ""}</div>
              </div>
              <div>
                <div className="text-[9px] uppercase text-[#747480] font-semibold">DB Tables ({tables.length})</div>
                <div className="font-mono text-[#2E2E38] break-words">{tables.join(", ") || "—"}</div>
              </div>
              <div className="col-span-2">
                <div className="text-[9px] uppercase text-[#747480] font-semibold">BR IDs ({brIds.length})</div>
                <div className="font-mono text-[#2E2E38] break-words">{brIds.join(", ") || "—"}</div>
              </div>
              <div className="col-span-2">
                <div className="text-[9px] uppercase text-[#747480] font-semibold flex items-center justify-between">
                  <span>Acceptance Criteria</span>
                  {!editing && (
                    <button
                      onClick={() => setEditing(true)}
                      className="text-[10px] px-1.5 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-white"
                    >
                      <Pencil className="w-3 h-3 inline" /> Edit
                    </button>
                  )}
                </div>
                {editing ? (
                  <>
                    <Textarea
                      value={ac}
                      onChange={(e) => setAc(e.target.value)}
                      rows={4}
                      className="mt-1 text-[11px] font-mono"
                    />
                    <div className="mt-2 flex items-center gap-2">
                      <label className="text-[10px] text-[#747480]">Status:</label>
                      <select
                        value={statusVal}
                        onChange={(e) => setStatusVal(e.target.value)}
                        className="text-[10px] border border-[#E6E6E6] rounded-sm px-1 py-0.5 bg-white"
                      >
                        <option value="draft">draft</option>
                        <option value="approved">approved</option>
                      </select>
                      <div className="flex-1" />
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => { setEditing(false); setAc(envelope.acceptance_criteria || ""); setStatusVal(envelope.status || "draft"); }}
                        className="h-7 text-[10px]"
                      >
                        Cancel
                      </Button>
                      <Button
                        size="sm"
                        onClick={save}
                        disabled={saving}
                        className="h-7 text-[10px] bg-[#2E2E38] text-white hover:bg-[#1E1E28]"
                      >
                        {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} Save
                      </Button>
                    </div>
                  </>
                ) : (
                  <div className="mt-1 text-[11px] font-mono whitespace-pre-wrap text-[#2E2E38] bg-white border border-[#E6E6E6] rounded-sm p-2 min-h-[3rem]">
                    {envelope.acceptance_criteria || <span className="italic text-[#B0B0B8]">— no acceptance criteria —</span>}
                  </div>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// -----------------------------------------------------------------------
// Task edit dialog
// -----------------------------------------------------------------------
function TaskEditDialog({ open, onClose, task, projectId, onSaved }) {
  const [targetPath, setTargetPath] = useState(task?.target_path || "");
  const [assignedTo, setAssignedTo] = useState(task?.assigned_to || "coder_be");
  const [description, setDescription] = useState(task?.description || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setTargetPath(task?.target_path || "");
    setAssignedTo(task?.assigned_to || "coder_be");
    setDescription(task?.description || "");
  }, [task]);

  if (!task) return null;

  const save = async () => {
    setSaving(true);
    try {
      const res = await updateCodegenMultiAgentTask(projectId, task.task_id, {
        target_path: targetPath,
        assigned_to: assignedTo,
        description,
      });
      const serverAssigned = res.data?.assigned_to;
      if (serverAssigned && serverAssigned !== assignedTo) {
        toast.warning(
          `Router re-routed task to ${serverAssigned} (BE/FE heuristic overrode your pick).`,
        );
      } else {
        toast.success(`Task ${task.task_id} saved`);
      }
      onSaved?.();
      onClose?.();
    } catch (e) {
      toast.error("Save failed: " + errMsg(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose?.()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-sm font-display">
            Edit task <span className="font-mono text-[#747480]">{task.task_id}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-xs">
          <div>
            <label className="text-[10px] uppercase text-[#747480] font-semibold">Target Path</label>
            <Input
              value={targetPath}
              onChange={(e) => setTargetPath(e.target.value)}
              className="mt-1 text-xs font-mono"
              placeholder="src/backend/app/services/..."
            />
            <p className="text-[10px] text-[#747480] mt-1">
              Server will re-apply BE/FE heuristic from this path — a toast fires if it overrides your pick.
            </p>
          </div>
          <div>
            <label className="text-[10px] uppercase text-[#747480] font-semibold">Assigned To</label>
            <select
              value={assignedTo}
              onChange={(e) => setAssignedTo(e.target.value)}
              className="mt-1 w-full text-xs border border-[#E6E6E6] rounded-sm px-2 py-1.5 bg-white"
            >
              <option value="coder_be">coder_be</option>
              <option value="coder_fe">coder_fe</option>
              <option value="verifier">verifier</option>
              <option value="reviewer">reviewer</option>
              <option value="tester">tester</option>
            </select>
          </div>
          <div>
            <label className="text-[10px] uppercase text-[#747480] font-semibold">Description</label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={5}
              className="mt-1 text-xs"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button
            size="sm"
            onClick={save}
            disabled={saving}
            className="bg-[#2E2E38] text-white hover:bg-[#1E1E28]"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// -----------------------------------------------------------------------
// Build-system override popover — appears when tasks_pending.
// -----------------------------------------------------------------------
function BuildSystemPopover({ projectId, state, onSaved }) {
  const [open, setOpen] = useState(false);
  const [be, setBe] = useState(state?.build_system_be || "");
  const [fe, setFe] = useState(state?.build_system_fe || "");
  const [saving, setSaving] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    setBe(state?.build_system_be || "");
    setFe(state?.build_system_fe || "");
  }, [state?.build_system_be, state?.build_system_fe]);

  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (ref.current && ref.current.contains(e.target)) return;
      setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  const save = async () => {
    if (!be.trim() && !fe.trim()) {
      toast.error("At least one of BE / FE required.");
      return;
    }
    setSaving(true);
    try {
      await setCodegenMultiAgentBuildSystem(projectId, be.trim(), fe.trim());
      toast.success("Build systems updated");
      setOpen(false);
      onSaved?.();
    } catch (e) {
      toast.error("Save failed: " + errMsg(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="relative shrink-0" ref={ref}>
      <button
        data-testid="codegen-ma-btn-buildsys"
        onClick={() => setOpen((v) => !v)}
        className="h-7 text-[11px] px-2 border border-[#E6E6E6] rounded-sm bg-white text-[#2E2E38] hover:bg-[#F6F6FA] flex items-center gap-1"
        title="Override BE / FE build systems"
      >
        <Wrench className="w-3 h-3" /> Override Build Systems
        <ChevronDown className="w-3 h-3" />
      </button>
      {open && (
        <div className="absolute right-0 top-8 z-30 w-64 bg-white border border-[#E6E6E6] rounded-sm shadow-lg p-3 text-[11px]">
          <div className="text-[10px] uppercase text-[#747480] font-semibold mb-1">Backend</div>
          <Input
            value={be}
            onChange={(e) => setBe(e.target.value)}
            placeholder="e.g. fastapi_poetry"
            className="h-7 text-xs"
          />
          <div className="text-[10px] uppercase text-[#747480] font-semibold mt-2 mb-1">Frontend</div>
          <Input
            value={fe}
            onChange={(e) => setFe(e.target.value)}
            placeholder="e.g. cra_yarn"
            className="h-7 text-xs"
          />
          <div className="mt-3 flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setOpen(false)} className="h-7 text-[10px]">Cancel</Button>
            <Button
              size="sm"
              onClick={save}
              disabled={saving}
              className="h-7 text-[10px] bg-[#2E2E38] text-white hover:bg-[#1E1E28]"
            >
              {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} Save
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Agent-run entry (timeline row)
// -----------------------------------------------------------------------
function AgentRunRow({ run }) {
  const [open, setOpen] = useState(false);
  const Icon = AGENT_ICON[run.agent] || Bot;
  const statusPill = run.status === "completed"
    ? "bg-emerald-100 text-emerald-800"
    : run.status === "failed"
      ? "bg-red-100 text-red-800"
      : "bg-sky-100 text-sky-800 animate-pulse";
  const runId = run.run_id || run.id || `${run.agent}-${run.created_at}`;

  return (
    <div
      data-testid={`codegen-ma-run-${runId}`}
      className="border-b border-[#F6F6FA] py-1.5 px-2 hover:bg-[#FAFAFC]"
    >
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 text-[11px] text-left"
      >
        {open ? <ChevronDown className="w-3 h-3 text-[#747480]" /> : <ChevronRightIcon className="w-3 h-3 text-[#747480]" />}
        <Icon className="w-3 h-3 text-[#2E2E38]" />
        <span className="font-semibold text-[#2E2E38]">{run.agent}</span>
        <span className="text-[10px] text-[#747480]">{run.phase || "—"}</span>
        <span className={cls("text-[9px] uppercase font-bold px-1.5 py-0.5 rounded-sm", statusPill)}>
          {run.status}
        </span>
        {run.task_id && (
          <span className="text-[10px] font-mono text-[#747480]">task {run.task_id}</span>
        )}
        {run.envelope_id && (
          <span className="text-[10px] font-mono text-[#747480]">env {run.envelope_id}</span>
        )}
        {run.score != null && (
          <span className="text-[10px] text-emerald-700 font-semibold">score {Number(run.score).toFixed(2)}</span>
        )}
        <span className="ml-auto text-[10px] text-[#747480]">{fmtMs(run.duration_ms)}</span>
        <span className="text-[10px] text-[#747480]">{fmtTs(run.created_at)}</span>
      </button>
      {open && (
        <div className="mt-1 ml-6 grid grid-cols-2 gap-2 text-[10px]">
          <div>
            <div className="text-[9px] uppercase text-[#747480] font-semibold">Input</div>
            <pre className="mt-0.5 whitespace-pre-wrap font-mono text-[#2E2E38] bg-white border border-[#E6E6E6] rounded-sm p-1.5 max-h-40 overflow-y-auto">
              {run.input_summary || "—"}
            </pre>
          </div>
          <div>
            <div className="text-[9px] uppercase text-[#747480] font-semibold">Output</div>
            <pre className="mt-0.5 whitespace-pre-wrap font-mono text-[#2E2E38] bg-white border border-[#E6E6E6] rounded-sm p-1.5 max-h-40 overflow-y-auto">
              {run.output_summary || "—"}
            </pre>
          </div>
          {run.error && (
            <div className="col-span-2">
              <div className="text-[9px] uppercase text-red-700 font-semibold">Error</div>
              <pre className="mt-0.5 whitespace-pre-wrap font-mono text-red-800 bg-red-50 border border-red-200 rounded-sm p-1.5 max-h-40 overflow-y-auto">
                {run.error}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Main component
// -----------------------------------------------------------------------
export default function CodeGenMultiAgentPanel({ projectId }) {
  const {
    state,
    envelopes,
    tasks,
    runs,
    runsFilter,
    setRunsFilter,
    runsLimit,
    setRunsLimit,
    runsHasMore,
    traceability,
    loading,
    status,
    isTerminal,
    refetchState,
    refetchRuns,
    refetchTraceability,
  } = useCodegenMultiAgentState(projectId);

  // iter-17.10 — Live generated-code viewer state
  const { services: liveServices, totalFiles: liveTotalFiles, loadedOnce: liveLoaded, refetch: refetchFiles } =
    useLiveCodegenFiles(projectId, { isTerminal });
  const [filesCollapsed, setFilesCollapsed] = useState(false);
  // iter-17.11 — Maximize the Generated Code section to full viewport so
  // long paths / wide files don't wrap in the constrained inline layout.
  // Esc key restores. Locks page scroll while maximized.
  const [filesMaximized, setFilesMaximized] = useState(false);
  useEffect(() => {
    if (!filesMaximized) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setFilesMaximized(false); };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [filesMaximized]);
  const [liveServiceFilter, setLiveServiceFilter] = useState("");   // "" = all
  const [selectedFileId, setSelectedFileId] = useState(null);
  const [selectedFileMeta, setSelectedFileMeta] = useState(null);   // { path, service_name, version, language }
  const [selectedFileContent, setSelectedFileContent] = useState("");
  const [selectedFileLoading, setSelectedFileLoading] = useState(false);
  const userPickedFileRef = useRef(false);
  const seenFileIdsRef = useRef(new Set());

  const [starting, setStarting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [rerunOpen, setRerunOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [editingTask, setEditingTask] = useState(null);
  const [collapsedWaves, setCollapsedWaves] = useState({});
  // iter-17.5 — Accordion state
  const [openEnvelopeId, setOpenEnvelopeId] = useState(null);   // one envelope detail expanded at a time
  const [envelopesCollapsed, setEnvelopesCollapsed] = useState(false);
  const [tasksCollapsed, setTasksCollapsed] = useState(false);
  const [runsCollapsed, setRunsCollapsed] = useState(false);
  const toggleEnvelopeOpen = useCallback(
    (id) => setOpenEnvelopeId((cur) => (cur === id ? null : id)),
    [],
  );
  const traceabilityRef = useRef(null);

  // iter-17.1 — Elapsed-time tick for a live progress indicator during
  // long-running Context Manager / Planner / wave phases. Re-renders once
  // per second while the pipeline is in an active state.
  const [nowMs, setNowMs] = useState(() => Date.now());
  const isActive =
    status === "envelopes_pending" ||
    status === "envelopes_confirmed" ||
    status === "tasks_pending" ||
    status === "tasks_confirmed" ||
    status === "executing" ||
    status === "traceability_gate";
  useEffect(() => {
    if (!isActive) return undefined;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isActive]);

  const runningRun = useMemo(
    () => runs.find((r) => r.status === "running") || null,
    [runs],
  );
  const startedAtMs = useMemo(() => {
    const s = state?.started_at;
    if (!s) return null;
    const t = Date.parse(s);
    return Number.isFinite(t) ? t : null;
  }, [state?.started_at]);
  const elapsedSec = startedAtMs ? Math.max(0, Math.floor((nowMs - startedAtMs) / 1000)) : 0;
  const fmtElapsed = (sec) => {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  };

  // iter-17.1 — Detect "context_manager COMPLETED but 0 envelopes" dead-end
  // so the UI surfaces the stuck-empty state instead of hanging silently.
  const contextManagerDone = runs.some(
    (r) => r.agent === "context_manager" && r.status === "completed",
  );
  const envelopesEmptyDeadEnd =
    status === "envelopes_pending" &&
    contextManagerDone &&
    envelopes.length === 0 &&
    !runs.some((r) => r.agent === "context_manager" && r.status === "running");

  // iter-17.3 — Same for Planner: status advanced to tasks_pending but 0 tasks
  // (Planner returned empty). Without this the "Confirm Tasks" button stays
  // greyed and the user has no idea why.
  const plannerDone = runs.some(
    (r) => r.agent === "planner" && r.status === "completed",
  );
  const tasksEmptyDeadEnd =
    status === "tasks_pending" &&
    plannerDone &&
    tasks.length === 0 &&
    !runs.some((r) => r.agent === "planner" && r.status === "running");

  // Refetch runs when the filter changes.
  useEffect(() => { refetchRuns({ agent: runsFilter, limit: runsLimit }); }, [runsFilter, runsLimit, refetchRuns]);

  // iter-17.5 — Auto-collapse Envelopes once Tasks appear so the Task list
  // becomes the primary focus. Users can re-expand manually via the header.
  const [envelopesAutoCollapsed, setEnvelopesAutoCollapsed] = useState(false);
  useEffect(() => {
    if (tasks.length > 0 && !envelopesAutoCollapsed) {
      setEnvelopesCollapsed(true);
      setEnvelopesAutoCollapsed(true);
    }
  }, [tasks.length, envelopesAutoCollapsed]);

  // -------------------------------------------------------------
  // Derived data
  // -------------------------------------------------------------
  const badge = STATUS_BADGE[status] || STATUS_BADGE.idle;
  const filteredRuns = runsFilter ? runs.filter((r) => r.agent === runsFilter) : runs;

  const waveGroups = useMemo(() => {
    const map = new Map();
    for (const t of tasks) {
      const key = t.wave ?? 0;
      if (!map.has(key)) map.set(key, { wave: key, wave_name: t.wave_name || `Wave ${key}`, tasks: [] });
      map.get(key).tasks.push(t);
    }
    return Array.from(map.values()).sort((a, b) => (a.wave || 0) - (b.wave || 0));
  }, [tasks]);

  const waveStats = (waveTasks) => {
    const be = waveTasks.filter((t) => t.assigned_to === "coder_be").length;
    const fe = waveTasks.filter((t) => t.assigned_to === "coder_fe").length;
    const done = waveTasks.filter((t) => t.status === "DONE" || t.status === "TESTED").length;
    const blocked = waveTasks.filter((t) => t.status === "BLOCKED").length;
    const inProgress = waveTasks.filter((t) => t.status === "IN_PROGRESS").length;
    return { be, fe, done, blocked, inProgress };
  };

  // -------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------
  const doStart = async () => {
    setStarting(true);
    try {
      await startCodegenMultiAgent(projectId);
      toast.success("Multi-agent pipeline started");
      refetchState();
      refetchRuns({});
    } catch (e) {
      const detail = errMsg(e);
      if (e?.response?.status === 400 && /architecture/i.test(detail)) {
        toast.error("Freeze Architecture stage first");
      } else {
        toast.error("Start failed: " + detail);
      }
    } finally {
      setStarting(false);
    }
  };

  const doConfirmEnvelopes = async () => {
    setConfirming(true);
    try {
      await confirmCodegenMultiAgentEnvelopes(projectId);
      toast.success("Envelopes confirmed — planner running");
      refetchState();
      refetchRuns({});
    } catch (e) {
      toast.error("Confirm failed: " + errMsg(e));
    } finally {
      setConfirming(false);
    }
  };

  const doConfirmTasks = async () => {
    setConfirming(true);
    try {
      await confirmCodegenMultiAgentTasks(projectId);
      toast.success("Tasks confirmed — waves executing");
      refetchState();
      refetchRuns({});
    } catch (e) {
      toast.error("Confirm failed: " + errMsg(e));
    } finally {
      setConfirming(false);
    }
  };

  const doCancel = async () => {
    setCancelling(true);
    try {
      await cancelCodegenMultiAgent(projectId);
      toast.info("Pipeline cancelled");
      refetchState();
    } catch (e) {
      toast.error("Cancel failed: " + errMsg(e));
    } finally {
      setCancelling(false);
      setCancelOpen(false);
    }
  };

  const doRerun = async () => {
    try {
      await rerunCodegenMultiAgent(projectId);
      toast.success("Rerun started from scratch");
      setRerunOpen(false);
      refetchState();
      refetchRuns({});
    } catch (e) {
      toast.error("Rerun failed: " + errMsg(e));
    }
  };

  const [retryingPlanner, setRetryingPlanner] = useState(false);
  const doRetryPlanner = async () => {
    setRetryingPlanner(true);
    try {
      await retryCodegenMultiAgentPlanner(projectId);
      toast.success("Retrying Planner — envelopes preserved");
      refetchState();
      refetchRuns({});
    } catch (e) {
      toast.error("Retry Planner failed: " + errMsg(e));
    } finally {
      setRetryingPlanner(false);
    }
  };

  const scrollToTraceability = () => {
    traceabilityRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    refetchTraceability();
  };

  const copyToClipboard = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Copied");
    } catch (_) {
      toast.error("Copy failed");
    }
  };

  // ─────────────────────────────────────────────
  // iter-17.10 — Live generated-code viewer helpers
  // ─────────────────────────────────────────────
  const filteredLiveServices = useMemo(() => {
    if (!liveServiceFilter) return liveServices;
    return liveServices.filter((s) => s.name === liveServiceFilter);
  }, [liveServices, liveServiceFilter]);

  const flatLiveFiles = useMemo(() => {
    const rows = [];
    for (const svc of filteredLiveServices) {
      for (const f of svc.files || []) {
        rows.push({ ...f, service_name: svc.name });
      }
    }
    return rows;
  }, [filteredLiveServices]);

  // iter-17.18 — Nested directory tree (matches Quick Generate look).
  // Build a hierarchical tree from the flat file list so the operator
  // sees `services / <svc> / src / main / java / com / lama / <svc> /
  // controller / …` instead of a flat basename list.
  const [expandedDirs, setExpandedDirs] = useState({});
  const treeRoot = useMemo(() => {
    const root = { name: "", dirs: {}, files: [] };
    for (const f of flatLiveFiles) {
      const parts = String(f.path || "").split("/").filter(Boolean);
      let cur = root;
      for (let i = 0; i < parts.length - 1; i++) {
        const p = parts[i];
        if (!cur.dirs[p]) cur.dirs[p] = { name: p, dirs: {}, files: [] };
        cur = cur.dirs[p];
      }
      cur.files.push({ ...f, basename: parts[parts.length - 1] || f.path });
    }
    return root;
  }, [flatLiveFiles]);
  const treeRows = useMemo(() => {
    const rows = [];
    const walk = (node, depth, baseKey) => {
      const dirs = Object.values(node.dirs).sort((a, b) => a.name.localeCompare(b.name));
      for (const d of dirs) {
        const key = baseKey ? `${baseKey}/${d.name}` : d.name;
        const isOpen = expandedDirs[key] !== false; // default open
        rows.push({ kind: "dir", key, name: d.name, depth, isOpen });
        if (isOpen) walk(d, depth + 1, key);
      }
      const files = [...node.files].sort((a, b) =>
        (a.basename || "").localeCompare(b.basename || ""),
      );
      for (const f of files) {
        rows.push({ kind: "file", key: `f:${f.id}`, depth, file: f });
      }
    };
    walk(treeRoot, 0, "");
    return rows;
  }, [treeRoot, expandedDirs]);
  const toggleDir = useCallback(
    (k) => setExpandedDirs((prev) => ({ ...prev, [k]: prev[k] === false ? true : false })),
    [],
  );

  // iter-17.18 — Code Chat state (parity with Quick Generate). Reuses
  // the /api/codegen/{pid}/chat endpoint so the operator can refactor /
  // fix / add-tests on any file the Multi-Agent pipeline emitted.
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatConvId, setChatConvId] = useState(null);

  const onSendChat = useCallback(async () => {
    const m = chatInput.trim();
    if (!m || !projectId) return;
    setChatBusy(true);
    setChatMessages((prev) => [...prev, { role: "user", content: m }]);
    setChatInput("");
    try {
      const r = await sendCodegenChat({
        project_id: projectId,
        message: m,
        conversation_id: chatConvId,
        file_id: selectedFileId,
        service_name: selectedFileMeta?.service_name || liveServiceFilter || "",
      });
      setChatConvId(r.conversation_id);
      setChatMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: r.content,
          file_changes: r.file_changes || [],
          message_id: r.message_id,
        },
      ]);
    } catch (e) {
      toast.error("Chat failed: " + (e?.response?.data?.detail || e.message));
    } finally {
      setChatBusy(false);
    }
  }, [chatInput, projectId, chatConvId, selectedFileId, selectedFileMeta, liveServiceFilter]);

  const onApplyFileChange = useCallback(async (fc, msgId) => {
    if (!fc.file_id) {
      toast.message("Target file not in repo yet — the pipeline must generate it first.");
      return;
    }
    try {
      await applyCodegenFileChange(projectId, fc.file_id, fc.new_content, msgId);
      toast.success("Applied to " + fc.file_path);
      if (selectedFileId === fc.file_id) {
        setSelectedFileContent(fc.new_content);
      }
      refetchFiles();
    } catch (_e) {
      toast.error("Apply failed");
    }
  }, [projectId, selectedFileId, refetchFiles]);

  // Auto-select the newest file the operator hasn't yet clicked one manually.
  // "Newest" = first file in the flat list we haven't shown yet (backend
  // sorts by file_path but new arrivals still enter this snapshot).
  useEffect(() => {
    if (userPickedFileRef.current) return;
    if (flatLiveFiles.length === 0) return;
    const previouslyKnown = seenFileIdsRef.current;
    let newestUnseen = null;
    for (const f of flatLiveFiles) {
      if (!previouslyKnown.has(f.id)) {
        newestUnseen = f;
      }
      previouslyKnown.add(f.id);
    }
    // Prefer a brand-new file for the "live tail" feel; otherwise fall
    // back to the last file in the list so first paint isn't blank.
    const target = newestUnseen || (selectedFileId ? null : flatLiveFiles[flatLiveFiles.length - 1]);
    if (target && target.id !== selectedFileId) {
      setSelectedFileId(target.id);
      setSelectedFileMeta({
        path: target.path,
        service_name: target.service_name,
        version: target.version,
        language: target.language,
      });
    }
  }, [flatLiveFiles, selectedFileId]);

  // Fetch content whenever the selected file id changes.
  useEffect(() => {
    if (!projectId || !selectedFileId) {
      setSelectedFileContent("");
      return;
    }
    let cancelled = false;
    setSelectedFileLoading(true);
    (async () => {
      try {
        const doc = await getCodegenFile(projectId, selectedFileId);
        if (cancelled) return;
        setSelectedFileContent(String(doc?.content || ""));
        // Refresh meta in case the version bumped between poll ticks.
        setSelectedFileMeta((prev) => ({
          ...(prev || {}),
          path: doc?.path || prev?.path,
          service_name: doc?.service_name || prev?.service_name,
          version: doc?.version ?? prev?.version,
          language: doc?.language || prev?.language,
        }));
      } catch (_) {
        if (!cancelled) setSelectedFileContent("");
      } finally {
        if (!cancelled) setSelectedFileLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [projectId, selectedFileId]);

  const handleLiveFileClick = useCallback((f) => {
    userPickedFileRef.current = true;
    setSelectedFileId(f.id);
    setSelectedFileMeta({
      path: f.path,
      service_name: f.service_name,
      version: f.version,
      language: f.language,
    });
  }, []);

  // -------------------------------------------------------------
  // Render
  // -------------------------------------------------------------
  if (loading && !state) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#F6F6FA]" data-testid="codegen-ma-loading">
        <Loader2 className="w-5 h-5 animate-spin text-[#2E2E38]" />
      </div>
    );
  }

  return (
    <div className="flex-1 min-w-0 min-h-0 overflow-y-auto bg-[#F6F6FA]" data-testid="codegen-ma-panel">
      <div className="max-w-[1600px] mx-auto p-4 space-y-4">

        {/* ─────────────────────────────────────────────
            A. State header strip
           ───────────────────────────────────────────── */}
        <section
          data-testid="codegen-ma-status"
          className="bg-white border border-[#E6E6E6] rounded-sm p-3"
        >
          <div className="flex items-center flex-wrap gap-3">
            <div>
              <div className="text-[9px] uppercase tracking-widest text-[#747480]">Multi-Agent CodeGen</div>
              <div className="flex items-center gap-2 mt-0.5">
                <span
                  data-testid="codegen-ma-status-badge"
                  className={cls(
                    "text-[10px] uppercase font-bold px-2 py-0.5 rounded-sm border",
                    badge.cls,
                  )}
                >
                  {badge.label}
                </span>
                {state?.current_wave > 0 && (
                  <span className="text-[10px] px-1.5 py-0.5 bg-[#F6F6FA] border border-[#E6E6E6] rounded-sm text-[#2E2E38]">
                    Wave {state.current_wave}
                  </span>
                )}
              </div>
            </div>

            <div className="h-8 w-px bg-[#E6E6E6]" />

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-[10px]">
              <div>
                <div className="uppercase text-[#747480] font-semibold">BE Build</div>
                <div className="font-mono text-[#2E2E38]">{state?.build_system_be || "—"}</div>
              </div>
              <div>
                <div className="uppercase text-[#747480] font-semibold">FE Build</div>
                <div className="font-mono text-[#2E2E38]">{state?.build_system_fe || "—"}</div>
              </div>
              <div>
                <div className="uppercase text-[#747480] font-semibold">Envelopes</div>
                <div className="font-mono text-[#2E2E38]">{state?.envelope_count ?? envelopes.length}</div>
              </div>
              <div>
                <div className="uppercase text-[#747480] font-semibold">Tasks</div>
                <div className="font-mono text-[#2E2E38]">{state?.task_count ?? tasks.length}</div>
              </div>
            </div>

            <div className="flex-1 min-w-4" />

            {/* ─── B. Action bar ─── */}
            <div className="flex items-center gap-2 flex-wrap">
              {(status === "idle" || status === "failed" || status === "completed") && (
                <Button
                  data-testid="codegen-ma-btn-start"
                  onClick={doStart}
                  disabled={starting}
                  className="h-8 text-[11px] px-3 bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500] font-semibold"
                >
                  {starting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
                  {" "}Start Pipeline
                </Button>
              )}

              {status === "envelopes_pending" && (
                <Button
                  data-testid="codegen-ma-btn-confirm-envelopes"
                  onClick={doConfirmEnvelopes}
                  disabled={confirming || envelopes.length === 0}
                  className="h-8 text-[11px] px-3 bg-[#2E2E38] text-white hover:bg-[#1E1E28]"
                >
                  {confirming ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
                  {" "}Confirm Envelopes
                </Button>
              )}

              {status === "envelopes_confirmed" && (
                <span className="inline-flex items-center gap-2 text-[11px] text-[#747480]">
                  <Loader2 className="w-3 h-3 animate-spin" /> Planner running…
                </span>
              )}

              {status === "tasks_pending" && (
                <>
                  <BuildSystemPopover projectId={projectId} state={state} onSaved={refetchState} />
                  <Button
                    data-testid="codegen-ma-btn-confirm-tasks"
                    onClick={doConfirmTasks}
                    disabled={confirming || tasks.length === 0}
                    className="h-8 text-[11px] px-3 bg-[#2E2E38] text-white hover:bg-[#1E1E28]"
                  >
                    {confirming ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
                    {" "}Confirm Tasks
                  </Button>
                </>
              )}

              {status === "tasks_confirmed" && (
                <span className="inline-flex items-center gap-2 text-[11px] text-[#747480]">
                  <Loader2 className="w-3 h-3 animate-spin" /> Queuing waves…
                </span>
              )}

              {status === "executing" && (
                <Button
                  data-testid="codegen-ma-btn-cancel"
                  variant="destructive"
                  onClick={() => setCancelOpen(true)}
                  disabled={cancelling}
                  className="h-8 text-[11px] px-3"
                >
                  {cancelling ? <Loader2 className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}
                  {" "}Cancel
                </Button>
              )}

              {status === "traceability_gate" && (
                <Button
                  data-testid="codegen-ma-btn-review-brs"
                  onClick={scrollToTraceability}
                  className="h-8 text-[11px] px-3 bg-purple-600 text-white hover:bg-purple-700"
                >
                  <Target className="w-3 h-3" /> Review BR Coverage
                </Button>
              )}

              {!TERMINAL_STATUSES.has(status) && (
                <span className="text-[10px] text-[#747480] italic">auto-polling…</span>
              )}

              <Button
                data-testid="codegen-ma-btn-rerun"
                variant="outline"
                onClick={() => setRerunOpen(true)}
                className="h-8 text-[11px] px-3 border-orange-300 text-orange-700 hover:bg-orange-50"
              >
                <RotateCcw className="w-3 h-3" /> Rerun From Scratch
              </Button>
            </div>
          </div>

          {state?.last_error && (
            <div className="mt-2 text-[11px] text-red-800 bg-red-50 border border-red-200 rounded-sm p-2 flex items-start gap-2">
              <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
              <span className="font-mono break-words">{state.last_error}</span>
            </div>
          )}

          {/* iter-17.1 — Live progress strip: elapsed timer + indeterminate
              bar during running phases so operators can gauge progress
              instead of staring at a blank screen. */}
          {isActive && (
            <div
              data-testid="codegen-ma-progress"
              className="mt-2 border border-[#E6E6E6] rounded-sm bg-[#F6F6FA] p-2"
            >
              <div className="flex items-center gap-2 text-[10px] text-[#2E2E38]">
                <Loader2 className="w-3 h-3 animate-spin text-[#2E2E38]" />
                <span className="font-semibold">
                  {status === "envelopes_pending" && "Context Manager discovering envelopes"}
                  {status === "envelopes_confirmed" && "Planner producing task list"}
                  {status === "tasks_pending" && (
                    tasks.length === 0
                      ? "Planner produced 0 tasks — check the Agent Runs timeline"
                      : "Awaiting task confirmation"
                  )}
                  {status === "tasks_confirmed" && "Preparing wave execution"}
                  {status === "executing" && `Executing wave ${state?.current_wave || 1}`}
                  {status === "traceability_gate" && "Running traceability gate"}
                </span>
                {runningRun && (
                  <span className="text-[#747480]">
                    · {runningRun.agent}
                    {runningRun.phase ? ` (${runningRun.phase})` : ""}
                  </span>
                )}
                <span className="flex-1" />
                <span className="font-mono text-[#747480]">
                  Elapsed <span className="text-[#2E2E38] font-semibold">{fmtElapsed(elapsedSec)}</span>
                </span>
                <span className="font-mono text-[#747480]">
                  Envelopes <span className="text-[#2E2E38] font-semibold">{state?.envelope_count ?? envelopes.length}</span>
                </span>
                <span className="font-mono text-[#747480]">
                  Tasks <span className="text-[#2E2E38] font-semibold">{state?.task_count ?? tasks.length}</span>
                </span>
              </div>
              {/* Indeterminate bar (no known ETA — cadence depends on LLM latency). */}
              <div className="mt-1.5 h-1 w-full bg-[#E6E6E6] rounded-sm overflow-hidden">
                <div className="h-full w-1/3 bg-[#FFE600] animate-[codegen-ma-slide_1.6s_ease-in-out_infinite]" />
              </div>
              <style>{`@keyframes codegen-ma-slide{0%{transform:translateX(-100%)}50%{transform:translateX(150%)}100%{transform:translateX(300%)}}`}</style>
            </div>
          )}

          {/* iter-17.1 — Explicit dead-end surface when Context Manager
              returned zero envelopes. Without this the pipeline looks hung. */}
          {envelopesEmptyDeadEnd && (
            <div
              data-testid="codegen-ma-empty-envelopes-warning"
              className="mt-2 text-[11px] text-amber-900 bg-amber-50 border border-amber-300 rounded-sm p-2 flex items-start gap-2"
            >
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-amber-700" />
              <div>
                <div className="font-semibold">Context Manager returned zero envelopes.</div>
                <div className="mt-0.5">
                  This usually means the LLM response was empty or the frozen Architecture
                  StageContext has no API routes to discover. Options: (a) expand the Agent Run
                  below to inspect the Context Manager output/error, (b) verify Architecture is
                  frozen with a service map, then <strong>Rerun From Scratch</strong>.
                </div>
              </div>
            </div>
          )}

          {/* iter-17.3 — Same for Planner. */}
          {tasksEmptyDeadEnd && (
            <div
              data-testid="codegen-ma-empty-tasks-warning"
              className="mt-2 text-[11px] text-amber-900 bg-amber-50 border border-amber-300 rounded-sm p-2 flex items-start gap-2"
            >
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-amber-700" />
              <div className="flex-1">
                <div className="font-semibold">Planner produced zero tasks.</div>
                <div className="mt-0.5">
                  The pipeline advanced past Planner but no tasks were emitted — usually a stale
                  pipeline from before the deterministic Planner fix, or a silent LLM timeout on
                  the fallback path. Confirm Tasks is disabled because there is nothing to
                  confirm. Click <strong>Retry Planner</strong> below to re-run <em>only</em>
                  the Planner step (envelopes are preserved), or expand the <em>planner</em>
                  Agent Run to inspect the failure.
                </div>
                <div className="mt-2">
                  <Button
                    data-testid="codegen-ma-btn-retry-planner"
                    onClick={doRetryPlanner}
                    disabled={retryingPlanner}
                    className="h-7 text-[11px] px-3 bg-amber-600 text-white hover:bg-amber-700 font-semibold"
                  >
                    {retryingPlanner ? <Loader2 className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />}
                    {" "}Retry Planner
                  </Button>
                </div>
              </div>
            </div>
          )}
        </section>

        {/* ─────────────────────────────────────────────
            C. Envelopes section
           ───────────────────────────────────────────── */}
        {envelopes.length > 0 ? (
          <section
            data-testid="codegen-ma-envelopes"
            className="bg-white border border-[#E6E6E6] rounded-sm"
          >
            <button
              type="button"
              data-testid="codegen-ma-envelopes-toggle"
              onClick={() => setEnvelopesCollapsed((v) => !v)}
              className="w-full px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2 hover:bg-[#FAFAFC] text-left"
            >
              {envelopesCollapsed
                ? <ChevronRightIcon className="w-3.5 h-3.5 text-[#747480]" />
                : <ChevronDown className="w-3.5 h-3.5 text-[#747480]" />}
              <Layers className="w-3.5 h-3.5 text-[#2E2E38]" />
              <h2 className="font-display font-bold text-[13px] text-[#2E2E38]">Envelopes</h2>
              <span className="text-[10px] text-[#747480]">
                {envelopes.length} · click row to expand · inline-edit acceptance criteria + status
              </span>
            </button>
            {!envelopesCollapsed && (
              <div className="max-h-[60vh] overflow-auto">
                <table className="w-full text-left">
                  <thead className="bg-[#F6F6FA] sticky top-0 z-10 shadow-[0_1px_0_#E6E6E6]">
                    <tr className="text-[9px] uppercase text-[#747480]">
                      <th className="px-2 py-1.5">Envelope</th>
                      <th className="px-2 py-1.5">Method · Path</th>
                      <th className="px-2 py-1.5">Side</th>
                      <th className="px-2 py-1.5">Risk</th>
                      <th className="px-2 py-1.5">Layer</th>
                      <th className="px-2 py-1.5"># Tables</th>
                      <th className="px-2 py-1.5">Status</th>
                      <th className="px-2 py-1.5 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {envelopes.map((e) => (
                      <EnvelopeRow
                        key={e.envelope_id}
                        envelope={e}
                        projectId={projectId}
                        onSaved={refetchState}
                        isOpen={openEnvelopeId === e.envelope_id}
                        onToggle={toggleEnvelopeOpen}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        ) : (
          status !== "idle" && (
            <section
              data-testid="codegen-ma-envelopes-empty"
              className="bg-white border border-[#E6E6E6] rounded-sm px-3 py-2 flex items-center gap-2 text-[11px] text-[#747480]"
            >
              {status === "envelopes_pending" || status === "executing" ? (
                <Loader2 className="w-3.5 h-3.5 text-[#2E2E38] animate-spin" />
              ) : (
                <Layers className="w-3.5 h-3.5 text-[#747480]" />
              )}
              <span className="font-semibold text-[#2E2E38]">Envelopes</span>
              <span>·</span>
              <span>
                {status === "envelopes_pending"
                  ? "Context Manager is emitting envelopes — watch the Agent Runs timeline below."
                  : "No envelopes yet. They will appear once Context Manager runs."}
              </span>
            </section>
          )
        )}

        {/* ─────────────────────────────────────────────
            D. Tasks section (grouped by wave)
           ───────────────────────────────────────────── */}
        {tasks.length > 0 && (
          <section
            data-testid="codegen-ma-tasks"
            className="bg-white border border-[#E6E6E6] rounded-sm"
          >
            <button
              type="button"
              data-testid="codegen-ma-tasks-toggle"
              onClick={() => setTasksCollapsed((v) => !v)}
              className="w-full px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2 hover:bg-[#FAFAFC] text-left"
            >
              {tasksCollapsed
                ? <ChevronRightIcon className="w-3.5 h-3.5 text-[#747480]" />
                : <ChevronDown className="w-3.5 h-3.5 text-[#747480]" />}
              <ClipboardCopy className="w-3.5 h-3.5 text-[#2E2E38]" />
              <h2 className="font-display font-bold text-[13px] text-[#2E2E38]">Tasks</h2>
              <span className="text-[10px] text-[#747480]">
                {tasks.length} across {waveGroups.length} wave(s)
              </span>
            </button>
            {!tasksCollapsed && (
              <div className="max-h-[65vh] overflow-auto">
              {waveGroups.map((group) => {
                const stats = waveStats(group.tasks);
                const isOpen = !collapsedWaves[group.wave];
                return (
                  <div key={`w-${group.wave}`} className="border-b border-[#E6E6E6] last:border-b-0">
                    <button
                      data-testid={`codegen-ma-wave-header-${group.wave}`}
                      onClick={() =>
                        setCollapsedWaves((s) => ({ ...s, [group.wave]: isOpen }))
                      }
                      className="w-full flex items-center gap-2 px-3 py-1.5 hover:bg-[#F6F6FA] text-left"
                    >
                      {isOpen ? <ChevronDown className="w-3 h-3 text-[#747480]" /> : <ChevronRightIcon className="w-3 h-3 text-[#747480]" />}
                      <span className="text-[11px] font-semibold text-[#2E2E38]">
                        Wave {group.wave} · {group.wave_name}
                      </span>
                      <span className="text-[10px] text-[#747480]">
                        {group.tasks.length} tasks · BE {stats.be} · FE {stats.fe} · done {stats.done}
                        {stats.blocked > 0 && <span className="ml-1 text-red-700 font-semibold">· blocked {stats.blocked}</span>}
                        {stats.inProgress > 0 && <span className="ml-1 text-sky-700 font-semibold">· in-progress {stats.inProgress}</span>}
                      </span>
                    </button>
                    {isOpen && (
                      <div className="overflow-x-auto">
                        <table className="w-full text-left">
                          <thead className="bg-[#FAFAFC]">
                            <tr className="text-[9px] uppercase text-[#747480]">
                              <th className="px-2 py-1.5">Task</th>
                              <th className="px-2 py-1.5">Title</th>
                              <th className="px-2 py-1.5">Layer</th>
                              <th className="px-2 py-1.5">Assigned</th>
                              <th className="px-2 py-1.5">Target Path</th>
                              <th className="px-2 py-1.5">Status</th>
                              <th className="px-2 py-1.5">Score</th>
                              <th className="px-2 py-1.5 text-right">Actions</th>
                            </tr>
                          </thead>
                          <tbody>
                            {group.tasks.map((t) => {
                              const asg = ASSIGNED_BADGE[t.assigned_to] || { label: t.assigned_to || "—", cls: "bg-slate-200 text-slate-800" };
                              const stCls = TASK_STATUS_BADGE[t.status] || "bg-slate-100 text-slate-700";
                              return (
                                <tr
                                  key={t.task_id}
                                  data-testid={`codegen-ma-task-row-${t.task_id}`}
                                  className="border-b border-[#F6F6FA] hover:bg-[#FAFAFC]"
                                >
                                  <td className="px-2 py-1.5 text-[10px] font-mono text-[#2E2E38]">{t.task_id}</td>
                                  <td className="px-2 py-1.5 text-[11px] text-[#2E2E38]" title={t.title}>{short(t.title, 60)}</td>
                                  <td className="px-2 py-1.5 text-[10px] text-[#747480]">{t.layer || "—"}</td>
                                  <td className="px-2 py-1.5 text-[10px]">
                                    <span className={cls("uppercase font-bold px-1.5 py-0.5 rounded-sm", asg.cls)}>
                                      {asg.label}
                                    </span>
                                  </td>
                                  <td className="px-2 py-1.5 text-[10px] font-mono text-[#747480] truncate max-w-[240px]" title={t.target_path}>
                                    {t.target_path || "—"}
                                  </td>
                                  <td className="px-2 py-1.5 text-[10px]">
                                    <span className={cls("uppercase font-bold px-1.5 py-0.5 rounded-sm", stCls)}>
                                      {t.status}
                                    </span>
                                  </td>
                                  <td className="px-2 py-1.5 text-[10px] text-[#2E2E38]">
                                    {t.verifier_score != null ? Number(t.verifier_score).toFixed(2) : "—"}
                                    {t.rejection_count > 0 && (
                                      <span className="ml-1 text-[9px] text-red-700">×{t.rejection_count}</span>
                                    )}
                                  </td>
                                  <td className="px-2 py-1.5 text-[10px] text-right">
                                    <button
                                      data-testid={`codegen-ma-task-edit-${t.task_id}`}
                                      onClick={() => setEditingTask(t)}
                                      className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA]"
                                    >
                                      <Pencil className="w-3 h-3" /> Edit
                                    </button>
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            )}
          </section>
        )}

        {/* ─────────────────────────────────────────────
            E. Traceability panel
           ───────────────────────────────────────────── */}
        {(status === "traceability_gate" || status === "completed") && (
          <section
            ref={traceabilityRef}
            data-testid="codegen-ma-traceability"
            className="bg-white border border-[#E6E6E6] rounded-sm"
          >
            <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2">
              <Target className="w-3.5 h-3.5 text-[#2E2E38]" />
              <h2 className="font-display font-bold text-[13px] text-[#2E2E38]">Traceability — BR Coverage</h2>
              <button
                onClick={refetchTraceability}
                className="ml-auto text-[10px] text-[#747480] hover:text-[#2E2E38] underline"
              >
                Refresh
              </button>
            </div>
            {traceability ? (
              <div className="p-3">
                <div className="flex items-baseline gap-3">
                  <span
                    className={cls(
                      "font-display font-bold text-4xl leading-none",
                      traceability.coverage_pct >= 100 ? "text-emerald-600" : "text-red-600",
                    )}
                  >
                    {Number(traceability.coverage_pct || 0).toFixed(1)}%
                  </span>
                  <span className="text-[11px] text-[#747480]">BR coverage across all tasks</span>
                </div>

                {Array.isArray(traceability.missing_brs) && traceability.missing_brs.length > 0 && (
                  <div className="mt-3">
                    <div className="text-[10px] uppercase text-[#747480] font-semibold flex items-center gap-2">
                      <AlertTriangle className="w-3 h-3 text-red-600" /> Missing BRs ({traceability.missing_brs.length})
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {traceability.missing_brs.map((br) => (
                        <button
                          key={br}
                          onClick={() => copyToClipboard(br)}
                          className="text-[10px] font-mono px-1.5 py-0.5 bg-red-50 border border-red-200 rounded-sm text-red-800 hover:bg-red-100 inline-flex items-center gap-1"
                          title="Click to copy"
                        >
                          {br} <ClipboardCopy className="w-3 h-3" />
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {Array.isArray(traceability.per_envelope) && traceability.per_envelope.length > 0 && (
                  <div className="mt-4">
                    <div className="text-[10px] uppercase text-[#747480] font-semibold mb-1">
                      Per-Envelope Coverage
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-[10px]">
                        <thead className="bg-[#F6F6FA]">
                          <tr className="text-[9px] uppercase text-[#747480]">
                            <th className="px-2 py-1.5">Envelope</th>
                            <th className="px-2 py-1.5">Expected</th>
                            <th className="px-2 py-1.5">Covered</th>
                            <th className="px-2 py-1.5">Missing</th>
                          </tr>
                        </thead>
                        <tbody>
                          {traceability.per_envelope.map((row) => (
                            <tr key={row.envelope_id} className="border-b border-[#F6F6FA]">
                              <td className="px-2 py-1.5 font-mono text-[#2E2E38]">{row.envelope_id}</td>
                              <td className="px-2 py-1.5 font-mono text-[#747480]">{(row.expected || []).join(", ") || "—"}</td>
                              <td className="px-2 py-1.5 font-mono text-emerald-700">{(row.covered || []).join(", ") || "—"}</td>
                              <td className={cls(
                                "px-2 py-1.5 font-mono",
                                (row.missing || []).length > 0 ? "text-red-700 font-semibold" : "text-[#747480]",
                              )}>
                                {(row.missing || []).join(", ") || "—"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="p-4 text-[11px] text-[#747480]">Loading traceability…</div>
            )}
          </section>
        )}

        {/* ─────────────────────────────────────────────
            F. Agent Runs timeline
           ───────────────────────────────────────────── */}
        <section
          data-testid="codegen-ma-runs-list"
          className="bg-white border border-[#E6E6E6] rounded-sm"
        >
          <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2">
            <button
              type="button"
              data-testid="codegen-ma-runs-toggle"
              onClick={() => setRunsCollapsed((v) => !v)}
              className="flex items-center gap-2 hover:opacity-80"
              title={runsCollapsed ? "Expand Agent Runs" : "Collapse Agent Runs"}
            >
              {runsCollapsed
                ? <ChevronRightIcon className="w-3.5 h-3.5 text-[#747480]" />
                : <ChevronDown className="w-3.5 h-3.5 text-[#747480]" />}
              <Wand2 className="w-3.5 h-3.5 text-[#2E2E38]" />
              <h2 className="font-display font-bold text-[13px] text-[#2E2E38]">Agent Runs</h2>
            </button>
            <span className="text-[10px] text-[#747480]">
              newest first · {filteredRuns.length}{runsHasMore ? "+" : ""} shown
            </span>
            <div className="flex-1" />
            <select
              data-testid="codegen-ma-runs-filter"
              value={runsFilter}
              onChange={(e) => setRunsFilter(e.target.value)}
              className="text-[10px] border border-[#E6E6E6] rounded-sm px-1 py-0.5 bg-white"
            >
              {AGENT_OPTIONS.map((a) => (
                <option key={a || "all"} value={a}>{a || "All agents"}</option>
              ))}
            </select>
          </div>
          {!runsCollapsed && (filteredRuns.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-[#747480] flex items-center gap-2">
              {status === "idle" ? (
                <Wand2 className="w-3.5 h-3.5 text-[#747480]" />
              ) : (
                <Loader2 className="w-3.5 h-3.5 text-[#2E2E38] animate-spin" />
              )}
              <span>
                {status === "idle"
                  ? "Once the pipeline starts, every agent invocation (Context Manager, Planner, Coder BE/FE, Verifier, Reviewer, Tester, Traceability Gate, Finalizer) streams here in real time."
                  : "Waiting for the first agent run…"}
              </span>
            </div>
          ) : (
            <>
              <div className="max-h-[600px] overflow-y-auto">
                {filteredRuns.map((r) => (
                  <AgentRunRow key={r.run_id || r.id || `${r.agent}-${r.created_at}`} run={r} />
                ))}
              </div>
              {runsHasMore && (
                <div className="p-2 border-t border-[#E6E6E6] text-center">
                  <button
                    data-testid="codegen-ma-runs-load-more"
                    onClick={() => setRunsLimit((n) => n + 20)}
                    className="text-[10px] px-3 py-1 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA] text-[#2E2E38]"
                  >
                    Load More
                  </button>
                </div>
              )}
            </>
          ))}
        </section>

        {/* ─────────────────────────────────────────────
            G. Generated Code (Live)                     iter-17.10
            IDE-style split: LEFT = per-service file
            tree, RIGHT = Monaco viewer for the picked
            file. Polls listCodegenFiles every 3s while
            the pipeline is non-terminal so Coder-emitted
            files appear the moment the backend writes
            them (no need to leave this panel or wait for
            the wave to finish).
           ───────────────────────────────────────────── */}
        <section
          data-testid="codegen-ma-files"
          className={cls(
            filesMaximized
              ? "fixed inset-0 z-[60] bg-white flex flex-col"
              : "bg-white border border-[#E6E6E6] rounded-sm",
          )}
        >
          <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2">
            <button
              type="button"
              data-testid="codegen-ma-files-toggle"
              onClick={() => setFilesCollapsed((v) => !v)}
              disabled={filesMaximized}
              className={cls(
                "flex items-center gap-2 hover:opacity-80",
                filesMaximized ? "cursor-default opacity-100" : "",
              )}
              title={filesCollapsed ? "Expand Generated Code" : "Collapse Generated Code"}
            >
              {filesMaximized || !filesCollapsed
                ? <ChevronDown className="w-3.5 h-3.5 text-[#747480]" />
                : <ChevronRightIcon className="w-3.5 h-3.5 text-[#747480]" />}
              <FileCode className="w-3.5 h-3.5 text-[#2E2E38]" />
              <h2 className="font-display font-bold text-[13px] text-[#2E2E38]">Generated Code</h2>
            </button>
            <span className="text-[10px] text-[#747480]" data-testid="codegen-ma-files-count">
              {liveTotalFiles > 0
                ? `${liveTotalFiles} file${liveTotalFiles === 1 ? "" : "s"} · ${liveServices.length} service${liveServices.length === 1 ? "" : "s"}`
                : liveLoaded
                  ? (isTerminal ? "no files" : "waiting for Coder…")
                  : "loading…"}
            </span>
            {!isTerminal && (
              <span
                className="inline-flex items-center gap-1 text-[9px] uppercase tracking-widest text-emerald-700"
                title="Auto-refreshing every 3s while the pipeline runs"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Live
              </span>
            )}
            <div className="flex-1" />
            {liveServices.length > 1 && (
              <select
                data-testid="codegen-ma-files-service-filter"
                value={liveServiceFilter}
                onChange={(e) => setLiveServiceFilter(e.target.value)}
                className="text-[10px] border border-[#E6E6E6] rounded-sm px-1 py-0.5 bg-white"
                title="Filter file tree by service"
              >
                <option value="">All services</option>
                {liveServices.map((s) => (
                  <option key={s.name} value={s.name}>{s.name}</option>
                ))}
              </select>
            )}
            <button
              type="button"
              data-testid="codegen-ma-files-refresh"
              onClick={refetchFiles}
              className="text-[10px] px-2 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA] text-[#2E2E38] inline-flex items-center gap-1"
              title="Refresh file list now"
            >
              <RotateCcw className="w-3 h-3" />
              Refresh
            </button>
            {/* iter-17.11 — Maximize/restore toggle for the Generated Code
                section so operators can view long paths / wide files
                without the inline layout constraint. Esc restores. */}
            <button
              type="button"
              data-testid="codegen-ma-files-maximize"
              onClick={() => {
                if (filesMaximized) {
                  setFilesMaximized(false);
                } else {
                  setFilesCollapsed(false);
                  setFilesMaximized(true);
                }
              }}
              className="text-[10px] px-2 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA] text-[#2E2E38] inline-flex items-center gap-1"
              title={filesMaximized ? "Restore (Esc)" : "Maximize to full screen"}
            >
              {filesMaximized
                ? (<><Minimize2 className="w-3 h-3" />Restore</>)
                : (<><Maximize2 className="w-3 h-3" />Maximize</>)}
            </button>
          </div>

          {(filesMaximized || !filesCollapsed) && (
            flatLiveFiles.length === 0 ? (
              <div className="px-3 py-4 text-[11px] text-[#747480] flex items-center gap-2">
                <FileCode className="w-3.5 h-3.5 text-[#747480]" />
                <span>
                  {isTerminal
                    ? "This project has no generated files yet. Start the pipeline above to produce code."
                    : "Coder agents will write files here as the waves execute. This list refreshes automatically."}
                </span>
              </div>
            ) : (
              <div
                className={cls("w-full", filesMaximized ? "flex-1 min-h-0" : "")}
                style={filesMaximized ? undefined : { height: 560 }}
              >
                <PanelGroup direction="horizontal" autoSaveId="codegen-ma-files-split">
                  {/* LEFT — nested directory tree (iter-17.18) */}
                  <Panel defaultSize={25} minSize={16}>
                    <div
                      className="h-full overflow-y-auto border-r border-[#E6E6E6] bg-[#FAFAFC] py-1"
                      data-testid="codegen-ma-files-tree"
                    >
                      {treeRows.length === 0 && (
                        <div className="text-[11px] text-[#747480] p-3">No files yet.</div>
                      )}
                      {treeRows.map((r) => {
                        if (r.kind === "dir") {
                          return (
                            <button
                              key={`d:${r.key}`}
                              type="button"
                              onClick={() => toggleDir(r.key)}
                              data-testid={`codegen-ma-dir-${r.key}`}
                              className="w-full flex items-center gap-1 text-[11px] py-0.5 hover:bg-[#F6F6FA] text-[#2E2E38]"
                              style={{ paddingLeft: 8 + r.depth * 10 }}
                              title={r.key}
                            >
                              {r.isOpen
                                ? <ChevronDown className="w-3 h-3 text-[#747480]" />
                                : <ChevronRightIcon className="w-3 h-3 text-[#747480]" />}
                              {r.isOpen
                                ? <FolderOpen className="w-3 h-3 text-[#FFE600]" />
                                : <Folder className="w-3 h-3 text-[#FFE600]" />}
                              <span className="truncate">{r.name}</span>
                            </button>
                          );
                        }
                        const f = r.file;
                        const isSel = selectedFileId === f.id;
                        return (
                          <button
                            key={r.key}
                            type="button"
                            data-testid={`codegen-ma-file-row-${f.id}`}
                            onClick={() => handleLiveFileClick({ ...f, service_name: f.service_name })}
                            className={cls(
                              "w-full text-left flex items-center gap-1 text-[11px] font-mono py-0.5 transition-colors",
                              isSel
                                ? "bg-[#FFFCE6] text-[#2E2E38] font-semibold"
                                : "hover:bg-[#F6F6FA] text-[#2E2E38]",
                            )}
                            style={{ paddingLeft: 8 + r.depth * 10 }}
                            title={f.path}
                          >
                            <FileText className="w-3 h-3 shrink-0 text-[#747480]" />
                            <span className="truncate flex-1">{f.basename}</span>
                            {f.edited && (
                              <span
                                className="text-[8px] uppercase font-bold text-amber-700 shrink-0"
                                title="Manually edited"
                              >
                                edit
                              </span>
                            )}
                            <span className="text-[8px] text-[#B0B0B8] shrink-0" title="Version">
                              v{f.version}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </Panel>
                  <PanelResizeHandle className="w-1 bg-[#E6E6E6] hover:bg-[#FFE600] transition-colors" />
                  {/* CENTER — Monaco viewer */}
                  <Panel defaultSize={45} minSize={25}>
                    <div className="h-full flex flex-col bg-white">
                      <div className="px-3 py-1.5 border-b border-[#E6E6E6] flex items-center gap-2 bg-[#FAFAFC]">
                        <FileText className="w-3.5 h-3.5 text-[#747480]" />
                        {selectedFileMeta ? (
                          <>
                            <span
                              className="text-[11px] font-mono text-[#2E2E38] truncate flex-1"
                              data-testid="codegen-ma-file-selected-path"
                            >
                              {selectedFileMeta.path}
                            </span>
                            <span className="text-[9px] uppercase bg-[#F6F6FA] px-1.5 py-0.5 rounded-sm text-[#747480]">
                              {selectedFileMeta.service_name}
                            </span>
                            <span className="text-[9px] uppercase bg-[#F6F6FA] px-1.5 py-0.5 rounded-sm text-[#747480]">
                              v{selectedFileMeta.version}
                            </span>
                            <button
                              type="button"
                              onClick={() => copyToClipboard(selectedFileContent)}
                              className="text-[10px] px-2 py-0.5 border border-[#E6E6E6] rounded-sm hover:bg-white text-[#2E2E38] inline-flex items-center gap-1"
                              title="Copy file content"
                            >
                              <ClipboardCopy className="w-3 h-3" />
                              Copy
                            </button>
                          </>
                        ) : (
                          <span className="text-[11px] text-[#747480]">Pick a file to view</span>
                        )}
                      </div>
                      <div className="flex-1 min-h-0 relative">
                        {selectedFileLoading && (
                          <div className="absolute top-1 right-2 z-10 inline-flex items-center gap-1 text-[10px] text-[#747480]">
                            <Loader2 className="w-3 h-3 animate-spin" />
                            loading
                          </div>
                        )}
                        {selectedFileId ? (
                          <Editor
                            height="100%"
                            path={selectedFileMeta?.path}
                            language={
                              selectedFileMeta?.language && selectedFileMeta.language !== "text"
                                ? selectedFileMeta.language
                                : guessLanguage(selectedFileMeta?.path || "")
                            }
                            value={selectedFileContent}
                            theme="vs"
                            options={{
                              readOnly: true,
                              minimap: { enabled: false },
                              fontSize: 12,
                              lineNumbers: "on",
                              scrollBeyondLastLine: false,
                              wordWrap: "on",
                              renderLineHighlight: "none",
                            }}
                          />
                        ) : (
                          <div className="h-full flex items-center justify-center text-[11px] text-[#747480]">
                            Pick a file from the tree to view it here.
                          </div>
                        )}
                      </div>
                    </div>
                  </Panel>
                  <PanelResizeHandle className="w-1 bg-[#E6E6E6] hover:bg-[#FFE600] transition-colors" />
                  {/* RIGHT — Code Chat (iter-17.18) */}
                  <Panel defaultSize={30} minSize={18}>
                    <div className="h-full bg-white flex flex-col">
                      <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-1 bg-[#FAFAFC]">
                        <Code2 className="w-3 h-3" />
                        <span className="text-[11px] font-semibold">Code Chat</span>
                        <span className="text-[10px] text-[#747480] ml-auto truncate">
                          {selectedFileMeta
                            ? `→ ${String(selectedFileMeta.path || "").split("/").pop()}`
                            : "no file"}
                        </span>
                      </div>
                      <div
                        className="flex-1 overflow-y-auto p-3 space-y-2"
                        data-testid="codegen-ma-chat-log"
                      >
                        {chatMessages.length === 0 && (
                          <div className="text-[11px] text-[#747480]">
                            Ask the codegen-LLM to refactor, fix, or add tests. The LLM may
                            emit one or more{" "}
                            <code>[FILE_CHANGE:path/to/file]…[/FILE_CHANGE]</code> blocks —
                            review and click Apply.
                          </div>
                        )}
                        {chatMessages.map((m, i) => (
                          <div
                            key={i}
                            className={cls(
                              "text-[12px] p-2 rounded-sm",
                              m.role === "user"
                                ? "bg-[#FFFCE6] border border-[#FFE600]"
                                : "bg-[#F6F6FA] border border-[#E6E6E6]",
                            )}
                          >
                            <div className="text-[9px] uppercase font-bold text-[#747480] mb-1">
                              {m.role}
                            </div>
                            <pre className="whitespace-pre-wrap text-[12px] leading-snug text-[#2E2E38]">
                              {m.content}
                            </pre>
                            {m.file_changes?.length > 0 && (
                              <div className="mt-1.5 space-y-1">
                                {m.file_changes.map((fc, j) => (
                                  <div key={j} className="text-[10px] flex items-center gap-1">
                                    <span className="font-mono truncate flex-1">{fc.file_path}</span>
                                    <button
                                      type="button"
                                      onClick={() => onApplyFileChange(fc, m.message_id)}
                                      data-testid={`codegen-ma-apply-fc-${i}-${j}`}
                                      className="px-2 py-0.5 bg-[#2E2E38] text-white rounded-sm"
                                    >
                                      Apply
                                    </button>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                        {chatBusy && (
                          <div className="text-[11px] text-[#747480] flex items-center gap-1">
                            <Loader2 className="w-3 h-3 animate-spin" /> Thinking…
                          </div>
                        )}
                      </div>
                      <div className="border-t border-[#E6E6E6] p-2 flex gap-1">
                        <textarea
                          rows={2}
                          value={chatInput}
                          onChange={(e) => setChatInput(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && !e.shiftKey) {
                              e.preventDefault();
                              onSendChat();
                            }
                          }}
                          placeholder="Ask about the code…"
                          data-testid="codegen-ma-chat-input"
                          className="flex-1 text-[12px] border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm px-2 py-1.5 resize-none"
                        />
                        <Button
                          data-testid="codegen-ma-chat-send"
                          onClick={onSendChat}
                          disabled={chatBusy}
                          className="h-auto bg-[#2E2E38] text-white px-3"
                        >
                          <Send className="w-3 h-3" />
                        </Button>
                      </div>
                    </div>
                  </Panel>
                </PanelGroup>
              </div>
            )
          )}
        </section>

      </div>

      {/* ── Modals ── */}
      <TypedConfirmModal
        open={rerunOpen}
        word="RERUN"
        title="Rerun From Scratch"
        warning="This wipes envelopes, tasks and agent runs for the multi-agent pipeline and restarts from Context Manager. Generated code files (single-shot output) are preserved."
        confirmLabel="Rerun From Scratch"
        onClose={() => setRerunOpen(false)}
        onConfirm={doRerun}
      />

      <TypedConfirmModal
        open={cancelOpen}
        word="CANCEL"
        title="Cancel Pipeline"
        warning="Marks the multi-agent pipeline as failed. In-flight LLM calls will finish but no new tasks will be dispatched. You can re-Start or Rerun afterwards."
        confirmLabel="Cancel Pipeline"
        onClose={() => setCancelOpen(false)}
        onConfirm={doCancel}
      />

      <TaskEditDialog
        open={!!editingTask}
        task={editingTask}
        projectId={projectId}
        onClose={() => setEditingTask(null)}
        onSaved={refetchState}
      />
    </div>
  );
}
