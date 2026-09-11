import React, { useEffect, useMemo, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import {
  Settings as SettingsIcon, History, Save, RefreshCw, BarChart3,
  Loader2, CheckCircle2, ArrowRight, FilePlus2, Trash2, AlertTriangle,
  HelpCircle, Download,
} from "lucide-react";
import { toast } from "sonner";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Cell, Legend,
  PieChart, Pie, LabelList,
} from "recharts";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useProjects } from "@/state/ProjectContext";
import { getUsageSummary, createProject, deleteProject } from "@/lib/api";
import {
  labelForAgentKey, colorForAgentKey, isAgentDrivenProjectType,
  projectTypeMeta, PROJECT_TYPE_META,
} from "@/lib/agentLabels";

/**
 * SettingsMenu — replaces the old "Refresh App" button with a four-pane
 * popover. Panes:
 *   1. History     — list every project; reopen each at the last visited
 *                    stage (or the deepest unlocked stage if no history).
 *   2. Auto-save   — toggle persisted in localStorage. When ON we record
 *                    the last visited stage path per project on every
 *                    navigation (see useAutoSaveTracker hook below).
 *   3. Refresh App — same destructive wipe the old button did.
 *   4. Statistics  — per-stage token usage bar chart, fed by the existing
 *                    /api/console/usage/summary endpoint.
 *
 * All four panes use existing backend endpoints. No new server work.
 */

const STAGE_PATHS = {
  Discovery: "/",
  DataModel: "/data-model",
  Architecture: "/architecture",
  CodeGen: "/code-gen",
  Living: "/living",
};
const STAGE_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"];
const STAGE_COLORS = {
  Discovery: "#FFE600", DataModel: "#7C3AED", Architecture: "#10B981",
  CodeGen: "#0EA5E9", Living: "#F97316",
  "Knowledge Base": "#94A3B8",
};
// Backend logs token rows without a stage tag (KB build, chat, ontology…)
// under the bucket "unknown". For the UI we surface these as "Knowledge Base"
// because that's where the bulk of pre-stage LLM work actually happens.
const UNKNOWN_STAGE_LABEL = "Knowledge Base";

const lsKey = (pid) => `lama:lastStage:${pid}`;
const AUTOSAVE_KEY = "lama:autoSave";

/** Compute the deepest stage the user could reasonably resume at. Prefers
 *  the localStorage-recorded last stage; otherwise the last non-locked
 *  stage from project.stage_status. */
function resumePathFor(project) {
  if (!project?.id) return "/";
  try {
    const saved = localStorage.getItem(lsKey(project.id));
    if (saved && Object.values(STAGE_PATHS).includes(saved)) return saved;
  } catch (_) { /* ignore */ }
  const status = project.stage_status || {};
  let target = "Discovery";
  for (const k of STAGE_ORDER) {
    const s = status[k];
    if (s === "frozen" || s === "available" || s === "active") target = k;
  }
  return STAGE_PATHS[target] || "/";
}

/** Hook to be mounted once at the app shell. When auto-save is ON, every
 *  navigation persists `{projectId, pathname}` to localStorage so that
 *  re-opening a project from History returns the user to the same stage. */
export function useAutoSaveTracker(activeProjectId, pathname) {
  useEffect(() => {
    if (!activeProjectId) return;
    try {
      if (localStorage.getItem(AUTOSAVE_KEY) === "off") return;
      if (Object.values(STAGE_PATHS).includes(pathname)) {
        localStorage.setItem(lsKey(activeProjectId), pathname);
      }
    } catch (_) { /* ignore */ }
  }, [activeProjectId, pathname]);
}

// ─── Sub-panes ────────────────────────────────────────────────────────────

