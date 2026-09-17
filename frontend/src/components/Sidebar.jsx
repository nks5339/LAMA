import React, { useState, useEffect, lazy, Suspense } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { Lock, CheckCircle2, Library, BookOpen, Database, Boxes, Code2, Activity, Settings as SettingsIcon, ChevronLeft, ChevronRight, Terminal, Network, MoreHorizontal, Plug, SkipForward, LogOut, ShieldCheck, Building2, X as CloseIcon, Info, FolderOpen, FileSearch, ArrowRightLeft, Wand2 } from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
import { useAuth } from "@/state/AuthContext";
import { getPipelineStatus, factoryReset, getProjectSettings, updateProjectSettings, cancelSRSGeneration, skipStage, unskipStage, getJourneySettings, updateJourneySettings } from "@/lib/api";
import HelpIcon from "@/components/HelpIcon";
// SettingsMenu is 1,378 lines and imports recharts for its usage charts.
// The Sidebar is on the eager critical path, so a static import kept
// recharts in main.js for a dialog that is closed almost all the time.
const SettingsMenu = lazy(() => import("@/components/SettingsMenu"));
import { useAutoSaveTracker } from "@/hooks/useAutoSaveTracker";
// Warm a route's lazy chunk on hover/focus, so by the time the click
// lands the chunk is usually already parsed.
import { prefetchRoute } from "@/lib/routes";
import ProjectSwitcher from "@/components/ProjectSwitcher";
import { useIsMobile } from "@/hooks/useBreakpoint";
import { toast } from "sonner";
// iter-13.119.3 — accordion-grouped bottom nav so the section never
// pushes Pipeline off-screen and everything stays one click away.
import {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from "@/components/ui/accordion";

const LEGACY_STAGES = [
  { key: "Discovery", label: "1. Discovery & SRS", icon: BookOpen, desc: "Upload legacy files, ask gap questions, freeze SRS.", path: "/" },
  { key: "DataModel", label: "2. Data Model", icon: Database, desc: "OLTP + OLAP DDL, Bus Matrix, migration scripts.", path: "/data-model" },
  { key: "Architecture", label: "3. Architecture", icon: Boxes, desc: "Decompose into microservices.", path: "/architecture" },
  { key: "CodeGen", label: "4. Code Generation", icon: Code2, desc: "Generate target code + unit tests.", path: "/code-gen" },
  { key: "Living", label: "5. Living System", icon: Activity, desc: "Selenium tests, SRS diffs, monitoring.", path: "/living" },
];

// iter-14.93 — Tool project types (Gap Analyzer, Technology Transformer)
// have their own 3-stage pipelines mirroring the tool's tab layout. Each
// stage navigates to the same tool page with a URL hash that activates
// the corresponding tab.
const GAP_ANALYSIS_STAGES = [
  { key: "Input",         label: "1. Input Files",    icon: FolderOpen, desc: "Upload source code + SRS/FRS documents.",           path: "/gap-analyzer#input" },
  { key: "KnowledgeBase", label: "2. Knowledge Base", icon: Database,   desc: "Auto-extracted UI→API→DB traceability + requirements.", path: "/gap-analyzer#kb" },
  { key: "Report",        label: "3. Gap Report",     icon: FileSearch, desc: "KB-driven coverage matrix + gap list with severity.",   path: "/gap-analyzer#report" },
];

const TRANSFORMER_STAGES = [
  { key: "Input",         label: "1. Input Files",    icon: FolderOpen,    desc: "Upload source code + choose target stack.",      path: "/transformer#input" },
  { key: "KnowledgeBase", label: "2. Knowledge Base", icon: Database,      desc: "Auto-extracted entities + dependency graph.",    path: "/transformer#kb" },
  { key: "Output",        label: "3. Transformed",    icon: ArrowRightLeft, desc: "Per-file transformed source tree + downloads.", path: "/transformer#output" },
];

// iter-22 — Direct Transform. Three stages mirroring its three panes; no
// KnowledgeBase, because it is folder-path driven and builds no KB.
const DIRECT_TRANSFORM_STAGES = [
  { key: "Input",     label: "1. Configure",  icon: FolderOpen,     desc: "Pick the source folder, the source and target stacks, and the destination.", path: "/direct-transform#input" },
  { key: "Transform", label: "2. Transform",  icon: Wand2,          desc: "Run the job and watch the engine + agent event stream.",                    path: "/direct-transform#transform" },
  { key: "Output",    label: "3. Output",     icon: ArrowRightLeft, desc: "Reports and the per-file transform record.",                                path: "/direct-transform#output" },
];

const STAGES_BY_TYPE = {
  legacy_migration:  LEGACY_STAGES,
  gap_analysis:      GAP_ANALYSIS_STAGES,
  tech_transformer:  TRANSFORMER_STAGES,
  direct_transform:  DIRECT_TRANSFORM_STAGES,
};

// iter-13.89 — Responsive Sidebar.
//   • Desktop (≥ lg): inline column, collapse/expand via the chevron as
//     before (state persisted to `lama:panel:sidebar`).
//   • Mobile / tablet (< lg): rendered by <Shell/> as a fixed off-canvas
//     drawer that slides in from the left over a tinted backdrop. The
//     hamburger in Shell's top bar flips `mobileOpen`; tapping the
//     backdrop OR any nav link auto-closes via `onMobileClose`.
export default function Sidebar({ mobileOpen = false, onMobileClose = () => {} } = {}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { active, refresh: refreshProjects } = useProjects();
  const { user, tenant, logout } = useAuth();
  const isMobile = useIsMobile();
  const [collapsed, setCollapsed] = useState(
    typeof window !== "undefined" && localStorage.getItem("lama:panel:sidebar") === "true"
  );
  const [pipeline, setPipeline] = useState({});
  // iter-13.31 — graph-KB toggle (per-project override, env-default fallback).
  const [graphSettings, setGraphSettings] = useState(null);
  const [graphBusy, setGraphBusy] = useState(false);
  // iter-14.25 — Journey-KB toggle (per-project override, env-default fallback).
  const [journeySettings, setJourneySettings] = useState(null);
  const [journeyBusy, setJourneyBusy] = useState(false);
  // Settings popover (History / Auto-save / Refresh / Statistics).
  const [menuOpen, setMenuOpen] = useState(false);
  const [refreshBusy, setRefreshBusy] = useState(false);

  // Record the last visited stage per project whenever auto-save is on,
  // so the Settings → History pane can resume each project at the right
  // page. Mounted here once; reads `lama:autoSave` from localStorage.
  useAutoSaveTracker(active?.id, location.pathname);

  // iter-14.93 — Resolve pipeline stages based on the active project's type.
  // Falls back to Legacy 5-stage layout when project_type is missing (older projects).
  const STAGES = STAGES_BY_TYPE[active?.project_type || "legacy_migration"] || LEGACY_STAGES;

  // iter-14.94 — For tool project types, "stage" progression is really tab
  // progression (URL hash). We derive an effective status here so the sidebar
  // reflects the user's actual position instead of the never-updated
  // stage_status doc.
  const isToolProject = ["gap_analysis", "tech_transformer", "direct_transform"]
    .includes(active?.project_type);
  const isTransformer = active?.project_type === "tech_transformer";
  const currentHash = (location.hash || "").replace("#", "");
  const HASH_TO_KEY = { input: "Input", kb: "KnowledgeBase", report: "Report",
                       output: "Output", transform: "Transform" };
  const toolCurrentKey = HASH_TO_KEY[currentHash] || "Input";
  const toolCurrentIdx = STAGES.findIndex(s => s.key === toolCurrentKey);

  // iter-15.14 — Listen for Transformer phase broadcasts so the sidebar
  // reflects the *actual* running phase (KB build vs Transforming) rather
  // than just the URL hash. See `broadcastTransformerPhase` in Transformer.jsx.
  const [transformerPhase, setTransformerPhase] = useState(() => {
    try { return window.localStorage.getItem("lama:transformer:phase"); } catch (_) { return null; }
  });
  useEffect(() => {
    if (!isTransformer) return undefined;
    const onEvt = (e) => setTransformerPhase(e?.detail?.phase || null);
    const onStorage = (e) => { if (e.key === "lama:transformer:phase") setTransformerPhase(e.newValue); };
    window.addEventListener("lama:transformer:phase", onEvt);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("lama:transformer:phase", onEvt);
      window.removeEventListener("storage", onStorage);
    };
  }, [isTransformer]);

  // Map transformer phase → (activeIdx, frozenBeforeIdx). Returns null when
  // no phase is known (fallback to hash-based derivation).
  const transformerStageMap = React.useMemo(() => {
    if (!isTransformer) return null;
    switch (transformerPhase) {
      case null:
      case undefined:
      case "":
        return null;
      case "uploading":
      case "queued":
      case "building_kb":
        return { activeIdx: 1, frozenBefore: 1, allDone: false };
      case "transforming":
      case "paused":
        return { activeIdx: 2, frozenBefore: 2, allDone: false };
      case "completed":
        return { activeIdx: 2, frozenBefore: 3, allDone: true };
      case "failed":
      case "stopped":
        // Freeze whatever was done before halt; leave current stage as active.
        return { activeIdx: 2, frozenBefore: 2, allDone: false };
      default:
        return null;
    }
  }, [isTransformer, transformerPhase]);

  useEffect(() => {
    if (!active?.id) { setGraphSettings(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const s = await getProjectSettings(active.id);
        if (!cancelled) setGraphSettings(s);
      } catch (_) { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [active?.id]);

  const toggleGraphKb = async () => {
    if (!active?.id || graphBusy) return;
    const cur = graphSettings?.use_graph_kb_effective ?? true;
    setGraphBusy(true);
    try {
      const next = await updateProjectSettings(active.id, { use_graph_kb: !cur });
      setGraphSettings(next);
      toast.success(`Graph KB ${next.use_graph_kb_effective ? "enabled" : "disabled"} for this project`);
    } catch (e) {
      toast.error("Failed to update graph-KB setting");
    } finally {
      setGraphBusy(false);
    }
  };

  // iter-14.25 — Journey KB toggle (per-project, mirrors Graph KB pattern).
  useEffect(() => {
    if (!active?.id) { setJourneySettings(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const s = await getJourneySettings(active.id);
        if (!cancelled) setJourneySettings(s);
      } catch (_) { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [active?.id]);

  const toggleJourneyKb = async () => {
    if (!active?.id || journeyBusy) return;
    const cur = !!journeySettings?.enabled;
    setJourneyBusy(true);
    try {
      const res = await updateJourneySettings(active.id, { enabled: !cur });
      // Endpoint returns { project_id, config }; refresh effective view.
      const fresh = await getJourneySettings(active.id);
      setJourneySettings(fresh);
      toast.success(
        `Journey KB ${fresh?.enabled ? "enabled" : "disabled"} for this project` +
        (!cur ? " — re-run Build KB to materialise journeys" : ""),
      );
      return res;
    } catch (e) {
      toast.error("Failed to update journey-KB setting");
    } finally {
      setJourneyBusy(false);
    }
  };

  useEffect(() => {
    if (!active?.id) return;
    let cancelled = false;
    const load = async () => {
      try {
        const data = await getPipelineStatus(active.id);
        if (!cancelled) setPipeline(data || {});
      } catch (_) { /* ignore */ }
    };
    load();
    // Skip the poll entirely while the tab is hidden.
    const t = setInterval(() => { if (!document.hidden) load(); }, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, [active?.id]);

  const toggle = () => {
    const v = !collapsed;
    localStorage.setItem("lama:panel:sidebar", String(v));
    setCollapsed(v);
  };

  // iter-13.89 — when the user navigates anywhere from the mobile
  // drawer, auto-close it so the page content is visible underneath.
  // Watching `location.pathname` keeps every navigate(...) call (rail
  // shortcuts, stage buttons, side-panel links) compliant without
  // having to thread `onMobileClose` through every onClick.
  useEffect(() => {
    if (isMobile && mobileOpen) onMobileClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  // Refresh App — wipes the active project's KB, SRS, Discovery chat, data
  // model artifacts, stage context, audit log and Qdrant vectors via the
  // backend factory-reset endpoint, then clears UI state and hard-reloads
  // the SPA. After this the project itself is kept but every stage is
  // re-locked to Discovery — same state as a brand-new project.
  const refreshApp = async () => {
    const projectName = active?.name ? `"${active.name}"` : "the active project";
    const ok = window.confirm(
      `Refresh the LAMA app?\n\n` +
      `This will PERMANENTLY DELETE for ${projectName}:\n` +
      `  • Knowledge Base (files, OWL entities, TOON, vectors)\n` +
      `  • SRS document (all sections, frozen or not)\n` +
      `  • Discovery chat history\n` +
      `  • Data Model artifacts + stage context\n` +
      `  • Audit log entries\n\n` +
      `The project itself is kept; all stages are re-locked to Discovery.\n` +
      `This action CANNOT be undone.`
    );
    if (!ok) return;

    setRefreshBusy(true);
    // 1) Server-side factory reset on the active project (if any).
    if (active?.id) {
      // iter-13.35 — proactively cancel any running SRS job FIRST so the
      // factory-reset cleanup doesn't race the background task. Errors here
      // are ignored (404 = no job running, which is the common case).
      try { await cancelSRSGeneration(active.id); } catch (_) { /* no-op */ }
      try {
        const r = await factoryReset(active.id);
        const killed = (r?.killed_jobs || []).length;
        toast.success(
          killed
            ? `Project wiped — KB, SRS, chat cleared. Cancelled ${killed} in-flight job(s).`
            : "Project wiped — KB, SRS, chat cleared."
        );
      } catch (e) {
        toast.error(`Server reset failed: ${e?.message || e}. Reloading anyway.`);
      }
    }

    // 2) Drop all LAMA-prefixed UI prefs (panel sizes, sidebar collapse, etc.)
    try {
      const toRemove = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith("lama:")) toRemove.push(k);
      }
      toRemove.forEach((k) => localStorage.removeItem(k));
      sessionStorage.clear();
    } catch (_) { /* ignore storage errors */ }

    // 3) Hard reload (bypass SPA router cache) to the root page.
    window.location.replace("/");
  };

  // Resolve per-stage status combining project.stage_status + pipeline (StageContext)
  // iter-13.66 — "skipped" is treated like "frozen" for the purpose of
  // unlocking the next stage but is rendered distinctly in the UI.
  const stageStatus = (key, idx) => {
    // iter-14.94 — Tool project types: derive status from the current tab
    // hash, not from a stale stage_status document. Prior stages are frozen,
    // current is active, remaining are locked (but clickable so the user can
    // navigate around freely).
    if (isToolProject) {
      // iter-15.14 — For tech_transformer, prefer real backend phase over
      // URL hash so the sidebar correctly shows "Input done · KB done ·
      // Transformed in-progress" during an actual run.
      if (transformerStageMap) {
        if (transformerStageMap.allDone) return "frozen";
        if (idx < transformerStageMap.frozenBefore) return "frozen";
        if (idx === transformerStageMap.activeIdx) return "active";
        return "available";
      }
      if (idx < toolCurrentIdx) return "frozen";
      if (idx === toolCurrentIdx) return "active";
      return "available";
    }
    const ctx = pipeline[key];
    const projStatus = active?.stage_status?.[key];
    if (projStatus === "skipped") return "skipped";
    if (ctx?.frozen) return "frozen";
    if (projStatus === "frozen") return "frozen";
    if (projStatus === "available") return "available";
    if (projStatus === "active") return "active";
    // Auto-promote: if previous stage is frozen OR skipped, this one becomes "available"
    if (idx > 0) {
      const prev = STAGES[idx - 1];
      const prevProj = active?.stage_status?.[prev.key];
      if (
        pipeline[prev.key]?.frozen ||
        prevProj === "frozen" ||
        prevProj === "skipped"
      ) {
        return "available";
      }
    }
    return projStatus || "locked";
  };

  // iter-13.66 — Skippable intermediate stages (mirrors backend SKIPPABLE_STAGES).
  // Discovery (initial) and CodeGen (final deliverable) are never skippable.
  const SKIPPABLE = new Set(["DataModel", "Architecture", "Living"]);

  const refreshPipeline = async () => {
    if (!active?.id) return;
    try {
      const data = await getPipelineStatus(active.id);
      setPipeline(data || {});
    } catch (_) { /* ignore */ }
  };

  const handleSkip = async (stageKey, label, isFrozen = false) => {
    if (!active?.id) return;
    const ok = window.confirm(
      isFrozen
        ? `"${label}" is currently FROZEN.\n\n` +
          `Skipping will OVERWRITE its existing artifacts with a skip marker. ` +
          `This cannot be reversed except by re-running the stage from scratch. ` +
          `Continue?`
        : `Skip "${label}"?\n\n` +
          `This stage will be marked as skipped and the next stage will unlock. ` +
          `No artifacts will be produced for this stage. You can undo this from the same menu.`
    );
    if (!ok) return;
    try {
      await skipStage(active.id, stageKey, { force: isFrozen });
      toast.success(`${label} skipped`, { description: "Next stage unlocked." });
      await refreshPipeline();
      try { await refreshProjects(); } catch (_) { /* */ }
    } catch (e) {
      toast.error("Skip failed", { description: e?.response?.data?.detail || e.message });
    }
  };

  const handleUnskip = async (stageKey, label) => {
    if (!active?.id) return;
    try {
      await unskipStage(active.id, stageKey);
      toast.success(`${label} un-skipped`, { description: "Stage is editable again." });
      await refreshPipeline();
      try { await refreshProjects(); } catch (_) { /* */ }
    } catch (e) {
      toast.error("Un-skip failed", { description: e?.response?.data?.detail || e.message });
    }
  };

  // ----- Collapsed (rail) mode — desktop only -----
  // On mobile we always render the expanded sidebar inside the drawer
  // because rail-mode icons are too small for touch targets.
  if (collapsed && !isMobile) {
    return (
      <aside
        data-testid="sidebar-collapsed"
        className="w-12 shrink-0 h-full min-h-0 flex flex-col bg-surface border-r border-border"
      >
        <button
          type="button"
          onClick={toggle}
          data-testid="expand-sidebar"
          className="w-9 h-9 m-1.5 bg-brand text-fg flex items-center justify-center rounded-sm font-display font-bold text-base"
          aria-label="Expand sidebar"
        >
          L
        </button>
        {/* iter-14.77 — Collapsed project button: click to expand and access switcher */}
        {active && (
          <button
            type="button"
            onClick={toggle}
            title={`Project: ${active.name} — click to expand`}
            className="w-9 h-9 flex items-center justify-center rounded-sm border border-brand bg-brand-tint text-fg hover:bg-brand transition-colors"
          >
            <FolderOpen className="w-4 h-4" />
          </button>
        )}
        <div className="flex flex-col items-center gap-1 mt-3">
          {STAGES.map((s, idx) => {
            const status = stageStatus(s.key, idx);
            const isLocked = status === "locked";
            const isActive = status === "active";
            const isFrozen = status === "frozen";
            const isSkipped = status === "skipped";
            const isCurrent = isToolProject
              ? (isActive && !isLocked)
              : (location.pathname === s.path && !isLocked);
            const Icon = s.icon;
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => {
                  if (isLocked) {
                    toast.message("Locked", { description: `Complete and freeze (or skip) previous stage to unlock ${s.label}.` });
                    return;
                  }
                  navigate(s.path);
                }}
                onMouseEnter={() => !isLocked && prefetchRoute(s.path)}
                onFocus={() => !isLocked && prefetchRoute(s.path)}
                aria-label={isSkipped ? `${s.label} (skipped)` : s.label}
                aria-current={isCurrent ? "page" : undefined}
                aria-disabled={isLocked || undefined}
                title={isSkipped ? `${s.label} (skipped)` : s.label}
                className={`w-9 h-9 flex items-center justify-center rounded-sm border ${
                  isLocked
                    ? "border-border bg-bg text-fg-muted cursor-not-allowed"
                    : isCurrent
                    ? "border-fg bg-ink text-ink-fg"
                    : isFrozen
                    ? "border-brand bg-brand-tint text-fg"
                    : isSkipped
                    ? "border-border-strong bg-surface-2 text-fg-subtle"
                    : "border-border hover:bg-surface-2 text-fg"
                }`}
              >
                {isFrozen
                  ? <CheckCircle2 className="w-4 h-4" />
                  : isSkipped
                  ? <SkipForward className="w-4 h-4" />
                  : isLocked
                  ? <Lock className="w-3.5 h-3.5" />
                  : <Icon className="w-4 h-4" />}
              </button>
            );
          })}
        </div>
        <div className="mt-auto flex flex-col items-center gap-1 pb-2 border-t border-border pt-2">
          <button type="button" onClick={() => navigate("/console")} onMouseEnter={() => prefetchRoute("/console")} onFocus={() => prefetchRoute("/console")} aria-label="Console" title="Console" className="w-9 h-9 flex items-center justify-center rounded-sm hover:bg-surface-2">
            <Terminal className="w-4 h-4 text-fg-muted" />
          </button>
          <button type="button" onClick={() => navigate("/prompts")} onMouseEnter={() => prefetchRoute("/prompts")} onFocus={() => prefetchRoute("/prompts")} aria-label="Prompt Library" title="Prompt Library" className="w-9 h-9 flex items-center justify-center rounded-sm hover:bg-surface-2">
            <Library className="w-4 h-4 text-fg-muted" />
          </button>
          <button type="button" onClick={() => navigate("/settings")} onMouseEnter={() => prefetchRoute("/settings")} onFocus={() => prefetchRoute("/settings")} aria-label="Settings & GitHub" title="Settings & GitHub" className="w-9 h-9 flex items-center justify-center rounded-sm hover:bg-surface-2">
            <SettingsIcon className="w-4 h-4 text-fg-muted" />
          </button>
          <button type="button" onClick={() => navigate("/audit")} onMouseEnter={() => prefetchRoute("/audit")} onFocus={() => prefetchRoute("/audit")} aria-label="Audit Log" title="Audit Log" className="w-9 h-9 flex items-center justify-center rounded-sm hover:bg-surface-2">
            <Activity className="w-4 h-4 text-fg-muted" />
          </button>
          <button type="button" onClick={toggle} title="Expand sidebar" className="w-9 h-9 flex items-center justify-center rounded-sm hover:bg-surface-2 mt-1" data-testid="expand-sidebar-bottom">
            <ChevronRight className="w-4 h-4 text-fg" />
          </button>
        </div>
      </aside>
    );
  }

  return (
    <>
      {/* iter-13.89 — Mobile backdrop. Tinted full-screen overlay that
          dismisses the drawer when tapped. Only rendered on small
          screens AND while the drawer is open. */}
      {isMobile && mobileOpen && (
        <div
          data-testid="sidebar-backdrop"
          aria-hidden="true"
          onClick={onMobileClose}
          className="fixed inset-0 z-40 bg-ink/40 backdrop-blur-sm lg:hidden"
        />
      )}
      <aside
        data-testid="sidebar"
        className={
          isMobile
            ? `fixed inset-y-0 left-0 z-50 w-[280px] max-w-[85vw] flex flex-col bg-surface border-r border-border shadow-2xl transform transition-transform duration-200 ease-out lg:hidden ${
                mobileOpen ? "translate-x-0" : "-translate-x-full"
              }`
            : "w-[260px] shrink-0 h-full min-h-0 flex flex-col bg-surface border-r border-border"
        }
        aria-hidden={isMobile && !mobileOpen}
      >
        {/* iter-13.89 — Close button shown only while drawer is open on mobile. */}
        {isMobile && (
          <button
            type="button"
            onClick={onMobileClose}
            data-testid="sidebar-drawer-close"
            aria-label="Close menu"
            className="absolute top-2 right-2 z-10 w-8 h-8 flex items-center justify-center rounded-sm hover:bg-surface-2"
          >
            <CloseIcon className="w-4 h-4 text-fg" />
          </button>
        )}
      {/* Brand + Project (static, single-tenant) */}
      <div className="px-5 py-5 border-b border-border">
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-9 h-9 bg-brand text-fg flex items-center justify-center rounded-sm font-display font-bold text-base">
              L
            </div>
            <div>
              <div className="font-display font-bold text-xl leading-none tracking-tight text-fg" data-testid="brand-name">LAMA</div>
              <div className="text-micro uppercase tracking-widest text-fg-muted mt-1 leading-tight">Legacy Application<br/>Modernisation AI Studio</div>
            </div>
          </div>
          <button
            type="button"
            onClick={toggle}
            data-testid="collapse-sidebar"
            className="text-fg-muted hover:text-fg p-1 -mr-1"
            aria-label="Collapse sidebar"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
        </div>
        {/* iter-14.79 — ProjectSwitcher with prominent "NEW PROJECT" button,
            current project card with progress bar, and "ALL PROJECTS" dropdown.
            Migration Path moved inside project card hover state. */}
        <div data-testid="active-project-header" className="mt-2">
          <ProjectSwitcher collapsed={false} />
        </div>
      </div>

      {/* iter-13.119.5 — Single accordion holds Pipeline + Tools + Project
          + Admin so the user can collapse anything to free vertical space.
          Pipeline is default-OPEN; the others default-CLOSED. The full
          open-state set is persisted to localStorage so the user's choice
          survives reloads. The whole accordion lives in one scrollable
          flex-1 container so the user footer stays pinned at the bottom. */}
      <div className="px-2 py-1 flex-1 min-h-0 overflow-y-auto mos-scroll">
        <Accordion
          type="multiple"
          defaultValue={(() => {
            try {
              const raw = localStorage.getItem("lama:sidebar:bottomNav");
              if (raw) {
                const v = JSON.parse(raw);
                if (Array.isArray(v)) return v;
              }
            } catch (_e) {
            // localStorage/CustomEvent may be unavailable (private mode,
            // blocked site data). The feature degrades; it never fails.
            }
            // Pipeline open by default — it's the primary nav. Tools also
            // open so Console/Integrations/Prompts are one click away.
            return ["pipeline", "tools"];
          })()}
          onValueChange={(v) => {
            try { localStorage.setItem("lama:sidebar:bottomNav", JSON.stringify(v)); } catch (_e) {
            // localStorage/CustomEvent may be unavailable (private mode,
            // blocked site data). The feature degrades; it never fails.
            }
          }}
          className="w-full"
        >
          {/* ── Pipeline (the 5 migration stages) ──────────────── */}
          <AccordionItem value="pipeline" className="border-b border-border">
            <AccordionTrigger
              data-testid="sidebar-acc-pipeline"
              className="px-2 py-2 text-micro font-bold uppercase tracking-wider text-fg-muted hover:no-underline hover:bg-surface-2 rounded-sm"
            >
              <span className="flex items-center gap-2">
                <BookOpen className="w-3.5 h-3.5" /> Pipeline
                <HelpIcon text="The 5 stages of migration. Freeze each stage to advance. Stages 2-5 are coming soon." testId="help-pipeline" />
              </span>
            </AccordionTrigger>
            <AccordionContent className="pb-2 pt-0">
              <nav className="space-y-1 px-1">
                {STAGES.map((s, idx) => {
                  const status = stageStatus(s.key, idx);
                  const ctx = pipeline[s.key];
                  const isLocked = status === "locked";
                  const isActive = status === "active";
                  const isAvailable = status === "available";
                  const isFrozen = status === "frozen";
                  const isSkipped = status === "skipped";
                  // iter-15.7 — For tool projects the sidebar `path` includes
                  // the URL hash (e.g. "/gap-analyzer#kb") so a plain
                  // pathname compare never matches. Use the hash-derived
                  // "active" status to highlight the current stage, and
                  // fall back to pathname compare for pipeline projects.
                  const isCurrent = isToolProject
                    ? (isActive && !isLocked)
                    : (location.pathname === s.path && !isLocked);
                  const canSkip = SKIPPABLE.has(s.key) && !isSkipped;
                  const canUnskip = SKIPPABLE.has(s.key) && isSkipped;
                  const Icon = s.icon;
                  return (
                    <div
                      key={s.key}
                      className={`rounded-lg overflow-hidden transition-all ${
                        isLocked
                          ? "bg-surface-2 opacity-60"
                          : isCurrent
                          ? "bg-brand shadow-md ring-2 ring-fg/10"
                          : isFrozen
                          ? "bg-surface border border-border shadow-sm"
                          : isSkipped
                          ? "bg-orange-50/80 border border-orange-200/60"
                          : "bg-surface border border-border hover:shadow-md hover:border-border-strong"
                      }`}
                    >
                      {/* Stage row */}
                      <button
                        type="button"
                        data-testid={`stage-${s.key}`}
                        onMouseEnter={() => !isLocked && prefetchRoute(s.path)}
                        onFocus={() => !isLocked && prefetchRoute(s.path)}
                        aria-current={isCurrent ? "page" : undefined}
                        aria-disabled={isLocked || undefined}
                        onClick={() => {
                          if (isLocked) {
                            toast.message("Locked", { description: `Complete previous stage first.` });
                            return;
                          }
                          navigate(s.path);
                        }}
                        className={`w-full text-left flex items-center gap-2 px-3 py-2.5 ${
                          isLocked
                            ? "text-fg-subtle cursor-not-allowed"
                            : isCurrent
                            ? "text-fg"
                            : isFrozen
                            ? "text-fg"
                            : isSkipped
                            ? "text-warn"
                            : "text-fg-muted"
                        }`}
                      >
                        <div className="shrink-0">
                          {isFrozen
                            ? <CheckCircle2 className={`w-4 h-4 ${isCurrent ? "text-fg" : "text-fg"}`} />
                            : isSkipped
                            ? <SkipForward className={`w-4 h-4 ${isCurrent ? "text-fg" : "text-orange-500"}`} />
                            : isLocked
                            ? <Lock className="w-4 h-4" />
                            : <Icon className={`w-4 h-4 ${isCurrent ? "text-fg" : ""}`} />}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-[12px] font-semibold truncate">{s.label}</div>
                        </div>
                      </button>
                      
                      {/* iter-14.89 — Clean action bar with subtle styling */}
                      {!isLocked && (
                        <div className={`flex items-center gap-1 px-3 py-1.5 ${
                          isCurrent 
                            ? "bg-ink"
                            : isFrozen 
                            ? "bg-surface-2 border-t border-border"
                            : isSkipped
                            ? "bg-orange-50/50 border-t border-orange-100"
                            : "bg-surface-2/80 border-t border-border"
                        }`}>
                          {/* Status badge */}
                          {isFrozen ? (
                            <span 
                              data-testid={`stage-${s.key}-badge-frozen`}
                              className={`flex-1 text-micro font-semibold flex items-center gap-1 ${isCurrent ? "text-white" : "text-emerald-600"}`}
                            >
                              <CheckCircle2 className="w-3 h-3" /> Frozen v{ctx?.version ?? "1"}
                            </span>
                          ) : isSkipped ? (
                            <span 
                              data-testid={`stage-${s.key}-badge-skipped`}
                              className={`flex-1 text-micro font-semibold flex items-center gap-1 ${isCurrent ? "text-white" : "text-orange-500"}`}
                            >
                              <SkipForward className="w-3 h-3" /> Skipped
                            </span>
                          ) : isAvailable ? (
                            <span 
                              data-testid={`stage-${s.key}-badge-ready`}
                              className={`flex-1 text-micro font-medium ${isCurrent ? "text-white/90" : "text-fg-subtle"}`}
                            >
                              Ready to work
                            </span>
                          ) : (
                            <span className={`flex-1 text-micro font-medium ${isCurrent ? "text-white/70" : "text-fg-subtle"}`}>
                              In progress
                            </span>
                          )}
                          
                          {/* Skip / Unskip button - always visible with background when selected */}
                          {canSkip && (
                            <button
                              type="button"
                              data-testid={`stage-${s.key}-skip`}
                              onClick={(e) => { e.stopPropagation(); handleSkip(s.key, s.label, isFrozen); }}
                              title={isFrozen ? "Skip (overwrites frozen)" : "Skip this stage"}
                              className={`px-2 py-0.5 rounded text-micro font-semibold transition-colors ${
                                isCurrent 
                                  ? "bg-rose-500 text-white hover:bg-rose-600" 
                                  : "text-rose-500 bg-rose-50 hover:bg-rose-100"
                              }`}
                            >
                              Skip
                            </button>
                          )}
                          {canUnskip && (
                            <button
                              type="button"
                              data-testid={`stage-${s.key}-unskip`}
                              onClick={(e) => { e.stopPropagation(); handleUnskip(s.key, s.label); }}
                              title="Restore this stage"
                              className={`px-2 py-0.5 rounded text-micro font-semibold transition-colors ${
                                isCurrent
                                  ? "bg-surface text-fg hover:bg-surface-2"
                                  : "text-fg-subtle bg-surface-2 hover:bg-surface-3"
                              }`}
                            >
                              Restore
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </nav>
            </AccordionContent>
          </AccordionItem>

          {/* ── Tools (Console / Integrations / Prompts) ───────── */}
          <AccordionItem value="tools" className="border-b border-border">
            <AccordionTrigger
              data-testid="sidebar-acc-tools"
              className="px-2 py-2 text-micro font-bold uppercase tracking-wider text-fg-muted hover:no-underline hover:bg-surface-2 rounded-sm"
            >
              <span className="flex items-center gap-2">
                <Terminal className="w-3.5 h-3.5" /> Tools
              </span>
            </AccordionTrigger>
            <AccordionContent className="pb-1 pt-0 space-y-0.5">
              <button
                data-testid="nav-console"
                onClick={() => navigate("/console")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/console" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <Terminal className="w-4 h-4" />
                Console
                <HelpIcon text="Model Fabric, Agent Fabric, and Prompt Engineering — configure providers, override per-agent models, test prompts, and watch token usage." testId="help-console" />
              </button>
              <button
                data-testid="nav-integrations"
                onClick={() => navigate("/integrations")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/integrations" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <Plug className="w-4 h-4" />
                Integrations
                <HelpIcon
                  text="Enable Indian govt-service integrations (PAN, Aadhaar e-KYC, GSTIN, DigiLocker, e-Sign, UPI). Clicking 'Inject into codebase' drops a mock-first, env-configurable client + router into the generated code."
                  testId="help-integrations"
                />
              </button>
              <button
                data-testid="nav-prompts"
                onClick={() => navigate("/prompts")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/prompts" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <Library className="w-4 h-4" />
                Prompt Library
                <HelpIcon text="Global system prompts (admin) and per-project overrides for each stage." testId="help-prompts" />
              </button>
            </AccordionContent>
          </AccordionItem>

          {/* ── Project (Graph KB toggle) ──────────────────────── */}
          {active?.id && (
            <AccordionItem value="project" className="border-b border-border">
              <AccordionTrigger
                data-testid="sidebar-acc-project"
                className="px-2 py-2 text-micro font-bold uppercase tracking-wider text-fg-muted hover:no-underline hover:bg-surface-2 rounded-sm"
              >
                <span className="flex items-center gap-2">
                  <Network className="w-3.5 h-3.5" /> Project
                </span>
              </AccordionTrigger>
              <AccordionContent className="pb-1 pt-0 space-y-0.5">
                {/* iter-13.31 — Graph KB toggle (per-project). */}
                <div
                  data-testid="graph-kb-toggle-row"
                  className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-sm text-[13px] text-fg-muted hover:bg-surface-2"
                  title={
                    graphSettings?.use_graph_kb === null || graphSettings?.use_graph_kb === undefined
                      ? `Using env default (LAMA_USE_GRAPH_KB) — currently ${graphSettings?.use_graph_kb_env_default ? "ON" : "OFF"}. Click to override.`
                      : `Project override: ${graphSettings?.use_graph_kb ? "ON" : "OFF"}. Click to flip.`
                  }
                >
                  <span className="flex items-center gap-2">
                    <Network className="w-4 h-4" />
                    Graph KB
                    <HelpIcon
                      text="When ON, SRS / Architecture / CodeGen prompts include a compact YAML subgraph from your KB property graph. When OFF, they run on TOON + RAG only — cheaper but less precise."
                      testId="help-graph-kb"
                    />
                  </span>
                  <button
                    type="button"
                    data-testid="graph-kb-toggle-btn"
                    onClick={toggleGraphKb}
                    disabled={graphBusy || !graphSettings}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition ${
                      graphSettings?.use_graph_kb_effective ? "bg-info" : "bg-surface-3"
                    } ${graphBusy ? "opacity-50" : ""}`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-surface transition ${
                        graphSettings?.use_graph_kb_effective ? "translate-x-4" : "translate-x-0.5"
                      }`}
                    />
                  </button>
                </div>
                {/* iter-14.25 — Journey KB toggle (per-project). */}
                <div
                  data-testid="journey-kb-toggle-row"
                  className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-sm text-[13px] text-fg-muted hover:bg-surface-2"
                  title={
                    journeySettings?.override
                      ? `Project override: ${journeySettings?.enabled ? "ON" : "OFF"}. Click to flip.`
                      : `Using env default (LAMA_USE_JOURNEY_KB) — currently ${journeySettings?.env_default ? "ON" : "OFF"}. Click to override.`
                  }
                >
                  <span className="flex items-center gap-2">
                    <Network className="w-4 h-4" />
                    Journey KB
                    <HelpIcon
                      text={`When ON, CodeGen prompts include compact api_journey + ui_journey slices (Route/View-anchored migration units) from your KB graph. iter-14.25.8: SRS is opted OUT of journey KB by default — the journey slice was crowding §5 Detailed Use Cases into one long story per journey instead of enumerating every WF-*/UC-* on the completeness roster. SRS now grounds on the deterministic UC roster + glossary whitelist + analysis digest (the same recipe that produced the original CGHS SRS). Operators who want journey grounding back on SRS can set stages_enabled: ["srs", "codegen"] in the per-project override or LAMA_JOURNEY_KB_STAGES=srs,codegen. Additive on top of Graph KB. Rebuild the KB after flipping ON so journeys get materialised.`}
                      testId="help-journey-kb"
                    />
                  </span>
                  <button
                    type="button"
                    data-testid="journey-kb-toggle-btn"
                    onClick={toggleJourneyKb}
                    disabled={journeyBusy || !journeySettings}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition ${
                      journeySettings?.enabled ? "bg-info" : "bg-surface-3"
                    } ${journeyBusy ? "opacity-50" : ""}`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-surface transition ${
                        journeySettings?.enabled ? "translate-x-4" : "translate-x-0.5"
                      }`}
                    />
                  </button>
                </div>
              </AccordionContent>
            </AccordionItem>
          )}

          {/* ── Admin (Settings & GitHub / Audit Log / More) ──── */}
          <AccordionItem value="admin" className="border-b-0">
            <AccordionTrigger
              data-testid="sidebar-acc-admin"
              className="px-2 py-2 text-micro font-bold uppercase tracking-wider text-fg-muted hover:no-underline hover:bg-surface-2 rounded-sm"
            >
              <span className="flex items-center gap-2">
                <SettingsIcon className="w-3.5 h-3.5" /> Admin
              </span>
            </AccordionTrigger>
            <AccordionContent className="pb-1 pt-0 space-y-0.5">
              {/* iter-13.119.4 — Super-admin shortcut lives inside the
                  Admin accordion now (previously a big button in the
                  user footer that crowded the bottom of the sidebar). */}
              {user?.role === "super_admin" && (
                <button
                  type="button"
                  data-testid="nav-admin"
                  onClick={() => navigate("/admin")}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                    location.pathname === "/admin"
                      ? "bg-brand-tint text-fg font-semibold border border-brand"
                      : "text-fg-muted font-semibold hover:bg-brand-tint border border-transparent"
                  }`}
                >
                  <ShieldCheck className="w-4 h-4 text-fg" />
                  Admin Dashboard
                </button>
              )}
              <button
                data-testid="nav-settings"
                onClick={() => navigate("/settings")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/settings" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <SettingsIcon className="w-4 h-4" />
                Settings &amp; GitHub
                <HelpIcon text="Configure GitHub repository and credentials (personal access token OR username + password) for pushing generated code." testId="help-settings" />
              </button>
              <button
                data-testid="nav-audit"
                onClick={() => navigate("/audit")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/audit" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <Activity className="w-4 h-4" />
                Audit Log
              </button>
              <button
                data-testid="nav-about"
                onClick={() => navigate("/about")}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] ${
                  location.pathname === "/about" ? "bg-bg text-fg font-semibold" : "text-fg-muted hover:bg-surface-2"
                }`}
              >
                <Info className="w-4 h-4" />
                About Us
              </button>

              {/* Settings (More) — opens a popover with History, Auto-save,
                  Refresh App, and Statistics panes. */}
              <button
                data-testid="nav-settings-menu"
                onClick={() => setMenuOpen(true)}
                title="History, Auto-save, Refresh App, Statistics"
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-[13px] font-semibold text-fg bg-brand-tint border border-brand hover:bg-brand mt-1"
              >
                <MoreHorizontal className="w-4 h-4" />
                Settings
                <span className="ml-auto text-micro uppercase tracking-wider text-fg-subtle">More</span>
              </button>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>

      {/* iter-13.68 — Tenant + user footer with logout. Always rendered when
          a session exists. iter-13.119.4 — Admin Dashboard button moved
          into the Admin accordion group above so the footer stays a
          single compact row (was overlapping the accordion when admins
          logged in). */}
      {user && (
        <div className="px-4 py-2 border-t border-border bg-bg shrink-0" data-testid="auth-footer">
          <div className="flex items-center gap-2">
            <div className={`w-7 h-7 rounded-full flex items-center justify-center text-micro font-bold ${user.role === "super_admin" ? "bg-brand text-fg" : "bg-ink text-ink-fg"}`}>
              {(user.full_name || user.username || "?").slice(0, 1).toUpperCase()}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[12px] font-bold text-fg truncate" data-testid="auth-username">
                {user.full_name || user.username}
              </div>
              <div className="text-micro text-fg-muted truncate flex items-center gap-1">
                <Building2 className="w-2.5 h-2.5" />
                {user.role === "super_admin" ? "All tenants" : (tenant?.name || user.tenant_id || "—")}
                <span className="ml-1 px-1 py-0.5 rounded-sm bg-surface border border-border uppercase tracking-wider text-micro">
                  {user.role === "super_admin" ? "Admin" : user.role === "tenant_admin" ? "T-Admin" : "User"}
                </span>
              </div>
            </div>
            <button
              type="button"
              data-testid="auth-logout"
              onClick={logout}
              title="Sign out"
              className="p-1.5 rounded-sm text-rose-500 hover:bg-surface"
            >
              <LogOut className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}

      {menuOpen && (
        <Suspense fallback={null}>
          <SettingsMenu
            open={menuOpen}
            onOpenChange={setMenuOpen}
            onRefreshApp={refreshApp}
            refreshBusy={refreshBusy}
          />
        </Suspense>
      )}
    </aside>
    </>
  );
}
