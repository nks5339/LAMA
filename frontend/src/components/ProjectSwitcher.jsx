import { useState, useRef, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  ChevronRight, Plus, Trash2, CheckCircle2,
  ArrowRight, Loader2, AlertTriangle, History, X,
  GitBranch, ScanSearch, Wand2, ArrowLeft,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import { createProject, deleteProject } from "@/lib/api";

/**
 * iter-14.79 — ProjectSwitcher: Migration Path shown on hover
 * iter-14.93 — New Project now offers 3 project types (Legacy Migration,
 * Gap Analyzer, Technology Transformer). Sidebar renders per-type pipeline.
 */

const PROJECT_TYPES = [
  {
    key: "legacy_migration",
    label: "Legacy Modernization",
    tagline: "PHP/JSP/.NET → FastAPI/Node/Spring",
    desc: "Full 5-stage pipeline: Discovery → DataModel → Architecture → CodeGen → Living.",
    icon: GitBranch,
    accent: "from-amber-500 to-amber-600",
    landing: "/",
  },
  {
    key: "gap_analysis",
    label: "Gap Analyzer",
    tagline: "SRS ↔ Source code coverage",
    desc: "KB-driven verification of requirements against code. Produces coverage matrix + gap report.",
    icon: ScanSearch,
    accent: "from-rose-500 to-rose-600",
    landing: "/gap-analyzer",
  },
  {
    key: "tech_transformer",
    label: "Technology Transformer",
    tagline: "Cross-stack transformation",
    desc: "KB-anchored per-file transformation between technology stacks with dependency preservation.",
    icon: Wand2,
    accent: "from-violet-500 to-violet-600",
    landing: "/transformer",
  },
];

const STAGE_PATHS = {
  Discovery: "/",
  DataModel: "/data-model",
  Architecture: "/architecture",
  CodeGen: "/code-gen",
  Living: "/living",
  // iter-14.93 — tool project types
  Input: "",         // resolved per-project below
  KnowledgeBase: "", // resolved per-project below
  Report: "",        // gap-analysis final stage
  Output: "",        // tech-transformer final stage
};
const STAGE_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"];
const TOOL_STAGE_ORDER = {
  gap_analysis: ["Input", "KnowledgeBase", "Report"],
  tech_transformer: ["Input", "KnowledgeBase", "Output"],
};
const TOOL_LANDING = {
  gap_analysis: "/gap-analyzer",
  tech_transformer: "/transformer",
};
const TOOL_HASH = {
  Input: "#input",
  KnowledgeBase: "#kb",
  Report: "#report",
  Output: "#output",
};

const lsKey = (pid) => `lama:lastStage:${pid}`;

function resumePathFor(project) {
  if (!project?.id) return "/";
  const ptype = project.project_type || "legacy_migration";
  // Tool project types resume on their landing route with the appropriate hash.
  if (ptype === "gap_analysis" || ptype === "tech_transformer") {
    const order = TOOL_STAGE_ORDER[ptype];
    const status = project.stage_status || {};
    let target = order[0];
    for (const k of order) {
      const s = status[k];
      if (s === "frozen" || s === "available" || s === "active") target = k;
    }
    return `${TOOL_LANDING[ptype]}${TOOL_HASH[target] || ""}`;
  }
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

export default function ProjectSwitcher({ collapsed = false }) {
  const navigate = useNavigate();
  const { projects, active, activeId, setActiveId, refresh, loading } = useProjects();
  const [showProjects, setShowProjects] = useState(false);
  const [showNewForm, setShowNewForm] = useState(false);
  // iter-14.93 — two-step wizard: 1) type picker, 2) name input
  const [newStep, setNewStep] = useState(1);
  const [newType, setNewType] = useState(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [hoverProject, setHoverProject] = useState(false);
  const containerRef = useRef(null);

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setShowProjects(false);
        setShowNewForm(false);
        setDeleteConfirm(null);
      }
    };
    if (showProjects || showNewForm) document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showProjects, showNewForm]);

  const handleSwitch = (p) => {
    setActiveId(p.id);
    const path = resumePathFor(p);
    setShowProjects(false);
    setTimeout(() => navigate(path), 30);
    toast.success(`Switched to ${p.name}`);
  };

  const handleCreate = async () => {
    if (!newName.trim()) {
      toast.error("Project name required");
      return;
    }
    const type = newType || "legacy_migration";
    setCreating(true);
    try {
      const created = await createProject({
        name: newName.trim(),
        project_type: type,
      });
      await refresh();
      setActiveId(created.id);
      setNewName("");
      setNewType(null);
      setNewStep(1);
      setShowNewForm(false);
      const landing = PROJECT_TYPES.find(t => t.key === type)?.landing || "/";
      navigate(landing);
      toast.success(`Created "${created.name}"`, {
        description: PROJECT_TYPES.find(t => t.key === type)?.label,
      });
    } catch (e) {
      toast.error("Create failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setCreating(false);
    }
  };

  const cancelNew = () => {
    setShowNewForm(false);
    setNewName("");
    setNewType(null);
    setNewStep(1);
  };

  const handleDelete = async (pid, pname) => {
    setDeleting(true);
    try {
      await deleteProject(pid);
      toast.success(`Deleted "${pname}"`);
      setDeleteConfirm(null);
      await refresh();
      if (pid === activeId) {
        const remaining = projects.filter(p => p.id !== pid);
        if (remaining.length > 0) {
          setActiveId(remaining[0].id);
          navigate(resumePathFor(remaining[0]));
        }
      }
    } catch (e) {
      toast.error("Delete failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setDeleting(false);
    }
  };

  if (collapsed) return null;

  const frozenCount = (p) => {
    const status = p?.stage_status || {};
    const order = TOOL_STAGE_ORDER[p?.project_type] || STAGE_ORDER;
    return order.filter((k) => status[k] === "frozen" || status[k] === "skipped").length;
  };

  const totalStages = (p) =>
    (TOOL_STAGE_ORDER[p?.project_type] || STAGE_ORDER).length;

  const resumeLabel = (p) => {
    const ptype = p?.project_type || "legacy_migration";
    if (ptype === "gap_analysis" || ptype === "tech_transformer") {
      const path = resumePathFor(p);
      const hash = path.split("#")[1];
      const map = { input: "Input", kb: "Knowledge Base", report: "Report", output: "Output" };
      return map[hash] || "Input";
    }
    const path = resumePathFor(p);
    return Object.keys(STAGE_PATHS).find((k) => STAGE_PATHS[k] === path) || "Discovery";
  };

  const hasTechStack = active && (active.source_tech || active.target_tech);

  return (
    <div className="relative space-y-2" ref={containerRef}>
      {/* ═══ PRIMARY CTA: New Project Button — iter-14.90 compact ═══ */}
      <button
        data-testid="new-project-btn"
        onClick={() => {
          if (showNewForm) { cancelNew(); } else { setShowNewForm(true); setNewStep(1); }
        }}
        className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-brand hover:bg-brand-hover text-fg font-bold text-[12px] transition-all shadow-sm hover:shadow active:scale-[0.98]"
      >
        <Plus className="w-3.5 h-3.5" strokeWidth={2.5} />
        NEW PROJECT
      </button>

      {/* iter-14.93 — New Project two-step wizard: type picker → name */}
      {showNewForm && (
        <div className="bg-surface border-2 border-brand rounded-lg p-3 shadow-lg space-y-2">
          {newStep === 1 ? (
            <>
              <div className="flex items-center justify-between">
                <div className="text-micro font-bold text-fg-muted uppercase tracking-wider">
                  Choose Project Type
                </div>
                <button
                  onClick={cancelNew}
                  className="p-1 text-fg-subtle hover:text-fg-muted rounded hover:bg-surface-2"
                  title="Cancel"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
              <div className="space-y-1.5">
                {PROJECT_TYPES.map((t) => {
                  const Icon = t.icon;
                  const isSel = newType === t.key;
                  return (
                    <button
                      key={t.key}
                      data-testid={`project-type-${t.key}`}
                      onClick={() => { setNewType(t.key); setNewStep(2); }}
                      className={`w-full flex items-start gap-2.5 p-2 rounded-md border text-left transition-all ${
                        isSel
                          ? "border-brand bg-brand-tint"
                          : "border-border bg-surface hover:border-border-strong hover:bg-surface-2"
                      }`}
                    >
                      <div className={`shrink-0 w-7 h-7 rounded bg-gradient-to-br ${t.accent} flex items-center justify-center`}>
                        <Icon className="w-3.5 h-3.5 text-white" strokeWidth={2.4} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-[12px] font-semibold text-fg leading-tight">
                          {t.label}
                        </div>
                        <div className="text-micro text-fg-subtle mt-0.5 leading-snug">
                          {t.tagline}
                        </div>
                      </div>
                      <ChevronRight className="w-3.5 h-3.5 text-fg-subtle mt-1 shrink-0" />
                    </button>
                  );
                })}
              </div>
            </>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => { setNewStep(1); }}
                  className="p-1 text-fg-subtle hover:text-fg-muted rounded hover:bg-surface-2"
                  title="Back"
                >
                  <ArrowLeft className="w-3.5 h-3.5" />
                </button>
                {(() => {
                  const t = PROJECT_TYPES.find(x => x.key === newType);
                  const Icon = t?.icon || Plus;
                  return (
                    <>
                      <div className={`w-6 h-6 rounded bg-gradient-to-br ${t?.accent || ""} flex items-center justify-center`}>
                        <Icon className="w-3 h-3 text-white" strokeWidth={2.4} />
                      </div>
                      <div className="text-micro font-semibold text-fg-muted flex-1 truncate">
                        {t?.label}
                      </div>
                    </>
                  );
                })()}
                <button
                  onClick={cancelNew}
                  className="p-1 text-fg-subtle hover:text-fg-muted rounded hover:bg-surface-2"
                  title="Cancel"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
              <div className="flex gap-1.5">
                <input
                  data-testid="new-project-name-input"
                  type="text"
                  placeholder="Project name..."
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                    if (e.key === "Escape") cancelNew();
                  }}
                  className="flex-1 min-w-0 px-2.5 py-1.5 text-[12px] border border-border rounded focus:outline-none focus:ring-1 focus:ring-brand focus:border-transparent"
                  autoFocus
                />
                <button
                  data-testid="new-project-create-btn"
                  onClick={handleCreate}
                  disabled={creating || !newName.trim()}
                  className="px-2.5 py-1.5 text-micro font-bold bg-ink text-ink-fg rounded hover:bg-ink disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                >
                  {creating ? <Loader2 className="w-3 h-3 animate-spin" /> : "Create"}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* ═══ CURRENT PROJECT CARD (with hover for Migration Path) ═══ */}
      {active && (
        <div 
          className="bg-surface border border-border rounded-lg overflow-hidden shadow-sm cursor-pointer transition-all hover:border-brand hover:shadow-md group"
          onMouseEnter={() => setHoverProject(true)}
          onMouseLeave={() => setHoverProject(false)}
        >
          <div className="px-3 py-1.5 bg-gradient-to-r from-slate-50 to-white border-b border-border">
            <div className="text-micro font-bold uppercase tracking-wider text-fg-subtle">
              Active Project
            </div>
          </div>
          {/* iter-14.90: More compact - combined progress + name */}
          <div className="px-3 py-2">
            <div className="flex items-center gap-2">
              <div className="flex-1 min-w-0">
                <div className="text-[13px] font-bold text-fg truncate">{active.name}</div>
              </div>
              <div className="flex items-center gap-1.5 text-micro shrink-0">
                <div className="w-16 h-1.5 bg-surface-2 rounded-full overflow-hidden">
                  <div 
                    className="h-full bg-emerald-500 transition-all"
                    style={{ width: `${(frozenCount(active) / totalStages(active)) * 100}%` }}
                  />
                </div>
                <span className="font-semibold text-emerald-600">{frozenCount(active)}/{totalStages(active)}</span>
              </div>
            </div>
            
            {/* Migration Path - shown on hover, compact */}
            {hasTechStack && hoverProject && (
              <div className="mt-1.5 pt-1.5 border-t border-border text-micro text-fg-subtle">
                <span className="text-rose-500">{active.source_tech || "—"}</span>
                <span className="mx-1">→</span>
                <span className="text-emerald-500">{active.target_tech || "—"}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ═══ ALL PROJECTS / HISTORY — iter-14.90 compact ═══ */}
      <div className="relative">
        <button
          data-testid="all-projects-btn"
          onClick={() => setShowProjects(!showProjects)}
          className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg border transition-all ${
            showProjects 
              ? "border-fg bg-ink text-ink-fg" 
              : "border-border bg-surface text-fg-muted hover:border-border-strong"
          }`}
        >
          <span className="flex items-center gap-1.5 text-micro font-semibold">
            <History className="w-3.5 h-3.5" />
            All Projects
            <span className={`px-1 py-0.5 rounded text-micro font-bold ${
              showProjects ? "bg-surface/20 text-white" : "bg-surface-2 text-fg-subtle"
            }`}>
              {projects.length}
            </span>
          </span>
          <ChevronRight className={`w-3.5 h-3.5 transition-transform duration-200 ${showProjects ? "translate-x-0.5" : ""}`} />
        </button>

        {/* Projects List Dropdown — opens to the RIGHT of the button */}
        {showProjects && (
          <div className="absolute left-full top-0 ml-2 w-72 bg-surface border border-border rounded-lg shadow-xl z-50 max-h-[60vh] overflow-hidden">
            {/* Header */}
            <div className="px-3 py-2 border-b border-border bg-surface-2 flex items-center justify-between sticky top-0">
              <span className="text-micro font-bold text-fg-muted">Project History</span>
              <span className="text-micro text-fg-subtle">{projects.length} projects</span>
            </div>
            {/* Scrollable list */}
            <div className="max-h-[calc(60vh-40px)] overflow-y-auto">
              {loading && projects.length === 0 ? (
                <div className="p-4 text-sm text-fg-subtle flex items-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" /> Loading...
                </div>
              ) : projects.length === 0 ? (
                <div className="p-4 text-center text-sm text-fg-subtle">
                  No projects yet. Click "NEW PROJECT" above.
                </div>
              ) : (
                <ul>
                  {projects.map((p) => {
                    const isActive = p.id === activeId;
                    const isDeleting = deleteConfirm === p.id;
                    const pHasTech = p.source_tech || p.target_tech;

                    return (
                      <li key={p.id} className="border-b border-border last:border-b-0">
                        {isDeleting ? (
                          <div className="px-3 py-3 bg-red-50">
                            <div className="flex items-center gap-2 text-[12px] text-red-700 mb-2">
                              <AlertTriangle className="w-4 h-4" />
                              Delete "{p.name}"?
                            </div>
                            <div className="flex gap-2">
                              <button
                                onClick={() => handleDelete(p.id, p.name)}
                                disabled={deleting}
                                className="flex-1 px-3 py-1.5 text-micro font-bold bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50"
                              >
                                {deleting ? <Loader2 className="w-3 h-3 animate-spin mx-auto" /> : "Yes, Delete"}
                              </button>
                              <button
                                onClick={() => setDeleteConfirm(null)}
                                className="px-3 py-1.5 text-micro font-semibold border border-border-strong rounded-md hover:bg-surface-2"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className={`group flex items-center gap-2 px-3 py-2.5 cursor-pointer transition-colors ${
                            isActive ? "bg-brand-tint" : "hover:bg-surface-2"
                          }`}>
                            <button
                              onClick={() => handleSwitch(p)}
                              className="flex-1 min-w-0 text-left"
                            >
                              <div className="flex items-center gap-2">
                                <span className={`text-[13px] font-semibold truncate ${isActive ? "text-fg" : "text-fg-muted"}`}>
                                  {p.name}
                                </span>
                                {isActive && (
                                  <span className="shrink-0 text-micro uppercase tracking-wider bg-brand text-fg px-1.5 py-0.5 rounded font-bold">
                                    Active
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-3 mt-1 text-micro text-fg-subtle">
                                <span className="flex items-center gap-1">
                                  <CheckCircle2 className="w-3 h-3 text-emerald-500" />
                                  {frozenCount(p)}/{totalStages(p)}
                                </span>
                                <span className="flex items-center gap-1">
                                  <ArrowRight className="w-3 h-3" />
                                  {resumeLabel(p)}
                                </span>
                              </div>
                              {/* Migration Path in dropdown */}
                              {pHasTech && (
                                <div className="mt-1.5 text-micro text-fg-subtle truncate">
                                  {p.source_tech || "?"} → {p.target_tech || "?"}
                                </div>
                              )}
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setDeleteConfirm(p.id);
                              }}
                              className="shrink-0 p-1.5 text-fg-subtle hover:text-red-500 hover:bg-red-50 rounded opacity-0 group-hover:opacity-100 transition-all"
                              title="Delete project"
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
