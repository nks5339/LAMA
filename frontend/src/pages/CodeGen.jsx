import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import Editor from "@monaco-editor/react";
import {
  Loader2,
  Sparkles,
  Download,
  Lock,
  Pencil,
  Check,
  Send,
  Code2,
  FileText,
  RotateCcw,
  ChevronDown,
  ChevronRight as ChevronRightIcon,
  Github,
  PackageCheck,
  Wand2,
  FolderOpen,
  Folder,
  File as FileIcon,
  ShieldCheck,
  Monitor,
  Pause,
  Play,
  Square,
  Trash2,
  // iter-13.119 — Auto-Validate & Improve
  Target,
  X,
  // iter-13.140 — Kebab overflow menu
  MoreVertical,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import { useIsMobile } from "@/hooks/useBreakpoint";
import { EmptyState } from "@/components/ux";
import {
  startCodegenJob,
  startGapRecoveryBackend,
  startGapRecoveryFrontend,
  getCodegenJob,
  pauseCodegenJob,
  resumeCodegenJob,
  stopCodegenJob,
  listCodegenFiles,
  getCodegenApiMapping,
  getCodegenFile,
  updateCodegenFile,
  deleteCodegenFile,
  deleteCodegenPath,
  startCodegenZipDownload,
  exportCodegenToDisk,
  getCodegenExportRoot,
  startGithubPushJob,
  sendCodegenChat,
  applyCodegenFileChange,
  freezeCodegen,
  resetCodegen,
  // iter-13.119 — Auto-Validate & Improve loop
  startAutoValidate,
  getParityReport,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import ConfidenceBadge from "@/components/ConfidenceBadge";
// iter-13.119 — render the markdown Auto-Validate report in the modal
// iter-17 — Multi-Agent CodeGen pipeline panel (feature-flagged by the
// header mode toggle; single-shot Quick Generate flow below is preserved
// verbatim).
import CodeGenMultiAgentPanel from "@/components/CodeGenMultiAgentPanel";

// language detection by extension
const EXT_LANG = {
  js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  py: "python", java: "java", go: "go", rs: "rust", rb: "ruby",
  json: "json", yml: "yaml", yaml: "yaml", md: "markdown", txt: "plaintext",
  sql: "sql", xml: "xml", html: "html", css: "css", sh: "shell",
  Dockerfile: "dockerfile", dockerfile: "dockerfile", env: "ini",
};
function pathLang(path = "") {
  const base = path.split("/").pop() || path;
  if (base.toLowerCase() === "dockerfile") return "dockerfile";
  const ext = base.split(".").pop();
  return EXT_LANG[ext] || "plaintext";
}

// -----------------------------------------------------------
// Reset modal
// -----------------------------------------------------------
function ResetModal({ open, onClose, onConfirm, title, warning }) {
  const [typed, setTyped] = useState("");
  useEffect(() => { if (!open) setTyped(""); }, [open]);
  if (!open) return null;
  const enabled = typed === "RESET";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40" data-testid="codegen-reset-modal">
      <div className="bg-surface max-w-md w-full rounded-sm border-2 border-orange-500 p-5">
        <h3 className="font-display font-bold text-lg text-orange-700 flex items-center gap-2"><RotateCcw className="w-4 h-4" /> {title}</h3>
        <p className="text-xs text-fg mt-2 leading-snug">{warning}</p>
        <p className="text-xs text-fg-muted mt-3">Type <code className="bg-bg px-1">RESET</code> to confirm.</p>
        <input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="codegen-reset-input" className="mt-1 w-full border border-border focus:border-orange-500 outline-none px-2 py-1.5 text-sm rounded-sm" />
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose} className="text-xs px-3 py-1.5 border border-border rounded-sm">Cancel</button>
          <button disabled={!enabled} onClick={onConfirm} data-testid="codegen-reset-confirm" className={`text-xs px-3 py-1.5 rounded-sm font-bold text-white ${enabled ? "bg-orange-600 hover:bg-orange-700" : "bg-orange-300 cursor-not-allowed"}`}>Reset Stage 4</button>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------
// File tree nesting helper (group flat paths into nested folders)
// -----------------------------------------------------------

// iter-13.119.1 — Auto-Validate launch modal. Lets the user pick a single
// service (or "All services"), confidence threshold, max-iterations and
// max-files-per-iter before kicking off the loop. The backend orchestrator
// already supports `only_service`; this modal just makes it discoverable.
function AutoValidateModal({ open, onClose, onStart, services, defaultService }) {
  const [service, setService] = useState(defaultService || "");
  const [threshold, setThreshold] = useState(95);
  const [maxIter, setMaxIter] = useState(5);
  const [maxFiles, setMaxFiles] = useState(20);
  useEffect(() => {
    if (open) setService(defaultService || "");
  }, [open, defaultService]);
  if (!open) return null;
  const valid =
    threshold >= 50 && threshold <= 100 &&
    maxIter >= 1 && maxIter <= 10 &&
    maxFiles >= 1 && maxFiles <= 50;
  return (
    <div aria-hidden="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
      data-testid="auto-validate-modal"
      onClick={onClose}
    >
      <div role="presentation"
        className="bg-surface max-w-md w-full rounded-sm border-2 border-emerald-500 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="font-display font-bold text-base text-emerald-700 flex items-center gap-2">
          <span>🎯</span> Auto-Validate &amp; Improve
        </h3>
        <p className="text-micro text-fg-muted mt-1 leading-snug">
          Scores every generated source file across 6 axes, then runs
          gap-recovery on the worst files and re-scores — looping until
          confidence ≥ threshold (or max iterations is hit).
        </p>

        <div className="mt-4 space-y-3">
          <div>
            <label className="text-micro font-bold text-fg" htmlFor="av-service">
              Service scope
            </label>
            <select
              id="av-service"
              data-testid="av-service-select"
              value={service}
              onChange={(e) => setService(e.target.value)}
              className="mt-1 w-full text-[12px] border border-border focus:border-emerald-500 outline-none px-2 py-1.5 rounded-sm bg-surface"
            >
              <option value="">All services ({services.length})</option>
              {services.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <div className="text-micro text-fg-muted mt-0.5">
              {service
                ? `Only "${service}" will be scored & repaired. Other services untouched.`
                : "Every service will be scored & repaired. Recommended for first run."}
            </div>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="text-micro font-bold text-fg" htmlFor="av-threshold">
                Threshold %
              </label>
              <input
                id="av-threshold"
                data-testid="av-threshold"
                type="number"
                min={50}
                max={100}
                step={1}
                value={threshold}
                onChange={(e) => setThreshold(parseFloat(e.target.value) || 0)}
                className="mt-1 w-full text-[12px] border border-border focus:border-emerald-500 outline-none px-2 py-1.5 rounded-sm"
              />
            </div>
            <div>
              <label className="text-micro font-bold text-fg" htmlFor="av-max-iter">
                Max iter
              </label>
              <input
                id="av-max-iter"
                data-testid="av-max-iter"
                type="number"
                min={1}
                max={10}
                step={1}
                value={maxIter}
                onChange={(e) => setMaxIter(parseInt(e.target.value, 10) || 0)}
                className="mt-1 w-full text-[12px] border border-border focus:border-emerald-500 outline-none px-2 py-1.5 rounded-sm"
              />
            </div>
            <div>
              <label className="text-micro font-bold text-fg" htmlFor="av-max-files">
                Files/iter
              </label>
              <input
                id="av-max-files"
                data-testid="av-max-files"
                type="number"
                min={1}
                max={50}
                step={1}
                value={maxFiles}
                onChange={(e) => setMaxFiles(parseInt(e.target.value, 10) || 0)}
                className="mt-1 w-full text-[12px] border border-border focus:border-emerald-500 outline-none px-2 py-1.5 rounded-sm"
              />
            </div>
          </div>
          <div className="text-micro text-fg-muted">
            Threshold 50–100 · Max iterations 1–10 · Files/iter 1–50 ·
            Each in-flight recovery is an LLM call (Semaphore=2 concurrent).
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 border border-border rounded-sm"
          >
            Cancel
          </button>
          <button
            data-testid="av-start"
            disabled={!valid}
            onClick={() => onStart({
              serviceName: service,
              threshold,
              maxIterations: maxIter,
              maxFilesPerIter: maxFiles,
            })}
            className={`text-xs px-3 py-1.5 rounded-sm font-bold text-white ${
              valid ? "bg-emerald-600 hover:bg-emerald-700" : "bg-emerald-300 cursor-not-allowed"
            }`}
          >
            Start Auto-Validate
          </button>
        </div>
      </div>
    </div>
  );
}

function buildTree(files) {
  // files: [{ path, ... }]
  const root = { name: "", dirs: {}, files: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let cur = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const p = parts[i];
      if (!cur.dirs[p]) cur.dirs[p] = { name: p, dirs: {}, files: [] };
      cur = cur.dirs[p];
    }
    cur.files.push({ ...f, basename: parts[parts.length - 1] });
  }
  return root;
}

// Flatten tree to a list of rows (no recursive JSX)
function flattenTree(root, expanded) {
  const rows = [];
  const walk = (node, depth, baseKey) => {
    const dirs = Object.values(node.dirs).sort((a, b) => a.name.localeCompare(b.name));
    for (const d of dirs) {
      const key = baseKey ? `${baseKey}/${d.name}` : d.name;
      const isOpen = expanded[key] !== false; // default open
      rows.push({ kind: "dir", key, name: d.name, depth, isOpen });
      if (isOpen) walk(d, depth + 1, key);
    }
    for (const f of node.files) {
      rows.push({ kind: "file", key: f.id, depth: depth + 1, file: f });
    }
  };
  walk(root, 0, "");
  return rows;
}

// -----------------------------------------------------------
// useJobPoll — with persistence for tab switches
// -----------------------------------------------------------
const CODEGEN_JOB_KEY = "lama:codegen:activeJob";

function useJobPoll(getter, onComplete, persistKey = null) {
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const startId = useRef(null);
  
  // On mount, restore persisted job if exists
  useEffect(() => {
    if (!persistKey) return;
    try {
      const saved = localStorage.getItem(persistKey);
      if (saved) {
        const { jid, status } = JSON.parse(saved);
        if (jid && status !== "complete" && status !== "error" && status !== "stopped") {
          startId.current = jid;
          setJob({ id: jid, status: "restoring", step: "Restoring session…", pct: 0 });
          setRunning(true);
        } else {
          localStorage.removeItem(persistKey);
        }
      }
    } catch (_e) { /* ignore */ }
  }, [persistKey]);

  useEffect(() => {
    if (!startId.current || !job) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const j = await getter(startId.current);
        if (cancelled) return;
        setJob(j);
        // Persist status for restore
        if (persistKey) {
          localStorage.setItem(persistKey, JSON.stringify({ jid: j.id, status: j.status }));
        }
        // iter-13.54 — `stopped` is also terminal (user clicked Stop).
        if (j.status === "complete" || j.status === "error" || j.status === "stopped") {
          setRunning(false);
          if (persistKey) localStorage.removeItem(persistKey);
          if (j.status === "complete" && onComplete) onComplete(j);
          return;
        }
      } catch (e) {
        // If job not found (404), clear persistence
        if (persistKey) localStorage.removeItem(persistKey);
      }
      if (!cancelled) setTimeout(tick, 2000);
    };
    setRunning(true);
    tick();
    return () => { cancelled = true; };
  }, [job?.id]); // eslint-disable-line

  const start = (jid) => {
    startId.current = jid;
    setJob({ id: jid, status: "queued", step: "Starting…", pct: 0 });
    setRunning(true);
    if (persistKey) {
      localStorage.setItem(persistKey, JSON.stringify({ jid, status: "queued" }));
    }
  };
  
  return { job, running, start };
}

// -----------------------------------------------------------
// iter-13.119 — Auto-Validate Report dashboard helpers
// -----------------------------------------------------------

function _latestIteration(report) {
  const iters = report?.iterations || [];
  return iters.length ? iters[iters.length - 1] : null;
}

function _scoreColor(score) {
  if (score == null) return "bg-border text-fg";
  if (score >= 95) return "bg-emerald-100 text-emerald-800";
  if (score >= 80) return "bg-lime-100 text-lime-800";
  if (score >= 60) return "bg-amber-100 text-amber-800";
  return "bg-red-100 text-red-800";
}

function _barColor(score) {
  if (score == null) return "bg-border";
  if (score >= 95) return "bg-emerald-500";
  if (score >= 80) return "bg-lime-500";
  if (score >= 60) return "bg-amber-500";
  return "bg-red-500";
}

