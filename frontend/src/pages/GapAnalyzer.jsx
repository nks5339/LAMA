/**
 * Gap Analyzer — Two-phase KB-driven verification tool.
 *
 * Phase 1: build KB (entities + requirements + UI→API→DB traceability).
 * Phase 2: LLM verifier compares KB against docs, produces gap report.
 *
 * Layout: compact 3-tab workspace (Input | Knowledge Base | Report) in the
 * LAMA enterprise theme — EY Yellow accent, Slate palette, dense typography.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import {
  Upload, FileText, Code2, Trash2, X, Play, RefreshCw, CheckCircle,
  AlertTriangle, AlertCircle, Search, Download, FileSpreadsheet, FileJson,
  Database, Network, Layers, ChevronDown, ArrowRight,
  Filter, ExternalLink, Clock, Target, Zap, Info, Github, FolderUp, Plus, Link as LinkIcon,
  MoreVertical, History as HistoryIcon, Lock, Unlock,
} from "lucide-react";
import {
  createGapAnalysis,
  pollGapAnalysisStatus,
  getGapAnalysis,
  getGapAnalysisKB,
  gapAnalysisExportUrl,
  runGapAnalysis,
  listModels,
  listGapAnalyses,
  deleteGapAnalysis,
  freezeGapAnalysis,
  unfreezeGapAnalysis,
} from "../lib/api";
import ZipFileRow from "../components/ZipFileRow";
import {
  PieChart, Pie, Cell, ResponsiveContainer, Tooltip as RTooltip,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, } from "recharts";

/* ═════════ Constants ═════════ */

// iter-14.95 — Doc-type taxonomy mirrored from backend `DOC_TYPE_LABELS`.
// Keep in sync with routes/tools.py. `/api/tools/gap-analyzer/meta` exposes
// the canonical list; this is a static fallback so the UI renders instantly.
const DOC_TYPES = [
  { key: "srs",        label: "SRS — Software Requirements Spec" },
  { key: "frs",        label: "FRS — Functional Requirements Spec" },
  { key: "brd",        label: "BRD — Business Requirements Doc" },
  { key: "user_manual",label: "User Manual / Guide" },
  { key: "data_dict",  label: "Data Dictionary" },
  { key: "design",     label: "Design Doc / HLD / LLD" },
  { key: "api_spec",   label: "API Spec (OpenAPI / Postman)" },
  { key: "test_plan",  label: "Test Plan / Test Cases" },
  { key: "other",      label: "Other" },
];

const BACKEND_PHASE_MAP = {
  queued: "upload",
  building_kb: "kb",
  extracting: "extract",
  analyzing: "verify",
  llm_call: "verify",     // iter-15.6 sub-phase
  parsing: "verify",      // iter-15.6 sub-phase
  finalizing: "verify",   // iter-15.6 sub-phase
  completed: "report",
};

/* ═════════ Helpers ═════════ */