function HistoryPane({ onClose }) {
  const { projects, activeId, setActiveId, loading } = useProjects();
  const navigate = useNavigate();

  const handleOpen = (p) => {
    setActiveId(p.id);
    const path = resumePathFor(p);
    onClose();
    // Defer the navigate so the dialog close animation doesn't fight router state.
    setTimeout(() => navigate(path), 30);
    toast.success(`Opened ${p.name}`);
  };

  if (loading && projects.length === 0) {
    return <div className="p-4 text-sm text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading projects…</div>;
  }
  if (!projects.length) {
    return <div className="p-4 text-sm text-slate-500">No projects yet.</div>;
  }
  return (
    <div data-testid="settings-history-pane" className="max-h-[55vh] overflow-y-auto -mx-1">
      <ul className="space-y-1.5">
        {projects.map((p) => {
          const isActive = p.id === activeId;
          const status = p.stage_status || {};
          const frozen = STAGE_ORDER.filter((k) => status[k] === "frozen").length;
          const lastVisited = (() => {
            try { return localStorage.getItem(lsKey(p.id)); } catch (_) { return null; }
          })();
          const resumeLabel = (() => {
            const path = lastVisited || resumePathFor(p);
            const key = Object.keys(STAGE_PATHS).find((k) => STAGE_PATHS[k] === path);
            return key || "Discovery";
          })();
          return (
            <li key={p.id}>
              <button
                data-testid={`history-project-${p.id}`}
                onClick={() => handleOpen(p)}
                className={`w-full text-left px-3 py-2.5 rounded-sm border flex items-start gap-3 ${
                  isActive
                    ? "border-[#FFE600] bg-[#FFFCE6]"
                    : "border-[#E6E6E6] bg-white hover:bg-slate-50"
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] font-semibold text-slate-900 truncate flex items-center gap-2">
                    {p.name}
                    {isActive && (
                      <span className="text-[9px] uppercase tracking-wider bg-[#FFE600] text-[#2E2E38] px-1.5 py-0.5 rounded-sm">
                        Active
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] text-slate-500 mt-0.5 truncate">
                    {p.source_tech || "—"} <span className="text-slate-400">→</span> {p.target_tech || "—"}
                  </div>
                  <div className="flex items-center gap-3 mt-1.5 text-[10px] text-slate-500">
                    <span className="flex items-center gap-1">
                      <CheckCircle2 className="w-3 h-3 text-emerald-600" /> {frozen}/5 frozen
                    </span>
                    <span className="flex items-center gap-1">
                      <ArrowRight className="w-3 h-3" /> Resume at <b className="text-slate-700">{resumeLabel}</b>
                    </span>
                  </div>
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function AutoSavePane() {
  const [enabled, setEnabled] = useState(() => {
    try { return localStorage.getItem(AUTOSAVE_KEY) !== "off"; } catch (_) { return true; }
  });
  const toggle = () => {
    const next = !enabled;
    setEnabled(next);
    try { localStorage.setItem(AUTOSAVE_KEY, next ? "on" : "off"); } catch (_) { /* ignore */ }
    toast.success(`Auto-save ${next ? "enabled" : "disabled"}`);
  };
  const clearHistory = () => {
    if (!window.confirm("Clear all per-project last-visited-stage history? (Project data is NOT deleted.)")) return;
    try {
      const remove = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith("lama:lastStage:")) remove.push(k);
      }
      remove.forEach((k) => localStorage.removeItem(k));
      toast.success(`Cleared ${remove.length} entr${remove.length === 1 ? "y" : "ies"}.`);
    } catch (_) { /* ignore */ }
  };
  return (
    <div data-testid="settings-autosave-pane" className="space-y-4 p-1">
      <div className="flex items-start justify-between gap-4 p-3 border border-[#E6E6E6] rounded-sm">
        <div>
          <div className="text-[13px] font-semibold flex items-center gap-2">
            <Save className="w-4 h-4" /> Auto-save current stage
          </div>
          <div className="text-[11px] text-slate-500 mt-1 leading-snug max-w-md">
            When ON, LAMA remembers the last stage you visited in each project so
            you can resume from where you left off via <b>History</b>. All stage
            artifacts (KB, SRS, DDL, etc.) are <i>always</i> persisted on the
            server regardless of this setting — this only governs the
            "resume-at" UX hint.
          </div>
        </div>
        <button
          data-testid="autosave-toggle"
          onClick={toggle}
          className={`relative inline-flex h-5 w-9 items-center rounded-full transition shrink-0 ${
            enabled ? "bg-[#7C3AED]" : "bg-slate-300"
          }`}
          aria-pressed={enabled}
        >
          <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${enabled ? "translate-x-4" : "translate-x-0.5"}`} />
        </button>
      </div>
      <button
        data-testid="autosave-clear"
        onClick={clearHistory}
        className="text-[12px] text-slate-600 underline hover:text-slate-900"
      >
        Clear remembered stage history
      </button>
    </div>
  );
}

function RefreshAppPane({ onTrigger, busy }) {
  return (
    <div data-testid="settings-refresh-pane" className="space-y-4 p-1">
      <div className="p-3 border border-[#FFE600] bg-[#FFFCE6] rounded-sm">
        <div className="text-[13px] font-semibold flex items-center gap-2">
          <RefreshCw className="w-4 h-4" /> Refresh App
        </div>
        <div className="text-[11px] text-slate-700 mt-1 leading-snug">
          DELETES the active project's Knowledge Base, SRS, Discovery chat, Data
          Model artifacts, stage context, vectors, and audit log — then reloads
          the UI. The project itself is kept; all stages return to locked.
          <b className="text-red-700"> Cannot be undone.</b>
        </div>
        <button
          data-testid="refresh-app-confirm"
          onClick={onTrigger}
          disabled={busy}
          className="mt-3 px-3 py-1.5 bg-[#2E2E38] text-white text-[12px] font-semibold rounded-sm hover:bg-black disabled:opacity-50 flex items-center gap-1.5"
        >
          {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          Refresh active project
        </button>
      </div>
    </div>
  );
}

// iter-13.59 — Target stack is chosen AFTER Build KB on the Discovery page
// via the TargetStackSuggester (top-3 recommendations + "Others" picker).
// The New Project form intentionally asks for nothing but a name.

function NewProjectPane({ onClose }) {
  const { setActiveId, refresh } = useProjects();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      toast.error("Project name is required");
      return;
    }
    setBusy(true);
    try {
      const proj = await createProject({
        name: trimmed,
        // Both stacks are deferred:
        //   source_tech ← auto-detected by tech_detector on Build KB.
        //   target_tech ← chosen by the user from the suggester after Build KB.
        source_tech: "",
        target_tech: "",
        description: description.trim(),
      });
      await refresh();
      setActiveId(proj.id);
      onClose();
      // Defer the navigate so the dialog close animation doesn't fight router state.
      setTimeout(() => navigate("/"), 30);
      toast.success(`Created "${proj.name}" — upload legacy files in Discovery to begin.`);
    } catch (err) {
      const msg = err?.response?.data?.detail || err?.message || "Failed to create project";
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form data-testid="settings-newproject-pane" onSubmit={submit} className="space-y-4">
      <div className="p-3 border border-[#FFE600] bg-[#FFFCE6] rounded-sm text-[12px] text-slate-700 leading-snug">
        Spin up a brand new project with an empty pipeline. The new project
        becomes the active one and you'll land on <b>Discovery</b> to upload
        legacy files. Existing projects stay intact and are reachable from
        the <b>History</b> tab.
      </div>

      <div>
        <label htmlFor="np-name" className="mos-label">Project name</label>
        <input
          id="np-name"
          data-testid="newproject-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Order Management Modernisation"
          maxLength={120}
          required
          className="w-full mt-1 px-3 py-2 border border-[#E6E6E6] rounded-sm text-[13px]"
        />
      </div>

      <div className="p-3 border border-[#E6E6E6] bg-slate-50 rounded-sm text-[11px] text-slate-600 leading-snug space-y-1.5">
        <div>
          <b className="text-slate-700">Source stack is auto-detected</b>{" "}
          from the legacy code you upload during <b>Build KB</b> in Discovery.
          LAMA scans file extensions, framework markers (Struts, Spring, EF,
          etc.), and SQL dialect to fingerprint the stack automatically.
        </div>
        <div>
          <b className="text-slate-700">Target stack is chosen after Build KB</b>{" "}
          — once the Knowledge Base is built, LAMA recommends the top-3 modern
          target stacks (e.g. Spring Boot 3 / FastAPI / .NET 8). You pick one,
          or click <b>Others</b> to assemble your own from a tech catalog
          (backend language &amp; framework, database, frontend, architecture
          pattern).
        </div>
      </div>

      <div>
        <label htmlFor="np-desc" className="mos-label">Description (optional)</label>
        <textarea
          id="np-desc"
          data-testid="newproject-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="One-line summary of the legacy app and migration goal."
          className="w-full mt-1 px-3 py-2 border border-[#E6E6E6] rounded-sm text-[13px]"
        />
      </div>

      <div className="flex items-center gap-2 pt-2">
        <button
          type="submit"
          data-testid="newproject-create"
          disabled={busy}
          className="px-4 py-2 bg-[#2E2E38] text-white text-[12px] font-semibold rounded-sm hover:bg-black disabled:opacity-50 flex items-center gap-1.5"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FilePlus2 className="w-3.5 h-3.5" />}
          Create &amp; Open
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-2 text-[12px] text-slate-600 hover:text-slate-900"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}


// iter-13.88 — Delete Project pane. Hard-deletes a project AND every
// artefact referencing it (KB, SRS, chat, stage outputs, codegen tree,
// audit-able stage history, GitHub config, Qdrant vectors, on-disk
// clone). Asks the user to type the project name as confirmation so the
// destructive call cannot fire by accident.
function DeleteProjectPane({ onClose }) {
  const { projects, activeId, setActiveId, refresh } = useProjects();
  const [targetId, setTargetId] = useState(activeId || "");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Default the selector to whatever project is currently active.
    if (activeId && !targetId) setTargetId(activeId);
  }, [activeId, targetId]);

  const target = useMemo(
    () => (projects || []).find((p) => p.id === targetId) || null,
    [projects, targetId],
  );
  const nameMatches = !!target && typed.trim() === target.name;

  const handleDelete = async () => {
    if (!target || !nameMatches || busy) return;
    setBusy(true);
    try {
      const r = await deleteProject(target.id);
      const kbCounts = Object.entries(r?.kb_purge || {})
        .filter(([, v]) => typeof v === "number" && v > 0)
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      toast.success(`Deleted project "${target.name}"`, {
        description: kbCounts ? `Purged: ${kbCounts}` : "All references removed.",
        duration: 8000,
      });
      // Drop the active pointer if we just nuked the active project.
      if (activeId === target.id) {
        setActiveId(null);
        try {
          localStorage.removeItem(`lama:lastStage:${target.id}`);
        } catch (_) { /* ignore */ }
      }
      await refresh();
      onClose();
    } catch (e) {
      toast.error("Delete failed", {
        description: e?.response?.data?.detail || e?.message || "Unknown error",
      });
    } finally {
      setBusy(false);
    }
  };

  if (!projects || projects.length === 0) {
    return (
      <div data-testid="settings-delete-pane" className="p-4 text-sm text-slate-500">
        No projects to delete.
      </div>
    );
  }

  return (
    <div data-testid="settings-delete-pane" className="space-y-4 p-1">
      <div className="p-3 border border-red-300 bg-red-50 rounded-sm">
        <div className="text-[13px] font-semibold flex items-center gap-2 text-red-800">
          <AlertTriangle className="w-4 h-4" /> Delete project
        </div>
        <div className="text-[11px] text-red-900/90 mt-1 leading-snug">
          Permanently removes the project AND every reference to it — the
          knowledge base, chat history, SRS, data model, architecture
          documents, code-gen tree, freeze gates, stage context, GitHub
          configuration, integrations, vector index, audit-tracked
          artefacts, and any on-disk git clone for this project.
          <b className="block mt-1 text-red-700">This cannot be undone.</b>
        </div>
      </div>

      <div className="space-y-2">
        <label htmlFor="dp-target" className="mos-label">Project</label>
        <select
          id="dp-target"
          data-testid="delete-project-select"
          value={targetId}
          onChange={(e) => { setTargetId(e.target.value); setTyped(""); }}
          className="w-full px-3 py-2 border border-[#E6E6E6] rounded-sm text-[13px] bg-white"
        >
          <option value="">— Choose a project —</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}{p.id === activeId ? "  (active)" : ""}
            </option>
          ))}
        </select>
      </div>

      {target && (
        <div className="space-y-2">
          <label htmlFor="dp-confirm" className="mos-label">
            Type <span className="font-mono bg-slate-100 px-1.5 py-0.5 rounded-sm border border-slate-200">{target.name}</span> to confirm
          </label>
          <input
            id="dp-confirm"
            data-testid="delete-project-confirm-input"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={target.name}
            autoComplete="off"
            className="w-full px-3 py-2 border border-[#E6E6E6] rounded-sm text-[13px] font-mono"
          />
        </div>
      )}

      <div className="flex items-center gap-2 pt-2">
        <button
          data-testid="delete-project-confirm"
          onClick={handleDelete}
          disabled={!target || !nameMatches || busy}
          className="px-4 py-2 bg-red-600 text-white text-[12px] font-semibold rounded-sm hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
          Delete project permanently
        </button>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-2 text-[12px] text-slate-600 hover:text-slate-900"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}


function StatisticsPane() {  const { activeId, projects } = useProjects();
  const [days, setDays] = useState(7);
  // Scope toggle: "active" → only the current project; "all" → every project
  // (drives the project-wise chart). Default to "active" so existing
  // single-project users see the same numbers as before.
  const [scope, setScope] = useState("active");
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // iter-16.x — Gap Analyzer / Code Transformer projects don't run the
  // legacy 5-stage pipeline; the backend tags every one of their LLM
  // calls `stage: "Tools"`, so the per-stage chart used to collapse all
  // of their usage into one bucket (or show four permanently-zero
  // legacy rows). When the active project is one of those types, group
  // by `by_agent` instead — it's already returned by the backend and
  // gives a real breakdown (Coder / Verifier / Tester / …).
  const activeProject = useMemo(
    () => (projects || []).find((p) => p.id === activeId) || null,
    [projects, activeId],
  );
  const activeProjectType = activeProject?.project_type || "legacy_migration";
  const agentDrivenView = scope === "active" && isAgentDrivenProjectType(activeProjectType);

  const load = useCallback(async () => {
    setBusy(true); setErr("");
    try {
      // Pass empty project_id when scope=all so the backend aggregates
      // across every project. The by_project breakdown is always returned.
      const d = await getUsageSummary(scope === "all" ? "" : activeId, days);
      setData(d);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "Failed to load usage");
    } finally {
      setBusy(false);
    }
  }, [activeId, days, scope]);

  useEffect(() => { load(); }, [load]);

  // iter-16.x — Aggregate `by_project` (already scope-filtered) by each
  // project's `project_type` so operators can see, at a glance, how much
  // of their token spend is Modernization vs Gap Analyzer vs Code
  // Transformer work — regardless of which single project is selected.
  const projectTypeChartData = useMemo(() => {
    const byId = new Map((projects || []).map((p) => [p.id, p.project_type || "legacy_migration"]));
    const buckets = {};
    (data?.by_project || []).forEach((r) => {
      const pt = byId.get(r.project_id) || "legacy_migration";
      const b = buckets[pt] || (buckets[pt] = { type: pt, tokens: 0, cost: 0, projects: 0 });
      b.tokens += r.tokens || 0;
      b.cost += r.cost || 0;
      b.projects += 1;
    });
    return Object.values(buckets).sort((a, b) => b.tokens - a.tokens);
  }, [data, projects]);
  const projectTypeTotal = useMemo(
    () => projectTypeChartData.reduce((n, r) => n + (r.tokens || 0), 0),
    [projectTypeChartData],
  );

  const stageChartData = useMemo(() => {
    if (agentDrivenView) {
      return (data?.by_agent || [])
        .map((r) => ({
          stage: labelForAgentKey(r.agent_key),
          tokens: r.tokens || 0,
          cost: Number((r.cost || 0).toFixed(4)),
          color: colorForAgentKey(r.agent_key),
        }))
        .filter((r) => r.tokens > 0)
        .sort((a, b) => b.tokens - a.tokens);
    }
    const rows = (data?.by_stage || []).map((r) => {
      const raw = r.stage || "unknown";
      // Relabel the backend's catch-all "unknown" bucket (KB build, chat,
      // ontology, anything fired before a stage is set) to "Knowledge Base"
      // so the chart axis is meaningful to a non-developer audience.
      const stage = raw === "unknown" ? UNKNOWN_STAGE_LABEL : raw;
      return {
        stage,
        tokens: r.tokens || 0,
        cost: Number((r.cost || 0).toFixed(4)),
      };
    });
    // Stable ordering: Knowledge Base first, then the 5 pipeline stages,
    // then anything we don't recognise at the end.
    const order = [UNKNOWN_STAGE_LABEL, ...STAGE_ORDER];
    rows.sort((a, b) => {
      const ai = order.indexOf(a.stage);
      const bi = order.indexOf(b.stage);
      const ax = ai === -1 ? 99 : ai;
      const bx = bi === -1 ? 99 : bi;
      return ax - bx;
    });
    return rows;
  }, [data, agentDrivenView]);

  const projectChartData = useMemo(() => {
    if (!data?.by_project_stage) return { rows: [], stages: [] };
    const liveNames = new Map((projects || []).map((p) => [p.id, p.name]));

    // Discover every stage that contributed at least one token across all
    // projects in this window; that is the union of stacked series we need
    // to render. Sort using the same canonical order as the per-stage chart
    // (Knowledge Base first, then the 5 pipeline stages, then anything else).
    const seenStages = new Set();
    data.by_project_stage.forEach((p) => {
      Object.keys(p.stages || {}).forEach((s) => {
        // Relabel "unknown" → "Knowledge Base" so the legend matches the
        // stage chart above.
        seenStages.add(s === "unknown" ? UNKNOWN_STAGE_LABEL : s);
      });
    });
    const canonical = [UNKNOWN_STAGE_LABEL, ...STAGE_ORDER];
    const stages = [...seenStages].sort((a, b) => {
      const ai = canonical.indexOf(a);
      const bi = canonical.indexOf(b);
      const ax = ai === -1 ? 99 : ai;
      const bx = bi === -1 ? 99 : bi;
      return ax - bx;
    });

    // Pivot to a Recharts-friendly row shape: one object per project with
    // one numeric field per stage (zero-filled so the tooltip is complete).
    const rows = data.by_project_stage.map((p) => {
      const row = {
        project_id: p.project_id,
        name: liveNames.get(p.project_id) || p.project_name || (p.project_id ? p.project_id.slice(0, 8) : "(no project)"),
        total: p.tokens || 0,
      };
      stages.forEach((s) => { row[s] = 0; });
      Object.entries(p.stages || {}).forEach(([rawStage, tokens]) => {
        const stage = rawStage === "unknown" ? UNKNOWN_STAGE_LABEL : rawStage;
        row[stage] = (row[stage] || 0) + (tokens || 0);
      });
      return row;
    }).sort((a, b) => b.total - a.total);

    return { rows, stages };
  }, [data, projects]);

  // iter-13.58 — Pre-compute totals so the donut/pie chart and the
  // custom tooltips can show share-of-total percentages.
  const stageTotalTokens = useMemo(
    () => stageChartData.reduce((n, r) => n + (r.tokens || 0), 0),
    [stageChartData],
  );

  // iter-13.58 — Stable gradient ids keyed by stage so the same gradient
  // is referenced by both the donut and the bar charts (one <defs> block
  // per chart still — recharts isolates them — but consistent naming).
  const gradId = (stage) => `lama-grad-${stage.replace(/[^a-z0-9]/gi, "-")}`;
  // iter-16.x — In agent-driven mode each row already carries its own
  // `color` (assigned by colorForAgentKey); fall back to the legacy
  // stage palette otherwise.
  const stageColorMap = useMemo(() => {
    const m = {};
    stageChartData.forEach((r) => { if (r.color) m[r.stage] = r.color; });
    return m;
  }, [stageChartData]);
  const stageBaseColor = (stage) =>
    stageColorMap[stage] || STAGE_COLORS[stage] || STAGE_COLORS[UNKNOWN_STAGE_LABEL];

  // Lighter "highlight" tone for the top of each gradient.
  const lighten = (hex, amt = 0.35) => {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
    if (!m) return hex;
    const mix = (c) =>
      Math.round(parseInt(c, 16) + (255 - parseInt(c, 16)) * amt)
        .toString(16).padStart(2, "0");
    return `#${mix(m[1])}${mix(m[2])}${mix(m[3])}`;
  };

  // Custom tooltip — accent strip on the left, mono numbers, soft shadow.
  const FancyTooltip = ({ active, payload, label, total }) => {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((p) => (p.value || 0) > 0);
    if (rows.length === 0) return null;
    const head = label || payload[0]?.payload?.stage || "";
    const grand = total ?? rows.reduce((n, r) => n + (r.value || 0), 0);
    return (
      <div
        className="rounded-md bg-white/95 backdrop-blur-sm border border-slate-200 shadow-xl text-[12px] overflow-hidden"
        style={{ minWidth: 180 }}
      >
        <div className="px-3 py-1.5 bg-gradient-to-r from-slate-900 to-slate-700 text-white text-[11px] font-semibold tracking-wide">
          {head}
        </div>
        <div className="px-3 py-2 space-y-1">
          {rows.map((r) => {
            const pct = grand > 0 ? (r.value / grand) * 100 : 0;
            const c = r.color || r.payload?.fill || stageBaseColor(r.dataKey);
            return (
              <div key={r.dataKey} className="flex items-center gap-2">
                <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: c }} />
                <span className="flex-1 text-slate-700 truncate">{r.name || r.dataKey}</span>
                <span className="font-mono tabular-nums text-slate-900">
                  {Number(r.value).toLocaleString()}
                </span>
                <span className="font-mono tabular-nums text-slate-500 text-[10px] w-10 text-right">
                  {pct.toFixed(1)}%
                </span>
              </div>
            );
          })}
          {payload[0]?.payload?.total != null && (
            <div className="pt-1 mt-1 border-t border-slate-100 flex items-center justify-between text-[11px]">
              <span className="text-slate-500">Total</span>
              <span className="font-mono tabular-nums font-semibold text-slate-900">
                {Number(payload[0].payload.total).toLocaleString()}
              </span>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div data-testid="settings-stats-pane" className="space-y-3">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 text-[12px] text-slate-600">
          <span>Window:</span>
          {[1, 7, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-2 py-1 rounded-sm border text-[11px] transition-all ${
                days === d
                  ? "border-[#2E2E38] bg-gradient-to-b from-[#3a3a44] to-[#2E2E38] text-white shadow-sm"
                  : "border-[#E6E6E6] hover:bg-slate-50"
              }`}
            >
              {d === 1 ? "Today" : `${d}d`}
            </button>
          ))}
          <span className="ml-3">Scope:</span>
          {[{ k: "active", l: "Active project" }, { k: "all", l: "All projects" }].map((opt) => (
            <button
              key={opt.k}
              data-testid={`stats-scope-${opt.k}`}
              onClick={() => setScope(opt.k)}
              className={`px-2 py-1 rounded-sm border text-[11px] transition-all ${
                scope === opt.k
                  ? "border-[#2E2E38] bg-gradient-to-b from-[#3a3a44] to-[#2E2E38] text-white shadow-sm"
                  : "border-[#E6E6E6] hover:bg-slate-50"
              }`}
            >
              {opt.l}
            </button>
          ))}
        </div>
        <button
          onClick={load} disabled={busy}
          className="text-[11px] text-slate-500 hover:text-slate-900 disabled:opacity-50 flex items-center gap-1"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          Refresh
        </button>
      </div>

      {err && <div className="text-[12px] text-red-700">{err}</div>}

      {/* iter-16.x — Which project type is this window scoped to? Makes
          the KPI tiles below unambiguous the moment a Gap Analyzer or
          Code Transformer project is active (their token profile looks
          very different from a legacy migration and shouldn't be read
          against the same "Discovery/DataModel/…" mental model). */}
      {scope === "active" && activeProject && (
        <div
          className="flex items-center gap-2 text-[11px] text-slate-600"
          data-testid="stats-active-project-type"
        >
          <span
            className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border font-semibold"
            style={{
              borderColor: `${projectTypeMeta(activeProjectType).color}55`,
              background: `${projectTypeMeta(activeProjectType).color}14`,
              color: projectTypeMeta(activeProjectType).color,
            }}
          >
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: projectTypeMeta(activeProjectType).color }} />
            {projectTypeMeta(activeProjectType).label}
          </span>
          <span className="truncate text-slate-500">{activeProject.name}</span>
        </div>
      )}

      {/* iter-13.58 — KPI tiles redesigned: gradient sheen + accent strip
          on the left, larger value, subtle hover lift. */}
      <div className="grid grid-cols-3 gap-2">
        {[
          { label: "Total tokens", value: (data?.total_tokens || 0).toLocaleString(),
            from: "#FDE68A", to: "#FFE600", accent: "#FFE600" },
          { label: "Cost (USD)", value: `$${(data?.total_cost_usd || 0).toFixed(4)}`,
            from: "#C7D2FE", to: "#7C3AED", accent: "#7C3AED" },
          { label: "LLM calls", value: (data?.total_runs || 0).toLocaleString(),
            from: "#A7F3D0", to: "#10B981", accent: "#10B981" },
        ].map((tile) => (
          <div
            key={tile.label}
            className="relative overflow-hidden rounded-md border border-slate-200 bg-white p-3 transition-shadow hover:shadow-md"
          >
            <div
              className="absolute inset-y-0 left-0 w-1"
              style={{ background: `linear-gradient(180deg, ${tile.from}, ${tile.to})` }}
            />
            <div className="absolute -right-6 -top-6 w-20 h-20 rounded-full opacity-20"
                 style={{ background: `radial-gradient(circle, ${tile.accent} 0%, transparent 70%)` }} />
            <div className="pl-2 relative">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">{tile.label}</div>
              <div className="text-[22px] font-bold text-slate-900 mt-0.5 leading-none font-mono tabular-nums">
                {tile.value}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* iter-16.x — Tokens by project type. Aggregates the already
          scope-filtered `by_project` rows by each project's project_type
          so operators can see Modernization vs Gap Analyzer vs Code
          Transformer spend at a glance — this is the one view that's
          meaningful for every project type at once, regardless of scope. */}
      {projectTypeChartData.length > 0 && (
        <div
          className="border border-slate-200 rounded-md p-3 bg-gradient-to-br from-white to-slate-50"
          data-testid="stats-project-type-breakdown"
        >
          <div className="flex items-center justify-between mb-2">
            <div className="text-[11px] uppercase tracking-wider text-slate-500">Tokens by project type</div>
            <div className="text-[10px] text-slate-400 font-mono tabular-nums">
              {projectTypeTotal.toLocaleString()} total
            </div>
          </div>
          <div className="flex h-2.5 rounded-full overflow-hidden bg-slate-100 mb-3">
            {projectTypeChartData.map((r) => {
              const meta = projectTypeMeta(r.type);
              const pct = projectTypeTotal > 0 ? (r.tokens / projectTypeTotal) * 100 : 0;
              return (
                <div
                  key={r.type}
                  title={`${meta.label}: ${r.tokens.toLocaleString()} tokens (${pct.toFixed(1)}%)`}
                  style={{ width: `${pct}%`, background: meta.color }}
                />
              );
            })}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {projectTypeChartData.map((r) => {
              const meta = projectTypeMeta(r.type);
              const pct = projectTypeTotal > 0 ? (r.tokens / projectTypeTotal) * 100 : 0;
              return (
                <div
                  key={r.type}
                  className="rounded-md border border-slate-200 bg-white p-2.5"
                  data-testid={`stats-project-type-${r.type}`}
                >
                  <div className="flex items-center gap-1.5 text-[11px] font-semibold" style={{ color: meta.color }}>
                    <span className="w-2 h-2 rounded-full" style={{ background: meta.color }} />
                    {meta.label}
                  </div>
                  <div className="font-mono text-[16px] font-bold text-slate-900 mt-0.5 tabular-nums">
                    {r.tokens.toLocaleString()}
                    <span className="text-[10px] text-slate-400 font-normal"> tok · {pct.toFixed(1)}%</span>
                  </div>
                  <div className="text-[10px] text-slate-500 font-mono">
                    ${r.cost.toFixed(4)} · {r.projects} project{r.projects === 1 ? "" : "s"}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* iter-13.58 — Tokens-by-stage redesigned as a 2-column split:
          left = donut (share-of-total), right = bar chart with gradient
          fills. Both charts use shared gradient ids and the FancyTooltip. */}
      <div className="border border-slate-200 rounded-md p-3 bg-gradient-to-br from-white to-slate-50">
        <div className="flex items-center justify-between mb-2">
          <div className="text-[11px] uppercase tracking-wider text-slate-500">
            {agentDrivenView ? "Tokens by agent" : "Tokens by stage"}
          </div>
          <div className="text-[10px] text-slate-400 font-mono tabular-nums">
            {stageTotalTokens.toLocaleString()} total
          </div>
        </div>
        {stageChartData.length === 0 ? (
          <div className="text-[12px] text-slate-500 py-10 text-center">
            No LLM activity in this window yet.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-5 gap-3 items-center">
            {/* Donut */}
            <div className="md:col-span-2" style={{ width: "100%", height: 220 }}>
              <ResponsiveContainer>
                <PieChart>
                  <defs>
                    {stageChartData.map((e) => (
                      <linearGradient key={`pg-${e.stage}`} id={`pg-${gradId(e.stage)}`} x1="0" y1="0" x2="1" y2="1">
                        <stop offset="0%" stopColor={lighten(stageBaseColor(e.stage), 0.5)} />
                        <stop offset="100%" stopColor={stageBaseColor(e.stage)} />
                      </linearGradient>
                    ))}
                  </defs>
                  <Pie
                    data={stageChartData}
                    dataKey="tokens"
                    nameKey="stage"
                    innerRadius={48}
                    outerRadius={80}
                    paddingAngle={2}
                    stroke="#fff"
                    strokeWidth={2}
                    isAnimationActive
                  >
                    {stageChartData.map((entry) => (
                      <Cell key={entry.stage} fill={`url(#pg-${gradId(entry.stage)})`} />
                    ))}
                  </Pie>
                  <Tooltip content={<FancyTooltip total={stageTotalTokens} />} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            {/* Bar */}
            <div className="md:col-span-3" style={{ width: "100%", height: 220 }}>
              <ResponsiveContainer>
                <BarChart data={stageChartData} margin={{ top: 18, right: 12, bottom: 4, left: 0 }}>
                  <defs>
                    {stageChartData.map((e) => (
                      <linearGradient key={`bg-${e.stage}`} id={`bg-${gradId(e.stage)}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={lighten(stageBaseColor(e.stage), 0.45)} />
                        <stop offset="100%" stopColor={stageBaseColor(e.stage)} />
                      </linearGradient>
                    ))}
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#EEF2F7" vertical={false} />
                  <XAxis
                    dataKey="stage"
                    tick={{ fontSize: 11, fill: "#475569" }}
                    axisLine={{ stroke: "#E2E8F0" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "#94A3B8" }}
                    width={50}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v) => v >= 1000 ? `${Math.round(v / 1000)}k` : v}
                  />
                  <Tooltip
                    cursor={{ fill: "#F1F5F9", opacity: 0.6 }}
                    content={<FancyTooltip total={stageTotalTokens} />}
                  />
                  <Bar dataKey="tokens" radius={[6, 6, 0, 0]} isAnimationActive animationDuration={700}>
                    {stageChartData.map((entry) => (
                      <Cell key={entry.stage} fill={`url(#bg-${gradId(entry.stage)})`} />
                    ))}
                    <LabelList
                      dataKey="tokens"
                      position="top"
                      formatter={(v) => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}
                      style={{ fontSize: 10, fill: "#475569", fontWeight: 600 }}
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </div>

      {/* Project-wise stacked chart — each bar shows a project's total
          tokens broken down by stage. Only meaningful when scope spans
          all projects, or when the active project has cross-stage data. */}
      <div data-testid="stats-project-chart" className="border border-slate-200 rounded-md p-3 bg-gradient-to-br from-white to-slate-50">
        <div className="flex items-center justify-between mb-2">
          <div className="text-[11px] uppercase tracking-wider text-slate-500">Tokens by project (stacked by stage)</div>
          {scope === "active" && projectChartData.rows.length <= 1 && (
            <button
              onClick={() => setScope("all")}
              className="text-[10px] text-[#7C3AED] hover:underline"
            >
              Switch to "All projects" to compare →
            </button>
          )}
        </div>
        {projectChartData.rows.length === 0 ? (
          <div className="text-[12px] text-slate-500 py-10 text-center">
            No project-level activity in this window.
          </div>
        ) : (
          <div style={{ width: "100%", height: Math.max(220, projectChartData.rows.length * 40 + 80) }}>
            <ResponsiveContainer>
              <BarChart
                data={projectChartData.rows}
                layout="vertical"
                margin={{ top: 8, right: 56, bottom: 4, left: 8 }}
                barCategoryGap={10}
              >
                <defs>
                  {projectChartData.stages.map((s) => (
                    <linearGradient key={`pj-${s}`} id={`pj-${gradId(s)}`} x1="0" y1="0" x2="1" y2="0">
                      <stop offset="0%" stopColor={lighten(stageBaseColor(s), 0.4)} />
                      <stop offset="100%" stopColor={stageBaseColor(s)} />
                    </linearGradient>
                  ))}
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#EEF2F7" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fontSize: 11, fill: "#94A3B8" }}
                  axisLine={false} tickLine={false}
                  tickFormatter={(v) => v >= 1000 ? `${Math.round(v / 1000)}k` : v}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  tick={{ fontSize: 11, fill: "#475569" }}
                  width={140}
                  axisLine={{ stroke: "#E2E8F0" }}
                  tickLine={false}
                />
                <Tooltip
                  cursor={{ fill: "#F1F5F9", opacity: 0.5 }}
                  content={<FancyTooltip />}
                />
                <Legend
                  wrapperStyle={{ fontSize: 11, paddingTop: 4 }}
                  iconType="circle"
                  iconSize={8}
                />
                {projectChartData.stages.map((stage, idx) => {
                  const isLast = idx === projectChartData.stages.length - 1;
                  return (
                    <Bar
                      key={stage}
                      dataKey={stage}
                      stackId="tokens"
                      fill={`url(#pj-${gradId(stage)})`}
                      radius={isLast ? [0, 6, 6, 0] : [0, 0, 0, 0]}
                      isAnimationActive
                      animationDuration={700}
                    >
                      {isLast && (
                        <LabelList
                          dataKey="total"
                          position="right"
                          formatter={(v) => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}
                          style={{ fontSize: 10, fill: "#0F172A", fontWeight: 700 }}
                        />
                      )}
                    </Bar>
                  );
                })}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {stageChartData.length > 0 && (
        <div className="border border-slate-200 rounded-md overflow-hidden">
          <table className="w-full text-[12px]">
            <thead className="bg-gradient-to-b from-slate-50 to-white text-slate-600">
              <tr>
                <th className="text-left px-3 py-1.5 font-semibold">{agentDrivenView ? "Agent" : "Stage"}</th>
                <th className="text-right px-3 py-1.5 font-semibold">Tokens</th>
                <th className="text-right px-3 py-1.5 font-semibold">Share</th>
                <th className="text-right px-3 py-1.5 font-semibold">Cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {stageChartData.map((r) => {
                const pct = stageTotalTokens > 0 ? (r.tokens / stageTotalTokens) * 100 : 0;
                const c = stageBaseColor(r.stage);
                return (
                  <tr key={r.stage} className="border-t border-slate-100 hover:bg-slate-50">
                    <td className="px-3 py-1.5">
                      <span className="inline-block w-2.5 h-2.5 rounded-full mr-2 align-middle" style={{ background: c }} />
                      {r.stage}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono tabular-nums">{r.tokens.toLocaleString()}</td>
                    <td className="px-3 py-1.5 text-right">
                      <div className="inline-flex items-center gap-1.5">
                        <span className="font-mono tabular-nums text-slate-600 text-[11px] w-9 text-right">{pct.toFixed(1)}%</span>
                        <span className="inline-block h-1.5 w-16 bg-slate-100 rounded-full overflow-hidden">
                          <span className="block h-full rounded-full" style={{ width: `${Math.min(100, pct)}%`, background: c }} />
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono tabular-nums">${r.cost.toFixed(4)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Help / User Manual ────────────────────────────────────────────────────

const MANUAL_SECTIONS = [
  {
    id: "overview",
    title: "1. What is LAMA?",
    body: [
      "LAMA (Legacy Application Modernization & Alignment) is an AI-assisted studio that walks a legacy application (PHP/CodeIgniter, JSP, .NET, Python, JS, etc.) through a deterministic 5-stage migration pipeline to a cloud-native target stack.",
      "The reference pilot migrates PHP 8 / CodeIgniter 4 / MariaDB → FastAPI / Python 3.12 / PostgreSQL. Every stage produces freezable, GitHub-pushable artifacts that feed the next stage.",
    ],
  },
  {
    id: "pipeline",
    title: "2. The 5-stage pipeline",
    body: [
      "Stages are strictly sequential. You must FREEZE stage N before stage N+1 becomes available. Freeze gates require typing FREEZE / RESET as a safety confirmation.",
      "Stage 1 — Discovery: Build KB from your legacy source (folder or ZIP), generate the IEEE-830 SRS (12 sections), and enrich the Business Ontology. Freeze unlocks DataModel.",
      "Stage 2 — DataModel: Generates OLTP DDL, OLAP DDL, Bus Matrix, and 3 migration scripts (schema, data, verification). Freeze unlocks Architecture.",
      "Stage 3 — Architecture: Produces Service Map, HLD, LLD, API Contracts, and Sequence Diagrams. Uses background jobs (2s polling — no SSE, K8s ingress limits). Freeze unlocks CodeGen.",
      "Stage 4 — CodeGen: Per-service source tree, Dockerfiles, downloadable ZIP, optional GitHub push. Freeze unlocks Living.",
      "Stage 5 — Living: Selenium tests, SRS drift detection, runtime observability.",
    ],
  },
  {
    id: "quickstart",
    title: "3. Quick start (first project)",
    body: [
      "Step 1 — Settings → New Project: enter a project name.",
      "Step 2 — Discovery page → Upload Panel: upload a ZIP or point at a folder containing your legacy source.",
      "Step 3 — Click Build KB. Watch the progress dialog; the KB Health badge turns green when done.",
      "Step 4 — Pick a target stack from the Target Stack Suggester (top-3 auto-recommended, plus 'Others').",
      "Step 5 — Chat with the Discovery agent to refine understanding, or click Generate SRS. Review each of the 12 sections.",
      "Step 6 — When SRS is complete, click Freeze Discovery (type FREEZE).",
      "Step 7 — Move to DataModel → generate OLTP/OLAP → Freeze → and so on through Architecture and CodeGen.",
    ],
  },
  {
    id: "models",
    title: "4. Model routing (Console → Model Fabric)",
    body: [
      "LAMA does NOT hard-code any LLM at call sites. Every stage call resolves its model via: agent_key → complexity tier (low/medium/high) → the active provider's routing[tier] in Console → Model Fabric.",
      "Supported providers: OpenRouter, Anthropic, OpenAI, Groq, Ollama (local + cloud), and Custom. Only one provider should have is_active=true at a time.",
      "Ollama Cloud mode: sign in with `ollama signin` on the host, pull a cloud-tagged model (e.g. qwen3-coder:480b-cloud), and set it as the Ollama provider's high tier. Zero local GPU cost.",
      "CodeGen runs at the 'high' tier by default. SRS first-pass runs at 'medium'; user-triggered regeneration runs at 'high' for more headroom.",
      "Fallback: if the configured provider returns empty or raises, LAMA falls back to env-var OpenRouter automatically (unless OPENROUTER_API_KEY is unset/invalid).",
    ],
  },
  {
    id: "backendlogs",
    title: "5. Backend logs (MiniConsole)",
    body: [
      "The MiniConsole footer (bottom-right corner) shows the currently-used model, cumulative tokens, and USD cost for the last 7 days.",
      "Click Backend logs to expand a live tail of the FastAPI server. Use the level filter (INFO/WARN/ERROR) and the free-text filter box to narrow down.",
      "Click the Maximize icon in the log toolbar to open a fullscreen viewer. Press Esc or click the X to return.",
      "Copy — grabs all currently visible lines to the clipboard. Clear — clears the client-side buffer only (the server buffer keeps history).",
    ],
  },
  {
    id: "confidence",
    title: "6. Confidence scoring",
    body: [
      "Every stage artifact (each of the 12 SRS sections, each DataModel artifact, each Architecture artifact, each generated code file) is scored 0–100 by an LLM-as-judge. The score answers: 'How likely is this artifact to be correct, complete, and grounded in the KB?' A confidence pill appears next to each artifact and next to the stage badge itself.",
      "Bands (single source of truth — see backend/confidence.py::band_of): ≥95% = Excellent (green), ≥85% = Good (blue), ≥70% = Moderate (amber), <70% = Poor (red). The pill's colour + label reflects the band; hover shows the numeric score.",
      "How it's computed — multi-model judge: LAMA sends the artifact + the relevant KB slice (TOON) + the section prompt to 2–3 different judge models (chosen from the active Console provider's routing tiers) and averages their verdicts. This is why the score you see is more trustworthy than a single-model self-eval.",
      "Where to find it: on each stage page (Discovery/DataModel/Architecture/CodeGen) look for the confidence pill next to the stage title. testids: `stage-confidence-{stage}`, `stage-confidence-pill-{stage}`. Click the pill to open the detail panel; right-click (or use the recompute button in the panel) to score again from scratch.",
      "High-water mark: `best_overall_score` is preserved across recomputes — a fresh run will NEVER lower the displayed 'best' number, only add to it. The 'latest' number in the popover shows the most recent run so you can spot regressions.",
      "Freeze gates and confidence: SRS freeze is guarded by a per-section threshold — mean per-section confidence must be ≥95% (Excellent band). Sections below threshold are flagged in the SRS panel with an 'Improve' button that regenerates them via the 'high' tier model. Regeneration uses more headroom (typically Opus/high) than the first-pass (Sonnet/medium).",
      "CodeGen has an iterative validate → regenerate loop: each generated file is scored, and any file below the CodeGen threshold is auto-regenerated up to MAX_AUTO_RETRIES times. The final report shows 'Final confidence: XX.XX%'.",
      "Optional enforcement: set the env-var LAMA_BR_ENFORCE=1 to hard-block SRS/Architecture/CodeGen freeze when Business-Requirements coverage falls below LAMA_BR_MIN_COVERAGE (default 100).",
      "Recompute a stage: click the confidence pill → 'Recompute'. This starts a background job (poll via /api/pipeline/{pid}/confidence/jobs/{jid}) — you can navigate away and come back; the pill turns amber while running.",
      "Troubleshooting confidence: (a) '0%' with no rationale usually means the judge returned malformed JSON — the row is retried automatically; check backend logs. (b) 'Poor' band on a section you know is correct → try 'Improve' (regenerate at 'high' tier) OR recompute (may have been a transient judge error). (c) Score not moving despite regeneration → the underlying KB may be missing evidence; go back to Discovery and re-run Build KB with a wider skip-pattern.",
    ],
  },
  {
    id: "settings",
    title: "7. Settings panes",
    body: [
      "New Project — create a fresh project (asks for name only; target stack is picked later on Discovery).",
      "History — reopen any prior project at its last-visited stage.",
      "Auto-save — toggle the 'resume-at' UX hint (server state is always saved regardless).",
      "Refresh App — destructive wipe: deletes the active project's KB, SRS, chat, DataModel artifacts, stage context, vectors, and audit log. All stages return to locked. The project itself is kept.",
      "Delete Project — permanently deletes a project and all its artifacts.",
      "Statistics — per-stage token usage and USD cost, powered by /api/console/usage/summary.",
    ],
  },
  {
    id: "troubleshooting",
    title: "8. Troubleshooting",
    body: [
      "Stage locked (HTTP 400) — the previous stage isn't frozen. Go back and Freeze it.",
      "Empty SRS section — the fabric provider returned empty. LAMA auto-falls-back to OpenRouter; if OPENROUTER_API_KEY is missing/invalid the section stays empty. Set a valid key or switch providers.",
      "Ollama model taking too much local resources — switch to a *-cloud tagged model in Console (needs `ollama signin`).",
      "401 cascade in logs — the active provider's API key is invalid but is_active=true. Fix the key in Console → Model Fabric or deactivate that provider.",
      "Freeze blocked by confidence — one or more sections are below the ≥95% threshold. Open the SRS panel, click 'Improve' on flagged sections, and re-check the pill.",
      "Backend won't boot — check backend logs for truncated route decorators (a historical footgun in routes/projects.py).",
      "Frontend build fails — this repo is yarn-only (corepack). Never run `npm install`.",
    ],
  },
  {
    id: "shortcuts",
    title: "9. Keyboard shortcuts & tips",
    body: [
      "Esc — closes the maximized log viewer and most dialogs.",
      "Cmd/Ctrl+K — opens the Command Palette (jump to any page or action).",
      "Right-click a confidence pill — quick recompute for that stage.",
      "The model dropdown in the Discovery chat drives the SRS generation model, and is persisted in localStorage as `lama:chat:model`.",
      "All state changes are audit-logged; view Audit Log from the sidebar.",
    ],
  },
];

function HelpPane() {
  const downloadPdf = () => {
    // Open a print-optimised popup and trigger the browser's print dialog.
    // Users select "Save as PDF" as the destination — no extra dependency.
    const w = window.open("", "_blank", "width=900,height=1000");
    if (!w) {
      toast.error("Popup blocked — allow popups for this site to download the PDF.");
      return;
    }
    const css = `
      * { box-sizing: border-box; }
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #1a1a1a; margin: 40px; line-height: 1.55; }
      h1 { color: #2E2E38; border-bottom: 3px solid #FFE600; padding-bottom: 8px; margin-top: 0; }
      h2 { color: #2E2E38; margin-top: 28px; font-size: 16px; border-left: 4px solid #FFE600; padding-left: 10px; }
      p { margin: 6px 0 10px; font-size: 12px; }
      .meta { color: #666; font-size: 11px; margin-bottom: 20px; }
      .footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #ccc; font-size: 10px; color: #888; text-align: center; }
      @media print { body { margin: 20mm; } h2 { page-break-after: avoid; } p { page-break-inside: avoid; } }
    `;
    const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>LAMA — User Manual</title><style>${css}</style></head>
<body>
  <h1>LAMA — User Manual</h1>
  <div class="meta">Legacy Application Modernization & Alignment · Generated ${new Date().toLocaleString()}</div>
  ${MANUAL_SECTIONS.map(s => `
    <h2>${s.title}</h2>
    ${s.body.map(p => `<p>${p.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</p>`).join("")}
  `).join("")}
  <div class="footer">LAMA User Manual · © LAMA Project</div>
</body></html>`;
    w.document.open();
    w.document.write(html);
    w.document.close();
    // Give the popup a beat to render before invoking print.
    setTimeout(() => {
      try { w.focus(); w.print(); } catch (_) { /* ignore */ }
    }, 300);
  };

  return (
    <div data-testid="settings-help-pane" className="space-y-4 p-1">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[15px] font-semibold flex items-center gap-2 text-[#2E2E38]">
            <HelpCircle className="w-4 h-4" /> User Manual
          </div>
          <div className="text-[11px] text-slate-600 mt-1">
            End-to-end guide to running LAMA — pipeline, model routing, logs,
            settings, and troubleshooting.
          </div>
        </div>
        <button
          data-testid="help-download-pdf"
          onClick={downloadPdf}
          className="shrink-0 px-3 py-1.5 bg-[#2E2E38] text-white text-[12px] font-semibold rounded-sm hover:bg-black flex items-center gap-1.5"
          title="Open the manual in a printable window — choose 'Save as PDF' in the print dialog"
        >
          <Download className="w-3.5 h-3.5" />
          Download PDF
        </button>
      </div>

      <div className="border border-[#E6E6E6] rounded-sm bg-white divide-y divide-[#F0F0F0]">
        {MANUAL_SECTIONS.map((s) => (
          <section key={s.id} data-testid={`help-section-${s.id}`} className="p-4">
            <h3 className="text-[13px] font-bold text-[#2E2E38] border-l-4 border-[#FFE600] pl-2 mb-2">
              {s.title}
            </h3>
            <div className="space-y-1.5">
              {s.body.map((p, i) => (
                <p key={i} className="text-[12px] text-slate-700 leading-relaxed">
                  {p}
                </p>
              ))}
            </div>
          </section>
        ))}
      </div>

      <div className="text-[10px] text-slate-500 italic px-1">
        Tip: the "Download PDF" button opens a print-optimised view. In the
        print dialog, pick "Save as PDF" as the destination.
      </div>
    </div>
  );
}

// ─── Main dialog ───────────────────────────────────────────────────────────

const TABS = [
  { key: "newproject", label: "New Project",  icon: FilePlus2 },
  { key: "history",    label: "History",      icon: History },
  { key: "autosave",   label: "Auto-save",    icon: Save },
  { key: "refresh",    label: "Refresh App",  icon: RefreshCw },
  { key: "delete",     label: "Delete Project", icon: Trash2 },
  { key: "stats",      label: "Statistics",   icon: BarChart3 },
  { key: "help",       label: "Help",         icon: HelpCircle },
];

export default function SettingsMenu({ open, onOpenChange, onRefreshApp, refreshBusy }) {
  const [tab, setTab] = useState("history");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="settings-menu-dialog"
        className="max-w-3xl p-0 gap-0 overflow-hidden"
      >
        <DialogHeader className="px-5 py-3 border-b border-[#E6E6E6]">
          <DialogTitle className="flex items-center gap-2 text-base">
            <SettingsIcon className="w-4 h-4" />
            Settings
          </DialogTitle>
        </DialogHeader>

        <div className="flex">
          {/* Tabs rail */}
          <nav className="w-44 shrink-0 border-r border-[#E6E6E6] bg-slate-50 py-2">
            {TABS.map((t) => {
              const Icon = t.icon;
              const isActive = tab === t.key;
              return (
                <button
                  key={t.key}
                  data-testid={`settings-tab-${t.key}`}
                  onClick={() => setTab(t.key)}
                  className={`w-full flex items-center gap-2 px-4 py-2 text-[13px] text-left ${
                    isActive
                      ? "bg-white text-[#2E2E38] font-semibold border-r-2 border-[#FFE600]"
                      : "text-slate-600 hover:bg-white"
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  {t.label}
                </button>
              );
            })}
          </nav>

          {/* Pane content */}
          <div className="flex-1 p-5 min-h-[360px] max-h-[70vh] overflow-y-auto">
            {tab === "newproject" && <NewProjectPane onClose={() => onOpenChange(false)} />}
            {tab === "history"    && <HistoryPane    onClose={() => onOpenChange(false)} />}
            {tab === "autosave"   && <AutoSavePane />}
            {tab === "refresh"    && <RefreshAppPane onTrigger={onRefreshApp} busy={refreshBusy} />}
            {tab === "delete"     && <DeleteProjectPane onClose={() => onOpenChange(false)} />}
            {tab === "stats"      && <StatisticsPane />}
            {tab === "help"       && <HelpPane />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}