function LiveLogTab({ lines, live, currentStep, inFlightCount }) {
  const preRef = React.useRef(null);
  const stickyBottom = React.useRef(true);

  React.useEffect(() => {
    const el = preRef.current;
    if (!el) return;
    if (stickyBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines.length]);

  const onScroll = () => {
    const el = preRef.current;
    if (!el) return;
    const slack = 16;
    stickyBottom.current =
      el.scrollHeight - (el.scrollTop + el.clientHeight) < slack;
  };

  return (
    <div className="space-y-2">
      {live && currentStep && (
        <div className="text-micro bg-emerald-50 border border-emerald-200 text-emerald-900 px-3 py-1.5 rounded-sm flex items-center gap-2">
          <Loader2 className="w-3 h-3 animate-spin shrink-0" />
          <span className="font-bold">Now:</span>
          <span className="truncate flex-1">{currentStep}</span>
          {inFlightCount > 0 && (
            <span className="text-micro bg-emerald-200 text-emerald-900 px-1.5 py-0.5 rounded-sm font-bold whitespace-nowrap">
              {inFlightCount} LLM call{inFlightCount === 1 ? "" : "s"} in-flight
            </span>
          )}
        </div>
      )}
      {!lines.length && (
        <div className="text-micro text-fg-muted">
          {live
            ? "Waiting for first log line from the orchestrator…"
            : "No log lines recorded for this run."}
        </div>
      )}
      {lines.length > 0 && (
        <pre
          ref={preRef}
          onScroll={onScroll}
          data-testid="parity-live-log"
          className="text-micro leading-[1.45] font-mono bg-ink text-border p-3 rounded-sm overflow-y-auto max-h-[55vh] whitespace-pre-wrap"
        >
          {lines.map((l, i) => {
            let cls = "text-border";
            if (l.startsWith("✓")) cls = "text-emerald-400";
            else if (l.startsWith("✗") || l.startsWith("⚠")) cls = "text-red-400";
            else if (l.startsWith("→")) cls = "text-sky-300";
            else if (l.startsWith("━")) cls = "text-brand font-bold";
            else if (l.startsWith("   ")) cls = "text-fg-subtle";
            return (
              <div key={`${i}-${l.slice(0, 24)}`} className={cls}>
                {l}
              </div>
            );
          })}
        </pre>
      )}
      <div className="text-micro text-fg-muted flex items-center justify-between">
        <span>
          {lines.length} line(s) · keeps the last 200 from the orchestrator
          {live && " · refreshing every 1s"}
        </span>
        {live && inFlightCount > 0 && (
          <span className="text-emerald-700 flex items-center gap-1">
            <Loader2 className="w-2.5 h-2.5 animate-spin" />
            {inFlightCount} file{inFlightCount === 1 ? "" : "s"} being recovered…
          </span>
        )}
      </div>
    </div>
  );
}

function ParityReportHeader({ report, live, onClose }) {
  const iters = report?.iterations || [];
  const last = _latestIteration(report);
  const overall = last?.overall_score ?? report?.final_score ?? null;
  const threshold = report?.threshold ?? 95;
  const status = report?.status || (live ? "running" : "—");
  const converged =
    report?.converged ?? (overall != null && overall >= threshold);
  return (
    <div className="px-5 py-3 border-b border-border flex items-center gap-3 flex-wrap">
      <Target className="w-4 h-4 text-emerald-600 shrink-0" />
      <h3 className="font-display font-bold text-base text-fg flex-1 min-w-0">
        Auto-Validate &amp; Improve — Live Confidence Dashboard
      </h3>
      <div
        className="flex items-center gap-1.5 text-micro"
        data-testid="codegen-iteration-count"
      >
        <span className="text-fg-muted">Iter</span>
        <span className="font-bold text-fg" data-testid="parity-iter-count">
          {iters.length}
        </span>
        <span className="text-fg-muted">/ {report?.max_iterations ?? "?"}</span>
      </div>
      <div className="flex items-center gap-1.5 text-micro">
        <span className="text-fg-muted">Threshold</span>
        <span className="font-bold text-fg">{Number(threshold).toFixed(0)}%</span>
      </div>
      {overall != null && (
        <span
          className={`text-micro px-2 py-0.5 rounded-sm font-bold ${_scoreColor(overall)}`}
          data-testid="parity-report-score"
          data-codegen-testid="codegen-confidence-badge"
        >
          {Number(overall).toFixed(2)}%{" "}
          {status === "complete"
            ? converged
              ? "PASS ✓"
              : "BELOW ⚠"
            : status === "running"
            ? "RUNNING…"
            : status === "stopped"
            ? "STOPPED"
            : status === "error"
            ? "ERROR"
            : ""}
        </span>
      )}
      {/* iter-14.21 — mirror testids the ownership agent contract requires
          without breaking the existing parity-* testids the smoke suite
          asserts on. Same DOM node is queryable under both names. */}
      {overall != null && (
        <span
          data-testid="codegen-confidence-badge"
          className="sr-only"
          aria-hidden="true"
        >
          {Number(overall).toFixed(2)}
        </span>
      )}
      {/* iter-14.21 — inline history sparkline (score-per-iteration). */}
      {iters.length > 0 && (
        <div
          className="flex items-center gap-0.5 text-micro text-fg-muted"
          data-testid="codegen-confidence-history"
          title={iters
            .map((r, i) => `#${i + 1}: ${Number(r.overall_score || 0).toFixed(1)}%`)
            .join(" → ")}
        >
          {iters.map((r, i) => (
            <span
              key={i}
              className={`px-1 rounded-sm ${_scoreColor(r.overall_score || 0)}`}
            >
              {Number(r.overall_score || 0).toFixed(0)}
            </span>
          ))}
        </div>
      )}
      <button
        onClick={onClose}
        data-testid="parity-report-close"
        className="ml-1 p-1 hover:bg-bg rounded-sm"
        title="Close"
      >
        <X className="w-4 h-4 text-fg-muted" />
      </button>
    </div>
  );
}

function ParityReportTabs({ report, tab, onTabChange, logLineCount }) {
  const last = _latestIteration(report);
  const services = last?.services || [];
  const feCount = services.filter((s) => s.frontend).length;
  const beCount = services.length - feCount;
  return (
    <div className="px-5 pt-2 border-b border-border flex items-center gap-1 text-[12px]">
      {[
        { key: "log", label: "Live Log", count: logLineCount || 0, color: "border-fg text-fg" },
        { key: "backend", label: "Backend", count: beCount, color: "border-emerald-500 text-emerald-700" },
        { key: "frontend", label: "Frontend", count: feCount, color: "border-sky-500 text-sky-700" },
        { key: "trajectory", label: "Trajectory", count: (report?.iterations || []).length, color: "border-brand text-fg" },
      ].map((t) => {
        const active = tab === t.key;
        return (
          <button
            key={t.key}
            data-testid={`parity-tab-${t.key}`}
            onClick={() => onTabChange(t.key)}
            className={`px-3 py-1.5 border-b-2 ${
              active ? t.color : "border-transparent text-fg-muted hover:text-fg"
            }`}
          >
            {t.label}{" "}
            <span className="text-micro font-bold ml-1 bg-bg px-1 rounded-sm">
              {t.count}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function AxisBar({ name, score, weight, detail }) {
  return (
    <div className="text-micro">
      <div className="flex items-center justify-between gap-2">
        <span className="text-fg-muted capitalize">
          {name}{" "}
          <span className="text-micro text-fg-subtle">
            ({Math.round((weight || 0) * 100)}%)
          </span>
        </span>
        <span className="font-bold text-fg">{Number(score).toFixed(0)}%</span>
      </div>
      <div className="h-1 bg-bg rounded-sm overflow-hidden">
        <div
          className={`h-full ${_barColor(score)}`}
          style={{ width: `${Math.max(0, Math.min(100, score))}%` }}
        />
      </div>
      {detail && (
        <div className="text-micro text-fg-subtle truncate" title={detail}>
          {detail}
        </div>
      )}
    </div>
  );
}

function ServiceCard({
  svc,
  expanded,
  onToggle,
  expandedFiles,
  onToggleFile,
}) {
  const files = svc.files || [];
  const wfMax = 6; // weights per file row
  return (
    <div className="border border-border rounded-sm bg-surface" data-testid={`parity-svc-${svc.service}`}>
      <button
        onClick={onToggle}
        className="w-full px-3 py-2 flex items-center gap-2 hover:bg-bg"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-fg-muted" />
        ) : (
          <ChevronRightIcon className="w-3 h-3 text-fg-muted" />
        )}
        <span className="font-mono text-[12px] text-fg truncate flex-1 text-left">
          {svc.service}
        </span>
        <span className="text-micro text-fg-muted">{svc.file_count} files</span>
        <span className="text-micro text-fg-muted">
          EP {Math.round(svc.endpoint_coverage_pct || 0)}% · TBL{" "}
          {Math.round(svc.table_coverage_pct || 0)}% · COL{" "}
          {Math.round(svc.column_coverage_pct || 0)}%
        </span>
        <span
          className={`text-micro px-1.5 py-0.5 rounded-sm font-bold ${_scoreColor(svc.score)}`}
        >
          {Number(svc.score).toFixed(1)}%
        </span>
      </button>
      {expanded && (
        <div className="border-t border-surface-2 divide-y divide-surface-2">
          {files.length === 0 && (
            <div className="px-3 py-2 text-micro text-fg-muted">
              No scored files in this service.
            </div>
          )}
          {files.map((f) => {
            const isOpen = !!expandedFiles[f.file_path];
            return (
              <div key={f.file_path} data-testid={`parity-file-${f.file_path}`}>
                <button
                  onClick={() => onToggleFile(f.file_path)}
                  className="w-full px-3 py-1.5 flex items-center gap-2 hover:bg-surface-2"
                >
                  {isOpen ? (
                    <ChevronDown className="w-3 h-3 text-fg-muted" />
                  ) : (
                    <ChevronRightIcon className="w-3 h-3 text-fg-muted" />
                  )}
                  <FileIcon className="w-3 h-3 text-fg-muted" />
                  <span className="font-mono text-micro text-fg truncate flex-1 text-left">
                    {f.file_path}
                  </span>
                  <span className="text-micro uppercase text-fg-subtle">{f.file_type}</span>
                  {!f.recoverable && (
                    <span className="text-micro text-fg-subtle italic">non-recoverable</span>
                  )}
                  <span
                    className={`text-micro px-1.5 py-0.5 rounded-sm font-bold ${_scoreColor(f.score)}`}
                  >
                    {Number(f.score).toFixed(0)}%
                  </span>
                </button>
                {isOpen && (
                  <div className="px-6 py-2 bg-surface-2 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5">
                    {(f.components || []).slice(0, wfMax).map((c) => (
                      <AxisBar
                        key={c.name}
                        name={c.name}
                        score={c.score}
                        weight={c.weight}
                        detail={c.detail}
                      />
                    ))}
                    {(f.issues || []).length > 0 && (
                      <div className="col-span-full mt-1 text-micro text-red-700">
                        <div className="font-bold mb-0.5">Issues</div>
                        <ul className="list-disc pl-4 space-y-0.5">
                          {f.issues.map((i, idx) => (
                            <li key={idx}>{i}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ParityReportBody({
  report,
  tab,
  live,
  jobLog,
  currentStep,
  expandedSvcs,
  onToggleSvc,
  expandedFiles,
  onToggleFile,
}) {
  // Live Log tab — always renderable, even when no report row exists yet,
  // because the in-memory job log is populated immediately by the
  // orchestrator and pollled via getCodegenJob.
  if (tab === "log") {
    const lines = jobLog || [];
    // iter-13.119 — derive in-flight recovery count from the log so the
    // user can tell at a glance how many LLM calls are running right now
    // (Semaphore(2) → typically 2 in-flight during the recovery phase).
    const inFlightFiles = new Set();
    for (const l of lines) {
      const startM = l.match(/^→ iter\d+ recovering (.+) \(score=/);
      if (startM) {
        inFlightFiles.add(startM[1].replace(/…$/, "").trim());
        continue;
      }
      const doneM = l.match(/^[✓✗] iter\d+ (\S+)/);
      if (doneM) {
        // Match by suffix because the "done" path is the bare file_path.
        for (const k of Array.from(inFlightFiles)) {
          if (k.endsWith(doneM[1]) || doneM[1].endsWith(k)) inFlightFiles.delete(k);
        }
      }
    }
    return (
      <LiveLogTab
        lines={lines}
        live={live}
        currentStep={currentStep}
        inFlightCount={inFlightFiles.size}
      />
    );
  }

  if (!report) {
    return (
      <div className="text-xs text-fg-muted flex items-center gap-2 py-4">
        <Loader2 className="w-3 h-3 animate-spin" /> Loading run…
      </div>
    );
  }
  const last = _latestIteration(report);

  // Trajectory tab — iteration history
  if (tab === "trajectory") {
    const iters = report.iterations || [];
    if (!iters.length) {
      return (
        <div className="text-xs text-fg-muted py-4">
          No iterations recorded yet — the run is initialising.
        </div>
      );
    }
    return (
      <div className="space-y-2">
        <table className="w-full text-micro">
          <thead>
            <tr className="text-left text-fg-muted border-b border-border">
              <th className="py-1.5 pr-2">#</th>
              <th className="py-1.5 pr-2">Score</th>
              <th className="py-1.5 pr-2">Files &lt; threshold</th>
              <th className="py-1.5 pr-2">Recovery</th>
              <th className="py-1.5">Notes</th>
            </tr>
          </thead>
          <tbody>
            {iters.map((it) => (
              <tr key={it.iteration} className="border-b border-surface-2">
                <td className="py-1 pr-2 font-bold">{it.iteration}</td>
                <td className="py-1 pr-2">
                  <span className={`px-1.5 py-0.5 rounded-sm font-bold ${_scoreColor(it.overall_score)}`}>
                    {Number(it.overall_score).toFixed(2)}%
                  </span>
                </td>
                <td className="py-1 pr-2">{it.files_below_threshold}</td>
                <td className="py-1 pr-2 text-fg-muted">
                  {it.recovery
                    ? `${it.recovery.applied}/${it.recovery.targets} applied`
                    : "—"}
                </td>
                <td className="py-1 text-fg-muted truncate max-w-md">{it.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {report.error && (
          <div className="text-micro text-red-700 bg-red-50 border border-red-200 p-2 rounded-sm">
            Error: {report.error}
          </div>
        )}
      </div>
    );
  }

  // FE / BE service tabs
  const services = (last?.services || []).filter((s) =>
    tab === "frontend" ? s.frontend : !s.frontend,
  );
  // Worst services first — fastest path to "what should I look at?"
  services.sort((a, b) => (a.score ?? 0) - (b.score ?? 0));

  if (!services.length) {
    return (
      <div className="text-xs text-fg-muted py-4">
        {live
          ? "Awaiting first score… the dashboard will populate as soon as iteration 1 completes."
          : `No ${tab} services in this run.`}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {services.map((svc) => (
        <ServiceCard
          key={svc.service}
          svc={svc}
          expanded={!!expandedSvcs[svc.service]}
          onToggle={() => onToggleSvc(svc.service)}
          expandedFiles={expandedFiles}
          onToggleFile={onToggleFile}
        />
      ))}
    </div>
  );
}

// -----------------------------------------------------------
// Main
// -----------------------------------------------------------
export default function CodeGenPage() {
  const navigate = useNavigate();
  const { active } = useProjects();
  const isMobile = useIsMobile();
  const projectId = active?.id;
  const archStatus = active?.stage_status?.["Architecture"] || "locked";
  const status = active?.stage_status?.["CodeGen"] || "locked";
  // iter-13.66 alignment — "skipped" unlocks downstream just like "frozen".
  const isLocked = archStatus !== "frozen" && archStatus !== "skipped";
  const isFrozen = status === "frozen";

  const [tree, setTree] = useState([]);     // [{ name, files: [...] }]
  const [totalFiles, setTotalFiles] = useState(0);
  const [expanded, setExpanded] = useState({});
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileContent, setFileContent] = useState("");
  const [editBuf, setEditBuf] = useState("");
  const [editing, setEditing] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [convId, setConvId] = useState(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [zipBusy, setZipBusy] = useState(false);
  const [model] = useState("");  // iter-13.30: empty → Console resolves via AGENT_COMPLEXITY["codegen.service"]
  const [filterService, setFilterService] = useState("");

  // iter-17 — top-level mode toggle. "quick" = existing single-shot
  // generate flow (default; preserved verbatim). "multi-agent" = new
  // Phase-1..3 Context-Manager → Planner → Coders/Verifier/Reviewer/
  // Tester → Traceability-Gate → Finalizer pipeline. Persisted in
  // localStorage so refreshes stay in the mode the operator picked.
  const [codegenMode, setCodegenMode] = useState(() => {
    try {
      const v = window.localStorage.getItem("lama:codegen:mode");
      return v === "multi-agent" ? "multi-agent" : "quick";
    } catch (_) { return "quick"; }
  });
  useEffect(() => {
    try { window.localStorage.setItem("lama:codegen:mode", codegenMode); } catch (_) {
    // localStorage/CustomEvent may be unavailable (private mode,
    // blocked site data). The feature degrades; it never fails.
    }
  }, [codegenMode]);

  // iter-13.120 — Multi-select for code-generation scope. The user
  // picks WHICH services to (re)generate before clicking Generate All.
  // Defaults to "everything checked" on first load; we never re-check a
  // service the user explicitly unchecked (`selectedGenTouched` guards).
  //
  // `archServices` is populated by the /files endpoint (iter-13.120
  // extension): [{ name, frontend, backend_lang, kind, display_name }].
  const [archServices, setArchServices] = useState([]);
  const [archPattern, setArchPattern] = useState("");
  // iter-13.122 — Legacy → New API mapping preview. Loaded from the new
  // /api/codegen/{pid}/api-mapping endpoint whenever the CodeGen page
  // opens (and after any refresh). Collapsible; expanded by default when
  // no code has been generated yet so the user validates coverage
  // BEFORE the first Generate.
  const [apiMapping, setApiMapping] = useState(null);
  const [apiMappingOpen, setApiMappingOpen] = useState(true);
  const [apiMappingSvc, setApiMappingSvc] = useState("");
  const [selectedGenServices, setSelectedGenServices] = useState(() => new Set());
  const [, setSelectedGenTouched] = useState(false);
  const [gsPopoverOpen, setGsPopoverOpen] = useState(false);
  const gsPopoverRef = useRef(null);
  // iter-13.140 — Kebab overflow menu state
  const [kebabOpen, setKebabOpen] = useState(false);
  const kebabRef = useRef(null);
  useEffect(() => {
    if (!kebabOpen) return;
    const close = (e) => {
      if (kebabRef.current && kebabRef.current.contains(e.target)) return;
      setKebabOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [kebabOpen]);
  useEffect(() => {
    if (!gsPopoverOpen) return;
    const close = (e) => {
      if (gsPopoverRef.current && gsPopoverRef.current.contains(e.target)) return;
      setGsPopoverOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") setGsPopoverOpen(false); });
    return () => window.removeEventListener("mousedown", close);
  }, [gsPopoverOpen]);

  // iter-13.56 — right-click context menu over the file tree.
  // ctxMenu = { x, y, kind: "file"|"dir", file?, dirKey? } | null
  const [ctxMenu, setCtxMenu] = useState(null);
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [ctxMenu]);

  const openCtxMenu = (e, payload) => {
    e.preventDefault();
    e.stopPropagation();
    // clamp inside viewport
    const x = Math.min(e.clientX, window.innerWidth - 200);
    const y = Math.min(e.clientY, window.innerHeight - 80);
    setCtxMenu({ x, y, ...payload });
  };

  const onDeleteFile = async (f) => {
    if (!f?.id) return;
    if (!window.confirm(`Delete file?\n\n${f.path}\n\nThis removes it from the codegen workspace. Re-run Generate / Gap Recovery to recreate.`)) return;
    try {
      await deleteCodegenFile(projectId, f.id);
      toast.success(`Deleted ${f.path}`);
      if (selectedFile?.id === f.id) {
        setSelectedFile(null);
        setFileContent("");
        setEditing(false);
      }
      await refresh();
    } catch (e) {
      toast.error("Delete failed: " + (e?.response?.data?.detail || e.message));
    }
  };

  const onDeleteDir = async (dirKey) => {
    if (!dirKey) return;
    if (!window.confirm(
      `Delete folder and ALL its files?\n\n${dirKey}\n\n` +
      "This removes every generated file under this path (recursive). " +
      "The action is logged to audit_log. Continue?"
    )) return;
    try {
      const r = await deleteCodegenPath(projectId, dirKey, filterService || null);
      toast.success(`Deleted ${r.deleted || 0} file(s) under ${dirKey}`);
      if (selectedFile && (selectedFile.path === dirKey || selectedFile.path.startsWith(dirKey + "/"))) {
        setSelectedFile(null);
        setFileContent("");
        setEditing(false);
      }
      await refresh();
    } catch (e) {
      toast.error("Delete failed: " + (e?.response?.data?.detail || e.message));
    }
  };

  // iter-13.100 — Persist main codegen job so tab switches don't lose state
  const genJob = useJobPoll(getCodegenJob, () => refresh(), CODEGEN_JOB_KEY);
  const pushJob = useJobPoll(getCodegenJob);
  const gapBackJob = useJobPoll(getCodegenJob, () => refresh());
  const gapFrontJob = useJobPoll(getCodegenJob, () => refresh());
  // iter-13.119 — Auto-Validate & Improve loop. Uses the same job-poll
  // hook so it gets the same pause / resume / stop controls for free.
  const autoValidateJob = useJobPoll(getCodegenJob, () => {
    refresh();
    // Auto-open the report modal when the loop terminates.
    loadParityReport().catch(() => {});
  });
  const [parityReport, setParityReport] = useState(null);
  const [parityReportOpen, setParityReportOpen] = useState(false);
  // iter-13.119 — dashboard UI state
  const [reportTab, setReportTab] = useState("log"); // "log" | "backend" | "frontend" | "trajectory"
  const [expandedSvcs, setExpandedSvcs] = useState({}); // { [svcName]: bool }
  const [expandedFiles, setExpandedFiles] = useState({}); // { [filePath]: bool }

  const loadParityReport = async () => {
    if (!projectId) return null;
    try {
      const r = await getParityReport(projectId);
      setParityReport(r);
      return r;
    } catch (_e) { return null; }
  };

  // iter-13.119 — live-poll the parity report while the modal is open and
  // the run is still in flight. Backend updates `parity_runs.iterations`
  // on every iteration so each tick refreshes the dashboard with the
  // latest per-service / per-file scores.
  useEffect(() => {
    if (!parityReportOpen || !projectId) return;
    let cancelled = false;
    let timer;
    const tick = async () => {
      if (cancelled) return;
      const r = await loadParityReport();
      const terminal = r && (r.status === "complete" || r.status === "error" || r.status === "stopped");
      if (!cancelled && !terminal) {
        timer = setTimeout(tick, 2000);
      }
    };
    tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [parityReportOpen, projectId, autoValidateJob.job?.status]);

  // iter-13.119 — faster log-only poll for the Live Log tab. useJobPoll
  // refreshes autoValidateJob.job every 2s but the user wants a console-
  // like feel. We poll getCodegenJob every 1s while the modal is open
  // and the run is in flight, and store the freshest snapshot in
  // liveJobSnapshot for the log/now-step rendering.
  const [liveJobSnapshot, setLiveJobSnapshot] = useState(null);
  useEffect(() => {
    const jid = autoValidateJob.job?.id;
    if (!parityReportOpen || !jid) {
      setLiveJobSnapshot(null);
      return;
    }
    let cancelled = false;
    let timer;
    const tick = async () => {
      if (cancelled) return;
      try {
        const j = await getCodegenJob(jid);
        if (!cancelled) setLiveJobSnapshot(j);
        const terminal = j.status === "complete" || j.status === "error" || j.status === "stopped";
        if (!cancelled && !terminal) timer = setTimeout(tick, 1000);
      } catch (_e) {
        if (!cancelled) timer = setTimeout(tick, 2000);
      }
    };
    tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [parityReportOpen, autoValidateJob.job?.id, autoValidateJob.job?.status]);

  const refresh = async () => {
    if (!projectId) return;
    try {
      const data = await listCodegenFiles(projectId);
      setTree(data.services || []);
      setTotalFiles(data.total_files || 0);
      // iter-13.121 — Reconcile `selectedGenServices` against the
      // freshly-loaded `arch` list. Stale names left over from a
      // previous refresh (e.g. two services the user merged in Stage 3
      // that no longer exist here) are dropped; brand-new services
      // (created by a merge or a re-run of `recommend`) are auto-
      // included so "Generate All" doesn't silently skip them. When the
      // reconciled set is empty (first load OR the user's picks were
      // all merged away) we default to "everything selected".
      const arch = Array.isArray(data.arch_services) ? data.arch_services : [];
      const nextArch = arch;
      setArchServices(nextArch);
      setArchPattern(
        typeof data.pattern === "string" && data.pattern ? data.pattern : ""
      );
      setSelectedGenServices((prev) => {
        const currentNames = new Set(nextArch.map((s) => s.name));
        const stillValid = new Set(
          Array.from(prev).filter((n) => currentNames.has(n))
        );
        if (stillValid.size === 0) {
          return new Set(currentNames);
        }
        for (const n of currentNames) stillValid.add(n);
        return stillValid;
      });
      // iter-13.122 — pull the deterministic legacy↔new API mapping
      // alongside the file tree so the preview stays in sync with the
      // latest arch_services state. Non-fatal: mapping errors just
      // leave the last-known snapshot visible.
      try {
        const m = await getCodegenApiMapping(projectId);
        setApiMapping(m);
      } catch (_e) { /* ignore — preview is best-effort */ }
    } catch (e) { /* ignore */ }
  };
  useEffect(() => { refresh(); }, [projectId]);

  useEffect(() => {
    if (genJob.job?.status === "error") toast.error("Codegen failed: " + genJob.job.error);
    // iter-13.54 — surface user-initiated stops
    if (genJob.job?.status === "stopped") toast.message("Codegen stopped: " + (genJob.job.step || "user request"));
    // iter-13.118.1 — Parity + Evidence audit summary. Always shown on
    // successful codegen so the user knows AT A GLANCE which services
    // ended up with deterministic scaffolds (LLM didn't pull legacy
    // logic) and which services had thin upstream evidence (Stage 1/2/3
    // didn't give the LLM enough to work with). Without this the user
    // had no signal beyond opening files one-by-one — exactly the loop
    // we're trying to break.
    if (genJob.job?.status === "complete") {
      const r = genJob.job.result || {};
      const parity = r.parity_report || [];
      const evidence = r.evidence_audit || [];
      const thin = evidence.filter((e) => e.verdict === "thin_evidence");
      const lowParitySvcs = parity
        .filter((p) => (p.parity_score ?? 1) < 0.6 && p.service !== "_root")
        .sort((a, b) => (a.parity_score ?? 1) - (b.parity_score ?? 1));
      const totalFiles = parity.reduce((s, p) => s + (p.files_total || 0), 0);
      const totalClean = parity.reduce((s, p) => s + (p.files_clean || 0), 0);
      const totalScaf  = parity.reduce((s, p) => s + (p.files_scaffolded || 0), 0);
      const totalTodo  = parity.reduce((s, p) => s + (p.files_with_todo || 0), 0);
      // iter-13.118.2 — Surface skeleton-only services. A non-frontend
      // service with < 12 planned files almost always means Stage 1/2/3
      // didn't produce real evidence and the codegen plan collapsed to
      // bootstrap-only (Dockerfile + pom.xml + Application.java + ...).
      // Parity score would be 100% in that case, hiding the gap — this
      // warning makes it explicit.
      const skeletonOnly = evidence
        .filter((e) => !e.frontend && (e.planned_file_count ?? 99) < 12);
      if (thin.length || lowParitySvcs.length || skeletonOnly.length) {
        const lines = [];
        if (totalFiles > 0) {
          lines.push(`Parity: ${totalClean}/${totalFiles} clean · ${totalScaf} scaffold · ${totalTodo} with TODO`);
        }
        if (skeletonOnly.length) {
          lines.push(
            `⚠ SKELETON ONLY (${skeletonOnly.length}): ` +
            skeletonOnly.slice(0, 5).map((e) =>
              `${e.service} (${e.planned_file_count} files — no Controller/Service/Entity emitted)`
            ).join("; ") +
            (skeletonOnly.length > 5 ? ` … +${skeletonOnly.length - 5} more` : "")
          );
          lines.push("→ Service has empty tables/endpoints. Re-run Stage 2 (DataModel) + Stage 3 (Architecture LLD) so the file plan can fan out per-resource.");
        }
        if (thin.length) {
          lines.push(
            `THIN EVIDENCE (${thin.length}): ` +
            thin.slice(0, 5).map((t) =>
              `${t.service} [${t.red_flags.join(", ")}]`
            ).join("; ") +
            (thin.length > 5 ? ` … +${thin.length - 5} more` : "")
          );
          lines.push("→ Re-run Stage 1 (KB) / Stage 2 (DDL) / Stage 3 (LLD) to deepen evidence.");
        }
        if (lowParitySvcs.length) {
          lines.push(
            `LOW PARITY (${lowParitySvcs.length}): ` +
            lowParitySvcs.slice(0, 5).map((p) =>
              `${p.service} ${(p.parity_score * 100).toFixed(0)}%`
            ).join("; ") +
            (lowParitySvcs.length > 5 ? ` … +${lowParitySvcs.length - 5} more` : "")
          );
          lines.push("→ Click Regenerate Backend / Frontend to enrich via Gap Recovery.");
        }
        toast.warning(lines.join("\n"), { duration: 20000 });
      } else if (totalFiles > 0) {
        toast.success(
          `Codegen complete · Parity ${totalClean}/${totalFiles} clean ` +
          `(${totalScaf} scaffold, ${totalTodo} TODO)`,
          { duration: 8000 }
        );
      }
    }
    if (pushJob.job?.status === "error") toast.error("GitHub push failed: " + pushJob.job.error);
    if (pushJob.job?.status === "complete") toast.success("Pushed to GitHub: " + (pushJob.job.result?.commit_sha?.slice(0, 8) || "ok"));
    if (gapBackJob.job?.status === "error") toast.error("Backend gap recovery failed: " + gapBackJob.job.error);
    if (gapBackJob.job?.status === "stopped") toast.message("Backend gap-recovery stopped: " + (gapBackJob.job.step || "user request"));
    if (gapBackJob.job?.status === "complete") {
      const r = gapBackJob.job.result || {};
      toast.success(`Backend gap recovery done — applied ${r.applied || 0}/${r.files_total || 0} files`);
    }
    if (gapFrontJob.job?.status === "error") toast.error("Frontend gap recovery failed: " + gapFrontJob.job.error);
    if (gapFrontJob.job?.status === "stopped") toast.message("Frontend gap-recovery stopped: " + (gapFrontJob.job.step || "user request"));
    if (gapFrontJob.job?.status === "complete") {
      const r = gapFrontJob.job.result || {};
      toast.success(`Frontend gap recovery done — applied ${r.applied || 0}/${r.files_total || 0} files`);
    }
    // iter-13.119 — Auto-Validate & Improve terminal-state toasts. Without
    // these the user has no signal that the loop finished beyond the
    // progress-bar disappearing.
    if (autoValidateJob.job?.status === "error") {
      toast.error("Auto-validate failed: " + autoValidateJob.job.error);
    }
    if (autoValidateJob.job?.status === "stopped") {
      toast.message("Auto-validate stopped: " + (autoValidateJob.job.step || "user request"));
    }
    if (autoValidateJob.job?.status === "complete") {
      const r = autoValidateJob.job.result || {};
      const score = (r.final_score ?? 0).toFixed(1);
      const thr = (r.threshold ?? 95).toFixed(0);
      if (r.converged) {
        toast.success(
          `Auto-validate ✓ converged at ${score}% (≥ ${thr}%) in ${r.iterations} iter · ` +
          `repaired ${r.applied || 0} file(s). Click Report for details.`,
          { duration: 12000 },
        );
      } else {
        toast.warning(
          `Auto-validate stopped at ${score}% (< ${thr}%) after ${r.iterations} iter. ` +
          `Click Report to see the per-axis breakdown and root-cause hints.`,
          { duration: 15000 },
        );
      }
    }
  }, [
    genJob.job?.status,
    pushJob.job?.status,
    gapBackJob.job?.status,
    gapFrontJob.job?.status,
    autoValidateJob.job?.status,
  ]);

  const flatFiles = useMemo(() => {
    const out = [];
    for (const s of tree) {
      if (filterService && s.name !== filterService) continue;
      for (const f of s.files) out.push({ ...f, service_name: s.name });
    }
    return out;
  }, [tree, filterService]);

  const builtTree = useMemo(() => buildTree(flatFiles), [flatFiles]);
  const treeRows = useMemo(() => flattenTree(builtTree, expanded), [builtTree, expanded]);
  const services = useMemo(() => tree.map((s) => s.name), [tree]);

  const onSelectFile = async (f) => {
    setSelectedFile(f);
    setEditing(false);
    setFileContent("Loading…");
    try {
      const doc = await getCodegenFile(projectId, f.id);
      setFileContent(doc.content || "");
    } catch (e) { toast.error("Could not load file"); }
  };

  const onGenerate = async () => {
    try {
      // iter-13.120 — Multi-select. Send the explicit list of picked
      // services when it's a strict subset; send null (= "all") when
      // every arch service is checked so the backend can also emit the
      // root-level files (docker-compose, ci.yml, README).
      const picked = archServices
        .filter((s) => selectedGenServices.has(s.name))
        .map((s) => s.name);
      if (archServices.length === 0) {
        toast.error("No architecture services found. Freeze Architecture (Stage 3) first.");
        return;
      }
      if (picked.length === 0) {
        toast.error("Pick at least one service to generate.");
        return;
      }
      const arg = picked.length === archServices.length ? null : picked;
      const r = await startCodegenJob(projectId, model, arg);
      genJob.start(r.job_id);
      toast.message(
        picked.length === archServices.length
          ? "Code generation started for all services"
          : `Code generation started for ${picked.length} service(s)`
      );
    } catch (e) { toast.error("Could not start: " + (e?.response?.data?.detail || e.message)); }
  };

  // iter-13.120 — Multi-select popover helpers.
  const toggleGenService = (name) => {
    setSelectedGenTouched(true);
    setSelectedGenServices((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
        next.add(`__excluded:${name}`);
      } else {
        next.add(name);
        next.delete(`__excluded:${name}`);
      }
      return next;
    });
  };
  const toggleAllGenServices = (checked) => {
    setSelectedGenTouched(true);
    if (checked) {
      setSelectedGenServices(new Set(archServices.map((s) => s.name)));
    } else {
      setSelectedGenServices(new Set(archServices.map((s) => `__excluded:${s.name}`)));
    }
  };
  const genFileCountFor = (svcName) => {
    const row = tree.find((t) => t.name === svcName);
    return row?.files?.length || 0;
  };
  const hasFrontendService = useMemo(
    () => archServices.some((s) => s.frontend),
    [archServices]
  );
  const selectedCount = useMemo(
    () => archServices.filter((s) => selectedGenServices.has(s.name)).length,
    [archServices, selectedGenServices]
  );
  const selectedBackendCount = useMemo(
    () => archServices.filter((s) => !s.frontend && selectedGenServices.has(s.name)).length,
    [archServices, selectedGenServices]
  );
  const selectedFrontendCount = useMemo(
    () => archServices.filter((s) => s.frontend && selectedGenServices.has(s.name)).length,
    [archServices, selectedGenServices]
  );
  const selectedPlannedFileCount = useMemo(() => {
    let n = 0;
    for (const s of archServices) {
      if (selectedGenServices.has(s.name)) n += genFileCountFor(s.name);
    }
    return n;
  }, [archServices, selectedGenServices, tree]);

  const onGenerateOne = async () => {
    if (!filterService) { toast.message("Select a service in the filter first"); return; }
    try {
      const r = await startCodegenJob(projectId, model, filterService);
      genJob.start(r.job_id);
    } catch (e) { toast.error("Could not start: " + (e?.response?.data?.detail || e.message)); }
  };

  const onRegenBackend = async () => {
    // iter-14.36 — surface file count in the confirm so the user knows
    // the wall-clock cost upfront. Entities + repositories are already
    // excluded backend-side (they're pure JPA glue from the OLTP DDL);
    // this dialog reflects the recoverable-scope count.
    let fileCountHint = "";
    try {
      const svcTag = filterService ? ` in service "${filterService}"` : "";
      let stats = 0;
      for (const svc of tree) {
        if (filterService && svc.name !== filterService) continue;
        for (const f of svc.files || []) {
          if (!f.file_path?.startsWith("services/")) continue;
          if (["entity", "repository", "config", "dockerfile", "docs"].includes(f.file_type)) continue;
          stats += 1;
        }
      }
      fileCountHint = `\n\nScope: ${stats} file(s)${svcTag} (entities + repositories are auto-scaffolded from the DDL and skipped).`;
      if (stats > 100 && !filterService) {
        fileCountHint += `\n\n⚠ Large scope — expect 15-30 min on local Ollama. Consider selecting a single service in the filter above first.`;
      }
    } catch { /* ignore — best-effort hint */ }
    if (!window.confirm(
      "Regenerate Backend Service?\n\n" +
      "Runs a legacy-parity gap scan over every generated backend controller / service / " +
      "validator / mapper / exception file (using an independent model — DIFF_ANALYSIS_MODEL) " +
      "and injects missing flows / validations / fields / role guards in place. " +
      "Existing code is preserved; only gaps are repaired." +
      fileCountHint +
      "\n\nContinue?"
    )) return;
    try {
      const r = await startGapRecoveryBackend(projectId, null, filterService || null);
      gapBackJob.start(r.job_id);
      toast.message(`Backend gap recovery started (model: ${r.model || "default"})`);
    } catch (e) { toast.error("Could not start: " + (e?.response?.data?.detail || e.message)); }
  };

  const onRegenFrontend = async () => {
    // iter-14.41 — Real 1:1 JSP → React MIGRATION job. Prompts the user
    // to run a full LLM migration pass (one JSP → one production
    // component) BEFORE the gap-recovery loop. This replaces the
    // iter-14.40 stub-first expander which was misleading.
    let runMigration = false;
    let planTotal = 0;
    try {
      const backend = process.env.REACT_APP_BACKEND_URL || "";
      const token = localStorage.getItem("lama:auth:token") || "";
      const plan = await fetch(
        `${backend}/api/codegen/${projectId}/frontend/plan-jsps`,
        { method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : {} }
      ).then(x => x.json());
      planTotal = plan?.total || 0;
      const feService = (tree || []).find(s => (s.name || "").toLowerCase() === "frontend");
      const fePages = (feService?.files || []).filter(
        f => (f.path || "").startsWith("frontend/src/pages/")
      );
      if (planTotal > 0 && planTotal > fePages.length + 5) {
        runMigration = window.confirm(
          "Run full 1:1 legacy JSP → React MIGRATION first?\n\n" +
          `Detected ${planTotal} legacy JSPs but only ${fePages.length} React page(s) so far. ` +
          "This job generates ONE production-ready React component per legacy JSP " +
          "(real imports, state, handlers, API calls, Tailwind styling — NO TODOs or " +
          "placeholders).\n\n" +
          `Estimated time on local Ollama at concurrency 4: ~${Math.ceil(planTotal * 25 / 60)} min.\n\n` +
          "OK = Run full migration (recommended)\n" +
          "Cancel = Skip migration, run gap-recovery on existing tree only"
        );
      }
    } catch (e) { /* non-fatal */ }

    if (runMigration) {
      try {
        const backend = process.env.REACT_APP_BACKEND_URL || "";
        const token = localStorage.getItem("lama:auth:token") || "";
        const r = await fetch(
          `${backend}/api/codegen/${projectId}/frontend/migrate-jsps`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: JSON.stringify({ overwrite_existing: false }),
          }
        ).then(x => x.json());
        if (!r.job_id) {
          toast.error("Migration failed to start: " + (r?.detail || "unknown"));
          return;
        }
        gapFrontJob.start(r.job_id);
        toast.success(
          `Frontend migration started: ${r.total_planned} JSP → React pages ` +
          `(model: ${r.model || "default"}). Live progress below.`
        );
        return; // Migration IS the regen for the FE — no separate gap pass needed
      } catch (e) {
        toast.error("Migration failed: " + (e?.message || String(e)));
        return;
      }
    }

    if (!window.confirm(
      "Regenerate Frontend Service?\n\n" +
      "Runs a legacy-parity gap scan over EVERY generated frontend file (using an independent " +
      "model — DIFF_ANALYSIS_MODEL) and injects missing screens / validations / role-based " +
      "visibility in place. Existing code is preserved; only gaps are repaired.\n\n" +
      "Continue?"
    )) return;
    try {
      const r = await startGapRecoveryFrontend(projectId, null);
      gapFrontJob.start(r.job_id);
      toast.message(`Frontend gap recovery started (model: ${r.model || "default"})`);
    } catch (e) { toast.error("Could not start: " + (e?.response?.data?.detail || e.message)); }
  };

  // iter-13.119 — Auto-Validate & Improve. Iteratively scores every
  // generated source file across 6 axes (structural / parity / coverage
  // / schema / evidence / requirement) and runs gap-recovery on files
  // below threshold until run-confidence ≥ 95% OR max_iterations is hit.
  // iter-13.119.1 — replaced the old window.prompt() with a proper modal
  // so the user can pick the *specific service* (or all), threshold,
  // max-iterations, and files-per-iter cap before kicking off.
  const [autoValidateModalOpen, setAutoValidateModalOpen] = useState(false);
  const onAutoValidate = () => {
    setAutoValidateModalOpen(true);
  };

  const onAutoValidateStart = async ({ threshold, maxIterations, maxFilesPerIter, serviceName }) => {
    try {
      const r = await startAutoValidate(projectId, {
        threshold,
        maxIterations,
        maxFilesPerIter,
        serviceName: serviceName || undefined,
      });
      autoValidateJob.start(r.job_id);
      // Open the Live Log dashboard immediately so the user sees progress.
      setReportTab("log");
      setParityReportOpen(true);
      toast.message(
        `Auto-validate started${serviceName ? ` for "${serviceName}"` : " (all services)"} — ` +
        `threshold=${r.threshold}% · iter≤${r.max_iterations} · ${r.max_files_per_iter} files/iter ` +
        `(model: ${r.model})`,
      );
      setAutoValidateModalOpen(false);
    } catch (e) {
      toast.error("Could not start: " + (e?.response?.data?.detail || e.message));
    }
  };

  const onShowParityReport = async () => {
    // iter-13.119 — open the modal first, then poll. The polling effect
    // (driven by parityReportOpen) keeps it fresh so the dashboard renders
    // a live progress view even when the run was just kicked off and
    // parity_runs hasn't been populated yet.
    setParityReportOpen(true);
    const r = await loadParityReport();
    if (!r && !autoValidateJob.running) {
      toast.message("No auto-validate runs yet — click Auto-Validate to start.");
    }
  };

  const onDownloadZip = async () => {
    setZipBusy(true);
    try {
      const blob = await startCodegenZipDownload(projectId);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (active?.name || "lama").toLowerCase().replace(/\s+/g, "_") + ".zip";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { toast.error("Download failed: " + (e?.response?.data?.detail || e.message)); }
    finally { setZipBusy(false); }
  };

  // iter-13.110 / 13.111 — Write generated frontend + backend trees to disk
  // OUTSIDE the LAMA repo. Folder name = slug of project.name
  // ("Aarogyasri TJHS App" → Aarogyasri-TJHS-App). Parent dir overridable
  // via LAMA_EXPORT_ROOT on the backend.
  const [exportBusy, setExportBusy] = useState(false);
  const onExportToDisk = async () => {
    let preview = null;
    try { preview = await getCodegenExportRoot(projectId); } catch (_e) { /* best-effort */ }
    const target = preview?.project_root
      || `${preview?.export_root || "<parent-of-lama-main>"}/<${active?.name || "project"}-slug>`;
    if (!window.confirm(
      "Export the generated frontend + backend projects to disk?\n\n" +
      `Destination:\n  ${target}/\n    ├── frontend/\n    └── backend/\n\n` +
      "The folder name is derived from this project's name. If a previous " +
      "export of this project exists at the same path, it will be REPLACED. " +
      "Override the parent dir with the LAMA_EXPORT_ROOT env-var on the " +
      "backend.\n\nContinue?"
    )) return;
    setExportBusy(true);
    try {
      const r = await exportCodegenToDisk(projectId);
      const skipped = (r.skipped || []).length;
      toast.success(
        `Exported ${r.frontend_files} frontend + ${r.backend_files} backend files` +
        (skipped ? ` (${skipped} skipped — see audit log)` : "") +
        `\n→ ${r.project_root}`,
        { duration: 8000 }
      );
    } catch (e) {
      toast.error("Export failed: " + (e?.response?.data?.detail || e.message));
    } finally {
      setExportBusy(false);
    }
  };

  const onPush = async () => {
    try {
      const r = await startGithubPushJob(projectId);
      pushJob.start(r.job_id);
    } catch (e) { toast.error("Could not start push: " + (e?.response?.data?.detail || e.message)); }
  };

  // iter-13.54 — Pause / Resume / Stop the currently-active codegen-family
  // job. We pick the first job that's actively running or paused across
  // (genJob, gapBackJob, gapFrontJob) so the user can drive whichever
  // long-running fan-out is in flight. pushJob is fast (single API call),
  // not worth wiring controls for.
  const _activeCgJob = useMemo(() => {
    const candidates = [
      { hook: genJob, label: "codegen" },
      { hook: gapBackJob, label: "backend gap-recovery" },
      { hook: gapFrontJob, label: "frontend gap-recovery" },
      // iter-13.119 — Auto-Validate uses the same job-control infra so it
      // also needs Pause / Resume / Stop wired up.
      { hook: autoValidateJob, label: "auto-validate" },
    ];
    for (const c of candidates) {
      const j = c.hook.job;
      if (j && (j.status === "running" || j.status === "queued") && j.id) {
        return { ...c, j };
      }
    }
    return null;
  }, [genJob.job, gapBackJob.job, gapFrontJob.job, autoValidateJob.job]);

  const _activeCgControl = _activeCgJob?.j?.control || "running";

  const onPauseCg = async () => {
    if (!_activeCgJob) return;
    try {
      await pauseCodegenJob(_activeCgJob.j.id);
      toast.message(`Paused ${_activeCgJob.label} — current LLM call will finish, then nothing new will start.`);
    } catch (e) { toast.error("Pause failed: " + (e?.response?.data?.detail || e.message)); }
  };
  const onResumeCg = async () => {
    if (!_activeCgJob) return;
    try {
      await resumeCodegenJob(_activeCgJob.j.id);
      toast.success(`Resumed ${_activeCgJob.label}`);
    } catch (e) { toast.error("Resume failed: " + (e?.response?.data?.detail || e.message)); }
  };
  const onStopCg = async () => {
    if (!_activeCgJob) return;
    if (!window.confirm(
      `Stop ${_activeCgJob.label}?\n\n` +
      "In-flight LLM calls will finish; no further files will be generated. " +
      "Files already generated are kept. Job will be marked as 'stopped'.\n\n" +
      "This is COOPERATIVE — actual stop happens within ~1-2 seconds."
    )) return;
    try {
      await stopCodegenJob(_activeCgJob.j.id);
      toast.message(`Stop requested for ${_activeCgJob.label}.`);
    } catch (e) { toast.error("Stop failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onFreeze = async () => {
    try {
      await freezeCodegen(projectId);
      toast.success("Stage 4 frozen — Living unlocked");
    } catch (e) { toast.error("Freeze failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onSaveFile = async () => {
    if (!selectedFile) return;
    try {
      await updateCodegenFile(projectId, selectedFile.id, editBuf);
      setFileContent(editBuf);
      setEditing(false);
      toast.success("File saved");
      await refresh();
    } catch (e) { toast.error("Save failed"); }
  };

  const onSendChat = async () => {
    const m = chatInput.trim();
    if (!m) return;
    setChatBusy(true);
    setChatMessages((p) => [...p, { role: "user", content: m }]);
    setChatInput("");
    try {
      const r = await sendCodegenChat({
        project_id: projectId, message: m, conversation_id: convId,
        file_id: selectedFile?.id, service_name: filterService || selectedFile?.service_name,
      });
      setConvId(r.conversation_id);
      setChatMessages((p) => [...p, { role: "assistant", content: r.content, file_changes: r.file_changes || [], message_id: r.message_id }]);
    } catch (e) { toast.error("Chat failed: " + (e?.response?.data?.detail || e.message)); }
    finally { setChatBusy(false); }
  };

  const onApplyFileChange = async (fc, msgId) => {
    if (!fc.file_id) { toast.message("Target file not in repo. Generate it first."); return; }
    try {
      await applyCodegenFileChange(projectId, fc.file_id, fc.new_content, msgId);
      toast.success("Applied to " + fc.file_path);
      if (selectedFile?.id === fc.file_id) {
        setFileContent(fc.new_content);
      }
      await refresh();
    } catch (e) { toast.error("Apply failed"); }
  };

  const onReset = async () => {
    try {
      await resetCodegen(projectId);
      toast.success("Stage 4 reset");
      setResetOpen(false);
      setTree([]); setTotalFiles(0); setSelectedFile(null); setFileContent("");
      setChatMessages([]); setConvId(null);
    } catch (e) { toast.error("Reset failed"); }
  };

  const toggle = (k) => setExpanded((p) => ({ ...p, [k]: p[k] === false ? true : false }));

  // iter-13.55 — compact progress-bar renderer with inline Pause/Resume/Stop
  // controls. Centralises the bar markup so all three fan-out job kinds
  // (codegen, backend-gap, frontend-gap) share the same layout and the
  // controls are visually attached to the job they control.
  const renderJobBar = ({ job, label, testId, barColor, onPause, onResume, onStop, isActive }) => {
    if (!job) return null;
    const ctrl = job.control || "running";
    const isPaused = ctrl === "paused";
    const isStopping = ctrl === "stopping";
    const terminal = job.status === "complete" || job.status === "error" || job.status === "stopped";
    return (
      <div className="text-micro" data-testid={testId}>
        <div className="flex items-center justify-between gap-2">
          <span className="text-fg-muted truncate flex-1">
            {label}: {job.step}
          </span>
          {/* Only show controls for the currently-active job (the one
              that's running or paused). Once the job is terminal or
              another job has taken over, controls collapse. */}
          {isActive && !terminal && (
            <div className="flex items-center gap-1 shrink-0" data-testid={`${testId}-controls`}>
              {isPaused ? (
                <button
                  data-testid="btn-cg-resume"
                  onClick={onResume}
                  title="Resume the paused job"
                  className="flex items-center gap-0.5 px-1.5 h-5 text-micro bg-emerald-600 text-white rounded-sm hover:bg-emerald-700"
                >
                  <Play className="w-2.5 h-2.5" /> Resume
                </button>
              ) : (
                <button
                  data-testid="btn-cg-pause"
                  onClick={onPause}
                  disabled={isStopping}
                  title="Pause after the current LLM call(s) finish"
                  className="flex items-center gap-0.5 px-1.5 h-5 text-micro border border-border rounded-sm hover:bg-bg disabled:opacity-50"
                >
                  <Pause className="w-2.5 h-2.5" /> Pause
                </button>
              )}
              <button
                data-testid="btn-cg-stop"
                onClick={onStop}
                disabled={isStopping}
                title="Stop after the current LLM call(s) finish — already-generated files are kept"
                className="flex items-center gap-0.5 px-1.5 h-5 text-micro bg-red-600 text-white rounded-sm hover:bg-red-700 disabled:opacity-50"
              >
                <Square className="w-2.5 h-2.5" /> {isStopping ? "Stopping…" : "Stop"}
              </button>
            </div>
          )}
          <span className="text-fg font-semibold shrink-0">{job.pct || 0}%</span>
        </div>
        <div className="h-1 bg-bg rounded-sm overflow-hidden">
          <div
            className={`h-full ${isPaused ? "bg-amber-400" : barColor}`}
            style={{ width: `${job.pct || 0}%` }}
          />
        </div>
      </div>
    );
  };

  if (!projectId) return (
    <EmptyState
      icon={FolderOpen}
      title="No project selected"
      description="Create or open a project from the sidebar to generate code."
      action={
        <button
          type="button"
          onClick={() => navigate("/")}
          className="text-xs px-3 py-1.5 bg-brand text-fg rounded-sm hover:bg-yellow-300 font-semibold focus:outline-none focus:ring-2 focus:ring-fg"
          data-testid="empty-goto-discovery"
        >
          Go to Discovery →
        </button>
      }
    />
  );

  if (isLocked) {
    return (
      <div className="flex-1 flex flex-col bg-bg" data-testid="codegen-locked">
        <header className="bg-surface border-b-2 border-brand px-6 py-3">
          <div className="text-micro uppercase tracking-widest text-fg-muted">Stage 4 of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-fg">Code Generation</h1>
        </header>
        <div className="flex-1 flex items-center justify-center p-8">
          <div className="max-w-md bg-surface border border-border rounded-sm p-6 text-center">
            <Lock className="w-8 h-8 mx-auto text-fg-muted mb-3" />
            <h2 className="font-display font-bold text-fg">Locked — Architecture not frozen</h2>
            <p className="text-xs text-fg-muted mt-2">Click <span className="font-bold">Approve &amp; Freeze</span> on the Service Map in Stage 3 to unlock CodeGen. HLD / LLD / Sequence / API Contracts are optional and can be generated before or after CodeGen.</p>
            <button onClick={() => navigate("/architecture")} className="mt-4 text-xs px-3 py-1.5 bg-ink text-ink-fg rounded-sm">Open Architecture →</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-bg" data-testid="codegen-page">
      {/* ────────────────────────────────────────────────────────────────────
          iter-13.140 — Compact single-row header with kebab overflow menu.
          Only essential actions visible; rest in kebab dropdown.
          ──────────────────────────────────────────────────────────────────── */}
      <header className="bg-surface border-b-2 border-brand px-4 sm:px-6 py-2.5 relative z-20">
        <div className="flex items-center gap-2 flex-wrap">
          {/* Title */}
          <div className="shrink-0">
            <div className="text-micro uppercase tracking-widest text-fg-muted">Stage 4 of 5</div>
            <h1 className="font-display text-base font-bold tracking-tight text-fg">Code Generation</h1>
          </div>

          {/* Stats + Pattern */}
          <div className="flex items-center gap-1.5 shrink-0 ml-2">
            <span className="text-micro px-1.5 py-0.5 bg-bg border border-border rounded-sm text-fg-muted">
              {totalFiles} files · {services.length} svc
            </span>
            {archPattern && (
              <span
                data-testid="pattern-badge"
                className="text-micro uppercase font-semibold bg-bg text-fg border border-border px-1.5 py-0.5 rounded-sm"
              >
                {archPattern.replace("_", " ")}
              </span>
            )}
            {isFrozen && <span className="text-micro uppercase font-bold bg-brand text-fg px-1.5 py-0.5 rounded-sm">Frozen</span>}
          </div>

          {/* iter-17 — Mode toggle: [Quick Generate | Multi-Agent Pipeline].
              Persisted in localStorage. Default = Quick Generate so the
              existing single-shot flow is never disrupted. */}
          <div
            data-testid="codegen-mode-toggle"
            className="flex items-center border border-border rounded-sm bg-surface shrink-0 ml-1"
            role="tablist"
            aria-label="CodeGen mode"
          >
            <button
              type="button"
              role="tab"
              aria-selected={codegenMode === "quick"}
              data-testid="codegen-mode-quick"
              onClick={() => setCodegenMode("quick")}
              className={
                "h-7 text-micro px-2.5 font-semibold rounded-l-sm " +
                (codegenMode === "quick"
                  ? "bg-ink text-ink-fg"
                  : "text-fg hover:bg-bg")
              }
              title="Single-shot generate — the classic Stage 4 flow"
            >
              Quick Generate
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={codegenMode === "multi-agent"}
              data-testid="codegen-mode-multiagent"
              onClick={() => setCodegenMode("multi-agent")}
              className={
                "h-7 text-micro px-2.5 font-semibold rounded-r-sm border-l border-border " +
                (codegenMode === "multi-agent"
                  ? "bg-ink text-ink-fg"
                  : "text-fg hover:bg-bg")
              }
              title="Context Manager → Planner → Coder/Verifier/Reviewer/Tester → Traceability Gate → Finalizer"
            >
              Multi-Agent Pipeline
            </button>
          </div>

          {/* Spacer */}
          <div className="flex-1 min-w-4" />

          {/* ─── Actions ─── */}
          {/* Service selector */}
          <div className="relative shrink-0" ref={gsPopoverRef}>
            <button
              data-testid="btn-select-services"
              onClick={() => setGsPopoverOpen((v) => !v)}
              disabled={archServices.length === 0}
              className="h-7 text-micro px-2 border border-border rounded-sm bg-surface text-fg hover:bg-bg disabled:opacity-50 flex items-center gap-1"
              title="Choose services to (re)generate"
            >
              <Folder className="w-3 h-3" />
              ({selectedCount}/{archServices.length})
              <ChevronDown className="w-3 h-3" />
            </button>
            {gsPopoverOpen && archServices.length > 0 && (
              <div
                data-testid="gen-services-popover"
                className="absolute right-0 top-8 z-30 w-72 max-h-80 overflow-y-auto bg-surface border border-border rounded-sm shadow-lg text-micro"
              >
                <div className="px-2 py-1.5 border-b border-border flex items-center gap-2 sticky top-0 bg-surface">
                  <input
                    type="checkbox"
                    data-testid="gen-svc-checkbox-all"
                    checked={selectedCount === archServices.length && archServices.length > 0}
                    ref={(el) => { if (el) el.indeterminate = selectedCount > 0 && selectedCount < archServices.length; }}
                    onChange={(e) => toggleAllGenServices(e.target.checked)}
                  />
                  <span className="font-semibold text-fg">
                    {selectedCount === archServices.length ? "All selected" : `${selectedCount} of ${archServices.length}`}
                  </span>
                </div>
                {archServices.map((s) => {
                  const checked = selectedGenServices.has(s.name);
                  const cnt = genFileCountFor(s.name);
                  return (
                    <label
                      key={s.name}
                      className="flex items-center gap-2 px-2 py-1.5 hover:bg-bg cursor-pointer border-b border-surface-2 last:border-b-0"
                    >
                      <input
                        type="checkbox"
                        data-testid={`gen-svc-checkbox-${s.name}`}
                        checked={checked}
                        onChange={() => toggleGenService(s.name)}
                      />
                      <span className="truncate flex-1" title={s.display_name || s.name}>{s.name}</span>
                      <span
                        className={
                          "text-micro uppercase font-bold px-1 py-0.5 rounded-sm " +
                          (s.frontend
                            ? "bg-sky-100 text-sky-700"
                            : s.kind === "utility"
                              ? "bg-purple-100 text-purple-700"
                              : "bg-emerald-100 text-emerald-700")
                        }
                      >
                        {s.frontend ? "FE" : s.kind === "utility" ? "UTL" : "BE"}
                      </span>
                      {cnt > 0 && (
                        <span className="text-micro text-fg-muted">{cnt}f</span>
                      )}
                    </label>
                  );
                })}
              </div>
            )}
          </div>

          {/* Generate */}
          <Button data-testid="btn-generate" onClick={onGenerate} disabled={genJob.running} className="h-7 text-micro px-3 bg-brand text-fg hover:bg-brand-hover font-semibold shrink-0">
            {genJob.running
              ? (_activeCgControl === "paused"
                  ? <Pause className="w-3 h-3" />
                  : <Loader2 className="w-3 h-3 animate-spin" />)
              : <Wand2 className="w-3 h-3" />}
            {" "}Generate
          </Button>

          {/* Auto-Validate */}
          <Button
            data-testid="btn-auto-validate"
            onClick={onAutoValidate}
            disabled={autoValidateJob.running || totalFiles === 0}
            className="h-7 text-micro px-3 bg-emerald-600 text-white hover:bg-emerald-700 shrink-0"
            title="Score every file, regenerate the worst, loop until confidence ≥ 95%"
          >
            {autoValidateJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Target className="w-3 h-3" />}
            {" "}Validate
          </Button>

          {/* Confidence */}
          <div className="shrink-0">
            <ConfidenceBadge projectId={projectId} stage="CodeGen" compact />
          </div>

          {/* Freeze */}
          {!isFrozen && (
            <Button data-testid="btn-freeze-codegen" onClick={onFreeze} disabled={totalFiles === 0} className="h-7 text-micro px-3 bg-ink text-ink-fg hover:bg-ink shrink-0">
              <PackageCheck className="w-3 h-3" /> Freeze
            </Button>
          )}

          {/* ─── Kebab overflow menu ─── */}
          <div className="relative shrink-0" ref={kebabRef}>
            <button
              data-testid="btn-kebab-menu"
              onClick={() => setKebabOpen((v) => !v)}
              className="h-7 w-7 flex items-center justify-center border border-border rounded-sm bg-surface text-fg hover:bg-bg"
              title="More actions"
            >
              <MoreVertical className="w-4 h-4" />
            </button>
            {kebabOpen && (
              <div
                data-testid="kebab-dropdown"
                className="absolute right-0 top-full mt-1 z-[9999] w-52 bg-surface border border-border rounded-sm shadow-xl text-micro py-1"
              >
                {/* Regenerate section */}
                <div className="px-3 py-1 text-micro uppercase tracking-wide text-fg-muted font-semibold">Regenerate</div>
                <button
                  data-testid="btn-regen-backend"
                  onClick={() => { setKebabOpen(false); onRegenBackend(); }}
                  disabled={gapBackJob.running || totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  {gapBackJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <ShieldCheck className="w-3 h-3 text-emerald-600" />} Backend (Gap Recovery)
                </button>
                <button
                  data-testid="btn-regen-frontend"
                  onClick={() => { setKebabOpen(false); onRegenFrontend(); }}
                  disabled={gapFrontJob.running || totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  {gapFrontJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Monitor className="w-3 h-3 text-sky-600" />} Frontend (Gap Recovery)
                </button>
                <div className="border-t border-border my-1" />

                {/* Reports */}
                <div className="px-3 py-1 text-micro uppercase tracking-wide text-fg-muted font-semibold">Reports</div>
                <button
                  data-testid="btn-parity-report"
                  onClick={() => { setKebabOpen(false); onShowParityReport(); }}
                  disabled={totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  <FileText className="w-3 h-3 text-emerald-600" /> Parity Report
                </button>
                <div className="border-t border-border my-1" />

                {/* Export */}
                <div className="px-3 py-1 text-micro uppercase tracking-wide text-fg-muted font-semibold">Export</div>
                <button
                  data-testid="btn-export-disk"
                  onClick={() => { setKebabOpen(false); onExportToDisk(); }}
                  disabled={exportBusy || totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  {exportBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <FolderOpen className="w-3 h-3" />} Export to Disk
                </button>
                <button
                  data-testid="btn-push"
                  onClick={() => { setKebabOpen(false); onPush(); }}
                  disabled={pushJob.running || totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  {pushJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Github className="w-3 h-3" />} Push to GitHub
                </button>
                <button
                  data-testid="btn-zip"
                  onClick={() => { setKebabOpen(false); onDownloadZip(); }}
                  disabled={zipBusy || totalFiles === 0}
                  className="w-full px-3 py-1.5 text-left hover:bg-bg disabled:opacity-50 flex items-center gap-2"
                >
                  {zipBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Download className="w-3 h-3" />} Download ZIP
                </button>
                <div className="border-t border-border my-1" />

                {/* Reset */}
                <button
                  data-testid="btn-reset-codegen"
                  onClick={() => { setKebabOpen(false); setResetOpen(true); }}
                  className="w-full px-3 py-1.5 text-left hover:bg-orange-50 text-orange-600 flex items-center gap-2"
                >
                  <RotateCcw className="w-3 h-3" /> Reset Stage 4
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* iter-13.120 — Frontend-missing warning. Only fires when
          Architecture has produced services but zero of them are
          frontend-flagged. Points the user back to Stage 3 instead of
          silently generating a backend-only build. */}
      {archServices.length > 0 && !hasFrontendService && (
        <div
          data-testid="no-frontend-warning"
          className="bg-amber-50 border-b border-amber-200 px-4 py-1.5 text-micro text-amber-900 flex items-center gap-2"
        >
          <span>⚠</span>
          <span>
            No frontend service configured in Architecture. Frontend code will
            NOT be generated.
          </span>
          <button
            onClick={() => navigate("/architecture")}
            className="ml-1 underline text-amber-900 hover:text-amber-700 font-semibold"
          >
            Open Architecture →
          </button>
        </div>
      )}

      {/* iter-13.120 — Plan preview. Compact one-liner that tells the
          user WHICH services are about to be (re)generated + a rough
          file-count based on the last successful run. Purely
          informational — the actual planning happens on the backend. */}
      {archServices.length > 0 && (
        <div
          data-testid="gen-plan-preview"
          className="bg-surface border-b border-border px-4 py-1 text-micro text-fg-muted flex items-center gap-3"
        >
          <span className="font-semibold text-fg">Will generate:</span>
          <span>
            {selectedCount} service{selectedCount === 1 ? "" : "s"}
            {" "}({selectedBackendCount} backend, {selectedFrontendCount} frontend)
          </span>
          <span className="text-fg-subtle">·</span>
          <span>
            {selectedPlannedFileCount > 0
              ? `~${selectedPlannedFileCount} files planned (based on last run)`
              : "— run Generate to plan files"}
          </span>
          {selectedCount === 0 && (
            <span className="text-amber-700 font-semibold">— pick at least one service</span>
          )}
        </div>
      )}

      {/* iter-13.122 — Legacy → New API mapping preview.
          Deterministic (no LLM). Sources `legacy_routes` from
          `arch_services.routes_detail` (populated from KB OWL
          extraction) and `new_endpoints` from
          `arch_services.api_endpoints`. Rendered ABOVE the file tree so
          the user validates every legacy route is covered BEFORE
          clicking Generate. */}
      {apiMapping && Array.isArray(apiMapping.services) && apiMapping.services.length > 0 && (
        <div
          data-testid="api-mapping-preview"
          className="bg-surface border-b border-border"
        >
          <button
            type="button"
            onClick={() => setApiMappingOpen((v) => !v)}
            data-testid="api-mapping-toggle"
            className="w-full flex items-center gap-2 px-4 py-1.5 text-micro text-fg hover:bg-bg"
          >
            {apiMappingOpen
              ? <ChevronDown className="w-3 h-3" />
              : <ChevronRightIcon className="w-3 h-3" />}
            <span className="font-semibold">Legacy → New API Mapping</span>
            <span className="text-micro text-fg-muted">
              {apiMapping.totals?.services || 0} service(s) ·{" "}
              {apiMapping.totals?.legacy_total || 0} legacy routes →{" "}
              {apiMapping.totals?.new_total || 0} new endpoints
            </span>
            <span className="ml-auto text-micro text-fg-muted">
              {apiMappingOpen ? "Hide" : "Show"} — validate before Generate
            </span>
          </button>
          {apiMappingOpen && (
            <div className="px-4 pb-2 max-h-64 overflow-y-auto mos-scroll">
              <div className="flex items-center gap-2 mb-1.5">
                <select
                  data-testid="api-mapping-filter"
                  value={apiMappingSvc}
                  onChange={(e) => setApiMappingSvc(e.target.value)}
                  className="text-micro border border-border rounded-sm px-1 py-0.5"
                >
                  <option value="">All services ({apiMapping.services.length})</option>
                  {apiMapping.services.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}{s.frontend ? " (frontend)" : s.kind === "utility" ? " (utility)" : ""}
                    </option>
                  ))}
                </select>
                <span className="text-micro text-fg-muted">
                  Legacy routes come from the KB (Stage 1). New endpoints come from Architecture (Stage 3).
                </span>
              </div>
              {apiMapping.services
                .filter((s) => !apiMappingSvc || s.name === apiMappingSvc)
                .map((s) => (
                  <div
                    key={s.name}
                    data-testid={`api-mapping-service-${s.name}`}
                    className="mb-2 border border-border rounded-sm bg-surface-2"
                  >
                    <div className="px-2 py-1 border-b border-border flex items-center gap-2 text-micro">
                      <span className="font-semibold text-fg">{s.display_name || s.name}</span>
                      <span className="text-micro text-fg-muted">({s.name})</span>
                      <span
                        className={
                          "text-micro uppercase font-bold px-1 py-0.5 rounded-sm " +
                          (s.frontend
                            ? "bg-sky-100 text-sky-700"
                            : s.kind === "utility"
                              ? "bg-purple-100 text-purple-700"
                              : "bg-emerald-100 text-emerald-700")
                        }
                      >
                        {s.frontend ? "FRONTEND" : s.kind === "utility" ? "UTILITY" : "BACKEND"}
                      </span>
                      {s.merged_from && s.merged_from.length > 0 && (
                        <span
                          className="text-micro uppercase font-bold px-1 py-0.5 rounded-sm bg-amber-100 text-amber-800"
                          title={"Merged from: " + s.merged_from.join(", ")}
                        >
                          MERGED × {s.merged_from.length}
                        </span>
                      )}
                      <span className="ml-auto text-micro text-fg-muted">
                        {s.legacy_count} legacy → {s.new_count} new
                        {s.legacy_count > 0 && s.new_count === 0 && (
                          <span className="text-red-600 font-semibold ml-1">⚠ uncovered</span>
                        )}
                      </span>
                    </div>
                    {s.frontend ? (
                      <div className="px-2 py-1 text-micro text-fg-muted">
                        React frontend — consumes the mapped backend endpoints above.
                        No 1:1 route mapping.
                      </div>
                    ) : (
                      <div className="grid grid-cols-2 gap-0 text-micro">
                        <div className="border-r border-border">
                          <div className="px-2 py-0.5 bg-bg font-semibold text-fg-muted uppercase tracking-wide text-micro">
                            Legacy ({s.legacy_count})
                          </div>
                          <div className="max-h-40 overflow-y-auto mos-scroll">
                            {(s.legacy_routes || []).length === 0 && (
                              <div className="px-2 py-1 text-fg-subtle italic">
                                No legacy routes attributed to this service.
                              </div>
                            )}
                            {(s.legacy_routes || []).map((r, i) => (
                              <div
                                key={`l:${i}`}
                                className="px-2 py-0.5 border-b border-surface-2 font-mono truncate flex items-center gap-1"
                                title={`${r.class || ""} · ${r.module || ""}`}
                              >
                                <span className="inline-block w-10 text-micro font-bold text-fg">
                                  {r.verb}
                                </span>
                                <span className="truncate">{r.path}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                        <div>
                          <div className="px-2 py-0.5 bg-bg font-semibold text-fg-muted uppercase tracking-wide text-micro">
                            New ({s.new_count})
                          </div>
                          <div className="max-h-40 overflow-y-auto mos-scroll">
                            {(s.new_endpoints || []).length === 0 && (
                              <div className="px-2 py-1 text-red-600 italic">
                                ⚠ No new endpoints defined — regenerate Architecture (Stage 3).
                              </div>
                            )}
                            {(s.new_endpoints || []).map((r, i) => (
                              <div
                                key={`n:${i}`}
                                className="px-2 py-0.5 border-b border-surface-2 font-mono truncate flex items-center gap-1"
                              >
                                <span className="inline-block w-10 text-micro font-bold text-emerald-700">
                                  {r.verb}
                                </span>
                                <span className="truncate">{r.path}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Job progress strip */}
      {(genJob.job || pushJob.job || gapBackJob.job || gapFrontJob.job || autoValidateJob.job) && (
        <div className="bg-surface border-b border-border px-4 py-1.5 space-y-1">
          {/*
            iter-13.55 — Compact, inline Pause / Resume / Stop controls
            rendered right next to the job they belong to (instead of in
            the header where they crowded out the title). Renders only for
            running/paused fan-out jobs (codegen / gap-recovery), never
            for the single-shot push job.
          */}
          {renderJobBar({
            job: genJob.job, label: "codegen", testId: "codegen-job-bar",
            barColor: "bg-brand",
            onPause: onPauseCg, onResume: onResumeCg, onStop: onStopCg,
            isActive: _activeCgJob?.j?.id === genJob.job?.id,
          })}
          {renderJobBar({
            job: gapBackJob.job, label: "backend gap-recovery", testId: "gap-back-job-bar",
            barColor: "bg-emerald-500",
            onPause: onPauseCg, onResume: onResumeCg, onStop: onStopCg,
            isActive: _activeCgJob?.j?.id === gapBackJob.job?.id,
          })}
          {renderJobBar({
            job: gapFrontJob.job, label: "frontend gap-recovery", testId: "gap-front-job-bar",
            barColor: "bg-sky-500",
            onPause: onPauseCg, onResume: onResumeCg, onStop: onStopCg,
            isActive: _activeCgJob?.j?.id === gapFrontJob.job?.id,
          })}
          {/* iter-13.119 — Auto-Validate & Improve progress bar */}
          {renderJobBar({
            job: autoValidateJob.job, label: "auto-validate", testId: "auto-validate-job-bar",
            barColor: "bg-emerald-600",
            onPause: onPauseCg, onResume: onResumeCg, onStop: onStopCg,
            isActive: _activeCgJob?.j?.id === autoValidateJob.job?.id,
          })}
          {pushJob.job && (
            <div className="text-micro" data-testid="push-job-bar">
              <div className="flex items-center justify-between">
                <span className="text-fg-muted truncate">github push: {pushJob.job.step}</span>
                <span className="text-fg font-semibold">{pushJob.job.pct || 0}%</span>
              </div>
              <div className="h-1 bg-bg rounded-sm overflow-hidden"><div className="h-full bg-ink" style={{ width: `${pushJob.job.pct || 0}%` }} /></div>
            </div>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0">
        {/* iter-17 — Mode toggle: render the multi-agent pipeline panel
            in place of the tree/editor/chat PanelGroup when the user
            picks that mode. Quick Generate flow below is unchanged. */}
        {codegenMode === "multi-agent" ? (
          <CodeGenMultiAgentPanel projectId={projectId} />
        ) : (
        <PanelGroup
          key={isMobile ? "v" : "h"}
          direction={isMobile ? "vertical" : "horizontal"}
        >
          {/* LEFT: file tree */}
          <Panel defaultSize={20} minSize={14}>
            <div className="h-full bg-surface border-r border-border flex flex-col">
              <div className="px-2 py-1.5 border-b border-border flex items-center gap-1">
                <select data-testid="service-filter" value={filterService} onChange={(e) => setFilterService(e.target.value)} className="text-micro border border-border rounded-sm px-1 py-0.5 flex-1">
                  <option value="">All services</option>
                  {services.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                {filterService && (
                  <button onClick={onGenerateOne} title="Regenerate this service" data-testid="btn-regen-service" className="text-micro px-1.5 py-0.5 bg-brand text-fg rounded-sm font-bold">↻</button>
                )}
              </div>
              <div className="flex-1 overflow-y-auto mos-scroll py-1" data-testid="file-tree">
                {flatFiles.length === 0 && (
                  <div className="text-micro text-fg-muted p-3">No files yet. Click <strong>Generate All</strong>.</div>
                )}
                {treeRows.map((r) => {
                  if (r.kind === "dir") {
                    return (
                      <button key={`d:${r.key}`} onClick={() => toggle(r.key)} onContextMenu={(e) => openCtxMenu(e, { kind: "dir", dirKey: r.key, name: r.name })} data-testid={`dir-${r.key}`} className="w-full flex items-center gap-1 text-micro py-0.5 hover:bg-bg" style={{ paddingLeft: 8 + r.depth * 10 }}>
                        {r.isOpen ? <ChevronDown className="w-3 h-3 text-fg-muted" /> : <ChevronRightIcon className="w-3 h-3 text-fg-muted" />}
                        {r.isOpen ? <FolderOpen className="w-3 h-3 text-brand" /> : <Folder className="w-3 h-3 text-brand" />}
                        <span className="text-fg truncate">{r.name}</span>
                      </button>
                    );
                  }
                  const f = r.file;
                  const isSel = selectedFile?.id === f.id;
                  const gs = f.gap_stats || null;
                  const gapCount = gs ? Number(gs.evidence_gaps || 0) : 0;
                  const riskCount = gs ? Number(gs.parity_risks || 0) : 0;
                  const autoFixable = !!(gs && gs.auto_fixable);
                  const showRibbon = gs && (gapCount > 0 || riskCount > 0);
                  return (
                    <button
                      key={`f:${f.id}`}
                      onClick={() => onSelectFile(f)}
                      onContextMenu={(e) => openCtxMenu(e, { kind: "file", file: f })}
                      data-testid={`file-${f.path}`}
                      className={`w-full flex items-center gap-1 text-micro py-0.5 ${isSel ? "bg-brand-tint text-fg font-semibold" : "hover:bg-bg text-fg"}`}
                      style={{ paddingLeft: 8 + r.depth * 10 }}
                    >
                      <FileIcon className="w-3 h-3 text-fg-muted" />
                      <span className="truncate">{f.basename}</span>
                      {showRibbon && (
                        <span
                          data-testid={`codegen-file-gapstats-${f.basename}`}
                          title={
                            autoFixable
                              ? "KB has field evidence for this file — gaps are auto-fixable via Gap Recovery"
                              : "Evidence genuinely missing in KB"
                          }
                          className={`ml-auto shrink-0 text-micro px-1 rounded-sm font-bold ${
                            autoFixable
                              ? "bg-red-100 text-red-700 border border-red-300"
                              : "bg-surface-2 text-fg-muted border border-border"
                          }`}
                        >
                          {gapCount} gap{gapCount === 1 ? "" : "s"} · {riskCount} risk{riskCount === 1 ? "" : "s"}
                        </span>
                      )}
                      {f.edited && !showRibbon && <span className="text-micro text-orange-500 ml-auto">●</span>}
                      {f.edited && showRibbon && <span className="text-micro text-orange-500 ml-1">●</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          </Panel>

          <PanelResizeHandle className="w-1 bg-border hover:bg-brand" />

          {/* CENTER: editor */}
          <Panel defaultSize={50}>
            <div className="h-full flex flex-col bg-surface">
              <div className="px-3 py-1.5 border-b border-border flex items-center gap-2">
                {selectedFile ? (
                  <>
                    <FileText className="w-3 h-3 text-fg-muted" />
                    <span className="text-[12px] font-mono text-fg truncate flex-1" data-testid="selected-file-path">{selectedFile.path}</span>
                    <span className="text-micro uppercase bg-bg px-1 rounded-sm">v{selectedFile.version}</span>
                    {!editing && (
                      <button onClick={() => { setEditBuf(fileContent); setEditing(true); }} data-testid="edit-file-btn" className="text-micro px-2 py-1 border border-border hover:bg-bg rounded-sm flex items-center gap-1"><Pencil className="w-3 h-3" /> Edit</button>
                    )}
                    {editing && (
                      <>
                        <button onClick={() => setEditing(false)} className="text-micro px-2 py-1 border border-border rounded-sm">Cancel</button>
                        <button onClick={onSaveFile} data-testid="save-file-btn" className="text-micro px-2 py-1 bg-ink text-ink-fg rounded-sm flex items-center gap-1"><Check className="w-3 h-3" /> Save</button>
                      </>
                    )}
                  </>
                ) : (
                  <span className="text-micro text-fg-muted">No file selected</span>
                )}
              </div>
              <div className="flex-1 min-h-0">
                {!selectedFile && (
                  <div className="h-full flex items-center justify-center text-fg-muted">
                    <div className="text-center">
                      <Sparkles className="w-8 h-8 mx-auto mb-2 text-brand" />
                      <div className="text-sm">Select a file from the tree to view or edit it.</div>
                    </div>
                  </div>
                )}
                {selectedFile && (
                  <Editor
                    height="100%"
                    path={selectedFile.path}
                    language={pathLang(selectedFile.path)}
                    value={editing ? editBuf : fileContent}
                    onChange={(v) => editing && setEditBuf(v ?? "")}
                    options={{
                      readOnly: !editing,
                      minimap: { enabled: false },
                      fontSize: 12,
                      lineNumbers: "on",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                    }}
                  />
                )}
              </div>
            </div>
          </Panel>

          <PanelResizeHandle className="w-1 bg-border hover:bg-brand" />

          {/* RIGHT: chat */}
          <Panel defaultSize={30} minSize={20}>
            <div className="h-full bg-surface flex flex-col">
              <div className="px-3 py-2 border-b border-border flex items-center gap-1">
                <Code2 className="w-3 h-3" />
                <span className="text-micro font-semibold">Code Chat</span>
                <span className="text-micro text-fg-muted ml-auto truncate">
                  {selectedFile ? `→ ${selectedFile.basename || selectedFile.path.split("/").pop()}` : "no file"}
                </span>
              </div>
              <div className="flex-1 overflow-y-auto mos-scroll p-3 space-y-2" data-testid="codegen-chat-log">
                {chatMessages.length === 0 && (
                  <div className="text-micro text-fg-muted">Ask the codegen-LLM to refactor, fix, or add tests. The LLM may emit one or more <code>[FILE_CHANGE:path/to/file]…[/FILE_CHANGE]</code> blocks — review and click Apply.</div>
                )}
                {chatMessages.map((m, i) => (
                  <div key={i} className={`text-[12px] p-2 rounded-sm ${m.role === "user" ? "bg-brand-tint border border-brand" : "bg-bg border border-border"}`}>
                    <div className="text-micro uppercase font-bold text-fg-muted mb-1">{m.role}</div>
                    <pre className="whitespace-pre-wrap text-[12px] leading-snug text-fg">{m.content}</pre>
                    {m.file_changes?.length > 0 && (
                      <div className="mt-1.5 space-y-1">
                        {m.file_changes.map((fc, j) => (
                          <div key={j} className="text-micro flex items-center gap-1">
                            <span className="font-mono truncate flex-1">{fc.file_path}</span>
                            <button onClick={() => onApplyFileChange(fc, m.message_id)} data-testid={`apply-fc-${i}-${j}`} className="px-2 py-0.5 bg-ink text-ink-fg rounded-sm">Apply</button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {chatBusy && <div className="text-micro text-fg-muted flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> Thinking…</div>}
              </div>
              <div className="border-t border-border p-2 flex gap-1">
                <textarea
                  rows={2}
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSendChat(); } }}
                  placeholder="Ask about the code…"
                  data-testid="codegen-chat-input"
                  className="flex-1 text-[12px] border border-border focus:border-fg outline-none rounded-sm px-2 py-1.5 resize-none"
                />
                <Button data-testid="codegen-chat-send" onClick={onSendChat} disabled={chatBusy} className="h-auto bg-ink text-ink-fg px-3"><Send className="w-3 h-3" /></Button>
              </div>
            </div>
          </Panel>
        </PanelGroup>
        )}
      </div>

      <ResetModal
        open={resetOpen}
        onClose={() => setResetOpen(false)}
        onConfirm={onReset}
        title="Reset Stage 4 — CodeGen"
        warning="Removes all generated files and codegen runs. Stage 5 (Living) context will also be cleared."
      />

      <AutoValidateModal
        open={autoValidateModalOpen}
        onClose={() => setAutoValidateModalOpen(false)}
        onStart={onAutoValidateStart}
        services={services}
        defaultService={filterService}
      />

      {/* iter-13.119 — Auto-Validate & Improve LIVE dashboard. Polls
          /api/codegen/{pid}/parity-report every 2s while open and the run
          is still in flight. Renders:
            • header  → overall score, threshold, iteration N/M, status pill
            • tabs    → Frontend services | Backend services
            • cards   → one per service, expandable to per-file breakdown
            • per-file → axis bars (structural/parity/coverage/schema/
                         evidence/requirement) + issues list */}
      {parityReportOpen && (
        <div aria-hidden="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
          data-testid="parity-report-modal"
          onClick={() => setParityReportOpen(false)}
        >
          <div role="presentation"
            className="bg-surface max-w-5xl w-full max-h-[90vh] rounded-sm border-2 border-emerald-500 flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            {/* ── Header ─────────────────────────────────────────── */}
            <ParityReportHeader
              report={parityReport}
              live={autoValidateJob.running}
              onClose={() => setParityReportOpen(false)}
            />

            {/* ── Tabs ───────────────────────────────────────────── */}
            <ParityReportTabs
              report={parityReport}
              tab={reportTab}
              onTabChange={setReportTab}
              logLineCount={(liveJobSnapshot?.log || autoValidateJob.job?.log || []).length}
            />

            {/* ── Body ───────────────────────────────────────────── */}
            <div className="flex-1 overflow-y-auto mos-scroll px-5 py-3">
              <ParityReportBody
                report={parityReport}
                tab={reportTab}
                live={autoValidateJob.running}
                jobLog={liveJobSnapshot?.log || autoValidateJob.job?.log || []}
                currentStep={liveJobSnapshot?.step || autoValidateJob.job?.step}
                expandedSvcs={expandedSvcs}
                onToggleSvc={(name) =>
                  setExpandedSvcs((p) => ({ ...p, [name]: !p[name] }))
                }
                expandedFiles={expandedFiles}
                onToggleFile={(path) =>
                  setExpandedFiles((p) => ({ ...p, [path]: !p[path] }))
                }
              />
            </div>

            {/* ── Footer ─────────────────────────────────────────── */}
            <div className="px-5 py-2 border-t border-border flex items-center justify-between text-micro text-fg-muted">
              <span>
                Started: {parityReport?.started_at || "—"} · Ended:{" "}
                {parityReport?.ended_at || "—"}
              </span>
              <span className="flex items-center gap-2">
                {parityReport?.report_path && (
                  <>
                    Report saved to{" "}
                    <code className="bg-bg px-1">
                      {parityReport.report_path}
                    </code>
                  </>
                )}
                {autoValidateJob.running && (
                  <span className="text-emerald-700 flex items-center gap-1">
                    <Loader2 className="w-2.5 h-2.5 animate-spin" /> live · auto-refresh 2s
                  </span>
                )}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* iter-13.56 — right-click context menu over the file tree.
          Positioned at click coords; dismissed by any click / scroll / Escape. */}
      {ctxMenu && (
        <div role="presentation"
          data-testid="codegen-tree-ctxmenu"
          onClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
          className="fixed z-50 bg-surface border border-border rounded-sm shadow-lg py-1 text-[12px] min-w-[180px]"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
        >
          <div className="px-3 py-1 text-micro uppercase tracking-wider text-fg-muted border-b border-surface-2 truncate">
            {ctxMenu.kind === "file" ? ctxMenu.file?.path : ctxMenu.dirKey}
          </div>
          {ctxMenu.kind === "file" && (
            <button
              data-testid="ctxmenu-delete-file"
              onClick={() => { const f = ctxMenu.file; setCtxMenu(null); onDeleteFile(f); }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-red-50 text-red-700"
            >
              <Trash2 className="w-3 h-3" /> Delete file
            </button>
          )}
          {ctxMenu.kind === "dir" && (
            <button
              data-testid="ctxmenu-delete-dir"
              onClick={() => { const k = ctxMenu.dirKey; setCtxMenu(null); onDeleteDir(k); }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-red-50 text-red-700"
            >
              <Trash2 className="w-3 h-3" /> Delete folder &amp; all files
            </button>
          )}
        </div>
      )}
    </div>
  );
}