const fmtBytes = (b) => {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(1)} MB`;
};

const sevColor = (s) => ({
  critical: "text-red-700 bg-red-50 border-red-200",
  major: "text-amber-700 bg-amber-50 border-amber-200",
  minor: "text-fg-muted bg-surface-2 border-border",
}[s] || "text-fg-muted bg-surface-2 border-border");

const sevDot = (s) => ({
  critical: "bg-red-500",
  major: "bg-amber-500",
  minor: "bg-fg-subtle",
}[s] || "bg-fg-subtle");

/* ═════════ Sub-components ═════════ */

const StatCard = ({ label, value, icon: Icon, tone = "slate" }) => (
  <div className="border border-border rounded-md px-2.5 py-1.5 bg-surface">
    <div className="flex items-center justify-between">
      <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">{label}</span>
      {Icon && <Icon size={11} className="text-fg-subtle" />}
    </div>
    <div className={`text-base font-bold tabular-nums leading-tight text-${tone}-700`}>{value}</div>
  </div>
);

/* ═════════ Tabs ═════════ */

const InputTab = ({
  name, setName,
  sourceMode, setSourceMode,
  codeFiles, setCodeFiles,
  githubUrl, setGithubUrl,
  githubBranch, setGithubBranch,
  githubToken, setGithubToken,
  docFiles, setDocFiles,
  onCreate, creating, canSubmit, validationMsg,
}) => {
  const codeInputRef = useRef(null);
  const docInputRef = useRef(null);

  const addCodeFiles = (fileList) => {
    const added = Array.from(fileList || []).map(f => ({ file: f }));
    setCodeFiles(prev => [...prev, ...added]);
  };
  const addDocFiles = (fileList) => {
    const added = Array.from(fileList || []).map(f => {
      const n = f.name.toLowerCase();
      // Heuristic default doc-type from filename
      let dt = "srs";
      if (/frs|functional/.test(n)) dt = "frs";
      else if (/brd|business/.test(n)) dt = "brd";
      else if (/manual|guide/.test(n)) dt = "user_manual";
      else if (/data.?dict/.test(n)) dt = "data_dict";
      else if (/hld|lld|design|architecture/.test(n)) dt = "design";
      else if (/api|openapi|swagger|postman/.test(n)) dt = "api_spec";
      else if (/test|qa/.test(n)) dt = "test_plan";
      return { file: f, docType: dt };
    });
    setDocFiles(prev => [...prev, ...added]);
  };

  return (
    <div className="flex-1 overflow-y-auto bg-surface-2/40">
      <div className="px-4 py-3 space-y-3">
        {/* Analysis name — compact single-row */}
        <div className="bg-surface border border-border rounded-lg px-3 py-2 flex items-center gap-3">
          <label className="text-micro uppercase tracking-wide text-fg-subtle font-semibold flex-shrink-0">
            Analysis Name
          </label>
          <input
            data-testid="gap-analysis-name-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. JHS Q4 Requirements Coverage"
            className="flex-1 px-2 py-1 text-sm border border-border rounded-md bg-surface focus:border-brand focus:ring-1 focus:ring-brand outline-none"
          />
          <span className="text-micro text-fg-subtle">
            {name.trim() ? "✓" : "required"}
          </span>
        </div>

        {/* 2-column grid: Source Code | Documentation */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          {/* Source Code Section */}
          <div className="bg-surface border border-border rounded-lg overflow-hidden flex flex-col">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-surface-2/70">
              <div className="flex items-center gap-2">
                <Code2 size={14} className="text-violet-600" />
                <span className="text-sm font-semibold text-fg">Source Code</span>
                <span className="text-micro text-fg-subtle">Required</span>
              </div>
              <div className="flex items-center rounded-md border border-border bg-surface p-0.5">
                <button
                  data-testid="src-mode-upload"
                  onClick={() => setSourceMode("upload")}
                  className={`text-micro px-2.5 py-1 rounded flex items-center gap-1.5 font-medium transition-colors ${
                    sourceMode === "upload" ? "bg-ink text-white" : "text-fg-muted hover:bg-surface-2"
                  }`}
                >
                  <FolderUp size={11} /> Upload
                </button>
                <button
                  data-testid="src-mode-github"
                  onClick={() => setSourceMode("github")}
                  className={`text-micro px-2.5 py-1 rounded flex items-center gap-1.5 font-medium transition-colors ${
                    sourceMode === "github" ? "bg-ink text-white" : "text-fg-muted hover:bg-surface-2"
                  }`}
                >
                  <Github size={11} /> GitHub
                </button>
              </div>
            </div>

            {sourceMode === "upload" ? (
              <div className="p-3 flex-1 flex flex-col">
                <div
                  role="button"
                tabIndex={0}
                aria-label="Choose source code files, or drop them here"
                onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      codeInputRef.current?.click();
                    }
                  }}
                onClick={() => codeInputRef.current?.click()}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { e.preventDefault(); addCodeFiles(e.dataTransfer.files); }}
                  className="border-2 border-dashed border-border-strong rounded-md py-4 flex flex-col items-center justify-center cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring hover:border-violet-400 hover:bg-violet-50/30 transition-colors"
                >
                  <Upload size={20} className="text-fg-subtle mb-1" />
                  <p className="text-xs font-medium text-fg-muted">Drop or click to add</p>
                  <p className="text-micro text-fg-subtle mt-0.5">
                    .zip, .tar, .jar, .war or individual source files
                  </p>
                  <input
                    ref={codeInputRef}
                    type="file"
                    multiple
                    className="hidden"
                    onChange={(e) => { addCodeFiles(e.target.files); e.target.value = ""; }}
                    accept=".zip,.tar,.gz,.tgz,.jar,.war,.java,.py,.js,.ts,.tsx,.jsx,.php,.jsp,.cs,.go,.rb,.swift,.kt,.scala,.sql,.yaml,.yml,.json"
                  />
                </div>
                {codeFiles.length > 0 && (
                  <div className="mt-2 border border-border rounded overflow-hidden">
                    <div className="px-2.5 py-1 bg-surface-2 border-b border-border flex items-center justify-between">
                      <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
                        {codeFiles.length} file{codeFiles.length > 1 ? "s" : ""}
                      </span>
                      <button
                        onClick={() => setCodeFiles([])}
                        className="text-micro text-fg-subtle hover:text-red-600 flex items-center gap-1"
                      >
                        <Trash2 size={10} /> Clear all
                      </button>
                    </div>
                    <div className="max-h-[260px] overflow-y-auto divide-y divide-border p-1 space-y-1">
                      {codeFiles.map((f, i) => (
                        <ZipFileRow
                          key={i}
                          file={f.file}
                          accentColor="violet"
                          RowIcon={Code2}
                          testId={`code-file-remove-${i}`}
                          onRemove={() => setCodeFiles(prev => prev.filter((_, idx) => idx !== i))}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="p-3 space-y-2 flex-1">
                <div>
                  <label className="text-micro uppercase tracking-wide text-fg-subtle font-semibold mb-1 flex items-center gap-1">
                    <LinkIcon size={10} /> Repository URL <span className="text-red-500">*</span>
                  </label>
                  <input
                    data-testid="github-url-input"
                    type="text"
                    value={githubUrl}
                    onChange={(e) => setGithubUrl(e.target.value)}
                    placeholder="https://github.com/owner/repo.git"
                    className="w-full px-2.5 py-1.5 text-sm border border-border rounded-md focus:border-brand focus:ring-1 focus:ring-brand outline-none font-mono"
                  />
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="text-micro uppercase tracking-wide text-fg-subtle font-semibold mb-1 block">
                      Branch
                    </label>
                    <input
                      data-testid="github-branch-input"
                      type="text"
                      value={githubBranch}
                      onChange={(e) => setGithubBranch(e.target.value)}
                      placeholder="main"
                      className="w-full px-2.5 py-1.5 text-sm border border-border rounded-md focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    />
                  </div>
                  <div>
                    <label className="text-micro uppercase tracking-wide text-fg-subtle font-semibold mb-1 block">
                      Access Token
                    </label>
                    <input
                      data-testid="github-token-input"
                      type="password"
                      value={githubToken}
                      onChange={(e) => setGithubToken(e.target.value)}
                      placeholder="ghp_… (private repos)"
                      className="w-full px-2.5 py-1.5 text-sm border border-border rounded-md focus:border-brand focus:ring-1 focus:ring-brand outline-none font-mono"
                    />
                  </div>
                </div>
                <p className="text-micro text-fg-subtle flex items-start gap-1.5 pt-0.5">
                  <Info size={11} className="mt-[1px] flex-shrink-0" />
                  Shallow clone (depth=1). Token used only for this clone, never persisted.
                </p>
              </div>
            )}
          </div>

          {/* Documents Section */}
          <div className="bg-surface border border-border rounded-lg overflow-hidden flex flex-col">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-surface-2/70">
              <div className="flex items-center gap-2">
                <FileText size={14} className="text-sky-600" />
                <span className="text-sm font-semibold text-fg">Documentation</span>
                <span className="text-micro text-fg-subtle">Required · tag each file</span>
              </div>
              <button
                data-testid="add-doc-btn"
                role="button"
                tabIndex={0}
                aria-label="Choose SRS, FRS, BRD or API documents, or drop them here"
                onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      docInputRef.current?.click();
                    }
                  }}
                onClick={() => docInputRef.current?.click()}
                className="text-micro px-2.5 py-1 rounded border border-border hover:bg-surface-2 text-fg-muted font-medium flex items-center gap-1"
              >
                <Plus size={11} /> Add file
              </button>
              <input
                ref={docInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => { addDocFiles(e.target.files); e.target.value = ""; }}
                accept=".zip,.pdf,.doc,.docx,.md,.markdown,.txt,.rtf,.yaml,.yml,.json"
              />
            </div>

            {docFiles.length === 0 ? (
              <div
                role="button"
                tabIndex={0}
                aria-label="Choose SRS, FRS, BRD or API documents, or drop them here"
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    docInputRef.current?.click();
                  }
                }}
                onClick={() => docInputRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => { e.preventDefault(); addDocFiles(e.dataTransfer.files); }}
                className="m-3 border-2 border-dashed border-border-strong rounded-md py-4 flex-1 flex flex-col items-center justify-center cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring hover:border-sky-400 hover:bg-sky-50/30 transition-colors"
              >
                <FileText size={20} className="text-fg-subtle mb-1" />
                <p className="text-xs font-medium text-fg-muted">Drop SRS / FRS / BRD / API docs</p>
                <p className="text-micro text-fg-subtle mt-0.5">
                  Individual files or ZIP · PDF / DOCX / MD / TXT
                </p>
              </div>
            ) : (
              <div className="flex-1 overflow-y-auto">
                <div className="grid grid-cols-[minmax(0,1fr)_180px_54px_28px] items-center px-3 py-1.5 text-micro uppercase tracking-wide text-fg-subtle font-semibold bg-surface-2/60 border-b border-border gap-2">
                  <span>File</span>
                  <span>Type</span>
                  <span className="text-right">Size</span>
                  <span></span>
                </div>
                {docFiles.map((f, i) => (
                  <div key={i} className="grid grid-cols-[minmax(0,1fr)_180px_54px_28px] items-center gap-2 px-3 py-1.5 hover:bg-surface-2/60 text-xs border-b border-border group">
                    <div className="flex items-center gap-2 min-w-0">
                      <FileText size={12} className="text-sky-500 flex-shrink-0" />
                      <span className="truncate text-fg-muted font-medium" title={f.file.name}>{f.file.name}</span>
                    </div>
                    <select
                      data-testid={`doc-type-select-${i}`}
                      value={f.docType}
                      onChange={(e) => setDocFiles(prev => prev.map((d, idx) => idx === i ? { ...d, docType: e.target.value } : d))}
                      className="text-micro border border-border rounded px-1.5 py-1 bg-surface focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    >
                      {DOC_TYPES.map(dt => (
                        <option key={dt.key} value={dt.key}>{dt.label}</option>
                      ))}
                    </select>
                    <span className="text-micro text-fg-subtle tabular-nums text-right">{fmtBytes(f.file.size)}</span>
                    <button
                      data-testid={`doc-file-remove-${i}`}
                      onClick={() => setDocFiles(prev => prev.filter((_, idx) => idx !== i))}
                      title="Remove file"
                      className="text-fg-subtle hover:text-red-600 hover:bg-red-50 rounded p-0.5"
                    >
                      <X size={12} />
                    </button>
                  </div>
                ))}
                <div className="px-3 py-1.5 flex items-center justify-between bg-surface-2/40">
                  <span className="text-micro text-fg-subtle">
                    {docFiles.length} document{docFiles.length > 1 ? "s" : ""}
                  </span>
                  <button
                    onClick={() => setDocFiles([])}
                    className="text-micro text-fg-subtle hover:text-red-600 flex items-center gap-1"
                  >
                    <Trash2 size={10} /> Clear all
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Submit bar — sticky bottom, full width */}
        <div className="sticky bottom-0 bg-surface border border-border rounded-lg shadow-sm px-4 py-2.5 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 text-micro text-fg-subtle">
            <span className="flex items-center gap-1">
              <Code2 size={11} className="text-violet-500" />
              Code: <span className="font-semibold text-fg-muted">
                {sourceMode === "github" ? (githubUrl ? "GitHub repo" : "—") : `${codeFiles.length} file(s)`}
              </span>
            </span>
            <span className="text-fg-subtle">•</span>
            <span className="flex items-center gap-1">
              <FileText size={11} className="text-sky-500" />
              Docs: <span className="font-semibold text-fg-muted">{docFiles.length} file(s)</span>
            </span>
            {!canSubmit && validationMsg && (
              <>
                <span className="text-fg-subtle">•</span>
                <span className="text-amber-600 font-medium">{validationMsg}</span>
              </>
            )}
          </div>
          <button
            data-testid="gap-analyzer-run-btn"
            onClick={onCreate}
            disabled={!canSubmit || creating}
            className="px-4 py-2 bg-brand hover:bg-brand-hover text-fg font-semibold rounded-md text-sm flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {creating ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
            {creating ? "Uploading…" : "Build KB & Run Analysis"}
          </button>
        </div>
      </div>
    </div>
  );
};

const KBTab = ({ kb, phase, phaseLabel, progressPct, requirements }) => {
  const [reqFilter, setReqFilter] = useState("all");
  const [chainFilter, setChainFilter] = useState("all");

  if (!kb) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-center p-6 bg-surface-2">
        <div className="w-14 h-14 rounded-full bg-surface shadow-sm flex items-center justify-center mb-3">
          {phase && phase !== "report" ? (
            <RefreshCw size={22} className="text-fg-subtle animate-spin" />
          ) : (
            <Database size={22} className="text-fg-subtle" />
          )}
        </div>
        <p className="text-sm font-medium text-fg-muted">
          {phaseLabel ||
           (phase === "kb" ? "Extracting code entities…" :
            phase === "extract" ? "Building UI→API→DB traceability map…" :
            phase ? "KB not ready yet" :
            "Run analysis to populate the knowledge base")}
        </p>
        {phase && phase !== "report" && (
          <div className="mt-4 w-72">
            <div className="h-2 bg-surface-3 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 transition-all duration-500"
                style={{ width: `${Math.max(3, Math.min(100, progressPct || 3))}%` }}
              />
            </div>
            <div className="text-micro text-fg-subtle mt-1 tabular-nums">
              {progressPct || 0}% complete
            </div>
          </div>
        )}
      </div>
    );
  }

  const stats = kb.stats || {};
  const chains = kb.chains || [];
  const reqs = requirements || kb.requirements || [];
  const orphanedApi = kb.orphaned_api || [];
  const orphanedDb = kb.orphaned_db || [];
  const orphanedUi = kb.orphaned_ui || [];
  const entitySample = kb.entity_sample || [];

  const filteredReqs = reqs.filter(r => reqFilter === "all" || r.category === reqFilter);
  const reqCategories = [...new Set(reqs.map(r => r.category))];

  const filteredChains = chains.filter(c => {
    if (chainFilter === "resolved") return c.resolved;
    if (chainFilter === "unresolved") return !c.resolved;
    return true;
  });

  return (
    <div className="flex-1 overflow-y-auto bg-surface-2">
      {/* Stats bar */}
      <div className="grid grid-cols-6 gap-2 p-3 bg-surface border-b border-border">
        <StatCard label="Code Files" value={stats.code_files || 0} icon={Code2} />
        <StatCard label="Entities" value={stats.entities || 0} icon={Layers} />
        <StatCard label="UI Files" value={stats.ui_files || 0} icon={Layers} tone="violet" />
        <StatCard label="API Routes" value={stats.api_routes || 0} icon={Network} tone="sky" />
        <StatCard label="DB Tables" value={stats.db_tables || 0} icon={Database} tone="emerald" />
        <StatCard label="Requirements" value={stats.requirements || 0} icon={Target} tone="amber" />
      </div>

      {/* Traceability + Requirements — two-column on wide screens so both are visible */}
      <div className="p-3 grid grid-cols-1 xl:grid-cols-2 gap-3">
        {/* UI → API → DB Traceability */}
        <div className="bg-surface border border-border rounded-md overflow-hidden flex flex-col min-h-0">
          <div className="px-3 py-2 border-b border-border flex items-center justify-between bg-surface-2/70 flex-shrink-0">
            <div className="flex items-center gap-2 min-w-0">
              <Network size={13} className="text-fg-muted flex-shrink-0" />
              <span className="text-xs font-semibold text-fg-muted truncate">UI → API → DB Traceability</span>
              <span className="text-micro px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-medium flex-shrink-0">
                {stats.resolved_chains || 0} resolved
              </span>
              <span className="text-micro px-1.5 py-0.5 rounded bg-red-50 text-red-700 font-medium flex-shrink-0">
                {stats.unresolved_chains || 0} unresolved
              </span>
            </div>
            <select
              value={chainFilter}
              onChange={(e) => setChainFilter(e.target.value)}
              className="text-micro px-2 py-0.5 border border-border rounded bg-surface flex-shrink-0"
            >
              <option value="all">All chains</option>
              <option value="resolved">Resolved only</option>
              <option value="unresolved">Unresolved only</option>
            </select>
          </div>
          <div className="max-h-[380px] overflow-y-auto">
            {filteredChains.length === 0 ? (
              <div className="text-center py-6 text-xs text-fg-subtle">No traceability chains detected</div>
            ) : (
              <table className="w-full text-xs">
                <thead className="bg-surface-2 text-fg-subtle uppercase text-micro tracking-wide sticky top-0">
                  <tr>
                    <th className="text-left px-3 py-1.5 font-semibold">UI Source</th>
                    <th className="px-2 py-1.5"></th>
                    <th className="text-left px-3 py-1.5 font-semibold">API Endpoint</th>
                    <th className="px-2 py-1.5"></th>
                    <th className="text-left px-3 py-1.5 font-semibold">DB Tables</th>
                    <th className="px-2 py-1.5 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {filteredChains.slice(0, 100).map((c, i) => (
                    <tr key={i} className="hover:bg-surface-2">
                      <td className="px-3 py-1.5 font-mono text-micro text-fg-muted max-w-[200px] truncate" title={c.ui}>{c.ui}</td>
                      <td className="text-fg-subtle"><ArrowRight size={11} /></td>
                      <td className="px-3 py-1.5 font-mono text-micro text-fg-muted">
                        <span className={`px-1 rounded ${
                          c.http_method === "GET" ? "text-emerald-700" :
                          c.http_method === "POST" ? "text-sky-700" :
                          c.http_method === "PUT" || c.http_method === "PATCH" ? "text-amber-700" :
                          c.http_method === "DELETE" ? "text-red-700" : "text-fg-muted"
                        }`}>{c.http_method || ""}</span>{" "}
                        {c.api_resolved || c.api_ref}
                      </td>
                      <td className="text-fg-subtle"><ArrowRight size={11} /></td>
                      <td className="px-3 py-1.5 text-micro text-fg-muted">
                        {c.tables?.length ? c.tables.map(t => (
                          <span key={t} className="inline-block mr-1 px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-mono">{t}</span>
                        )) : <span className="text-fg-subtle">—</span>}
                      </td>
                      <td className="px-2 py-1.5">
                        {c.resolved ? (
                          <CheckCircle size={12} className="text-emerald-500 mx-auto" />
                        ) : (
                          <AlertCircle size={12} className="text-red-500 mx-auto" />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Requirements */}
        <div className="bg-surface border border-border rounded-md overflow-hidden flex flex-col min-h-0">
          <div className="px-3 py-2 border-b border-border flex items-center justify-between bg-surface-2/70 flex-shrink-0">
            <div className="flex items-center gap-2 min-w-0">
              <Target size={13} className="text-fg-muted flex-shrink-0" />
              <span className="text-xs font-semibold text-fg-muted truncate">Extracted Requirements ({reqs.length})</span>
            </div>
            <select
              value={reqFilter}
              onChange={(e) => setReqFilter(e.target.value)}
              className="text-micro px-2 py-0.5 border border-border rounded bg-surface flex-shrink-0"
            >
              <option value="all">All categories</option>
              {reqCategories.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div className="max-h-[380px] overflow-y-auto divide-y divide-border">
            {filteredReqs.length === 0 ? (
              <div className="text-center py-6 text-xs text-fg-subtle">No requirements extracted</div>
            ) : filteredReqs.slice(0, 80).map((r, i) => (
              <div key={i} className="px-3 py-1.5 hover:bg-surface-2 text-xs flex items-start gap-2">
                <span className={`text-micro px-1.5 py-0.5 rounded font-mono font-semibold flex-shrink-0 ${
                  r.type === "EXPLICIT" ? "bg-emerald-50 text-emerald-700" : "bg-surface-2 text-fg-muted"
                }`}>{r.id}</span>
                <span className={`text-micro px-1.5 py-0.5 rounded font-medium flex-shrink-0 ${
                  r.category === "security" ? "bg-red-50 text-red-700" :
                  r.category === "api" ? "bg-sky-50 text-sky-700" :
                  r.category === "data" ? "bg-emerald-50 text-emerald-700" :
                  r.category === "ui" ? "bg-violet-50 text-violet-700" :
                  "bg-surface-2 text-fg-muted"
                }`}>{r.category}</span>
                <span className="flex-1 text-fg-muted leading-snug">{r.text}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="px-3 pb-3">
        {/* Orphans */}
        {(orphanedApi.length > 0 || orphanedDb.length > 0 || orphanedUi.length > 0) && (
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: "Orphan APIs", items: orphanedApi, icon: Network, tone: "sky" },
              { label: "Orphan DB Tables", items: orphanedDb, icon: Database, tone: "emerald" },
              { label: "Orphan UI Files", items: orphanedUi, icon: Layers, tone: "violet" },
            ].map(o => (
              <div key={o.label} className="bg-surface border border-border rounded-md overflow-hidden">
                <div className="px-3 py-1.5 border-b border-border bg-amber-50/50 flex items-center gap-2">
                  <AlertTriangle size={11} className="text-amber-600" />
                  <span className="text-micro font-semibold text-fg-muted">{o.label} ({o.items.length})</span>
                </div>
                <div className="p-2 max-h-[120px] overflow-y-auto text-micro font-mono text-fg-muted">
                  {o.items.length === 0 ? <span className="text-fg-subtle">none</span> :
                    o.items.slice(0, 10).map((x, i) => <div key={i} className="truncate py-0.5" title={x}>{x}</div>)}
                  {o.items.length > 10 && <div className="text-fg-subtle pt-1">+{o.items.length - 10} more</div>}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Entity sample */}
        {entitySample.length > 0 && (
          <div className="bg-surface border border-border rounded-md overflow-hidden mt-3">
            <div className="px-3 py-2 border-b border-border flex items-center gap-2 bg-surface-2/70">
              <Layers size={13} className="text-fg-muted" />
              <span className="text-xs font-semibold text-fg-muted">Extracted Entities Sample</span>
            </div>
            <div className="max-h-[200px] overflow-y-auto p-2 grid grid-cols-2 gap-1 text-micro">
              {entitySample.slice(0, 40).map((e, i) => (
                <div key={i} className="flex items-center gap-2 px-2 py-1 hover:bg-surface-2 rounded">
                  <span className="text-micro px-1 py-0.5 rounded bg-surface-2 text-fg-muted font-mono font-semibold flex-shrink-0">{e.type}</span>
                  <span className="font-mono text-fg-muted truncate">{e.name}</span>
                  {e.source_file && <span className="text-fg-subtle truncate ml-auto text-micro" title={e.source_file}>{e.source_file.split("/").pop()}</span>}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

const ReportChartsPanel = ({ result, matrix, gaps }) => {
  // iter-14.96 — Visual analytics: coverage donut, severity donut,
  // category bar. All derived from the LLM result — no extra fetches.
  const covered = matrix.filter(r => r.implemented).length;
  const missing = matrix.length - covered;
  const covPct = matrix.length ? Math.round((covered / matrix.length) * 100) : (result.coverage_pct || 0);

  const coverageData = [
    { name: "Covered",  value: covered  || 0, key: "covered"  },
    { name: "Missing",  value: missing  || 0, key: "missing"  },
  ];
  const COV_COLORS = { covered: "#10b981", missing: "#ef4444" };

  const severityData = [
    { name: "Critical", value: result.critical_gaps || 0, key: "critical" },
    { name: "Major",    value: result.major_gaps    || 0, key: "major"    },
    { name: "Minor",    value: result.minor_gaps    || 0, key: "minor"    },
  ];
  const SEV_COLORS = { critical: "#ef4444", major: "#f59e0b", minor: "#94a3b8" };

  // Category distribution — from gap.type or gap.category, fallback "other"
  const catCounts = {};
  gaps.forEach(g => {
    const k = (g.category || g.type || "other").toString().toLowerCase();
    catCounts[k] = (catCounts[k] || 0) + 1;
  });
  const categoryData = Object.entries(catCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([k, v]) => ({ name: k, gaps: v }));

  const noSeverity = severityData.every(d => d.value === 0);
  const noCategory = categoryData.length === 0;
  const noCoverage = matrix.length === 0;

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 p-3 bg-surface border-b border-border">
      {/* Coverage Donut */}
      <div className="border border-border rounded-md p-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
            Coverage
          </span>
          <span className="text-micro text-fg-subtle">
            {covered}/{matrix.length} requirements
          </span>
        </div>
        {noCoverage ? (
          <div className="h-[160px] flex items-center justify-center text-micro text-fg-subtle">
            No coverage matrix available
          </div>
        ) : (
          <div className="relative" style={{ height: 160 }}>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={coverageData}
                  cx="50%" cy="50%"
                  innerRadius={45} outerRadius={65}
                  paddingAngle={2}
                  dataKey="value"
                  stroke="none"
                >
                  {coverageData.map((entry) => (
                    <Cell key={entry.key} fill={COV_COLORS[entry.key]} />
                  ))}
                </Pie>
                <RTooltip
                  contentStyle={{ fontSize: 11, borderRadius: 6, border: "1px solid #e2e8f0" }}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
              <div className={`text-2xl font-bold tabular-nums ${
                covPct >= 70 ? "text-emerald-600" : covPct >= 40 ? "text-amber-600" : "text-red-600"
              }`}>{covPct}%</div>
              <div className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">Coverage</div>
            </div>
          </div>
        )}
        <div className="flex items-center justify-center gap-3 mt-1 text-micro text-fg-muted">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-emerald-500" /> Covered ({covered})
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-red-500" /> Missing ({missing})
          </span>
        </div>
      </div>

      {/* Severity Donut */}
      <div className="border border-border rounded-md p-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
            Gap Severity
          </span>
          <span className="text-micro text-fg-subtle">
            {gaps.length} total
          </span>
        </div>
        {noSeverity ? (
          <div className="h-[160px] flex items-center justify-center text-micro text-emerald-600 flex-col gap-1">
            <CheckCircle size={20} />
            <span>No gaps found</span>
          </div>
        ) : (
          <div style={{ height: 160 }}>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={severityData.filter(d => d.value > 0)}
                  cx="50%" cy="50%"
                  innerRadius={45} outerRadius={65}
                  paddingAngle={2}
                  dataKey="value"
                  stroke="none"
                >
                  {severityData.filter(d => d.value > 0).map((entry) => (
                    <Cell key={entry.key} fill={SEV_COLORS[entry.key]} />
                  ))}
                </Pie>
                <RTooltip
                  contentStyle={{ fontSize: 11, borderRadius: 6, border: "1px solid #e2e8f0" }}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}
        <div className="flex items-center justify-center gap-3 mt-1 text-micro text-fg-muted flex-wrap">
          {severityData.map(s => (
            <span key={s.key} className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full" style={{ background: SEV_COLORS[s.key] }} />
              {s.name} ({s.value})
            </span>
          ))}
        </div>
      </div>

      {/* Category Bar */}
      <div className="border border-border rounded-md p-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
            Gaps by Category
          </span>
          <span className="text-micro text-fg-subtle">
            {categoryData.length} type{categoryData.length !== 1 ? "s" : ""}
          </span>
        </div>
        {noCategory ? (
          <div className="h-[180px] flex items-center justify-center text-micro text-fg-subtle">
            No categorized gaps
          </div>
        ) : (
          <div style={{ height: 180 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={categoryData} margin={{ top: 5, right: 8, left: -20, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 10, fill: "#64748b" }}
                  axisLine={{ stroke: "#e2e8f0" }}
                  tickLine={false}
                  interval={0}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "#64748b" }}
                  axisLine={{ stroke: "#e2e8f0" }}
                  tickLine={false}
                  allowDecimals={false}
                />
                <RTooltip
                  contentStyle={{ fontSize: 11, borderRadius: 6, border: "1px solid #e2e8f0" }}
                />
                <Bar dataKey="gaps" fill="#6366f1" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
};

const _tcStatusStyle = (s) => {
  switch ((s || "").toUpperCase()) {
    case "PASS":
      return { bg: "bg-emerald-50", border: "border-emerald-200", text: "text-emerald-700", dot: "bg-emerald-500", label: "PASS" };
    case "FAIL":
      return { bg: "bg-red-50", border: "border-red-200", text: "text-red-700", dot: "bg-red-500", label: "FAIL" };
    case "BLOCKED":
      return { bg: "bg-amber-50", border: "border-amber-200", text: "text-amber-700", dot: "bg-amber-500", label: "BLOCKED" };
    case "NOT_APPLICABLE":
      return { bg: "bg-surface-2", border: "border-border", text: "text-fg-muted", dot: "bg-fg-subtle", label: "N/A" };
    default:
      return { bg: "bg-surface-2", border: "border-border", text: "text-fg-muted", dot: "bg-fg-subtle", label: (s || "UNKNOWN").toUpperCase() };
  }
};

const TestCasesView = ({ testCases, stats, filter, expanded, setExpanded }) => {
  if (!testCases || testCases.length === 0) {
    return (
      <div className="text-center py-12 text-sm text-fg-subtle">
        No test cases were generated. Re-run the analysis with the updated verifier prompt.
      </div>
    );
  }

  const filtered = testCases.filter((tc) => {
    if (filter === "all") return true;
    if (filter === "POSITIVE") return tc.case_type === "positive";
    if (filter === "NEGATIVE") return tc.case_type === "negative";
    return (tc.status || "").toUpperCase() === filter;
  });

  const denom = stats.passed + stats.failed + stats.blocked;
  const accuracy = denom > 0 ? Math.round((stats.passed / denom) * 100) : 0;

  // iter-15.3 — surface positive/negative split alongside status donut
  const positiveCount = stats.positive != null ? stats.positive : testCases.filter(t => t.case_type === "positive").length;
  const negativeCount = stats.negative != null ? stats.negative : testCases.filter(t => t.case_type === "negative").length;

  const donutData = [
    { name: "Passed", value: stats.passed, fill: "#10b981" },
    { name: "Failed", value: stats.failed, fill: "#ef4444" },
    { name: "Blocked", value: stats.blocked, fill: "#f59e0b" },
    { name: "N/A", value: stats.not_applicable, fill: "#94a3b8" },
  ].filter((d) => d.value > 0);

  const caseTypeData = [
    { name: "Positive", value: positiveCount, fill: "#22c55e" },
    { name: "Negative", value: negativeCount, fill: "#f43f5e" },
  ].filter((d) => d.value > 0);

  const toggle = (id) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next);
  };

  return (
    <div className="space-y-3">
      {/* Charts strip */}
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-surface border border-border rounded-md p-3">
          <div className="text-micro font-semibold uppercase text-fg-subtle mb-1">Accuracy</div>
          <div className="flex items-center gap-3">
            <div className="w-24 h-24 relative">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={donutData} innerRadius={28} outerRadius={44} paddingAngle={2} dataKey="value">
                    {donutData.map((entry, i) => <Cell key={i} fill={entry.fill} />)}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
              <div className="absolute inset-0 flex items-center justify-center">
                <span className={`text-lg font-bold ${accuracy >= 70 ? "text-emerald-600" : accuracy >= 40 ? "text-amber-600" : "text-red-600"}`}>{accuracy}%</span>
              </div>
            </div>
            <div className="flex-1 text-micro space-y-1">
              <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-emerald-500"/> Passed: <span className="font-semibold ml-auto">{stats.passed}</span></div>
              <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-500"/> Failed: <span className="font-semibold ml-auto">{stats.failed}</span></div>
              <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-amber-500"/> Blocked: <span className="font-semibold ml-auto">{stats.blocked}</span></div>
              <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-fg-subtle"/> N/A: <span className="font-semibold ml-auto">{stats.not_applicable}</span></div>
            </div>
          </div>
          <div className="mt-2 text-micro text-fg-subtle">Accuracy = Passed / (Passed + Failed + Blocked). N/A excluded.</div>
        </div>
        <div className="bg-surface border border-border rounded-md p-3">
          <div className="text-micro font-semibold uppercase text-fg-subtle mb-1 flex items-center justify-between">
            <span>Status Distribution</span>
            {(positiveCount + negativeCount) > 0 && (
              <span className="normal-case tracking-normal text-micro text-fg-subtle flex items-center gap-2">
                <span className="text-emerald-700">+{positiveCount} positive</span>
                <span className="text-rose-700">−{negativeCount} negative</span>
              </span>
            )}
          </div>
          <ResponsiveContainer width="100%" height={110}>
            <BarChart data={donutData} layout="vertical" margin={{ left: 0, right: 12, top: 4, bottom: 0 }}>
              <XAxis type="number" tick={{ fontSize: 10 }} allowDecimals={false} />
              <YAxis dataKey="name" type="category" tick={{ fontSize: 10 }} width={54} />
              <RTooltip cursor={{ fill: "rgba(148,163,184,0.1)" }} />
              <Bar dataKey="value" radius={[0, 3, 3, 0]}>
                {donutData.map((entry, i) => <Cell key={i} fill={entry.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          {caseTypeData.length > 0 && (
            <div className="mt-2 flex items-center gap-1 text-micro">
              <span className="text-fg-subtle font-semibold uppercase mr-1">Design Coverage:</span>
              <div className="flex-1 h-2 rounded-full overflow-hidden bg-surface-2 flex">
                {positiveCount > 0 && (
                  <div
                    style={{ width: `${(positiveCount / (positiveCount + negativeCount)) * 100}%` }}
                    className="bg-emerald-500"
                    title={`${positiveCount} positive`}
                  />
                )}
                {negativeCount > 0 && (
                  <div
                    style={{ width: `${(negativeCount / (positiveCount + negativeCount)) * 100}%` }}
                    className="bg-rose-500"
                    title={`${negativeCount} negative`}
                  />
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Detail rows */}
      {filtered.length === 0 ? (
        <div className="text-center py-8 text-sm text-fg-subtle">No test cases match the current filter.</div>
      ) : (
        <div className="space-y-1.5">
          {filtered.map((tc, i) => {
            const key = tc.id || `tc-${i}`;
            const isExp = expanded.has(key);
            const st = _tcStatusStyle(tc.status);
            const steps = Array.isArray(tc.steps) ? tc.steps : (tc.steps ? [tc.steps] : []);
            return (
              <div key={key} className={`border rounded-md bg-surface overflow-hidden ${st.border}`}>
                <button
                  onClick={() => toggle(key)}
                  className="w-full px-3 py-2 flex items-start gap-2 text-left hover:bg-surface-2/50"
                >
                  <span className={`w-2 h-2 rounded-full flex-shrink-0 mt-1.5 ${st.dot}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      {tc.id && <span className="text-micro font-mono text-fg-subtle">{tc.id}</span>}
                      <span className="text-xs font-semibold text-fg">{tc.title || "Untitled test case"}</span>
                      <span className={`text-micro px-1.5 py-0.5 rounded font-semibold uppercase ${st.bg} ${st.text}`}>{st.label}</span>
                      {tc.case_type && (
                        <span className={`text-micro px-1.5 py-0.5 rounded font-semibold uppercase ${
                          tc.case_type === "negative"
                            ? "bg-rose-50 text-rose-700 border border-rose-200"
                            : "bg-emerald-50 text-emerald-700 border border-emerald-200"
                        }`}>
                          {tc.case_type === "negative" ? "− Negative" : "+ Positive"}
                        </span>
                      )}
                      {tc.requirement_type && (
                        <span className="text-micro px-1.5 py-0.5 rounded bg-violet-50 text-violet-700 font-semibold uppercase">
                          {tc.requirement_type === "USE_CASE" ? "UC" : tc.requirement_type === "BUSINESS_RULE" ? "BR" : tc.requirement_type === "FUNCTIONAL" ? "FR" : tc.requirement_type === "UI_SPEC" ? "UI" : tc.requirement_type === "DATA_SPEC" ? "DATA" : tc.requirement_type}
                        </span>
                      )}
                      {tc.priority && tc.priority !== "P2" && (
                        <span className={`text-micro px-1.5 py-0.5 rounded font-semibold ${
                          tc.priority === "P0" ? "bg-red-100 text-red-800" :
                          tc.priority === "P1" ? "bg-amber-100 text-amber-800" :
                          "bg-surface-2 text-fg-muted"
                        }`}>{tc.priority}</span>
                      )}
                      {tc.severity && <span className="text-micro px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted font-semibold uppercase">{tc.severity}</span>}
                      {tc.category && <span className="text-micro px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 font-semibold">{tc.category}</span>}
                      {tc.requirement_id && <span className="text-micro text-emerald-700 font-mono">↳ Req: {tc.requirement_id}</span>}
                      {tc.scenario && (
                        <span className="text-micro text-fg-subtle font-mono">· {tc.scenario.replace(/_/g, " ")}</span>
                      )}
                      {tc.synthesized && <span className="text-micro px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 font-semibold">AUTO</span>}
                      {tc.source === "uploaded" && <span className="text-micro px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 font-semibold border border-teal-200" title="Authored by SME in uploaded documentation">SME</span>}
                    </div>
                    {tc.human_summary && (
                      <p className="text-[11.5px] text-fg-muted mt-1 leading-snug italic">
                        <span className="text-fg-subtle not-italic font-semibold mr-1">Summary:</span>
                        {tc.human_summary}
                      </p>
                    )}
                    {!isExp && tc.expected_result && (
                      <p className="text-micro text-fg-subtle mt-0.5 line-clamp-1"><span className="text-fg-subtle">Expected:</span> {tc.expected_result}</p>
                    )}
                  </div>
                  <ChevronDown size={13} className={`text-fg-subtle flex-shrink-0 transition-transform ${isExp ? "rotate-180" : ""}`} />
                </button>
                {isExp && (
                  <div className="px-3 pb-2.5 ml-4 space-y-1.5 text-micro border-t border-border pt-2">
                    {tc.preconditions && (
                      <div><span className="font-semibold text-fg-subtle uppercase text-micro">Preconditions:</span> <span className="text-fg-muted">{tc.preconditions}</span></div>
                    )}
                    {steps.length > 0 && (
                      <div>
                        <div className="font-semibold text-fg-subtle uppercase text-micro mb-0.5">Steps:</div>
                        <ol className="list-decimal ml-4 space-y-0.5 text-fg-muted">
                          {steps.map((s, si) => <li key={si}>{s}</li>)}
                        </ol>
                      </div>
                    )}
                    {tc.test_data && (
                      <div><span className="font-semibold text-fg-subtle uppercase text-micro">Test Data:</span> <span className="font-mono text-fg-muted">{typeof tc.test_data === "string" ? tc.test_data : JSON.stringify(tc.test_data)}</span></div>
                    )}
                    {tc.expected_result && (
                      <div><span className="font-semibold text-fg-subtle uppercase text-micro">Expected:</span> <span className="text-fg-muted">{tc.expected_result}</span></div>
                    )}
                    {tc.actual_result && (
                      <div><span className="font-semibold text-fg-subtle uppercase text-micro">Actual:</span> <span className="text-fg-muted">{tc.actual_result}</span></div>
                    )}
                    {tc.evidence && (Array.isArray(tc.evidence) ? tc.evidence.length > 0 : true) && (
                      <div>
                        <span className="font-semibold text-fg-subtle uppercase text-micro">Evidence:</span>{" "}
                        {Array.isArray(tc.evidence) ? (
                          <span className="inline-flex flex-wrap gap-1">
                            {tc.evidence.map((ev, ei) => (
                              <span key={ei} className="text-micro font-mono px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">{ev}</span>
                            ))}
                          </span>
                        ) : (
                          <span className="font-mono text-fg-muted">{tc.evidence}</span>
                        )}
                      </div>
                    )}
                    {tc.notes && (
                      <div className="bg-surface-2 border border-border rounded p-2 mt-1">
                        <span className="font-semibold text-fg-subtle uppercase text-micro">Notes:</span>{" "}
                        <span className="text-fg-muted">{tc.notes}</span>
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
};

const ReportTab = ({ result, analysisId, phase, phaseLabel, progressPct, error, currentModel, onReanalyzeStart }) => {
  const [severity, setSeverity] = useState("all");
  const [expanded, setExpanded] = useState(new Set());
  const [view, setView] = useState("gaps"); // "gaps" | "matrix" | "tests"
  const [matrixFilter, setMatrixFilter] = useState("all"); // "all" | "covered" | "missing"
  const [testFilter, setTestFilter] = useState("all"); // "all" | "PASS" | "FAIL" | "BLOCKED" | "NOT_APPLICABLE"
  const [testExpanded, setTestExpanded] = useState(new Set());
  const [chartsOpen, setChartsOpen] = useState(false); // iter-15.0 — collapsed by default so the list is above the fold

  // iter-15.1 — Re-analyze controls
  const [models, setModels] = useState([]);
  const [reModel, setReModel] = useState("");
  const [reOpen, setReOpen] = useState(false);
  const [reBusy, setReBusy] = useState(false);
  const [reErr, setReErr] = useState("");

  // iter-15.4 — Freeze controls
  const [freezeOpen, setFreezeOpen] = useState(false);
  const [freezeConfirm, setFreezeConfirm] = useState("");
  const [freezeBusy, setFreezeBusy] = useState(false);
  const [freezeErr, setFreezeErr] = useState("");
  const isFrozen = !!result?.frozen;

  const handleFreezeToggle = async () => {
    if (!analysisId) return;
    const expected = isFrozen ? "UNFREEZE" : "FREEZE";
    if (freezeConfirm.trim().toUpperCase() !== expected) {
      setFreezeErr(`Type "${expected}" to confirm.`);
      return;
    }
    setFreezeBusy(true);
    setFreezeErr("");
    try {
      if (isFrozen) {
        await unfreezeGapAnalysis(analysisId);
      } else {
        await freezeGapAnalysis(analysisId);
      }
      setFreezeOpen(false);
      setFreezeConfirm("");
      if (onReanalyzeStart) onReanalyzeStart(); // triggers parent refetch
    } catch (e) {
      setFreezeErr(e?.response?.data?.detail || e?.message || `Failed to ${isFrozen ? "unfreeze" : "freeze"}.`);
    } finally {
      setFreezeBusy(false);
    }
  };

  useEffect(() => {
    listModels().then((d) => {
      const list = (d && d.models) || [];
      setModels(list);
      // default to the model currently in use, else leave empty (auto)
      if (currentModel && list.some(m => m.id === currentModel)) setReModel(currentModel);
    }).catch(() => {});
  }, [currentModel]);

  const handleReanalyze = async () => {
    if (!analysisId) return;
    setReBusy(true);
    setReErr("");
    try {
      await runGapAnalysis(analysisId, reModel || null);
      setReOpen(false);
      if (onReanalyzeStart) onReanalyzeStart();
    } catch (e) {
      setReErr(e?.response?.data?.detail || e?.message || "Failed to start re-analysis");
    } finally {
      setReBusy(false);
    }
  };

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="max-w-md w-full bg-surface border border-red-200 rounded-md p-4">
          <div className="flex items-start gap-3">
            <AlertCircle size={18} className="text-red-600 flex-shrink-0 mt-0.5" />
            <div>
              <h3 className="text-sm font-semibold text-red-800 mb-1">Analysis Failed</h3>
              <p className="text-xs text-red-600">{error}</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!result) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-center p-6 bg-surface-2">
        <div className="w-14 h-14 rounded-full bg-surface shadow-sm flex items-center justify-center mb-3">
          {phase && phase !== "report" ? (
            <RefreshCw size={22} className="text-fg-subtle animate-spin" />
          ) : (
            <FileText size={22} className="text-fg-subtle" />
          )}
        </div>
        <p className="text-sm font-medium text-fg-muted">
          {phaseLabel ||
           (phase === "verify" ? "LLM verifier is comparing KB against requirements…" :
            phase === "kb" || phase === "extract" ? "Building knowledge base first…" :
            phase ? "Waiting for verification…" :
            "Report will appear here once analysis completes")}
        </p>
        {phase && phase !== "report" && (
          <div className="mt-4 w-72">
            <div className="h-2 bg-surface-3 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 transition-all duration-500"
                style={{ width: `${Math.max(3, Math.min(100, progressPct || 3))}%` }}
              />
            </div>
            <div className="text-micro text-fg-subtle mt-1 tabular-nums">
              {progressPct || 0}% complete
            </div>
          </div>
        )}
        {phase === "verify" && (
          <p className="text-micro text-fg-subtle mt-2">This typically takes 30–90 seconds</p>
        )}
      </div>
    );
  }

  const gaps = result.gaps || [];
  const filtered = gaps.filter(g => severity === "all" || g.severity === severity)
    .sort((a, b) => {
      const order = { critical: 0, major: 1, minor: 2 };
      return (order[a.severity] ?? 3) - (order[b.severity] ?? 3);
    });
  const matrix = result.coverage_matrix || [];
  const testCases = result.test_cases || [];
  const testStats = (() => {
    const base = result.test_stats || {
      total: testCases.length,
      passed: testCases.filter(t => t.status === "PASS").length,
      failed: testCases.filter(t => t.status === "FAIL").length,
      blocked: testCases.filter(t => t.status === "BLOCKED").length,
      not_applicable: testCases.filter(t => t.status === "NOT_APPLICABLE").length,
      accuracy_pct: result.accuracy_pct || 0,
    };
    if (base.positive == null) base.positive = testCases.filter(t => t.case_type === "positive").length;
    if (base.negative == null) base.negative = testCases.filter(t => t.case_type === "negative").length;
    return base;
  })();

  const toggle = (id) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-surface-2">
      {/* iter-14.94 — Warnings banner (KB signal / coverage confidence).
          Rendered ABOVE the stat cards so a "100% coverage" number cannot
          be misread when the underlying data is insufficient. */}
      {Array.isArray(result.warnings) && result.warnings.length > 0 && (
        <div className="px-3 pt-3">
          <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3">
            <AlertTriangle size={16} className="text-amber-600 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <div className="text-micro font-bold uppercase tracking-wide text-amber-800 mb-1">
                {result.kb_diagnostics?.is_ui_only
                  ? "Analysis inconclusive — frontend-only upload"
                  : "Analysis has caveats"}
              </div>
              <ul className="text-xs text-amber-900 space-y-1 list-disc list-inside">
                {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
              {result.kb_diagnostics && (
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-micro text-amber-800/80">
                  <span>Backend entities: <b className="tabular-nums">{result.kb_diagnostics.backend_entities || 0}</b></span>
                  <span>API routes: <b className="tabular-nums">{result.kb_diagnostics.api_routes || 0}</b></span>
                  <span>DB tables: <b className="tabular-nums">{result.kb_diagnostics.db_tables || 0}</b></span>
                  <span>Resolved chains: <b className="tabular-nums">{result.kb_diagnostics.resolved_chains || 0}</b></span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* iter-15.0 — Visual analytics: collapsed by default so KPI row + toolbar + list stay above the fold */}
      <div className="bg-surface border-b border-border">
        <button
          onClick={() => setChartsOpen(v => !v)}
          className="w-full flex items-center justify-between px-3 py-1.5 text-micro font-medium text-fg-muted hover:bg-surface-2"
        >
          <span className="flex items-center gap-2">
            <ChevronDown size={12} className={`transition-transform ${chartsOpen ? "" : "-rotate-90"}`} />
            <span className="uppercase tracking-wide">Visual Analytics</span>
            <span className="text-fg-subtle normal-case tracking-normal">
              coverage · severity · categories
            </span>
          </span>
          <span className="text-micro text-fg-subtle">{chartsOpen ? "Hide" : "Show"}</span>
        </button>
        {chartsOpen && <ReportChartsPanel result={result} matrix={matrix} gaps={gaps} />}
      </div>

      {/* Stats + export — compact */}
      <div className="grid grid-cols-6 gap-2 px-3 py-2 bg-surface border-b border-border">
        <StatCard label="Coverage" value={`${result.coverage_pct || 0}%`} icon={Target} tone={result.coverage_pct >= 70 ? "emerald" : result.coverage_pct >= 40 ? "amber" : "red"} />
        <StatCard label="Accuracy" value={`${testStats.accuracy_pct || 0}%`} icon={CheckCircle} tone={testStats.accuracy_pct >= 70 ? "emerald" : testStats.accuracy_pct >= 40 ? "amber" : "red"} />
        <StatCard label="Test Cases" value={testStats.total || 0} icon={FileText} tone="sky" />
        <StatCard label="Critical" value={result.critical_gaps || 0} icon={AlertCircle} tone="red" />
        <StatCard label="Major" value={result.major_gaps || 0} icon={AlertTriangle} tone="amber" />
        <StatCard label="Total Gaps" value={result.total_gaps || gaps.length} icon={Zap} tone="slate" />
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface">
        <div className="flex bg-surface-2 rounded-md p-0.5">
          <button
            onClick={() => setView("gaps")}
            className={`px-3 py-1 text-xs font-medium rounded ${view === "gaps" ? "bg-surface shadow-sm text-fg" : "text-fg-subtle"}`}
          >Gaps ({gaps.length})</button>
          <button
            onClick={() => setView("tests")}
            className={`px-3 py-1 text-xs font-medium rounded ${view === "tests" ? "bg-surface shadow-sm text-fg" : "text-fg-subtle"}`}
          >Test Cases ({testStats.total})</button>
          <button
            onClick={() => setView("matrix")}
            className={`px-3 py-1 text-xs font-medium rounded ${view === "matrix" ? "bg-surface shadow-sm text-fg" : "text-fg-subtle"}`}
          >Coverage Matrix ({matrix.length})</button>
        </div>

        {view === "gaps" && (
          <div className="flex items-center gap-1 ml-3">
            <Filter size={12} className="text-fg-subtle" />
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              className="text-xs px-2 py-0.5 border border-border rounded bg-surface"
            >
              <option value="all">All severity</option>
              <option value="critical">Critical</option>
              <option value="major">Major</option>
              <option value="minor">Minor</option>
            </select>
          </div>
        )}
        {view === "matrix" && (
          <div className="flex items-center gap-1 ml-3">
            <Filter size={12} className="text-fg-subtle" />
            <select
              value={matrixFilter}
              onChange={(e) => setMatrixFilter(e.target.value)}
              className="text-xs px-2 py-0.5 border border-border rounded bg-surface"
            >
              <option value="all">All requirements ({matrix.length})</option>
              <option value="covered">Covered only ({matrix.filter(r => r.implemented).length})</option>
              <option value="missing">Missing only ({matrix.filter(r => !r.implemented).length})</option>
            </select>
          </div>
        )}
        {view === "tests" && (
          <div className="flex items-center gap-1 ml-3">
            <Filter size={12} className="text-fg-subtle" />
            <select
              value={testFilter}
              onChange={(e) => setTestFilter(e.target.value)}
              className="text-xs px-2 py-0.5 border border-border rounded bg-surface"
            >
              <option value="all">All ({testStats.total})</option>
              <option value="POSITIVE">+ Positive ({testStats.positive != null ? testStats.positive : testCases.filter(t => t.case_type === "positive").length})</option>
              <option value="NEGATIVE">− Negative ({testStats.negative != null ? testStats.negative : testCases.filter(t => t.case_type === "negative").length})</option>
              <option value="PASS">Passed ({testStats.passed})</option>
              <option value="FAIL">Failed ({testStats.failed})</option>
              <option value="BLOCKED">Blocked ({testStats.blocked})</option>
              <option value="NOT_APPLICABLE">N/A ({testStats.not_applicable})</option>
            </select>
          </div>
        )}

        <div className="ml-auto flex items-center gap-2">
          {/* iter-15.4 — Frozen state badge */}
          {isFrozen && (
            <span
              className="px-2 py-1 text-micro font-semibold rounded flex items-center gap-1 bg-sky-50 text-sky-700 border border-sky-200"
              title={`Frozen at ${result.frozen_at || "n/a"} — Re-analyze is locked`}
            >
              <Lock size={11} /> Frozen
            </span>
          )}

          {/* iter-15.1 — Re-analyze with a different model (blocked when frozen) */}
          {analysisId && (
            <div className="relative">
              <button
                onClick={() => !isFrozen && setReOpen(v => !v)}
                disabled={reBusy || isFrozen}
                className="px-2 py-1 text-micro font-medium bg-indigo-50 text-indigo-700 rounded hover:bg-indigo-100 flex items-center gap-1 border border-indigo-100 disabled:opacity-50 disabled:cursor-not-allowed"
                title={isFrozen ? "Analysis is frozen — unfreeze to re-analyze" : "Re-run analysis on the same artefacts with the selected model"}
              >
                <RefreshCw size={11} className={reBusy ? "animate-spin" : ""} />
                {reBusy ? "Starting…" : "Re-analyze"}
                <ChevronDown size={10} className={`transition-transform ${reOpen ? "rotate-180" : ""}`} />
              </button>
              {reOpen && (
                <div className="absolute right-0 top-full mt-1 z-30 bg-surface border border-border rounded-md shadow-lg w-72 p-3 space-y-2">
                  <div className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">Re-analyze with model</div>
                  <select
                    value={reModel}
                    onChange={(e) => setReModel(e.target.value)}
                    className="w-full text-xs px-2 py-1 border border-border rounded bg-surface"
                  >
                    <option value="">Auto (Console routing)</option>
                    {models.map(m => (
                      <option key={m.id} value={m.id}>
                        {m.label || m.name || m.id}
                        {currentModel === m.id ? "  (current)" : ""}
                      </option>
                    ))}
                  </select>
                  <p className="text-micro text-fg-subtle leading-snug">
                    Reuses uploaded code &amp; docs. KB is rebuilt and the LLM re-authors gaps, coverage matrix, and test cases.
                  </p>
                  {reErr && <div className="text-micro text-red-600">{reErr}</div>}
                  <div className="flex justify-end gap-1.5">
                    <button
                      onClick={() => setReOpen(false)}
                      className="px-2 py-1 text-micro rounded text-fg-muted hover:bg-surface-2"
                    >Cancel</button>
                    <button
                      onClick={handleReanalyze}
                      disabled={reBusy}
                      className="px-2 py-1 text-micro rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-60 flex items-center gap-1"
                    >
                      <Play size={10} /> {reBusy ? "Starting…" : "Start"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          <span className="text-micro text-fg-subtle uppercase tracking-wide font-semibold mr-1">Export:</span>
          {analysisId && [
            { fmt: "csv", label: "CSV", icon: FileSpreadsheet, color: "emerald" },
            { fmt: "json", label: "JSON", icon: FileJson, color: "sky" },
            { fmt: "html", label: "PDF", icon: FileText, color: "violet" },
          ].map(x => (
            <a
              key={x.fmt}
              href={gapAnalysisExportUrl(analysisId, x.fmt)}
              target="_blank"
              rel="noreferrer"
              className={`px-2 py-1 text-micro font-medium bg-${x.color}-50 text-${x.color}-700 rounded hover:bg-${x.color}-100 flex items-center gap-1 border border-${x.color}-100`}
            >
              <x.icon size={11} /> {x.label}
            </a>
          ))}

          {/* iter-15.4 — Freeze / Unfreeze with typed confirmation */}
          {analysisId && result && Object.keys(result).length > 0 && (
            <div className="relative">
              <button
                onClick={() => { setFreezeOpen(v => !v); setFreezeConfirm(""); setFreezeErr(""); }}
                className={`px-2 py-1 text-micro font-semibold rounded flex items-center gap-1 border ${
                  isFrozen
                    ? "bg-amber-50 text-amber-700 hover:bg-amber-100 border-amber-200"
                    : "bg-ink text-white hover:bg-ink border-border-strong"
                }`}
                title={isFrozen ? "Unfreeze the report to allow re-analysis" : "Freeze the report to prevent further changes"}
              >
                {isFrozen ? <Unlock size={11} /> : <Lock size={11} />}
                {isFrozen ? "Unfreeze" : "Freeze"}
                <ChevronDown size={10} className={`transition-transform ${freezeOpen ? "rotate-180" : ""}`} />
              </button>
              {freezeOpen && (
                <div className="absolute right-0 top-full mt-1 z-30 bg-surface border border-border rounded-md shadow-lg w-80 p-3 space-y-2">
                  <div className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
                    {isFrozen ? "Unfreeze Report" : "Freeze Report"}
                  </div>
                  <p className="text-micro text-fg-muted leading-snug">
                    {isFrozen
                      ? "Unfreezing allows Re-analyze again with different models or configurations."
                      : "Once frozen, this report becomes the final version. Re-analyze is locked until you unfreeze."}
                  </p>
                  <div>
                    <label className="text-micro uppercase font-semibold text-fg-subtle">
                      Type <span className="font-mono text-fg">{isFrozen ? "UNFREEZE" : "FREEZE"}</span> to confirm
                    </label>
                    <input
                      type="text"
                      value={freezeConfirm}
                      onChange={(e) => setFreezeConfirm(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleFreezeToggle(); }}
                      autoFocus
                      className="w-full mt-1 text-xs px-2 py-1 border border-border-strong rounded font-mono focus:border-border-strong focus:outline-none"
                      placeholder={isFrozen ? "UNFREEZE" : "FREEZE"}
                    />
                  </div>
                  {freezeErr && <div className="text-micro text-red-600">{freezeErr}</div>}
                  <div className="flex justify-end gap-1.5">
                    <button
                      onClick={() => { setFreezeOpen(false); setFreezeConfirm(""); setFreezeErr(""); }}
                      className="px-2 py-1 text-micro rounded text-fg-muted hover:bg-surface-2"
                    >Cancel</button>
                    <button
                      onClick={handleFreezeToggle}
                      disabled={freezeBusy || freezeConfirm.trim().toUpperCase() !== (isFrozen ? "UNFREEZE" : "FREEZE")}
                      className={`px-2 py-1 text-micro rounded font-semibold flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed ${
                        isFrozen
                          ? "bg-amber-600 text-white hover:bg-amber-700"
                          : "bg-ink text-white hover:bg-ink"
                      }`}
                    >
                      {isFrozen ? <Unlock size={10} /> : <Lock size={10} />}
                      {freezeBusy ? "Working…" : (isFrozen ? "Unfreeze" : "Freeze")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Summary strip */}
      {result.summary && (
        <div className="px-3 py-2 bg-blue-50/40 border-b border-blue-100 text-xs text-fg-muted">
          <Info size={11} className="inline mr-1.5 text-blue-500" />
          {typeof result.summary === "string" ? result.summary : JSON.stringify(result.summary)}
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-3">
        {view === "gaps" && (
          filtered.length === 0 ? (
            <div className="text-center py-12 text-sm text-fg-subtle">
              {gaps.length === 0 ? "No gaps identified — full requirement coverage!" : "No gaps match the current filter"}
            </div>
          ) : (
            <div className="space-y-1.5">
              {filtered.map((g, i) => {
                const isExp = expanded.has(g.id || i);
                return (
                  <div key={g.id || i} className={`border rounded-md bg-surface overflow-hidden ${sevColor(g.severity)}`}>
                    <button
                      onClick={() => toggle(g.id || i)}
                      className="w-full px-3 py-2 flex items-start gap-2 text-left hover:bg-surface-2/50"
                    >
                      <span className={`w-2 h-2 rounded-full flex-shrink-0 mt-1.5 ${sevDot(g.severity)}`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          {g.id && <span className="text-micro font-mono text-fg-subtle">{g.id}</span>}
                          <span className="text-xs font-semibold text-fg">{g.title}</span>
                          <span className={`text-micro px-1.5 py-0.5 rounded font-semibold uppercase ${sevColor(g.severity)}`}>{g.severity}</span>
                          {g.type && <span className="text-micro px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted font-semibold">{g.type}</span>}
                          {g.doc_reference && <span className="text-micro text-emerald-700 font-mono">↳ {g.doc_reference}</span>}
                        </div>
                        {!isExp && g.description && (
                          <p className="text-micro text-fg-muted mt-0.5 line-clamp-1">{g.description}</p>
                        )}
                      </div>
                      <ChevronDown size={13} className={`text-fg-subtle flex-shrink-0 transition-transform ${isExp ? "rotate-180" : ""}`} />
                    </button>
                    {isExp && (
                      <div className="px-3 pb-2.5 ml-4 space-y-1.5 text-micro border-t border-border pt-2">
                        {g.description && (
                          <div><span className="font-semibold text-fg-subtle uppercase text-micro">Description:</span> <span className="text-fg-muted">{g.description}</span></div>
                        )}
                        {g.expected && (
                          <div><span className="font-semibold text-fg-subtle uppercase text-micro">Expected:</span> <span className="text-fg-muted">{g.expected}</span></div>
                        )}
                        {g.actual && (
                          <div><span className="font-semibold text-fg-subtle uppercase text-micro">Actual:</span> <span className="text-fg-muted">{g.actual}</span></div>
                        )}
                        {g.location && (
                          <div><span className="font-semibold text-fg-subtle uppercase text-micro">Location:</span> <span className="font-mono text-fg-muted">{g.location}</span></div>
                        )}
                        {g.recommendation && (
                          <div className="bg-emerald-50 border border-emerald-100 rounded p-2 mt-1">
                            <span className="font-semibold text-emerald-700 uppercase text-micro">💡 Recommendation:</span>{" "}
                            <span className="text-emerald-800">{g.recommendation}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )
        )}
        {view === "tests" && (
          <TestCasesView
            testCases={testCases}
            stats={testStats}
            filter={testFilter}
            expanded={testExpanded}
            setExpanded={setTestExpanded}
          />
        )}
        {view === "matrix" && (
          matrix.length === 0 ? (
            <div className="text-center py-12 text-sm text-fg-subtle">
              No coverage matrix returned by the verifier.
            </div>
          ) : (
            (() => {
              const filteredMatrix = matrix.filter(r => {
                if (matrixFilter === "covered") return r.implemented;
                if (matrixFilter === "missing") return !r.implemented;
                return true;
              });
              const coveredCount = matrix.filter(r => r.implemented).length;
              const missingCount = matrix.length - coveredCount;
              return (
                <div className="bg-surface border border-border rounded-md overflow-hidden">
                  {/* Summary strip — always visible, shows totals even if 0% covered */}
                  <div className="px-3 py-2 border-b border-border bg-surface-2/70 flex items-center gap-3 text-micro">
                    <span className="font-semibold text-fg-muted">
                      {filteredMatrix.length} of {matrix.length} requirement{matrix.length !== 1 ? "s" : ""}
                    </span>
                    <span className="text-fg-subtle">•</span>
                    <span className="flex items-center gap-1 text-emerald-700">
                      <CheckCircle size={11} /> {coveredCount} covered
                    </span>
                    <span className="flex items-center gap-1 text-red-700">
                      <AlertCircle size={11} /> {missingCount} missing
                    </span>
                    <span className="text-fg-subtle">•</span>
                    <div className="flex-1 h-1.5 bg-surface-3 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-emerald-500"
                        style={{ width: `${matrix.length ? (coveredCount / matrix.length) * 100 : 0}%` }}
                      />
                    </div>
                    <span className="tabular-nums font-semibold text-fg-muted">
                      {matrix.length ? Math.round((coveredCount / matrix.length) * 100) : 0}%
                    </span>
                  </div>

                  {filteredMatrix.length === 0 ? (
                    <div className="text-center py-8 text-xs text-fg-subtle">
                      No requirements match this filter.
                    </div>
                  ) : (
                    <div className="overflow-auto max-h-[560px]">
                      <table className="w-full text-xs">
                        <thead className="bg-surface-2 text-fg-subtle uppercase text-micro tracking-wide sticky top-0">
                          <tr>
                            <th className="text-left px-3 py-1.5 font-semibold w-24">Req. ID</th>
                            <th className="text-left px-3 py-1.5 font-semibold w-20">Doc Type</th>
                            <th className="text-left px-3 py-1.5 font-semibold">Description</th>
                            <th className="text-left px-3 py-1.5 font-semibold">Code Locations</th>
                            <th className="px-3 py-1.5 font-semibold w-24 text-center">Status</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {filteredMatrix.map((row, i) => (
                            <tr key={i} className={`hover:bg-surface-2 ${row.implemented ? "" : "bg-red-50/30"}`}>
                              <td className="px-3 py-1.5 font-mono text-micro text-fg-muted font-semibold">
                                {row.requirement_id}
                              </td>
                              <td className="px-3 py-1.5">
                                {row.doc_type ? (
                                  <span className="text-micro px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted font-semibold uppercase">
                                    {row.doc_type}
                                  </span>
                                ) : <span className="text-fg-subtle">—</span>}
                              </td>
                              <td className="px-3 py-1.5 text-fg-muted">{row.requirement_text}</td>
                              <td className="px-3 py-1.5 font-mono text-micro text-fg-subtle">
                                {row.code_locations?.length ? row.code_locations.join(", ") : "—"}
                              </td>
                              <td className="px-3 py-1.5 text-center">
                                {row.implemented ? (
                                  <span className="text-micro px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-semibold inline-flex items-center gap-1">
                                    <CheckCircle size={10} /> Covered
                                  </span>
                                ) : (
                                  <span className="text-micro px-1.5 py-0.5 rounded bg-red-50 text-red-700 font-semibold inline-flex items-center gap-1">
                                    <AlertCircle size={10} /> Missing
                                  </span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              );
            })()
          )
        )}
      </div>
    </div>
  );
};

/* ═════════ History dropdown (iter-15.2) ═════════ */

const HistoryMenu = ({ currentId, onOpen }) => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [confirmId, setConfirmId] = useState(null);
  const [menuOpen, setMenuOpen] = useState(null); // id whose kebab is open
  const containerRef = useRef(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listGapAnalyses();
      setItems(rows || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false); setMenuOpen(null); setConfirmId(null);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const handleRemove = async (id) => {
    try {
      await deleteGapAnalysis(id);
      setItems(prev => prev.filter(x => (x.id || x._id) !== id));
      setConfirmId(null);
      setMenuOpen(null);
    } catch (e) {
       
      alert(e?.response?.data?.detail || "Failed to remove analysis");
    }
  };

  const fmtWhen = (iso) => {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      return sameDay
        ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
          d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch { return iso; }
  };

  const statusBadge = (s) => {
    if (s === "completed") return <span className="text-micro px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-semibold">DONE</span>;
    if (s === "failed") return <span className="text-micro px-1.5 py-0.5 rounded bg-red-50 text-red-700 font-semibold">FAILED</span>;
    if (s === "analyzing") return <span className="text-micro px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 font-semibold">RUNNING</span>;
    return <span className="text-micro px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted font-semibold">{(s || "?").toUpperCase()}</span>;
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        className="text-micro px-2 py-1 border border-border rounded hover:bg-surface-2 text-fg-muted font-medium flex items-center gap-1"
        title="Show past analyses"
      >
        <HistoryIcon size={12} /> History
        <ChevronDown size={10} className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-40 w-96 max-h-[420px] flex flex-col bg-surface border border-border rounded-md shadow-lg">
          <div className="px-3 py-2 border-b border-border flex items-center justify-between">
            <span className="text-micro uppercase tracking-wide text-fg-subtle font-semibold">
              Saved analyses {items.length > 0 && `· ${items.length}`}
            </span>
            <button onClick={refresh} className="text-micro text-fg-subtle hover:text-fg-muted flex items-center gap-1">
              <RefreshCw size={10} className={loading ? "animate-spin" : ""} /> Refresh
            </button>
          </div>
          <div className="flex-1 overflow-y-auto">
            {loading && items.length === 0 && (
              <div className="p-6 text-center text-xs text-fg-subtle">Loading…</div>
            )}
            {!loading && items.length === 0 && (
              <div className="p-6 text-center text-xs text-fg-subtle">
                No saved analyses yet. Each run is auto-saved and appears here.
              </div>
            )}
            {items.map((it) => {
              const id = it.id || it._id;
              const isCurrent = id === currentId;
              return (
                <div
                  key={id}
                  className={`group px-3 py-2 border-b border-border hover:bg-surface-2 ${isCurrent ? "bg-amber-50/40" : ""}`}
                >
                  <div className="flex items-start gap-2">
                    <button
                      onClick={() => { setOpen(false); onOpen && onOpen(id); }}
                      className="flex-1 text-left min-w-0"
                    >
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-xs font-semibold text-fg truncate">{it.name || "Untitled analysis"}</span>
                        {statusBadge(it.status)}
                        {isCurrent && <span className="text-micro px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 font-semibold">CURRENT</span>}
                        {it.reanalyze_count > 0 && (
                          <span className="text-micro px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 font-semibold">
                            v{(it.reanalyze_count || 0) + 1}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex items-center gap-2 text-micro text-fg-subtle tabular-nums">
                        <span className="flex items-center gap-1"><Clock size={9} /> {fmtWhen(it.updated_at || it.created_at)}</span>
                        {it.code_file_count != null && <span>· {it.code_file_count} code</span>}
                        {it.doc_file_count != null && <span>· {it.doc_file_count} doc{it.doc_file_count === 1 ? "" : "s"}</span>}
                        {it.model && <span className="truncate">· {it.model}</span>}
                      </div>
                    </button>
                    <div className="relative">
                      <button
                        onClick={(e) => { e.stopPropagation(); setMenuOpen(menuOpen === id ? null : id); setConfirmId(null); }}
                        className="p-1 rounded hover:bg-surface-3 text-fg-subtle"
                        title="More"
                      >
                        <MoreVertical size={13} />
                      </button>
                      {menuOpen === id && (
                        <div className="absolute right-0 top-full mt-1 z-50 w-40 bg-surface border border-border rounded-md shadow-lg py-1">
                          <button
                            onClick={() => { setOpen(false); setMenuOpen(null); onOpen && onOpen(id); }}
                            className="w-full text-left px-3 py-1.5 text-xs text-fg-muted hover:bg-surface-2 flex items-center gap-2"
                          >
                            <ExternalLink size={11} /> Open
                          </button>
                          <a
                            href={gapAnalysisExportUrl(id, "csv")}
                            target="_blank" rel="noreferrer"
                            onClick={() => setMenuOpen(null)}
                            className="w-full text-left px-3 py-1.5 text-xs text-fg-muted hover:bg-surface-2 flex items-center gap-2"
                          >
                            <Download size={11} /> Download CSV
                          </a>
                          <div className="my-1 border-t border-border" />
                          {confirmId === id ? (
                            <div className="px-3 py-2 space-y-1">
                              <div className="text-micro text-red-700 font-semibold">Remove permanently?</div>
                              <div className="flex gap-1">
                                <button
                                  onClick={() => handleRemove(id)}
                                  className="flex-1 px-2 py-1 text-micro bg-red-600 text-white rounded hover:bg-red-700"
                                >Yes, remove</button>
                                <button
                                  onClick={() => setConfirmId(null)}
                                  className="px-2 py-1 text-micro text-fg-muted hover:bg-surface-2 rounded"
                                >Cancel</button>
                              </div>
                            </div>
                          ) : (
                            <button
                              onClick={() => setConfirmId(id)}
                              className="w-full text-left px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 flex items-center gap-2"
                            >
                              <Trash2 size={11} /> Remove
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
};


/* ═════════ Main Component ═════════ */

export default function GapAnalyzerPage() {
  const navigate = useNavigate();
  const location = useLocation();

  const [name, setName] = useState("");
  // iter-14.95 — split source-code inputs from doc inputs; add GitHub source
  const [sourceMode, setSourceMode] = useState("upload"); // "upload" | "github"
  const [codeFiles, setCodeFiles] = useState([]);          // [{ file }]
  const [githubUrl, setGithubUrl] = useState("");
  const [githubBranch, setGithubBranch] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [docFiles, setDocFiles] = useState([]);            // [{ file, docType }]
  const [creating, setCreating] = useState(false);
  const [analysisId, setAnalysisId] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [phase, setPhase] = useState(null);
  const [phaseLabel, setPhaseLabel] = useState(null);       // iter-15.6 — human phase label
  const [progressPct, setProgressPct] = useState(0);         // iter-15.6 — 0-100 for progress bar
  const [, setCompletedPhases] = useState([]);
  const [kb, setKb] = useState(null);
  const [result, setResult] = useState(null);
  const [analysisModel, setAnalysisModel] = useState(null); // iter-15.1 — track model of current analysis

  const [activeTab, setActiveTab] = useState(() => {
    if (typeof window !== "undefined") {
      const h = (window.location.hash || "").replace("#", "");
      if (h === "kb" || h === "report" || h === "input") return h;
    }
    return "input";
  });

  // iter-14.94 — When URL hash changes (e.g. sidebar/header click), reflect
  // it in local tab state. When local tab state changes (user clicked the
  // in-page tab), navigate() so React Router state updates and the sidebar
  // + top slider re-render with the new "current stage".
  useEffect(() => {
    const h = (location.hash || "").replace("#", "");
    if (h && h !== activeTab && ["kb", "report", "input"].includes(h)) {
      setActiveTab(h);
    }
  }, [location.hash]); // eslint-disable-line react-hooks/exhaustive-deps

  const changeTab = useCallback((tab) => {
    setActiveTab(tab);
    navigate(`${location.pathname}#${tab}`, { replace: true });
  }, [navigate, location.pathname]);

  const pollRef = useRef(null);

  const hasCode = sourceMode === "github" ? !!githubUrl.trim() : codeFiles.length > 0;
  const hasDocs = docFiles.length > 0;
  const canSubmit = name.trim() && hasCode && hasDocs;
  const validationMsg =
    !name.trim() ? "⚠ Enter an analysis name" :
    !hasCode ? (sourceMode === "github" ? "⚠ Enter a GitHub repository URL" : "⚠ Add at least one source-code file") :
    !hasDocs ? "⚠ Add at least one documentation file" : "";

  const kbAvailable = ["extract", "verify", "report"].includes(phase);

  // Polling: fetch status + KB (when ready) + full result (on completion)
  useEffect(() => {
    if (!analysisId || status === "completed" || status === "failed") return;
    const tick = async () => {
      try {
        const s = await pollGapAnalysisStatus(analysisId);
        const mapped = BACKEND_PHASE_MAP[s.phase] || s.phase;
        setPhase(mapped);
        // iter-15.6 — human phase label + numeric progress for the UI bar
        if (s.phase_label) setPhaseLabel(s.phase_label);
        if (typeof s.progress_pct === "number") setProgressPct(s.progress_pct);
        const order = ["upload", "kb", "extract", "verify", "report"];
        const idx = order.indexOf(mapped);
        if (idx > 0) setCompletedPhases(order.slice(0, idx));

        // Once we're past "kb" phase, fetch the KB snapshot for the KB tab
        if (["extract", "verify", "report"].includes(mapped) && !kb) {
          try {
            const kbData = await getGapAnalysisKB(analysisId);
            setKb(kbData.summary || kbData);
          } catch {
          // One failed poll tick is not an error — the next tick retries,
          // and a toast every 2s during a network blip would be worse.
          }
        }

        if (s.status === "completed") {
          const full = await getGapAnalysis(analysisId);
          setResult(full?.result || {});
          setAnalysisModel(full?.model || null);
          setStatus("completed");
          setPhase("report");
          setPhaseLabel("Analysis complete");
          setProgressPct(100);
          setCompletedPhases(order);
          setActiveTab("report");
          navigate(`${location.pathname}#report`, { replace: true });
          // Re-fetch KB if we didn't get it during polling
          if (!kb) {
            try {
              const kbData = await getGapAnalysisKB(analysisId);
              setKb(kbData.summary || kbData);
            } catch {
            // One failed poll tick is not an error — the next tick retries,
            // and a toast every 2s during a network blip would be worse.
            }
          }
        } else if (s.status === "failed") {
          setStatus("failed");
          setError(s.error || "Analysis failed");
        } else {
          pollRef.current = setTimeout(tick, 2000);
        }
      } catch {
        pollRef.current = setTimeout(tick, 3000);
      }
    };
    pollRef.current = setTimeout(tick, 1500);
    return () => clearTimeout(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisId, status]);

  const handleCreate = async () => {
    if (!canSubmit) return;
    setCreating(true);
    setStatus("analyzing");
    setError(null);
    setKb(null);
    setResult(null);
    setPhase("upload");
    setPhaseLabel("Uploading artefacts…");
    setProgressPct(3);
    setCompletedPhases([]);

    try {
      const fd = new FormData();
      fd.append("name", name);
      if (sourceMode === "github") {
        fd.append("github_url", githubUrl.trim());
        if (githubBranch.trim()) fd.append("github_branch", githubBranch.trim());
        if (githubToken.trim()) fd.append("github_token", githubToken.trim());
      } else {
        codeFiles.forEach(f => fd.append("code_zip", f.file));
      }
      // Docs — parallel arrays: files + JSON-array of types
      const types = docFiles.map(d => d.docType || "srs");
      docFiles.forEach(d => fd.append("docs_zip", d.file));
      fd.append("docs_types", JSON.stringify(types));

      const created = await createGapAnalysis(fd);
      if (created?._id) {
        setAnalysisId(created._id);
        setCompletedPhases(["upload"]);
        setPhase("kb");
        setActiveTab("kb");
        navigate(`${location.pathname}#kb`, { replace: true });
      }
    } catch (e) {
      setError(e?.message || "Upload failed");
      setStatus("failed");
    } finally {
      setCreating(false);
    }
  };

  const reset = () => {
    clearTimeout(pollRef.current);
    setName("");
    setSourceMode("upload");
    setCodeFiles([]);
    setGithubUrl("");
    setGithubBranch("");
    setGithubToken("");
    setDocFiles([]);
    setAnalysisId(null);
    setStatus(null);
    setError(null);
    setPhase(null);
    setPhaseLabel(null);
    setProgressPct(0);
    setCompletedPhases([]);
    setKb(null);
    setResult(null);
    setAnalysisModel(null);
    setActiveTab("input");
    navigate(`${location.pathname}#input`, { replace: true });
  };

  // iter-15.1 — After the report is complete, allow the user to re-run
  // the analysis with a different model. Reset just enough state so the
  // polling effect wakes back up and drives the pipeline UI forward again.
  const handleReanalyzeStart = useCallback(() => {
    setResult(null);
    setKb(null);
    setError(null);
    setStatus("analyzing");
    setPhase("kb");
    setPhaseLabel("Queued for re-analysis");
    setProgressPct(5);
    setCompletedPhases([]);
    setActiveTab("kb");
    navigate(`${location.pathname}#kb`, { replace: true });
  }, [navigate, location.pathname]);

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-surface">
      {/* Header */}
      <div className="h-11 px-4 flex items-center justify-between border-b border-border bg-surface">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded bg-brand flex items-center justify-center">
            <Search size={14} className="text-fg" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-semibold text-fg">Gap Analyzer</h1>
              <span className="text-micro text-fg-subtle">·</span>
              <span className="text-micro text-fg-subtle">Two-phase KB-driven verification</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {status === "completed" && (
            <span className="text-micro px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 font-medium flex items-center gap-1">
              <CheckCircle size={10} /> Completed
            </span>
          )}
          {status === "failed" && (
            <span className="text-micro px-2 py-0.5 rounded-full bg-red-50 text-red-700 font-medium flex items-center gap-1">
              <AlertCircle size={10} /> Failed
            </span>
          )}
          {status === "analyzing" && (
            <div className="flex items-center gap-2">
              <span className="text-micro px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 font-medium flex items-center gap-1">
                <RefreshCw size={10} className="animate-spin" />
                Running {progressPct ? `${progressPct}%` : ""}
              </span>
              {/* iter-15.6 — header progress bar so users see live progression */}
              <div className="w-40 h-1.5 bg-surface-3 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-500 transition-all duration-500"
                  style={{ width: `${Math.max(3, Math.min(100, progressPct || 3))}%` }}
                />
              </div>
              {phaseLabel && (
                <span className="text-micro text-fg-subtle max-w-[280px] truncate" title={phaseLabel}>
                  {phaseLabel}
                </span>
              )}
            </div>
          )}
          {/* iter-15.2 — History dropdown (auto-saved analyses + kebab remove) */}
          <HistoryMenu
            currentId={analysisId}
            onOpen={async (id) => {
              try {
                const full = await getGapAnalysis(id);
                setAnalysisId(id);
                setName(full.name || "");
                setResult(full.result || {});
                setAnalysisModel(full.model || null);
                setStatus(full.status === "failed" ? "failed" : "completed");
                setPhase("report");
                setCompletedPhases(["upload","kb","extract","verify","report"]);
                setActiveTab("report");
                navigate(`${location.pathname}#report`, { replace: true });
                try {
                  const kbData = await getGapAnalysisKB(id);
                  setKb(kbData.summary || kbData);
                } catch {
                // One failed poll tick is not an error — the next tick retries,
                // and a toast every 2s during a network blip would be worse.
                }
              } catch (e) {
                setError(e?.response?.data?.detail || "Failed to open analysis");
              }
            }}
          />
          {(status === "completed" || status === "failed") && (
            <button
              onClick={reset}
              className="text-micro px-2 py-1 border border-border rounded hover:bg-surface-2 text-fg-muted font-medium"
            >
              New Analysis
            </button>
          )}
        </div>
      </div>

      {/* iter-14.94 — PhaseStepper removed; sidebar + header slider carry
          stage progress. This page keeps only the tab bar. */}

      {/* Tabs */}
      <div className="flex items-center gap-0 px-3 border-b border-border bg-surface-2/50">
        {[
          { id: "input", label: "Input", icon: Upload, enabled: true },
          { id: "kb", label: "Knowledge Base", icon: Database, enabled: kbAvailable || !!kb, badge: kb?.stats?.entities },
          { id: "report", label: "Gap Report", icon: FileText, enabled: !!result || status === "failed", badge: result?.total_gaps },
        ].map(t => {
          const Icon = t.icon;
          const active = activeTab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => t.enabled && changeTab(t.id)}
              disabled={!t.enabled}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
                active ? "border-brand text-fg" :
                t.enabled ? "border-transparent text-fg-subtle hover:text-fg" :
                "border-transparent text-fg-subtle cursor-not-allowed"
              }`}
            >
              <Icon size={12} />
              <span>{t.label}</span>
              {t.badge != null && t.badge > 0 && (
                <span className="text-micro px-1.5 py-0.5 rounded-full bg-surface-3 text-fg-muted font-semibold tabular-nums">{t.badge}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      {activeTab === "input" && (
        <InputTab
          name={name} setName={setName}
          sourceMode={sourceMode} setSourceMode={setSourceMode}
          codeFiles={codeFiles} setCodeFiles={setCodeFiles}
          githubUrl={githubUrl} setGithubUrl={setGithubUrl}
          githubBranch={githubBranch} setGithubBranch={setGithubBranch}
          githubToken={githubToken} setGithubToken={setGithubToken}
          docFiles={docFiles} setDocFiles={setDocFiles}
          onCreate={handleCreate} creating={creating}
          canSubmit={canSubmit} validationMsg={validationMsg}
        />
      )}
      {activeTab === "kb" && <KBTab kb={kb} phase={phase} phaseLabel={phaseLabel} progressPct={progressPct} requirements={kb?.requirements} />}
      {activeTab === "report" && <ReportTab result={result} analysisId={analysisId} phase={phase} phaseLabel={phaseLabel} progressPct={progressPct} error={error} currentModel={analysisModel} onReanalyzeStart={handleReanalyzeStart} />}
    </div>
  );
}
