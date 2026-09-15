import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import {
  Play, Pause, Square, RefreshCw, CheckCircle, AlertTriangle,   X, Cpu, Database, Globe, Server, Check, ChevronDown, ChevronLeft, ChevronRight,
  AlertCircle, GitBranch, Upload, Archive, FileCode, Trash2,
  Network, Layers, Target, Download, Sparkles, Eye, Code2, Bot, FileJson,
  History, Loader2, Folder, FolderOpen, Settings2, RotateCcw, TerminalSquare,
  Send, MessageSquare, TestTube2, Package, FilePlus, FlaskConical,
  Copy, PanelRightClose, PanelRightOpen, Rocket,   Maximize2, Minimize2,
} from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  createTransformation,
  runTransformation,
  getTransformationStatus,
  getTransformationFiles,
  getTransformationFile,
  regenerateTransformationFile,
  pushTransformationToGithub,
  getGithubConfig,
  analyzeSourceStack,
  getTransformerKB,
  downloadTransformedCode,
  downloadTransformedTests,
  downloadTransformedBundle,
  listModels,
  pauseTransformation,
  resumeTransformation,
  stopTransformation,
  listTransformations,
  getTransformation,
  deleteTransformation,
  runMultiAgentTransformation,
  confirmTransformationPlan,
  confirmTransformationTasks,
  getTransformationEnvelopes,
  getTransformationTraceability,
  confirmTransformationTraceability,
  getTransformationTasks,
  getAgentTimeline,
  getCompilationResult,
  runCompilationAnalysis,
  listTransformerAgentConfigs,
  getTransformerAgentConfig,
  saveTransformerAgentConfig,
  rerunTransformerPipeline,
  sendAgentPlanChat,
  getFactoryOrchestratorConfig,
  suggestBuildTools,
} from "../lib/api";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import TransformerTelemetry from "../components/TransformerTelemetry";
import TransformerStepper from "../components/TransformerStepper";
import TransformerHeader from "../components/TransformerHeader";
import { useBreakpoint } from "../hooks/useBreakpoint";
import { useProjects } from "@/state/ProjectContext";
import { FACTORY_MODEL_OPTIONS } from "@/lib/factoryModels";

/* ─────────────── iter-15.54 design tokens ─────────────── */
// One card style, one panel style, consistent radii/palette. Kill the
// gray-*/slate-* drift and the rounded-lg/xl/2xl mix.
// Guard against LLM schema drift: tool-generated fields (e.g. tester
// checks[].details / fix_suggestion) are documented as plain strings but
// an LLM occasionally nests an object instead (e.g. a self-invented
// {total_checks, passed, failed, overall_status} summary). Rendering an
// object directly as a JSX child crashes the whole tree (React error
// #31), so any free-text field coming from agent/LLM output must be
// passed through this before being rendered.
function toSafeText(val) {
  if (val == null) return "";
  if (typeof val === "string" || typeof val === "number" || typeof val === "boolean") return String(val);
  try {
    return JSON.stringify(val);
  } catch (_) {
    return String(val);
  }
}

const CARD_CLS = "rounded-xl border border-slate-200 bg-white shadow-sm";
const CARD_HEAD_CLS = "px-5 py-4 border-b border-slate-100 flex items-center gap-3";
const CHIP_CLS = "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium";
const FOCUS_RING = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500";
const BTN_PRIMARY = `inline-flex items-center justify-center gap-2 rounded-lg bg-violet-600 text-white font-semibold hover:bg-violet-700 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed ${FOCUS_RING}`;
const BTN_OUTLINE = `inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white text-slate-700 font-medium hover:bg-slate-50 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed ${FOCUS_RING}`;

// iter-15.14 — Broadcast the transformer's current phase so the sidebar
// Pipeline + the top StageProgress bar can highlight the correct stage
// (Input → Knowledge Base → Transformed) based on real backend state,
// not just the URL hash. Same-tab consumers listen on the custom event;
// cross-tab consumers pick it up via the `storage` event.
const broadcastTransformerPhase = (phase) => {
  try {
    if (phase) {
      window.localStorage.setItem("lama:transformer:phase", phase);
    } else {
      window.localStorage.removeItem("lama:transformer:phase");
    }
    window.dispatchEvent(new CustomEvent("lama:transformer:phase", { detail: { phase } }));
  } catch (_) {}
};

const normalizeGithubRepo = (repoUrl) =>
  (repoUrl || "")
    .trim()
    .replace(/^https?:\/\/github\.com\//i, "")
    .replace(/\.git$/i, "")
    .replace(/\/+$/, "");

const buildStructureRows = (items) => {
  const rows = [];
  const seenDirs = new Set();
  (items || [])
    .map((f) => ({ ...f, path: f.path || "" }))
    .sort((a, b) => a.path.localeCompare(b.path))
    .forEach((f, idx) => {
      const parts = f.path.split("/").filter(Boolean);
      let prefix = "";
      parts.slice(0, -1).forEach((part, depth) => {
        prefix = prefix ? `${prefix}/${part}` : part;
        if (!seenDirs.has(prefix)) {
          seenDirs.add(prefix);
          rows.push({
            id: `dir:${prefix}`,
            type: "dir",
            depth,
            label: part,
            path: prefix,
          });
        }
      });
      rows.push({
        id: f.id || f.path || idx,
        type: "file",
        depth: Math.max(0, parts.length - 1),
        label: parts[parts.length - 1] || f.path || `file_${idx}`,
        path: f.path,
        confidence: f.confidence,
        raw: f,
      });
    });
  return rows;
};

const buildSourceStructureRows = (sourceFiles) => {
  const rows = [];
  const seenDirs = new Set();
  (Array.isArray(sourceFiles) ? sourceFiles : [])
    .map((file, idx) => {
      const path = file?.webkitRelativePath || file?.name || `source-${idx}`;
      return { file, idx, path: String(path) };
    })
    .sort((a, b) => a.path.localeCompare(b.path))
    .forEach(({ file, idx, path }) => {
      const parts = path.split("/").filter(Boolean);
      let prefix = "";
      parts.slice(0, -1).forEach((part, depth) => {
        prefix = prefix ? `${prefix}/${part}` : part;
        if (!seenDirs.has(prefix)) {
          seenDirs.add(prefix);
          rows.push({
            id: `source-dir:${prefix}`,
            type: "dir",
            depth,
            label: part,
            path: prefix,
          });
        }
      });
      rows.push({
        id: `source:${path}:${idx}`,
        type: "file",
        depth: Math.max(0, parts.length - 1),
        label: parts[parts.length - 1] || path,
        path,
        confidence: null,
        raw: null,
        sourceOnly: true,
        size: file?.size ?? null,
      });
    });
  return rows;
};

// Filter tree rows to hide descendants of collapsed directory nodes.
// `collapsed` is an object { [dirPath]: true } — dirs default to open.
const filterCollapsedRows = (rows, collapsed) => {
  if (!collapsed || Object.keys(collapsed).length === 0) return rows;
  const hiddenPrefixes = [];
  const out = [];
  for (const row of rows) {
    const path = row.path || "";
    const hidden = hiddenPrefixes.some(
      (p) => path === p || path.startsWith(`${p}/`),
    );
    if (hidden) continue;
    out.push(row);
    if (row.type === "dir" && collapsed[path]) {
      hiddenPrefixes.push(path);
    }
  }
  return out;
};

const buildKbFileRows = (kb) => {
  const files = Array.isArray(kb?.file_index) ? kb.file_index : [];
  const entitySample = Array.isArray(kb?.entity_sample) ? kb.entity_sample : [];
  const chains = Array.isArray(kb?.chains) ? kb.chains : [];
  const stats = kb?.stats || {};
  const byFile = new Map();

  entitySample.forEach((entity) => {
    const source = entity?.source_file || "";
    if (!source) return;
    const bucket = byFile.get(source) || { total: 0, api: 0, db: 0, ui: 0, kinds: {} };
    const kind = String(entity?.type || "").toUpperCase();
    bucket.total += 1;
    bucket.kinds[kind] = (bucket.kinds[kind] || 0) + 1;
    if (["ROUTE", "ENDPOINT", "METHOD", "HANDLER"].includes(kind)) bucket.api += 1;
    if (["TABLE", "COLUMN", "VIEW_DB"].includes(kind)) bucket.db += 1;
    if (["COMPONENT", "PAGE", "VIEW", "TEMPLATE"].includes(kind)) bucket.ui += 1;
    byFile.set(source, bucket);
  });

  const uiCounts = new Map();
  const resolvedCounts = new Map();
  chains.forEach((chain) => {
    const source = chain?.ui || "";
    if (!source) return;
    uiCounts.set(source, (uiCounts.get(source) || 0) + 1);
    if (chain?.resolved) {
      resolvedCounts.set(source, (resolvedCounts.get(source) || 0) + 1);
    }
  });

  const fileRows = files.map((file, index) => {
    const path = file?.path || "";
    const counts = byFile.get(path) || { total: 0, api: 0, db: 0, ui: 0, kinds: {} };
    const kind = counts.ui > 0 ? "ui" : counts.api > 0 ? "api" : counts.db > 0 ? "db" : "code";
    return {
      ...file,
      id: path || `file-${index}`,
      kind,
      entityCount: counts.total,
      apiCount: counts.api,
      dbCount: counts.db,
      uiCount: counts.ui,
      chainCount: uiCounts.get(path) || 0,
      resolvedCount: resolvedCounts.get(path) || 0,
      kindCounts: counts.kinds,
    };
  });

  if (fileRows.length > 0) {
    return fileRows;
  }

  const grouped = [...byFile.entries()]
    .map(([path, counts]) => ({
      id: path,
      path,
      filetype: "source",
      kind: counts.ui > 0 ? "ui" : counts.api > 0 ? "api" : counts.db > 0 ? "db" : "code",
      size: null,
      entityCount: counts.total,
      apiCount: counts.api,
      dbCount: counts.db,
      uiCount: counts.ui,
      chainCount: uiCounts.get(path) || 0,
      resolvedCount: resolvedCounts.get(path) || 0,
      kindCounts: counts.kinds,
    }))
    .sort((a, b) => (b.entityCount - a.entityCount) || a.path.localeCompare(b.path));

  if (grouped.length > 0) {
    return grouped;
  }

  return [
    {
      id: "kb-summary",
      path: "KB summary",
      filetype: "aggregate",
      kind: stats.ui_files > stats.db_tables && stats.ui_files > 0 ? "ui" : stats.api_routes > 0 ? "api" : "code",
      size: null,
      entityCount: stats.entities || 0,
      apiCount: stats.api_routes || 0,
      dbCount: stats.db_tables || 0,
      uiCount: stats.ui_files || 0,
      chainCount: stats.resolved_chains || 0,
      resolvedCount: stats.resolved_chains || 0,
      kindCounts: {
        ENTITY: stats.entities || 0,
        ROUTE: stats.api_routes || 0,
        TABLE: stats.db_tables || 0,
        UI: stats.ui_files || 0,
      },
    },
  ];
};

/* ─────────────── Constants ─────────────── */

const TECH_CATEGORIES = {
  backend: {
    label: "Backend",
    icon: Server,
    color: "purple",
    options: [
      { id: "spring-boot-3", name: "Spring Boot 3", color: "#6DB33F" },
      { id: "spring-boot-2", name: "Spring Boot 2", color: "#6DB33F" },
      { id: "helidon", name: "Helidon", color: "#1B1464" },
      { id: "quarkus", name: "Quarkus", color: "#4695EB" },
      { id: "micronaut", name: "Micronaut", color: "#000000" },
      { id: "fastapi", name: "FastAPI", color: "#009688" },
      { id: "django", name: "Django", color: "#092E20" },
      { id: "flask", name: "Flask", color: "#000000" },
      { id: "express", name: "Express.js", color: "#68A063" },
      { id: "nestjs", name: "NestJS", color: "#E0234E" },
      { id: "play", name: "Play Framework", color: "#92D13D" },
      { id: "ktor", name: "Ktor", color: "#7F52FF" },
      { id: "dropwizard", name: "Dropwizard", color: "#F58F00" },
      { id: "vertx", name: "Vert.x", color: "#800080" },
      { id: "aspnet-core", name: "ASP.NET Core", color: "#512BD4" },
      { id: "gin", name: "Gin (Go)", color: "#00ADD8" },
      { id: "echo", name: "Echo (Go)", color: "#00ADD8" },
      { id: "fiber", name: "Fiber (Go)", color: "#00ADD8" },
      { id: "rails", name: "Ruby on Rails", color: "#CC0000" },
      { id: "laravel", name: "Laravel", color: "#FF2D20" },
      { id: "symfony", name: "Symfony", color: "#000000" },
      { id: "phoenix", name: "Phoenix (Elixir)", color: "#FD4F00" },
      { id: "actix", name: "Actix (Rust)", color: "#DEA584" },
    ],
  },
  frontend: {
    label: "Frontend",
    icon: Globe,
    color: "blue",
    options: [
      { id: "react-18", name: "React 18", color: "#61DAFB" },
      { id: "react-19", name: "React 19", color: "#61DAFB" },
      { id: "nextjs", name: "Next.js", color: "#000000" },
      { id: "remix", name: "Remix", color: "#121212" },
      { id: "angular-17", name: "Angular 17", color: "#DD0031" },
      { id: "vue-3", name: "Vue 3", color: "#4FC08D" },
      { id: "nuxt", name: "Nuxt", color: "#00DC82" },
      { id: "svelte", name: "Svelte", color: "#FF3E00" },
      { id: "sveltekit", name: "SvelteKit", color: "#FF3E00" },
      { id: "solid", name: "SolidJS", color: "#2C4F7C" },
      { id: "qwik", name: "Qwik", color: "#AC7EF4" },
      { id: "astro", name: "Astro", color: "#FF5D01" },
      { id: "preact", name: "Preact", color: "#673AB8" },
      { id: "lit", name: "Lit", color: "#324FFF" },
      { id: "ember", name: "Ember", color: "#E04E39" },
      { id: "htmx", name: "HTMX", color: "#3D72D7" },
    ],
  },
  database: {
    label: "Database",
    icon: Database,
    color: "green",
    options: [
      { id: "postgresql", name: "PostgreSQL", color: "#336791" },
      { id: "mysql", name: "MySQL", color: "#4479A1" },
      { id: "mariadb", name: "MariaDB", color: "#003545" },
      { id: "oracle", name: "Oracle", color: "#F80000" },
      { id: "sqlserver", name: "SQL Server", color: "#CC2927" },
      { id: "sqlite", name: "SQLite", color: "#003B57" },
      { id: "mongodb", name: "MongoDB", color: "#47A248" },
      { id: "cockroachdb", name: "CockroachDB", color: "#6933FF" },
      { id: "cassandra", name: "Cassandra", color: "#1287B1" },
      { id: "dynamodb", name: "DynamoDB", color: "#4053D6" },
      { id: "redis", name: "Redis", color: "#DC382D" },
      { id: "snowflake", name: "Snowflake", color: "#29B5E8" },
      { id: "bigquery", name: "BigQuery", color: "#4285F4" },
      { id: "clickhouse", name: "ClickHouse", color: "#FFCC01" },
      { id: "tidb", name: "TiDB", color: "#D71DA1" },
    ],
  },
  runtime: {
    label: "Runtime",
    icon: Cpu,
    color: "orange",
    options: [
      { id: "java-25", name: "Java 25", color: "#ED8B00" },
      { id: "java-21", name: "Java 21", color: "#ED8B00" },
      { id: "java-17", name: "Java 17", color: "#ED8B00" },
      { id: "java-11", name: "Java 11", color: "#ED8B00" },
      { id: "python-3.13", name: "Python 3.13", color: "#3776AB" },
      { id: "python-3.12", name: "Python 3.12", color: "#3776AB" },
      { id: "python-3.11", name: "Python 3.11", color: "#3776AB" },
      { id: "node-22", name: "Node.js 22", color: "#339933" },
      { id: "node-20", name: "Node.js 20", color: "#339933" },
      { id: "deno", name: "Deno", color: "#000000" },
      { id: "bun", name: "Bun", color: "#F471B5" },
      { id: "dotnet-8", name: ".NET 8", color: "#512BD4" },
      { id: "dotnet-9", name: ".NET 9", color: "#512BD4" },
      { id: "go-1.23", name: "Go 1.23", color: "#00ADD8" },
      { id: "rust-1.80", name: "Rust 1.80", color: "#DEA584" },
      { id: "kotlin-2", name: "Kotlin 2", color: "#7F52FF" },
      { id: "elixir-1.17", name: "Elixir 1.17", color: "#4B275F" },
      { id: "ruby-3.3", name: "Ruby 3.3", color: "#CC342D" },
      { id: "php-8.3", name: "PHP 8.3", color: "#777BB4" },
    ],
  },
};

// Smart recommendations mapping
const SMART_RECOMMENDATIONS = {
  backend: {
    helidon: ["spring-boot-3", "quarkus", "micronaut"],
    "spring-boot-2": ["spring-boot-3"],
    quarkus: ["spring-boot-3", "helidon"],
    micronaut: ["spring-boot-3", "quarkus"],
    codeigniter: ["fastapi", "django", "spring-boot-3"],
    laravel: ["fastapi", "django", "nestjs"],
    php: ["fastapi", "django", "spring-boot-3"],
    express: ["nestjs", "fastapi"],
    django: ["fastapi"],
    flask: ["fastapi"],
  },
  frontend: {
    "angular-16": ["angular-17", "react-18"],
    "angular-17": ["react-18", "vue-3"],
    jquery: ["react-18", "vue-3"],
    jsp: ["react-18", "angular-17"],
    thymeleaf: ["react-18", "vue-3"],
    "react-16": ["react-18"],
    "vue-2": ["vue-3"],
  },
  database: {
    oracle: ["postgresql", "mysql"],
    mysql: ["postgresql"],
    mariadb: ["postgresql", "mysql"],
    sqlserver: ["postgresql"],
    mongodb: ["postgresql"],
    h2: ["postgresql"],
    sqlite: ["postgresql", "mysql"],
  },
  runtime: {
    "java-8": ["java-21", "java-17"],
    "java-11": ["java-21", "java-17"],
    "java-17": ["java-21"],
    "php-7": ["python-3.12", "node-20"],
    "php-8": ["python-3.12", "node-20"],
    "python-3.8": ["python-3.12"],
    "python-3.9": ["python-3.12"],
    "node-16": ["node-20"],
    "node-18": ["node-20"],
  },
};

const getRelevantOptions = (catKey, detected) => {
  const cat = TECH_CATEGORIES[catKey];
  if (!detected) return cat.options;

  const recommendations = SMART_RECOMMENDATIONS[catKey]?.[detected] || [];

  if (recommendations.length > 0) {
    return cat.options
      .filter(opt => recommendations.includes(opt.id) && opt.id !== detected)
      .sort((a, b) => recommendations.indexOf(a.id) - recommendations.indexOf(b.id));
  }

  return cat.options.filter(opt => opt.id !== detected);
};

const TAB_HASH = {
  input: "#input",
  kb: "#kb",
  code: "#output",
  compile: "#compile",
};

const HASH_TO_TAB = {
  input: "input",
  kb: "kb",
  output: "code",
  compile: "compile",
};

const getTabFromHash = (hash) => {
  const raw = String(hash || "").replace(/^#/, "").toLowerCase();
  return HASH_TO_TAB[raw] || null;
};

const AGENT_ORDER = ["super_agent", "context_manager", "planner", "coder", "verifier", "devops_expert", "tester"];

// iter-16.x — Terminal phases for the compile-fix loop's `compile_fix_progress`
// state. Any phase NOT in this list ("fixing", "verifying", "escalated_devops",
// etc.) means the loop is still actively iterating — used by
// `getAgentNodeStatus` to keep Coder/Verifier/Tester nodes honestly "running"
// instead of a stale/false-green "Completed" while a re-run is in flight.
const CFP_TERMINAL_PHASES = ["passed", "exhausted", "unfixable", "infra_blocked", "stagnant", "stalled"];

// iter-16.x — A compile-fix loop kicked off by "Rerun compile" runs as a
// background job that mutates `transformation.compile_fix_progress` without
// flipping the top-level `transformation.status` back to "running" — the
// pipeline as a whole is still "completed"/"completed_with_errors" from the
// earlier pass. Without a shared helper the FE polling loop, mount-time
// hydration, sidebar node status, and header status pill each had to
// re-derive "is the compile-fix loop actually alive right now" from raw
// `compile_fix_progress.phase` strings, and they diverged: mount-time
// hydration missed it entirely, so a tab switch or login/logout left
// Coder/Verifier/Tester falsely reading "Completed" and the header showing
// a green "Completed" pill while the backend correctly rejected further
// `/compile` calls with a 409 "already in progress".
const isCompileFixActive = (cfp) => {
  const phase = cfp && cfp.phase;
  return !!phase && !CFP_TERMINAL_PHASES.includes(phase);
};

const AGENT_META = {
  super_agent: {
    shortLabel: "SA",
    label: "Super Agent",
    description: "Overall transformation overview, detected stacks, and run readiness.",
  },
  context_manager: {
    shortLabel: "CM",
    label: "Context Manager",
    description: "Discovered architecture envelopes and the first human review gate.",
  },
  planner: {
    shortLabel: "Planner",
    label: "Planner",
    description: "Wave-based task plan with per-file actions before code generation starts.",
  },
  coder: {
    shortLabel: "Coder",
    label: "Coder",
    description: "Generated project structure with live file preview and regeneration controls.",
  },
  verifier: {
    shortLabel: "Verifier",
    label: "Verifier",
    description: "Per-file verification confidence, verdicts, and issue counts.",
  },
  devops_expert: {
    shortLabel: "DevOps",
    label: "DevOps Expert",
    description: "Build/infrastructure specialist, in two modes: it escalates into the compile-fix loop when the Coder's fix makes no difference, and it audits the generated build manifests for production readiness — sending anything it finds back to the Planner for repair.",
  },
  tester: {
    shortLabel: "Tester",
    label: "Tester",
    description: "Compilation-readiness analysis and post-generation quality checks.",
  },
};

const getAgentTabFromRunState = (phase, runStatus) => {
  if (runStatus === "awaiting_confirmation") return "context_manager";
  // iter-15.43 — Once the Planner has produced tasks (awaiting_task_confirmation
  // or any coder/verifier phase), land on the stacked Coder workspace.
  // Coder + Planner are rendered together so the operator no longer has
  // to tab-swap between the two mid-run — which was the #1 complaint in
  // the iter-15.42 UX review.
  if (runStatus === "awaiting_task_confirmation") return "coder";
  if (phase === "planner" || phase === "verifier") return "coder";
  // iter-19 — the DevOps gate runs as its own phase now (audit, then a
  // bounded Planner-driven remediation round) instead of only appearing as
  // an escalation inside the compile-fix loop.
  if (phase === "devops" || phase === "devops_remediation") return "devops_expert";
  if (phase && AGENT_ORDER.includes(phase)) return phase;
  if (runStatus === "completed" || runStatus === "completed_with_errors" || runStatus === "stopped") return "tester";
  return "super_agent";
};

// iter-15.54 — Center "Coder Workspace" tabs. The center panel is now a
// permanent tabbed surface (Coder always available as the default "Files"
// tab, Planner details one click away in "Planner Tasks") instead of a
// full agent-mode swap. Each tab maps 1:1 onto an agent so the left
// pipeline rail and the tab bar share a single source of truth
// (`selectedAgentTab`) — no extra state.
const CENTER_TABS = [
  { key: "overview", label: "Overview", agent: "super_agent" },
  { key: "files", label: "Files", agent: "coder" },
  { key: "planner", label: "Planner Tasks", agent: "planner" },
  { key: "envelopes", label: "Envelopes", agent: "context_manager" },
  { key: "compile", label: "Compile", agent: "tester" },
];

const AGENT_TO_CENTER_TAB = {
  super_agent: "overview",
  coder: "files",
  verifier: "files",
  planner: "planner",
  context_manager: "envelopes",
  devops_expert: "compile",
  tester: "compile",
};

/* ─────────────── Main Component ─────────────── */

export default function TransformerPage() {
  const { active, loading: projectsLoading } = useProjects();
  const navigate = useNavigate();
  const location = useLocation();
  const [sourceFiles, setSourceFiles] = useState([]);
  const [name, setName] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [detectedStack, setDetectedStack] = useState(null);
  const [selectedTransforms, setSelectedTransforms] = useState({});
  // iter-15.40 — Build system per active target component. Populated by
  // `suggestBuildTools` when the operator picks a target for a category,
  // and edited via the "Build System" dropdowns rendered under the
  // target-stack picker. Sent as JSON in the create/v2 POST so
  // `_run_compiler` (iter-15.44) knows which subprocess to invoke.
  const [buildToolOptions, setBuildToolOptions] = useState({});   // { component: [tool, ...] }
  const [selectedBuildTools, setSelectedBuildTools] = useState({}); // { component: tool }
  const [buildToolsConfirmed, setBuildToolsConfirmed] = useState(false);
  const [creating, setCreating] = useState(false);
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [files, setFiles] = useState([]);
  const [error, setError] = useState(null);
  const [transformId, setTransformId] = useState(null);
  const [progress, setProgress] = useState({ phase: null, percent: 0, message: "" });
  // iter-15.55 — Live Tester progress surfaced in the Activity Rail so the
  // (slow) tester agent is no longer a silent black box. Shape:
  //   { done, total, per_tier: { business: {done,total}, api:…, integration:… },
  //     in_flight: [{tier, envelope, started_at}], recent: [{path, tier, at}],
  //     started_at, updated_at, completed_at?, duration_ms?, model }
  const [testerProgress, setTesterProgress] = useState(null);
  // iter-15.58 — Live compile-fix loop progress:
  //   {phase: 'compiling'|'planning_fixes'|'fixing'|'passed'|'exhausted'|'unfixable',
  //    iteration, max_iterations, message, current_file?, failing_files?[]}
  const [compileFixProgress, setCompileFixProgress] = useState(null);
  // iter-15.6x — Live streaming Compile Console (real subprocess stdout/
  // stderr appended by the backend as `mvn clean install` / `gradle clean
  // build` / etc. actually run). `{lines: string[], updated_at}` — polled
  // alongside testerProgress/compileFixProgress every 2s.
  const [compileConsole, setCompileConsole] = useState({ lines: [], updatedAt: null });
  const compileConsoleRef = useRef(null);
  // iter-15.7x — Compile Console redesign: full-screen maximize toggle +
  // copy-to-clipboard feedback for the toolbar buttons.
  const [consoleMaximized, setConsoleMaximized] = useState(false);
  const [consoleCopied, setConsoleCopied] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [kb, setKb] = useState(null);
  const [selectedKbFile, setSelectedKbFile] = useState(null);
  // iter-15.10 additions
  const [selectedFile, setSelectedFile] = useState(null);   // { id, path, content, confidence }
  const [regeneratingIds, setRegeneratingIds] = useState({});
  const [regenModel, setRegenModel] = useState("");
  const [availableModels, setAvailableModels] = useState([]);
  // iter-15.39 — When Factory Droid is enabled for the active project, the
  // per-agent "Prompt & Model" and "Regenerate with" dropdowns must offer
  // Factory's own model catalogue instead of Console/Ollama's, because
  // fabric_call ignores the Console-provider model pick entirely whenever
  // Factory routing is active (see llm.py — Factory is the sole route
  // unless LAMA_FACTORY_PIN_BYPASS=1). Showing Ollama model names here was
  // misleading: picking one had zero effect on what actually ran.
  const [factoryEnabled, setFactoryEnabled] = useState(false);
  const [showGitHub, setShowGitHub] = useState(false);
  const [ghForm, setGhForm] = useState({ repo: "", branch: "transformation-output", commit_message: "Transformed code from LAMA", path_prefix: "" });
  const [pushing, setPushing] = useState(false);
  const [showOthers, setShowOthers] = useState({}); // iter-15.12 — per-category "Others" toggle
  const [othersAnchor, setOthersAnchor] = useState({}); // iter-15.13.1 — {catKey: {top, left, width}}
  // iter-15.14 additions
  const [paused, setPaused] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [showKebab, setShowKebab] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [historyItems, setHistoryItems] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [showRemoveConfirm, setShowRemoveConfirm] = useState(false);
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  const [busyControl, setBusyControl] = useState(null); // "pause" | "resume" | "stop" | null
  const [githubConfig, setGithubConfig] = useState(null);
  const [pushLoading, setPushLoading] = useState(false);
  // Multi-agent pipeline state (iter-16)
  const [pipelineMode, setPipelineMode] = useState("multi_agent"); // "single" | "multi_agent"
  const [envelopes, setEnvelopes] = useState([]);
  // iter-15.41 — Traceability gate data (mode="backend"|"frontend"|
  // "fullstack" + hop chains + FE screen→API mapping). Populated in the
  // awaiting_confirmation phase — powers the mandatory traceability
  // confirmation view rendered above the envelope table.
  const [traceability, setTraceability] = useState(null);
  const [agentTimeline, setAgentTimeline] = useState([]);
  const [taskList, setTaskList] = useState(null);
  const [compilationResult, setCompilationResult] = useState(null);
  // iter-19 — {production_ready, findings[], summary, remediation_rounds[]}
  const [dependencyAudit, setDependencyAudit] = useState(null);
  const [compilationLoading, setCompilationLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmingTasks, setConfirmingTasks] = useState(false);
  // iter-15.19 — Agent Pipeline config panel (click an agent node to
  // review/edit its per-transformation prompt + model override)
  const [agentConfigAgent, setAgentConfigAgent] = useState(null); // agent key or null (closed)
  const [agentConfigData, setAgentConfigData] = useState(null);   // effective config for agentConfigAgent
  const [agentConfigLoading, setAgentConfigLoading] = useState(false);
  const [agentConfigSaving, setAgentConfigSaving] = useState(false);
  const [agentConfigError, setAgentConfigError] = useState(null);
  const [agentConfigDraftPrompt, setAgentConfigDraftPrompt] = useState("");
  const [agentConfigDraftModel, setAgentConfigDraftModel] = useState("");
  const [rerunning, setRerunning] = useState(false);
  const [agentOverrideFlags, setAgentOverrideFlags] = useState({}); // {agent: is_overridden}
  // iter-15.20 — Envelope detail panel (click an API/envelope row in the
  // "Discovered Architecture" table to see its full trace)
  const [selectedEnvelope, setSelectedEnvelope] = useState(null);
  // iter-15.21 — Discovered Architecture table pagination (10 rows/page)
  const [envelopePage, setEnvelopePage] = useState(1);
  const [taskPages, setTaskPages] = useState({});
  // iter-15.35 — Verification Console pagination (10 rows/page)
  const [verifierPage, setVerifierPage] = useState(1);
  const ENVELOPES_PER_PAGE = 10;
  const TASKS_PER_PAGE = 10;
  const VERIFIER_PER_PAGE = 10;
  // iter-15.38 — Planner/Coder/Tester chat assistant: "remove all test
  // files from wave 2", "find files touching Order", "move DAO classes to
  // wave 3". One independent conversation per panel; each turn is applied
  // server-side immediately (no separate save step).
  const [planChat, setPlanChat] = useState({
    planner: { messages: [], input: "", busy: false },
    coder: { messages: [], input: "", busy: false },
    tester: { messages: [], input: "", busy: false },
  });
  const pollRef = useRef(null);
  const lastFilesDoneRef = useRef(0);
  const [activeTab, setActiveTab] = useState(() => getTabFromHash(window.location.hash) || "input");
  const [selectedAgentTab, setSelectedAgentTab] = useState("super_agent");
  const userPinnedAgentTabRef = useRef(false);
  // iter-15.54 — Running-workspace chrome. The right "Activity Rail" is
  // collapsible; the breakpoint drives the 3-col ⇄ stacked responsive layout.
  const [activityCollapsed, setActivityCollapsed] = useState(false);
  const bp = useBreakpoint();
  // iter-15.54 — Honest "elapsed" for the header metric strip: starts ticking
  // when a run goes live in this session and freezes on completion/stop.
  const runStartRef = useRef(null);
  const [elapsedMs, setElapsedMs] = useState(0);

  const changeTab = useCallback((tab) => {
    if (!["input", "kb", "code", "compile"].includes(tab)) return;
    setActiveTab(tab);
    const nextHash = TAB_HASH[tab] || "#input";
    if ((location.hash || "") !== nextHash) {
      navigate(`${location.pathname}${nextHash}`, { replace: true });
    }
  }, [location.hash, location.pathname, navigate]);

  useEffect(() => {
    const tab = getTabFromHash(location.hash);
    if (tab && tab !== activeTab) {
      setActiveTab(tab);
    }
  }, [activeTab, location.hash]);

  // iter-15.6x — Auto-scroll the live Compile Console to the bottom as new
  // lines stream in, so the operator sees the tail of the real
  // `mvn clean install` / etc. run without having to manually scroll.
  useEffect(() => {
    const el = compileConsoleRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [compileConsole.lines]);

  // iter-15.7x — Esc restores a maximized Compile Console back to inline.
  useEffect(() => {
    if (!consoleMaximized) return;
    const onKey = (e) => { if (e.key === "Escape") setConsoleMaximized(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [consoleMaximized]);

  useEffect(() => {
    if (!location.hash) {
      const fallback = status === "running"
        ? "code"
        : (status === "completed" || status === "completed_with_errors")
          ? "code"
          : "input";
      if (fallback !== activeTab) {
        setActiveTab(fallback);
      }
      navigate(`${location.pathname}${TAB_HASH[fallback] || "#input"}`, { replace: true });
    }
  }, [activeTab, location.hash, location.pathname, navigate, status]);

  // Fetch KB — retry during running because KB is built early in Phase 1 but
  // the first poll may 404 before build_tools_kb completes.
  useEffect(() => {
    if (!transformId || kb) return;
    if (status !== "completed" && status !== "running") return;
    let cancelled = false;
    let attempts = 0;
    const tryFetch = async () => {
      if (cancelled) return;
      try {
        const kbData = await getTransformerKB(transformId);
        if (!cancelled) setKb(kbData);
      } catch {
        // KB not ready yet — retry a few times while still running
        attempts += 1;
        if (!cancelled && status === "running" && attempts < 60) {
          setTimeout(tryFetch, 3000);
        }
      }
    };
    tryFetch();
    return () => { cancelled = true; };
  }, [transformId, status, kb]);

  // Load available models for the regenerate dropdown
  useEffect(() => {
    (async () => {
      try {
        const res = await listModels();
        const models = Array.isArray(res) ? res : (res?.models || []);
        setAvailableModels(models);
      } catch {}
    })();
  }, []);

  // iter-15.39 — Check whether Factory Droid is enabled for the active
  // project so model dropdowns can source Factory's catalogue instead of
  // Console/Ollama's (see factoryEnabled comment above for why).
  //
  // iter-15.50 — Bug fix: `getFactoryOrchestratorConfig` returns the
  // envelope `{ok, config}`, so `cfg?.enabled` was always `undefined`
  // → `factoryEnabled` stayed `false` even when Factory was up (CLI
  // mode with a live droid). Read from `cfg.config.routing_active`
  // (the server's authoritative "will actually route via factory"
  // flag, which is `true` for both API mode with app_key+computer AND
  // CLI mode with a resolvable droid binary), falling back to
  // `cfg.config.enabled` when older backends don't emit it.
  useEffect(() => {
    if (!active?.id) {
      setFactoryEnabled(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getFactoryOrchestratorConfig(active.id);
        const c = cfg?.config || cfg || {};
        // iter-16.x — Was `c.routing_active ?? c.enabled`, but nullish
        // coalescing is the WRONG operator here: `routing_active` is a
        // real boolean (never null/undefined), so `??` always returns
        // it verbatim and NEVER falls back to `enabled`. In API mode a
        // project with `enabled=true` but `app_key` / `computer_id` not
        // yet mapped reports `routing_active=false` — the operator's
        // INTENT is clearly Factory (MiniConsole already reads that as
        // "FACTORY UP" using just `c.enabled`) but this dropdown
        // silently fell back to Ollama's catalogue. Match the
        // MiniConsole's intent-based check: user has enabled Factory OR
        // Factory is fully wired → offer the Factory model catalogue.
        const on = Boolean(c.routing_active) || Boolean(c.enabled);
        if (!cancelled) setFactoryEnabled(on);
      } catch {
        if (!cancelled) setFactoryEnabled(false);
      }
    })();
    return () => { cancelled = true; };
  }, [active?.id]);

  // iter-15.54/15.60 — Elapsed timer. Seeded from persisted `created_at`
  // in pollStatus so it survives reload/resume, and only reset when the
  // user starts a *brand new* transformation (transformId flips to null).
  useEffect(() => {
    if (status === "running" && !paused) {
      if (!runStartRef.current) runStartRef.current = Date.now() - elapsedMs;
      const t = setInterval(() => {
        setElapsedMs(Date.now() - (runStartRef.current || Date.now()));
      }, 1000);
      return () => clearInterval(t);
    }
    return undefined;
  }, [status, paused]); // eslint-disable-line react-hooks/exhaustive-deps

  // Explicit reset only when the whole transformation goes away.
  useEffect(() => {
    if (!transformId) {
      runStartRef.current = null;
      setElapsedMs(0);
    }
  }, [transformId]);


  // and "Regenerate with" dropdowns. Factory's catalogue when Droid is
  // enabled (the pick actually reaches `route_via_factory_orchestrator`),
  // else fall back to the Console-configured provider list as before.
  const effectiveModelOptions = useMemo(() => {
    if (factoryEnabled) {
      return FACTORY_MODEL_OPTIONS.map((m) => ({ id: m.value, label: m.label }));
    }
    return availableModels.map((m) => {
      const id = typeof m === "string" ? m : (m.id || m.model || "");
      const label = typeof m === "string" ? m : (m.label || m.name || id);
      return { id, label };
    });
  }, [factoryEnabled, availableModels]);

  // Cleanup polling on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  // iter-15.27 / iter-16.x — Persist the in-progress/completed transformation
  // so the page can restore it after a browser refresh or when the user
  // leaves this tab/route and comes back. Without this, all in-memory React
  // state (transformId, files, envelopes, etc.) was lost on every remount
  // and the user had to start the whole transform over from "Input".
  // Scoped per active project since projects are (in principle) isolated
  // workspaces, even though today's UI is single-tenant.
  //
  // iter-16.x — Two-tier hydration. localStorage is the fast path (survives
  // reload for the same origin+profile), but it is INVISIBLE in InPrivate
  // / incognito windows, after clearing site data, or on a different
  // browser/device. In those cases we fall back to a project-scoped
  // `listTransformations({projectId})` and hydrate the most recent run —
  // this is what makes a completed "hiring-service-lts" show up with its
  // envelope/planning/coder/verifier/tester history when the operator
  // returns to the project, instead of the empty "Upload source" state.
  //
  // Also: re-run hydration whenever the active project changes (the old
  // one-shot `restoredRef` meant switching projects kept the previous
  // project's transformation on screen).
  const lastHydratedProjectRef = useRef(null);
  useEffect(() => {
    if (!transformId) return;
    try {
      window.localStorage.setItem(`lama:transformer:lastId:${active?.id || "default"}`, transformId);
    } catch (_) {}
  }, [transformId, active?.id]);

  useEffect(() => {
    if (projectsLoading) return; // wait for the active project to resolve
    const pid = active?.id || "default";
    if (lastHydratedProjectRef.current === pid) return; // already hydrated for this project
    lastHydratedProjectRef.current = pid;

    let cancelled = false;
    (async () => {
      // 1) Fast path — localStorage lastId for this project.
      let savedId = null;
      try {
        savedId = window.localStorage.getItem(`lama:transformer:lastId:${pid}`) || null;
      } catch (_) {}

      if (savedId) {
        try {
          await loadTransformationFromHistory(savedId, { silent: true });
          return;
        } catch (_) {
          // stored id no longer exists — fall through to backend lookup
        }
      }

      // 2) Fallback — ask the backend for this project's most recent run.
      //    Covers InPrivate/incognito, cleared site data, cross-device access.
      if (!active?.id) return;
      try {
        const items = await listTransformations({ projectId: active.id });
        if (cancelled) return;
        if (Array.isArray(items) && items.length > 0) {
          // Backend already sorts by created_at desc.
          const latest = items[0];
          const tid = latest.id || latest._id;
          if (tid) await loadTransformationFromHistory(tid, { silent: true });
        }
      } catch (_) {
        // Silent — the empty "Upload source" state is a safe default.
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectsLoading, active?.id]);

  // iter-15.21 — Reset Discovered Architecture pagination whenever the
  // envelope set changes (new run / rerun) so stale page numbers don't
  // point past the end of a shorter, freshly-discovered list.
  useEffect(() => { setEnvelopePage(1); }, [envelopes]);
  useEffect(() => { setTaskPages({}); }, [taskList]);

  const totalEnvelopePages = Math.max(1, Math.ceil(envelopes.length / ENVELOPES_PER_PAGE));
  const pagedEnvelopes = useMemo(
    () => envelopes.slice((envelopePage - 1) * ENVELOPES_PER_PAGE, envelopePage * ENVELOPES_PER_PAGE),
    [envelopes, envelopePage]
  );

  const dropRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const syncSelectedAgentTab = useCallback((phase, runStatus, { force = false } = {}) => {
    const nextTab = getAgentTabFromRunState(phase, runStatus);
    if (force || !userPinnedAgentTabRef.current) {
      setSelectedAgentTab(nextTab);
    }
  }, []);

  const handleSelectAgentTab = useCallback((agent) => {
    userPinnedAgentTabRef.current = true;
    setSelectedAgentTab(agent);
  }, []);

  const pollStatus = useCallback(async (tid) => {
    try {
      const s = await getTransformationStatus(tid);
      const pct = s.progress_pct ?? 0;
      syncSelectedAgentTab(s.phase, s.status);
      setProgress({
        phase: s.phase || "transforming",
        percent: pct,
        message: s.current_file
          ? `${s.phase_label || "Transforming"} · ${s.current_file}`
          : (s.phase_label || "Transforming code..."),
        filesDone: s.files_done,
        filesTotal: s.files_total,
        currentFile: s.current_file || null,
      });
      setPaused(!!s.paused);
      setStopped(!!s.stopped);
      // iter-15.60 — Seed the header elapsed timer from the persisted
      // `created_at` so the value survives a page reload / resume, and
      // reflects total wall-clock time since the transformation began
      // (matches the Coder card's cumulative counters).
      if (s.created_at) {
        const startedTs = Date.parse(s.created_at);
        if (!Number.isNaN(startedTs)) {
          runStartRef.current = startedTs;
          if (s.status !== "running" || !!s.paused) {
            // Frozen states — set once so the metric shows the total,
            // then the "running" effect below stops the interval.
            const endTs = s.updated_at ? Date.parse(s.updated_at) : Date.now();
            setElapsedMs(Math.max(0, (Number.isNaN(endTs) ? Date.now() : endTs) - startedTs));
          } else {
            setElapsedMs(Math.max(0, Date.now() - startedTs));
          }
        }
      }
      // iter-15.55 — Push tester progress into state so the Activity Rail
      // panel updates every 2s while the tester agent is running.
      setTesterProgress(s.tester_progress || null);
      // iter-15.58 — Compile→Planner→Coder auto-fix loop progress and the
      // latest compile result surface through the same status poll.
      setCompileFixProgress(s.compile_fix_progress || null);
      // iter-15.6x — Live streaming Compile Console lines.
      if (Array.isArray(s.compile_console)) {
        setCompileConsole({ lines: s.compile_console, updatedAt: s.compile_console_updated_at || null });
      }
      if (s.compilation_result) {
        setCompilationResult(s.compilation_result);
      }
      // iter-19 — DevOps gate verdict. Only overwrite when the backend
      // actually sent one, so a poll that lands mid-run does not blank a
      // verdict already on screen.
      if (s.dependency_audit) {
        setDependencyAudit(s.dependency_audit);
      }
      // iter-15.14 — broadcast to sidebar + top stage progress
      broadcastTransformerPhase(s.phase || null);
      const filesDone = Number(s.files_done || 0);
      const shouldRefreshFiles =
        s.status === "running" || s.status === "completed" || s.status === "completed_with_errors" || s.status === "stopped";
      if (shouldRefreshFiles && (filesDone !== lastFilesDoneRef.current || s.status !== "running")) {
        lastFilesDoneRef.current = filesDone;
        let loadedFiles = [];
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          loadedFiles = Array.isArray(arr) ? arr : [];
          setFiles(loadedFiles);
        } catch {}
      }
      // Multi-agent: fetch envelopes when awaiting_confirmation
      if (s.status === "awaiting_confirmation") {
        stopPolling();
        setStatus("awaiting_confirmation");
        broadcastTransformerPhase("awaiting_confirmation");
        syncSelectedAgentTab(s.phase, s.status, { force: true });
        try {
          const envData = await getTransformationEnvelopes(tid);
          setEnvelopes(envData?.envelopes || []);
        } catch {}
        // iter-15.41 — Load UI-ready traceability data (BE hop chains
        // and/or FE screen→API mapping) for the mandatory review gate.
        try {
          const trData = await getTransformationTraceability(tid);
          setTraceability(trData || null);
        } catch { setTraceability(null); }
        try {
          const tlData = await getAgentTimeline(tid);
          setAgentTimeline(tlData?.timeline || []);
        } catch {}
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
        return;
      }
      if (s.status === "awaiting_task_confirmation") {
        stopPolling();
        setStatus("awaiting_task_confirmation");
        broadcastTransformerPhase("awaiting_task_confirmation");
        syncSelectedAgentTab(s.phase, s.status, { force: true });
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
        try {
          const tlData = await getAgentTimeline(tid);
          setAgentTimeline(tlData?.timeline || []);
        } catch {}
        return;
      }
      // Fetch agent timeline periodically during multi-agent runs.
      // iter-15.6x — Broadened from an `AGENT_ORDER`-only phase check: an
      // orphan-recovered run (`/resume`, `/resume-orphan`) reports generic
      // phases like "transforming"/"paused_orphan" instead of the
      // multi-agent phase names, which previously starved the Planner
      // Details / Context Manager (envelopes) panels of any data refresh
      // for the rest of the run. Any live "running" status now keeps
      // these panels current regardless of the exact phase string.
      const isLiveRun = s.status === "running" || (s.phase && AGENT_ORDER.includes(s.phase));
      if (isLiveRun) {
        try {
          const tlData = await getAgentTimeline(tid);
          setAgentTimeline(tlData?.timeline || []);
        } catch {}
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
        try {
          const envData = await getTransformationEnvelopes(tid);
          if (envData?.envelopes?.length) setEnvelopes(envData.envelopes);
        } catch {}
      }
      if (s.status === "completed" || s.status === "completed_with_errors") {
        // iter-16.x — Do NOT stop polling if a compile-fix loop is still
        // actively iterating server-side. `transformation.status` stays
        // "completed"/"completed_with_errors" from the pipeline's earlier
        // pass while a "Rerun compile" mutates `compile_fix_progress` in
        // the background; stopping the poll here freezes the sidebar on
        // the pre-rerun state (Coder/Verifier/Tester falsely "Completed",
        // header pill green) even though the backend is still running.
        const cfpStillActive = isCompileFixActive(s.compile_fix_progress);
        if (!cfpStillActive) {
          stopPolling();
        }
        setResult(s.result || {});
        setStatus(s.status);
        broadcastTransformerPhase(s.status);
        syncSelectedAgentTab(s.phase, s.status, { force: true });
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          setFiles(Array.isArray(arr) ? arr : []);
        } catch {}
        try {
          const compResult = await getCompilationResult(tid);
          setCompilationResult(compResult);
        } catch {}
        try {
          const tlData = await getAgentTimeline(tid);
          setAgentTimeline(tlData?.timeline || []);
        } catch {}
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
      } else if (s.status === "failed") {
        stopPolling();
        setError(s.error || "Transformation failed");
        setStatus("failed");
        broadcastTransformerPhase("failed");
        syncSelectedAgentTab(s.phase, s.status, { force: true });
      } else if (s.status === "stopped") {
        stopPolling();
        setStatus("stopped");
        broadcastTransformerPhase("stopped");
        syncSelectedAgentTab(s.phase, s.status, { force: true });
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          setFiles(Array.isArray(arr) ? arr : []);
        } catch {}
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
      }
    } catch (e) {
      // transient poll errors are OK — keep the interval alive
    }
  }, [stopPolling, syncSelectedAgentTab]);

  // iter-15.14 — Pause / Resume / Stop cooperative controls
  const handlePause = async () => {
    if (!transformId) return;
    setBusyControl("pause");
    try {
      await pauseTransformation(transformId);
      setPaused(true);
    } catch (e) {
      alert(`Pause failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setBusyControl(null);
    }
  };
  const handleResume = async () => {
    if (!transformId) return;
    setBusyControl("resume");
    try {
      await resumeTransformation(transformId);
      setPaused(false);
    } catch (e) {
      alert(`Resume failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setBusyControl(null);
    }
  };
  const handleStop = async () => {
    if (!transformId) return;
    setBusyControl("stop");
    try {
      await stopTransformation(transformId);
      setStopped(true);
      setShowStopConfirm(false);
    } catch (e) {
      alert(`Stop failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setBusyControl(null);
    }
  };

  // iter-15.14 — Kebab menu: History + Remove
  // iter-16.x — Scope the history list to the active project so an
  // operator on `shpp_sikkim` doesn't see runs from CEOTS-GAP / CGHS
  // etc. Consistent with the mount-time hydration below.
  const openHistory = async () => {
    setShowKebab(false);
    setShowHistory(true);
    setHistoryLoading(true);
    try {
      const items = await listTransformations(
        active?.id ? { projectId: active.id } : {}
      );
      setHistoryItems(Array.isArray(items) ? items : []);
    } catch (e) {
      setHistoryItems([]);
    } finally {
      setHistoryLoading(false);
    }
  };
  const loadGithubConfig = useCallback(async () => {
    if (!active?.id) return null;
    try {
      return await getGithubConfig(active.id);
    } catch (_) {
      return null;
    }
  }, [active?.id]);
  const pushWithConfig = useCallback(async (cfg) => {
    if (!transformId || !cfg?.repo_url) return false;
    const fd = new FormData();
    fd.append("repo", normalizeGithubRepo(cfg.repo_url));
    fd.append("branch", cfg.branch || "main");
    fd.append("commit_message", "Transformed code from LAMA");
    return pushTransformationToGithub(transformId, fd);
  }, [transformId]);
  const openGithubPush = async () => {
    setShowKebab(false);
    if (!transformId) return;
    setPushLoading(true);
    try {
      let cfg = githubConfig;
      if (!cfg) {
        cfg = await loadGithubConfig();
        setGithubConfig(cfg);
      }
      if (cfg?.repo_url) {
        const res = await pushWithConfig(cfg);
        alert(`Pushed ${res?.files_pushed || 0} file(s) to ${res?.repo}#${res?.branch}`);
        return;
      }
      setGhForm((prev) => ({
        ...prev,
        repo: prev.repo,
        branch: prev.branch || "main",
        commit_message: prev.commit_message || "Transformed code from LAMA",
        path_prefix: prev.path_prefix || "",
      }));
      setShowGitHub(true);
    } finally {
      setPushLoading(false);
    }
  };
  const loadTransformationFromHistory = async (tid, opts = {}) => {
    // iter-16.x — `silent` skips the user-facing alert and rethrows so
    // the mount-time hydration effect can fall back to a project-scoped
    // backend lookup when a stale localStorage `lastId` no longer exists
    // (e.g. the transformation was deleted, or the id belongs to a
    // different browser profile / origin).
    const silent = !!opts.silent;
    if (!tid) return;
    setShowHistory(false);
    try {
      const t = await getTransformation(tid);
      userPinnedAgentTabRef.current = false;
      setTransformId(tid);
      setName(t.name || "");
      setDetectedStack(t.source_stack || {});
      setSelectedTransforms(t.transforms || {});
      // iter-15.40 — Restore persisted build-tool selection. A transform
      // loaded from history is by definition post-confirmation, so keep
      // the Run button unlocked without forcing a re-click.
      if (t.build_tools && Object.keys(t.build_tools).length > 0) {
        setSelectedBuildTools(t.build_tools);
        setBuildToolsConfirmed(true);
      }
      setStatus(t.status || null);
      setResult(t.result || null);
      setError(t.error || null);
      setPaused(!!t.paused);
      setStopped(!!t.stopped);
      lastFilesDoneRef.current = Number(t.files_done || 0);
      setKb(null);
      setSelectedFile(null);
      setSourceFiles([]);
      setEnvelopes([]);
      setTaskList(null);
      setCompilationResult(null);
      setSelectedEnvelope(null);
      // iter-16.x — Hydrate live-run fields from the persisted transformation
      // doc so a mount triggered by tab switch / login-logout / page reload
      // faithfully re-renders the pre-remount state instead of silently
      // dropping compile-fix / tester progress that the pipeline is still
      // actively writing. Without this, `compileFixProgress` stayed null
      // after remount, so `getAgentNodeStatus` fell through to the
      // `status === "completed_with_errors"` branch and painted
      // Coder/Verifier/Tester green ("Completed") even while the backend
      // was still running the fix loop — same reason the header pill read
      // "Completed" and the operator's "Rerun compile" click was rejected
      // with 409 "already in progress".
      setCompileFixProgress(t.compile_fix_progress || null);
      if (Array.isArray(t.compile_console)) {
        setCompileConsole({
          lines: t.compile_console,
          updatedAt: t.compile_console_updated_at || null,
        });
      } else {
        setCompileConsole({ lines: [], updatedAt: null });
      }
      setTesterProgress(t.tester_progress || null);
      syncSelectedAgentTab(t.phase, t.status, { force: true });
      // Load KB + files
      let loadedFiles = [];
      try {
        const kbData = await getTransformerKB(tid);
        setKb(kbData);
      } catch {}
      try {
        const genFiles = await getTransformationFiles(tid);
        const arr = genFiles?.files || genFiles || [];
        loadedFiles = Array.isArray(arr) ? arr : [];
        setFiles(loadedFiles);
      } catch {}
      // iter-15.27 — Reloading a transformation (whether via the History
      // panel or the auto-restore-on-mount effect below) used to only
      // rehydrate KB + generated files, silently dropping the
      // envelopes/agent-timeline/task-list/compilation panels that
      // `pollStatus()` populates live. That made a page refresh look like
      // "everything was lost" even though the backend still had the full
      // run recorded. Rehydrate every panel pollStatus would have filled
      // in, based on the persisted status.
      try {
        const tlData = await getAgentTimeline(tid);
        setAgentTimeline(tlData?.timeline || []);
      } catch {}
      if (t.status === "awaiting_confirmation") {
        try {
          const envData = await getTransformationEnvelopes(tid);
          setEnvelopes(envData?.envelopes || []);
        } catch {}
      }
      if (t.status === "awaiting_task_confirmation") {
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
      }
      if (t.status === "completed" || t.status === "completed_with_errors") {
        try {
          const compResult = await getCompilationResult(tid);
          setCompilationResult(compResult);
        } catch {}
        try {
          const taskData = await getTransformationTasks(tid);
          setTaskList(taskData);
        } catch {}
      }
      // Resume polling if still running
      stopPolling();
      // iter-16.x — Restart the 2s status poll not just when the pipeline
      // itself is running, but ALSO when a background compile-fix loop is
      // actively iterating on top of an already-"completed" pipeline (see
      // isCompileFixActive rationale above). Also re-arm the compile-loading
      // spinner so the Compile Console panel reads honestly as "in flight"
      // instead of "idle" during rehydration after a tab switch / relogin.
      const cfpActiveOnMount = isCompileFixActive(t.compile_fix_progress);
      if (t.status === "running" || t.status === "pending") {
        changeTab("kb");
        broadcastTransformerPhase(t.phase || "running");
        pollRef.current = setInterval(() => pollStatus(tid), 2000);
        pollStatus(tid);
      } else if (t.status === "awaiting_confirmation") {
        broadcastTransformerPhase("awaiting_confirmation");
        changeTab("kb");
      } else if (t.status === "awaiting_task_confirmation") {
        broadcastTransformerPhase("awaiting_task_confirmation");
      } else if (cfpActiveOnMount) {
        setCompilationLoading(true);
        broadcastTransformerPhase(t.phase || t.status || "compile_fix");
        changeTab("code");
        pollRef.current = setInterval(() => pollStatus(tid), 2000);
        pollStatus(tid);
      } else if (t.status === "completed" || t.status === "completed_with_errors" || loadedFiles.length > 0) {
        broadcastTransformerPhase(t.status || "completed");
        changeTab("code");
      } else {
        changeTab("input");
      }
    } catch (e) {
      if (silent) throw e;
      alert(`Failed to load transformation: ${e.response?.data?.detail || e.message}`);
    }
  };
  const handleRemoveCurrent = async () => {
    if (!transformId) return;
    try {
      await deleteTransformation(transformId);
      setShowRemoveConfirm(false);
      setShowKebab(false);
      broadcastTransformerPhase(null);
      reset();
    } catch (e) {
      alert(`Remove failed: ${e.response?.data?.detail || e.message}`);
    }
  };

  // Multi-agent: user confirms the discovered envelopes to proceed.
  // iter-15.41 — Uses the new `confirmTransformationTraceability` endpoint
  // so the audit log records that traceability was explicitly reviewed;
  // falls back to the legacy `confirmTransformationPlan` if the backend
  // is on an older revision that doesn't expose the new endpoint.
  const handleConfirmPlan = async () => {
    if (!transformId) return;
    setConfirming(true);
    try {
      try {
        await confirmTransformationTraceability(transformId);
      } catch (e) {
        if (e?.response?.status === 404) {
          await confirmTransformationPlan(transformId);
        } else { throw e; }
      }
      setStatus("running");
      setTraceability((prev) => prev
        ? { ...prev, confirmed_at: new Date().toISOString() }
        : prev);
      userPinnedAgentTabRef.current = false;
      syncSelectedAgentTab("planner", "running", { force: true });
      stopPolling();
      pollRef.current = setInterval(() => pollStatus(transformId), 2000);
      pollStatus(transformId);
    } catch (e) {
      alert(`Confirm failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setConfirming(false);
    }
  };

  const handleConfirmTasks = async () => {
    if (!transformId) return;
    setConfirmingTasks(true);
    try {
      await confirmTransformationTasks(transformId);
      setStatus("running");
      userPinnedAgentTabRef.current = false;
      syncSelectedAgentTab("coder", "running", { force: true });
      stopPolling();
      pollRef.current = setInterval(() => pollStatus(transformId), 2000);
      pollStatus(transformId);
    } catch (e) {
      alert(`Confirm failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setConfirmingTasks(false);
    }
  };

  // iter-15.38 — Planner/Coder/Tester chat assistant. Sends one turn to
  // `POST /tools/transformer/{id}/agent-chat`, appends the user + assistant
  // messages to that panel's conversation, and applies the server's
  // response immediately: refreshed task list always, refreshed generated
  // file list when the Coder panel's chat removed/regenerated a file.
  const handlePlanChatSend = async (panel) => {
    const draft = (planChat[panel]?.input || "").trim();
    if (!transformId || !draft || planChat[panel]?.busy) return;

    setPlanChat((prev) => ({
      ...prev,
      [panel]: {
        ...prev[panel],
        input: "",
        busy: true,
        messages: [...prev[panel].messages, { role: "user", text: draft }],
      },
    }));

    try {
      const res = await sendAgentPlanChat(transformId, panel, draft, regenModel || null);
      if (res?.tasks) setTaskList(res.tasks);
      if (panel === "coder" && (res?.actions_applied || []).some((a) => a.type === "remove" || a.type === "regenerate")) {
        try {
          const genFiles = await getTransformationFiles(transformId);
          const arr = genFiles?.files || genFiles || [];
          setFiles(Array.isArray(arr) ? arr : []);
        } catch {}
      }
      setPlanChat((prev) => ({
        ...prev,
        [panel]: {
          ...prev[panel],
          busy: false,
          messages: [...prev[panel].messages, {
            role: "assistant",
            text: res?.reply || "Done.",
            matchedCount: (res?.matched_ids || []).length,
          }],
        },
      }));
    } catch (e) {
      setPlanChat((prev) => ({
        ...prev,
        [panel]: {
          ...prev[panel],
          busy: false,
          messages: [...prev[panel].messages, {
            role: "assistant",
            text: `Sorry — that didn't go through: ${e.response?.data?.detail || e.message}`,
            isError: true,
          }],
        },
      }));
    }
  };

  const PLAN_CHAT_PLACEHOLDERS = {
    planner: "e.g. \"remove all test files from wave 2\", \"move the DAO classes to wave 3\"",
    coder: "e.g. \"remove the DAO files\", \"regenerate the failed controller files\"",
    tester: "e.g. \"find failed tests for OrderService\", \"dismiss the flaky verification result\"",
  };

  // Reusable across Planner review, Code Generation, and Verifier/Tester —
  // find/remove/reassign items in that panel's list using plain English
  // instead of clicking through every row.
  const renderPlanChatWidget = (panel) => {
    const state = planChat[panel] || { messages: [], input: "", busy: false };
    return (
      <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden" data-testid={`plan-chat-${panel}`}>
        <div className="px-4 py-3 border-b border-slate-100 flex items-center gap-2 bg-violet-50/60">
          <div className="w-7 h-7 rounded-full bg-violet-600 text-white flex items-center justify-center flex-shrink-0">
            <Bot size={14} />
          </div>
          <div>
            <h5 className="text-[12px] font-semibold text-slate-800">Ask the {panel === "planner" ? "Planner" : panel === "coder" ? "Coder" : "Verifier"} assistant</h5>
            <p className="text-[10px] text-slate-500">Find, remove, or reassign items with plain English — changes save instantly.</p>
          </div>
        </div>
        <div className="max-h-56 overflow-y-auto px-4 py-3 space-y-2 bg-slate-50/40">
          {state.messages.length === 0 ? (
            <p className="text-[11px] text-slate-400 italic">{PLAN_CHAT_PLACEHOLDERS[panel]}</p>
          ) : (
            state.messages.map((m, idx) => (
              <div key={idx} className={`flex items-start gap-2 ${m.role === "user" ? "justify-end" : ""}`}>
                {m.role !== "user" && (
                  <div className="w-6 h-6 rounded-full bg-violet-100 text-violet-700 flex items-center justify-center flex-shrink-0 mt-0.5">
                    <Bot size={12} />
                  </div>
                )}
                <div className={`max-w-[85%] rounded-xl px-3 py-1.5 text-[11px] ${
                  m.role === "user"
                    ? "bg-violet-600 text-white"
                    : m.isError
                      ? "bg-red-50 text-red-700 border border-red-200"
                      : "bg-white text-slate-700 border border-slate-200"
                }`}>
                  {m.text}
                  {typeof m.matchedCount === "number" && m.matchedCount > 0 && (
                    <span className="block mt-0.5 text-[10px] opacity-70">{m.matchedCount} item{m.matchedCount === 1 ? "" : "s"} matched</span>
                  )}
                </div>
              </div>
            ))
          )}
          {state.busy && (
            <div className="flex items-center gap-2 text-[11px] text-slate-400">
              <Loader2 size={12} className="animate-spin" /> thinking…
            </div>
          )}
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); handlePlanChatSend(panel); }}
          className="px-3 py-2 border-t border-slate-100 flex items-center gap-2"
        >
          <MessageSquare size={13} className="text-slate-400 flex-shrink-0" />
          <input
            type="text"
            value={state.input}
            onChange={(e) => setPlanChat((prev) => ({ ...prev, [panel]: { ...prev[panel], input: e.target.value } }))}
            placeholder={PLAN_CHAT_PLACEHOLDERS[panel]}
            disabled={state.busy}
            className="flex-1 text-[11px] px-2 py-1.5 rounded-lg border border-slate-200 focus:outline-none focus:ring-1 focus:ring-violet-400 disabled:opacity-60"
            data-testid={`plan-chat-${panel}-input`}
          />
          <button
            type="submit"
            disabled={state.busy || !state.input.trim()}
            className="p-1.5 rounded-lg bg-violet-600 text-white disabled:opacity-40 disabled:cursor-not-allowed hover:bg-violet-700"
            data-testid={`plan-chat-${panel}-send`}
            aria-label="Send"
          >
            <Send size={13} />
          </button>
        </form>
      </div>
    );
  };

  // iter-15.19 — Refresh the "overridden" badge flags shown on each Agent
  // Pipeline node (from the list endpoint) whenever the timeline updates
  // or an override is saved/reset/rerun.
  const refreshAgentOverrideFlags = useCallback(async (tid) => {
    if (!tid) return;
    try {
      const res = await listTransformerAgentConfigs(tid);
      const flags = {};
      (res?.agents || []).forEach((a) => { flags[a.agent] = !!a.is_overridden; });
      setAgentOverrideFlags(flags);
    } catch {}
  }, []);

  useEffect(() => {
    if (transformId && agentTimeline.length > 0) {
      refreshAgentOverrideFlags(transformId);
    }
  }, [transformId, agentTimeline.length, refreshAgentOverrideFlags]);

  // iter-15.19 — Agent Pipeline config panel: click an agent node to open,
  // fetch its effective (override-aware) prompt + model for THIS
  // transformation, let the user edit/save/reset, then optionally rerun.
  const openAgentConfig = async (agent) => {
    if (!transformId) return;
    setAgentConfigAgent(agent);
    setAgentConfigData(null);
    setAgentConfigError(null);
    setAgentConfigLoading(true);
    try {
      const cfg = await getTransformerAgentConfig(transformId, agent);
      setAgentConfigData(cfg);
      setAgentConfigDraftPrompt(cfg.override_template || cfg.base_template || "");
      setAgentConfigDraftModel(cfg.override_model || "");
    } catch (e) {
      setAgentConfigError(e.response?.data?.detail || e.message || "Failed to load agent config");
    } finally {
      setAgentConfigLoading(false);
    }
  };

  const closeAgentConfig = () => {
    setAgentConfigAgent(null);
    setAgentConfigData(null);
    setAgentConfigError(null);
  };

  const handleSaveAgentConfig = async () => {
    if (!transformId || !agentConfigAgent || !agentConfigData) return;
    setAgentConfigSaving(true);
    setAgentConfigError(null);
    try {
      // Only persist an override if the draft actually differs from the
      // shared default — otherwise clear it (empty strings => "no override").
      const promptOverride = agentConfigDraftPrompt.trim() !== (agentConfigData.base_template || "").trim()
        ? agentConfigDraftPrompt
        : "";
      const modelOverride = agentConfigDraftModel.trim();
      const updated = await saveTransformerAgentConfig(transformId, agentConfigAgent, {
        prompt_template: promptOverride,
        model: modelOverride,
      });
      setAgentConfigData(updated);
      setAgentConfigDraftPrompt(updated.override_template || updated.base_template || "");
      setAgentConfigDraftModel(updated.override_model || "");
      refreshAgentOverrideFlags(transformId);
    } catch (e) {
      setAgentConfigError(e.response?.data?.detail || e.message || "Save failed");
    } finally {
      setAgentConfigSaving(false);
    }
  };

  const handleResetAgentConfig = async () => {
    if (!transformId || !agentConfigAgent) return;
    setAgentConfigSaving(true);
    setAgentConfigError(null);
    try {
      const updated = await saveTransformerAgentConfig(transformId, agentConfigAgent, {
        prompt_template: "", model: "",
      });
      setAgentConfigData(updated);
      setAgentConfigDraftPrompt(updated.base_template || "");
      setAgentConfigDraftModel("");
      refreshAgentOverrideFlags(transformId);
    } catch (e) {
      setAgentConfigError(e.response?.data?.detail || e.message || "Reset failed");
    } finally {
      setAgentConfigSaving(false);
    }
  };

  const handleRerunPipeline = async () => {
    if (!transformId) return;
    if (!window.confirm(
      "Rerun the whole pipeline from Super Agent → Context Manager onward? " +
      "This clears the current envelopes, tasks, generated files, and agent history for this transformation."
    )) return;
    setRerunning(true);
    setAgentConfigError(null);
    try {
      await rerunTransformerPipeline(transformId);
      closeAgentConfig();
      // Reset local view state so the UI reflects a fresh run, then resume polling.
      userPinnedAgentTabRef.current = false;
      setEnvelopes([]);
      setSelectedEnvelope(null);
      setAgentTimeline([]);
      setTaskList(null);
      setCompilationResult(null);
      setFiles([]);
      setResult(null);
      setError(null);
      lastFilesDoneRef.current = 0;
      setStatus("running");
      setProgress({ phase: "super_agent", percent: 2, message: "Super Agent: Initializing pipeline (rerun)..." });
      syncSelectedAgentTab("super_agent", "running", { force: true });
      stopPolling();
      pollRef.current = setInterval(() => pollStatus(transformId), 2000);
      pollStatus(transformId);
    } catch (e) {
      setAgentConfigError(e.response?.data?.detail || e.message || "Rerun failed");
    } finally {
      setRerunning(false);
    }
  };

  // iter-15.7x — Compile Console toolbar actions: copy the full log to the
  // clipboard (with a brief "Copied" confirmation) and clear the on-screen
  // log (client-side only — it just resets what's rendered; the next poll
  // tick or "Rerun compile" repopulates it from the backend).
  const handleCopyConsoleLog = useCallback(async () => {
    const text = (compileConsole.lines || []).join("\n");
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for browsers/contexts without Clipboard API access.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* no-op */ }
      document.body.removeChild(ta);
    }
    setConsoleCopied(true);
    setTimeout(() => setConsoleCopied(false), 1500);
  }, [compileConsole.lines]);

  const handleClearConsoleLog = useCallback(() => {
    setCompileConsole({ lines: [], updatedAt: null });
  }, []);

  // Trigger compilation analysis
  const handleRunCompilation = async () => {
    if (!transformId) return;
    setCompilationLoading(true);
    setCompileFixProgress({ phase: "starting", message: "Kicking off compile…" });
    setCompileConsole({ lines: ["Kicking off compile…"], updatedAt: null });
    try {
      await runCompilationAnalysis(transformId);
      // iter-15.58 — Compile-fix loop can iterate up to 3x compile-runs.
      // Poll every 2s and consider the phase "done" when compile_fix_progress
      // reaches a terminal phase OR compilation_result lands.
      const started = Date.now();
      // iter-15.68 — Was a hard 20-minute client cap that silently marked
      // the UI "done" (compilationLoading=false) even while the backend
      // fix-loop was still genuinely iterating — live-observed to run
      // 45+ minutes on a real multi-file compile-fix pass (each failing
      // file needs a real Planner+Coder LLM round-trip, and a big dump
      // can have a dozen+ files across multiple recompile rounds). This
      // is very likely why coder/verifier looked "stuck running" while
      // the backend had actually kept working — the FE just stopped
      // watching. The real backstop for a truly-dead run is the
      // server-side `compile_fix_stalled` flag (fires when the
      // background job goes silent, e.g. a container restart killed it
      // mid-run) — that's authoritative, not a wall-clock guess. Keep a
      // generous outer cap only as a last-resort safety net.
      const TIMEOUT_MS = 120 * 60 * 1000; // 2-hour outer safety net
      const poll = setInterval(async () => {
        try {
          const s = await getTransformationStatus(transformId);
          if (s.compile_fix_progress) setCompileFixProgress(s.compile_fix_progress);
          // iter-15.6x — live console lines, streamed as the real build runs.
          if (Array.isArray(s.compile_console)) {
            setCompileConsole({ lines: s.compile_console, updatedAt: s.compile_console_updated_at || null });
          }
          if (s.compilation_result) {
            setCompilationResult(s.compilation_result);
            const terminal = ["passed", "exhausted", "unfixable", "infra_blocked", "stagnant"].includes(
              (s.compile_fix_progress || {}).phase,
            );
            if (terminal || s.compilation_result.compilation_ready) {
              clearInterval(poll);
              setCompilationLoading(false);
            }
          }
          // iter-15.62.9 — Backend flags a compile-fix run as "stalled"
          // when its background job has gone silent for 5+ minutes with
          // NO heartbeat at all (neither the per-file "fixing"/"verifying"
          // touch nor a compile_console update) — most likely a server
          // restart killed the background task, though a hung/unresponsive
          // LLM provider call can also trigger it. We don't have to sit
          // through the full 2-hour client-side timeout looking stuck.
          if (s.compile_fix_stalled) {
            clearInterval(poll);
            setCompilationLoading(false);
            setCompileFixProgress({
              phase: "stalled",
              message: "This compile run stopped responding for over 5 minutes "
                + "(likely a server restart, or the LLM provider hung) — click "
                + "Rerun compile to try again.",
            });
          }
        } catch {}
        if (Date.now() - started > TIMEOUT_MS) {
          clearInterval(poll);
          setCompilationLoading(false);
        }
      }, 2000);
    } catch (e) {
      alert(`Compilation analysis failed: ${e.response?.data?.detail || e.message}`);
      setCompilationLoading(false);
      setCompileFixProgress(null);
    }
  };

  const handleFilesChange = async (newFiles) => {
    const allFiles = [...sourceFiles, ...Array.from(newFiles)];
    setSourceFiles(allFiles);

    if (allFiles.length > 0) {
      setAnalyzing(true);
      try {
        const fd = new FormData();
        allFiles.forEach(f => fd.append("files", f));
        const res = await analyzeSourceStack(fd);
        setDetectedStack(res?.detected_stack || {});
      } catch (e) {
        console.error("Stack analysis failed:", e);
        setDetectedStack({ backend: "helidon", frontend: "angular-16", database: "oracle", runtime: "java-17" });
      } finally {
        setAnalyzing(false);
      }
    }
  };

  // Drag & Drop handlers
  const handleDragOver = (e) => { e.preventDefault(); setIsDragging(true); };
  const handleDragLeave = (e) => { e.preventDefault(); setIsDragging(false); };
  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    handleFilesChange(e.dataTransfer.files);
  };

  // iter-15.54 — Explicit re-analyse action for the redesigned Step 2 card.
  // Mirrors the auto-analysis in handleFilesChange without mutating the
  // uploaded file set, so the operator can re-run KB detection on demand.
  const handleReanalyse = async () => {
    if (sourceFiles.length === 0) return;
    setAnalyzing(true);
    try {
      const fd = new FormData();
      sourceFiles.forEach(f => fd.append("files", f));
      const res = await analyzeSourceStack(fd);
      setDetectedStack(res?.detected_stack || {});
    } catch (e) {
      console.error("Stack analysis failed:", e);
      setDetectedStack({ backend: "helidon", frontend: "angular-16", database: "oracle", runtime: "java-17" });
    } finally {
      setAnalyzing(false);
    }
  };

  // iter-15.54 — Copy the current pipeline error to the clipboard (error banner).
  const copyError = () => {
    try { navigator.clipboard?.writeText(String(error || "")); } catch (_) { /* noop */ }
  };

  const handleTransformChange = (cat, val) => {
    setSelectedTransforms(prev => ({ ...prev, [cat]: prev[cat] === val ? null : val }));
    // iter-15.40 — Target selection changed → any prior build-tool
    // confirmation is invalidated; operator must re-confirm before
    // "Run Multi-Agent Transformation" re-enables.
    setBuildToolsConfirmed(false);
  };

  // iter-15.40 — When the active target-stack components change, fetch
  // the candidate build tools per component and default-pick the first
  // suggestion. The user must EXPLICITLY confirm (via the "Confirm Build
  // System" button) before the Run button re-enables — matches the
  // typed-confirmation freeze-gate pattern used elsewhere in LAMA.
  useEffect(() => {
    let cancelled = false;
    const params = {
      backend: selectedTransforms.backend || undefined,
      frontend: selectedTransforms.frontend || undefined,
      runtime: selectedTransforms.runtime || undefined,
      database: selectedTransforms.database || undefined,
    };
    if (!Object.values(params).some(Boolean)) {
      setBuildToolOptions({});
      setSelectedBuildTools({});
      return;
    }
    (async () => {
      try {
        const res = await suggestBuildTools(params);
        if (cancelled) return;
        const opts = res?.suggestions || {};
        setBuildToolOptions(opts);
        setSelectedBuildTools((prev) => {
          const next = {};
          Object.keys(opts).forEach((component) => {
            const existing = prev[component];
            next[component] = (existing && opts[component].includes(existing))
              ? existing
              : opts[component][0];
          });
          return next;
        });
      } catch {
        if (!cancelled) {
          setBuildToolOptions({});
          setSelectedBuildTools({});
        }
      }
    })();
    return () => { cancelled = true; };
  }, [selectedTransforms]);

  const handleCreate = async () => {
    if (!name.trim()) return alert("Please enter a name");
    if (sourceFiles.length === 0) return alert("Please upload source files");
    const activeTransforms = Object.entries(selectedTransforms).filter(([_, v]) => v);
    if (activeTransforms.length === 0) return alert("Please select at least one transformation");

    setCreating(true);
    setStatus("running");
    setResult(null);
    setError(null);
    setFiles([]);
    setKb(null);
    setSelectedFile(null);
    setEnvelopes([]);
    setTraceability(null);
    setSelectedEnvelope(null);
    setAgentTimeline([]);
    setTaskList(null);
    setCompilationResult(null);
    userPinnedAgentTabRef.current = false;
    syncSelectedAgentTab("super_agent", "running", { force: true });
    setProgress({ phase: "uploading", percent: 0, message: "Preparing upload..." });

    try {
      const fd = new FormData();
      fd.append("name", name);
      fd.append("detected_stack", JSON.stringify(detectedStack || {}));
      fd.append("transforms", JSON.stringify(selectedTransforms));
      // iter-15.40 — Per-component build tool (maven/gradle/npm/...).
      fd.append("build_tool", JSON.stringify(selectedBuildTools || {}));
      // iter-15.51 — Anchor the transformation to the currently-active
      // LAMA project so backend fabric_call routes go through the
      // per-project Factory (Droid) config from Console.
      if (active?.id) fd.append("project_id", active.id);
      sourceFiles.forEach(f => fd.append("source_files", f));

      const created = await createTransformation(fd, { onProgress: (p) => setProgress(p) });
      const tid = created?._id;
      if (!tid) throw new Error("Server did not return a transformation id");
      setTransformId(tid);
      setProgress({ phase: "queued", percent: 5, message: "Queued — starting transformation..." });
      lastFilesDoneRef.current = 0;
      changeTab("code");

      // Fire-and-poll — multi-agent pipeline
      if (pipelineMode === "multi_agent") {
        await runMultiAgentTransformation(tid);
      } else {
        await runTransformation(tid);
      }
      stopPolling();
      pollRef.current = setInterval(() => pollStatus(tid), 2000);
      pollStatus(tid); // immediate first tick
    } catch (e) {
      setError(e.message || String(e));
      setStatus("failed");
      stopPolling();
      changeTab("input");
    } finally {
      setCreating(false);
    }
  };

  const loadFileContent = useCallback(async (fileMeta) => {
    if (!transformId) return;
    setSelectedFile({ ...fileMeta, content: "", loading: true });
    try {
      const full = await getTransformationFile(transformId, fileMeta.id);
      setSelectedFile({
        id: fileMeta.id,
        path: full.path || fileMeta.path,
        original_path: full.original_path,
        confidence: full.confidence ?? fileMeta.confidence,
        content: full.content || "",
        loading: false,
      });
    } catch (e) {
      setSelectedFile({ ...fileMeta, content: `// Failed to load: ${e.message}`, loading: false });
    }
  }, [transformId]);

  const handleRegenerate = async (fileMeta) => {
    if (!transformId || !fileMeta?.id) return;
    setRegeneratingIds(prev => ({ ...prev, [fileMeta.id]: true }));
    try {
      await regenerateTransformationFile(transformId, fileMeta.id, regenModel || null);
      // Refresh file list + reopen the regenerated file
      const genFiles = await getTransformationFiles(transformId);
      const arr = genFiles?.files || genFiles || [];
      setFiles(Array.isArray(arr) ? arr : []);
      const refreshed = (arr || []).find(f => f.id === fileMeta.id);
      if (refreshed) loadFileContent(refreshed);
    } catch (e) {
      alert(`Regeneration failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setRegeneratingIds(prev => { const n = { ...prev }; delete n[fileMeta.id]; return n; });
    }
  };

  const handleGitPush = async () => {
    if (!transformId) return;
    if (!ghForm.repo.trim()) return alert("Please enter GitHub repo (owner/repo)");
    setPushing(true);
    try {
      const fd = new FormData();
      fd.append("repo", ghForm.repo);
      fd.append("branch", ghForm.branch || "main");
      fd.append("commit_message", ghForm.commit_message || "Transformed code from LAMA");
      if (ghForm.path_prefix) fd.append("path_prefix", ghForm.path_prefix);
      const res = await pushTransformationToGithub(transformId, fd);
      alert(`Pushed ${res?.files_pushed || 0} file(s) to ${res?.repo}#${res?.branch}`);
      setShowGitHub(false);
    } catch (e) {
      alert(`GitHub push failed: ${e.response?.data?.detail || e.message}`);
    } finally {
      setPushing(false);
    }
  };

  const reset = () => {
    stopPolling();
    userPinnedAgentTabRef.current = false;
    setSourceFiles([]);
    setName("");
    setDetectedStack(null);
    setSelectedTransforms({});
    setBuildToolOptions({});
    setSelectedBuildTools({});
    setBuildToolsConfirmed(false);
    setStatus(null);
    setResult(null);
    setFiles([]);
    setError(null);
    setTransformId(null);
    setKb(null);
    setSelectedKbFile(null);
    setSelectedFile(null);
    setPaused(false);
    setStopped(false);
    setProgress({ phase: null, percent: 0, message: "" });
    setEnvelopes([]);
    setSelectedEnvelope(null);
    setAgentTimeline([]);
    setTaskList(null);
    setCompilationResult(null);
    lastFilesDoneRef.current = 0;
    broadcastTransformerPhase(null);
    setSelectedAgentTab("super_agent");
    setTaskPages({});
    // iter-15.27 — Explicitly drop the persisted "last open transformation"
    // pointer so a deliberate reset/remove doesn't get silently
    // auto-restored the next time this page mounts.
    try {
      window.localStorage.removeItem(`lama:transformer:lastId:${active?.id || "default"}`);
    } catch (_) {}
    changeTab("input");
  };

  const hasSelections = Object.values(selectedTransforms).some(v => v);
  const [collapsedDirs, setCollapsedDirs] = useState({});
  const toggleDir = useCallback((dirPath) => {
    setCollapsedDirs((prev) => {
      const next = { ...prev };
      if (next[dirPath]) delete next[dirPath];
      else next[dirPath] = true;
      return next;
    });
  }, []);
  const structureRows = useMemo(() => buildStructureRows(files), [files]);
  const sourceStructureRows = useMemo(() => buildSourceStructureRows(sourceFiles), [sourceFiles]);
  const activeStructureRows = useMemo(
    () => filterCollapsedRows(
      files.length > 0 ? structureRows : sourceStructureRows,
      collapsedDirs,
    ),
    [files.length, structureRows, sourceStructureRows, collapsedDirs],
  );
  const kbFileRows = useMemo(() => buildKbFileRows(kb), [kb]);

  useEffect(() => {
    if (!kbFileRows.length) return;
    if (!selectedKbFile || !kbFileRows.some((row) => row.path === selectedKbFile.path)) {
      setSelectedKbFile(kbFileRows[0]);
    }
  }, [kbFileRows, selectedKbFile]);

  useEffect(() => {
    if (files.length > 0) {
      if (selectedFile?.id && files.some((f) => f.id === selectedFile.id)) return;
      const latest = files[files.length - 1];
      if (latest && latest.id) {
        loadFileContent(latest);
      }
      return;
    }
    if (!sourceStructureRows.length) return;
    if (selectedFile?.sourceOnly && sourceStructureRows.some((r) => r.id === selectedFile.id)) return;
    const firstSource = sourceStructureRows.find((r) => r.type === "file");
    if (firstSource) {
      setSelectedFile({
        id: firstSource.id,
        path: firstSource.path,
        content: `// Waiting for generated output\n// Source file: ${firstSource.path}\n`,
        loading: false,
        confidence: null,
        sourceOnly: true,
      });
    }
  }, [files, loadFileContent, selectedFile?.id, selectedFile?.sourceOnly, sourceStructureRows]);

  // iter-15.10 — only show target-stack categories that were actually detected
  // in the uploaded source. If the user uploaded only a React FE, don't ask
  // them to pick a backend / database target.
  const categoryKeysToShow = Object.keys(TECH_CATEGORIES).filter((catKey) => {
    const detected = detectedStack?.[catKey];
    if (!detected) return false;
    const val = String(detected).toLowerCase();
    return val && val !== "unknown" && val !== "none" && val !== "n/a";
  });

  const showAgentWorkspace = !!transformId && ["pending", "running", "awaiting_confirmation", "awaiting_task_confirmation", "completed", "stopped", "failed"].includes(status || "");
  const selectedAgentMeta = AGENT_META[selectedAgentTab] || AGENT_META.super_agent;
  const taskStats = taskList?.stats || {};
  const taskWaves = Array.isArray(taskList?.waves) ? taskList.waves : [];
  const allTasks = Array.isArray(taskList?.tasks)
    ? taskList.tasks
    : taskWaves.flatMap((wave) => (Array.isArray(wave?.tasks) ? wave.tasks : []));
  const currentAgentFromStatus = getAgentTabFromRunState(progress.phase, status);

  const verifierRows = useMemo(() => (
    allTasks.map((task, idx) => {
      const checks = task?.verifier_checks || {};
      const matchingFile = files.find((file) =>
        (file?.path || "") === (task?.target_path || "") ||
        (file?.original_path || "") === (task?.source_path || "")
      );
      const rawConfidence = task?.verifier_score ?? checks?.confidence ?? matchingFile?.confidence ?? task?.confidence;
      const issueCount = Array.isArray(checks?.issues)
        ? checks.issues.length
        : Array.isArray(checks?.checks)
          ? checks.checks.filter((item) => String(item?.status || "").toUpperCase() === "FAIL").length
          : 0;
      return {
        id: task?.task_id || `${task?.target_path || task?.source_path || "task"}-${idx}`,
        path: task?.target_path || matchingFile?.path || task?.source_path || "—",
        sourcePath: task?.source_path || "",
        confidence: typeof rawConfidence === "number" ? (rawConfidence > 1 ? rawConfidence : rawConfidence * 100) : null,
        verdict: toSafeText(checks?.verdict || (task?.status === "VERIFIED" ? "PASS" : task?.status || "PENDING")),
        issueCount,
        summary: toSafeText(checks?.summary || task?.error || ""),
        status: task?.status || "PENDING",
      };
    })
  ), [allTasks, files]);

  // iter-15.35 — Verification Console pagination (matches the "Discovered
  // Architecture" pattern: 10 rows/page, reset to page 1 whenever the
  // underlying row set changes size).
  useEffect(() => { setVerifierPage(1); }, [verifierRows.length]);
  const totalVerifierPages = Math.max(1, Math.ceil(verifierRows.length / VERIFIER_PER_PAGE));
  const pagedVerifierRows = useMemo(
    () => verifierRows.slice((verifierPage - 1) * VERIFIER_PER_PAGE, verifierPage * VERIFIER_PER_PAGE),
    [verifierRows, verifierPage]
  );

  const formatConfidenceBadge = (value) => {
    if (typeof value !== "number") {
      return "bg-slate-100 text-slate-600";
    }
    // iter-16.x — the backend now only marks a file VERIFIED/compilable
    // at >= 95% confidence (operator-requested hard gate — see
    // VERIFIER_ACCEPT_THRESHOLD in tools.py). Keep this badge's "green"
    // cutoff in sync so a green pill always means "actually passing",
    // not just "reasonably high".
    return value >= 95
      ? "bg-emerald-100 text-emerald-700"
      : value >= 80
        ? "bg-amber-100 text-amber-700"
        : "bg-red-100 text-red-700";
  };

  const getTaskActionBadge = (action) => (
    action === "TRANSFORM" ? "bg-blue-50 text-blue-700 border-blue-200" :
    action === "REWRITE" ? "bg-amber-50 text-amber-700 border-amber-200" :
    action === "DELETE" ? "bg-red-50 text-red-700 border-red-200" :
    action === "NO_CHANGE" ? "bg-slate-100 text-slate-700 border-slate-200" :
    "bg-emerald-50 text-emerald-700 border-emerald-200"
  );

  const getTaskStatusBadge = (taskStatus) => {
    const normalized = String(taskStatus || "PENDING").toUpperCase();
    return normalized === "DONE" ? "bg-emerald-100 text-emerald-700 border-emerald-200" :
      normalized === "VERIFIED" ? "bg-sky-100 text-sky-700 border-sky-200" :
      normalized === "CODED" ? "bg-violet-100 text-violet-700 border-violet-200" :
      normalized === "IN_PROGRESS" ? "bg-amber-100 text-amber-700 border-amber-200" :
      normalized === "APPROVED" ? "bg-blue-100 text-blue-700 border-blue-200" :
      normalized === "BLOCKED" ? "bg-red-100 text-red-700 border-red-200" :
      "bg-slate-100 text-slate-700 border-slate-200";
  };

  const getAgentNodeStatus = (agent) => {
    const runs = agentTimeline.filter((row) => row.agent === agent);
    const latest = runs[runs.length - 1];
    const latestStatus = String(latest?.status || "").toLowerCase();
    if (status === "awaiting_confirmation" && agent === "context_manager") return "awaiting";
    if (status === "awaiting_task_confirmation" && agent === "planner") return "awaiting";
    // iter-16.x — Live compile-fix loop guard. A compile-fix loop can be
    // actively iterating (phase "fixing"/"verifying"/"escalated_devops"/
    // etc., see CFP_TERMINAL_PHASES below) even when the overall run
    // `status` already reads "completed"/"completed_with_errors" from an
    // earlier pass — e.g. an operator-triggered "Rerun compile" after the
    // pipeline first finished. In that window the live Console can still
    // be streaming a `BUILD FAILURE`, so Coder/Verifier/Tester must NOT
    // show a false-green "Completed" badge — they must read as actively
    // "running" until the loop reaches a terminal phase. Without this,
    // the sidebar and the live console visibly contradicted each other.
    const cfpActive = isCompileFixActive(compileFixProgress);
    if (cfpActive && (agent === "coder" || agent === "verifier" || agent === "tester")) {
      return "running";
    }
    // iter-15.70 — The DevOps Expert node only ever "runs" as an
    // escalation mid-compile-fix-loop (see `_run_compile_fix_loop`'s
    // `devops_escalated` state on the backend) — there's no dedicated
    // `agent_run` timeline row for it, so it has to be read directly off
    // `compile_fix_progress.acting_agent`/`phase` instead of the generic
    // timeline-based logic below.
    if (agent === "devops_expert") {
      const cfp = compileFixProgress;
      if (cfp?.acting_agent === "devops_expert" || cfp?.phase === "escalated_devops") {
        if (["passed", "stagnant", "exhausted"].includes(cfp?.phase)) return "completed";
        return "running";
      }
      if (status === "completed_with_errors") return "failed";
      if (status === "completed" || status === "stopped") return "pending";
      return "pending";
    }
    // iter-16.x — A transformation that has finished (`status ===
    // "completed"`) means every upstream agent necessarily finished too,
    // even though its LAST logged `agent_run` row may still say
    // "running" (e.g. an orphan-recovered run that resumed through the
    // legacy single-file worker, which never re-logs Coder/Verifier
    // agent_run completions). Without this short-circuit, Coder/Verifier
    // nodes stayed stuck on "Running" forever after the pipeline had
    // already completed. Check this BEFORE the stale per-agent
    // timeline status so a truly-finished run always wins.
    // iter-15.70 — `completed_with_errors` (compile-fix loop finished but
    // the build never went green) is ALSO a terminal "everything ran"
    // state for every node EXCEPT Tester, which must show the honest
    // "failed" tone rather than a false-green checkmark.
    if (status === "completed") return "completed";
    if (status === "completed_with_errors") return agent === "tester" ? "failed" : "completed";
    // iter-16.x — Live-pipeline wins over stale per-file timeline row.
    // Coder and Verifier iterate PER FILE: each file logs an `agent_run`
    // that flips running → completed. Between "Verifier completed for
    // file N" and "Coder starting for file N+1" (a window of hundreds of
    // ms to several seconds), BOTH latest rows read "completed" while
    // the pipeline is genuinely still hammering through 100+ more files.
    // Without this guard the sidebar reads Coder / Verifier as
    // "Completed" mid-run even though `progress.filesDone < filesTotal`
    // and `status === "running"`, contradicting the header progress bar
    // ("Code generation running · 65% · file 211/311"). Trust the live
    // pipeline state instead: as long as the run is progressing files,
    // Coder & Verifier must render "running".
    if (
      status === "running"
      && (agent === "coder" || agent === "verifier")
      && typeof progress?.filesTotal === "number"
      && progress.filesTotal > 0
      && (Number(progress.filesDone) || 0) < progress.filesTotal
      && latestStatus !== "failed"
    ) {
      return "running";
    }
    if (latestStatus === "completed" || latestStatus === "done") return "completed";
    if (latestStatus === "failed") return "failed";
    if (latestStatus === "running" || latestStatus === "in_progress") return "running";
    const currentIndex = AGENT_ORDER.indexOf(currentAgentFromStatus);
    const agentIndex = AGENT_ORDER.indexOf(agent);
    if ((status === "running" || status === "failed" || status === "stopped") && agentIndex > -1 && currentIndex > -1) {
      if (agentIndex < currentIndex) return "completed";
      if (agentIndex === currentIndex) return status === "failed" ? "failed" : status === "stopped" ? "stopped" : "running";
    }
    return "pending";
  };

  const renderKbSignalPanel = () => (
    <>
      {kb && kb.stats ? (
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
            <Database size={16} className="text-violet-500" />
            <div>
              <h4 className="text-sm font-semibold text-slate-800">Knowledge Base Signal</h4>
              <p className="text-[11px] text-slate-500">Live KB context reused across the downstream agent pipeline.</p>
            </div>
          </div>
          {/* iter-15.33 — API Surface preview. The Context Manager tab owns
              the full "Discovered Architecture" table (renderEnvelopeTable,
              paginated, click-through to the envelope detail modal); this
              is a compact read-only preview so the Super Agent overview
              leads with real API details/endpoints instead of only
              generic entity/file counts. */}
          {envelopes.length > 0 && (
            <div className="border-b border-slate-100">
              <div className="px-5 pt-3 pb-1 flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wide text-slate-500 font-semibold">
                  API Surface ({envelopes.length})
                </span>
                <button
                  onClick={() => setSelectedAgentTab("context_manager")}
                  className="text-[10px] font-semibold text-violet-600 hover:text-violet-800"
                >
                  View full list →
                </button>
              </div>
              <div className="max-h-[160px] overflow-y-auto divide-y divide-slate-100">
                {envelopes.slice(0, 8).map((env, idx) => (
                  <button
                    key={env.envelope_id || idx}
                    onClick={() => setSelectedEnvelope(env)}
                    className="w-full text-left px-5 py-2 hover:bg-violet-50/60 transition-colors flex items-center gap-2"
                  >
                    <span className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold uppercase flex-shrink-0 ${
                      env.endpoint_method === "GET" ? "bg-blue-100 text-blue-700" :
                      env.endpoint_method === "POST" ? "bg-green-100 text-green-700" :
                      env.endpoint_method === "PUT" ? "bg-amber-100 text-amber-700" :
                      env.endpoint_method === "DELETE" ? "bg-red-100 text-red-700" :
                      "bg-slate-100 text-slate-700"
                    }`}>
                      {env.endpoint_method || env.layer || "INFRA"}
                    </span>
                    <span className="font-mono text-[11px] text-slate-800 truncate">{env.endpoint_path || env.controller_class || "Infrastructure"}</span>
                  </button>
                ))}
                {envelopes.length > 8 && (
                  <div className="px-5 py-2 text-[10px] text-slate-500">+{envelopes.length - 8} more — view full list for details.</div>
                )}
              </div>
            </div>
          )}
          <div className="px-5 py-4 grid grid-cols-2 xl:grid-cols-5 gap-3">
            {[
              { label: "Entities", value: kb.stats?.entities ?? 0, bg: "bg-violet-50/60 border-violet-100", tx: "text-violet-700" },
              { label: "API Routes", value: kb.stats?.api_routes ?? 0, bg: "bg-blue-50/60 border-blue-100", tx: "text-blue-700" },
              { label: "DB Tables", value: kb.stats?.db_tables ?? 0, bg: "bg-emerald-50/60 border-emerald-100", tx: "text-emerald-700" },
              { label: "UI Files", value: kb.stats?.ui_files ?? 0, bg: "bg-amber-50/60 border-amber-100", tx: "text-amber-700" },
              { label: "Resolved Chains", value: kb.stats?.resolved_chains ?? 0, bg: "bg-green-50/60 border-green-100", tx: "text-green-700" },
            ].map((item) => (
              <div key={item.label} className={`rounded-xl border ${item.bg} px-3 py-2.5`}>
                <div className="text-[10px] uppercase tracking-wide text-slate-500">{item.label}</div>
                <div className={`text-lg font-bold ${item.tx} tabular-nums`}>{item.value}</div>
              </div>
            ))}
          </div>
          {kb.stats?.unresolved_chains > 0 && (
            <div className="px-5 py-2 border-t border-slate-100 bg-amber-50/70 text-[11px] text-amber-700 flex items-center gap-1.5">
              <AlertTriangle size={12} />
              {kb.stats.unresolved_chains} cross-file reference(s) remain unresolved.
            </div>
          )}
          <div className="border-t border-slate-100 px-5 py-4">
            <div className="grid xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)] gap-4">
              <div className="rounded-xl border border-slate-200 overflow-hidden">
                <div className="grid grid-cols-[minmax(0,1fr)_88px_70px_72px_88px] gap-2 px-3 py-2 bg-slate-50/80 border-b border-slate-200 text-[10px] uppercase tracking-wide text-slate-500 font-semibold">
                  <span>File</span>
                  <span>Type</span>
                  <span className="text-right">Size</span>
                  <span className="text-right">KB</span>
                  <span className="text-right">Refs</span>
                </div>
                <div className="max-h-[420px] overflow-y-auto divide-y divide-slate-100">
                  {(kbFileRows.length ? kbFileRows : (kb.entity_sample || []).map((e, i) => ({
                    id: e?.source_file || `entity-${i}`,
                    path: e?.source_file || `entity-${i}`,
                    filetype: "unknown",
                    kind: "code",
                    size: 0,
                    entityCount: 0,
                    apiCount: 0,
                    dbCount: 0,
                    uiCount: 0,
                    chainCount: 0,
                    resolvedCount: 0,
                  }))).slice(0, 120).map((file) => {
                    const isActive = (selectedKbFile?.path || "") === (file.path || "");
                    const badge = file.kind === "ui" ? "bg-blue-50 text-blue-700 border-blue-200"
                      : file.kind === "api" ? "bg-violet-50 text-violet-700 border-violet-200"
                      : file.kind === "db" ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                      : "bg-slate-50 text-slate-700 border-slate-200";
                    return (
                      <button
                        key={file.id || file.path}
                        onClick={() => setSelectedKbFile(file)}
                        className={`w-full text-left px-3 py-3 transition-colors ${isActive ? "bg-violet-50/70" : "hover:bg-slate-50"}`}
                      >
                        <div className="grid grid-cols-[minmax(0,1fr)_88px_70px_72px_88px] gap-2 items-center">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2 min-w-0">
                              <FileCode size={12} className="text-slate-400 flex-shrink-0" />
                              <div className="truncate text-[12px] font-medium text-slate-800" title={file.path}>{file.path}</div>
                            </div>
                            <div className="mt-1 flex items-center gap-1.5">
                              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badge}`}>
                                {file.filetype || "code"}
                              </span>
                              <span className="text-[10px] text-slate-400">
                                {file.kind === "ui" ? "UI file" : file.kind === "api" ? "API-heavy" : file.kind === "db" ? "Data-heavy" : "Source file"}
                              </span>
                            </div>
                          </div>
                          <div className="text-[11px] text-slate-600 text-right tabular-nums">{String(file.entityCount || 0)} entities</div>
                          <div className="text-[11px] text-slate-600 text-right tabular-nums">
                            {typeof file.size === "number" ? (file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`) : "—"}
                          </div>
                          <div className="text-[11px] text-slate-600 text-right tabular-nums">{String((file.chainCount || 0) + (file.resolvedCount || 0))}</div>
                          <div className="text-[11px] text-slate-500 text-right">
                            {file.kind === "ui" ? `${file.resolvedCount || 0} resolved` : file.kind === "api" ? `${file.apiCount || 0} API` : `${file.dbCount || 0} DB`}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                {selectedKbFile ? (
                  <div className="space-y-4">
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-slate-500 font-semibold">Selected file</div>
                      <div className="text-sm font-semibold text-slate-800 break-all">{selectedKbFile.path}</div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white border border-slate-200 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
                          {selectedKbFile.filetype || "code"}
                        </span>
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white border border-slate-200 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
                          {selectedKbFile.kind === "ui" ? "UI" : selectedKbFile.kind === "api" ? "API" : selectedKbFile.kind === "db" ? "DB" : "Source"}
                        </span>
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white border border-slate-200 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
                          {typeof selectedKbFile.size === "number" ? `${Math.max(1, Math.round(selectedKbFile.size / 1024))} KB` : "—"}
                        </span>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {[
                        ["Entities", selectedKbFile.entityCount || 0],
                        ["API", selectedKbFile.apiCount || 0],
                        ["DB", selectedKbFile.dbCount || 0],
                        ["UI", selectedKbFile.uiCount || 0],
                      ].map(([label, value]) => (
                        <div key={label} className="rounded-lg border border-slate-200 bg-white px-3 py-2">
                          <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
                          <div className="text-base font-bold text-slate-800 tabular-nums">{value}</div>
                        </div>
                      ))}
                    </div>
                    <div className="rounded-lg border border-slate-200 bg-white p-3">
                      <div className="text-[10px] uppercase tracking-wide text-slate-500 font-semibold mb-2">Extracted entities</div>
                      <div className="max-h-40 overflow-y-auto space-y-1 text-[11px] text-slate-700">
                        {(kb.entity_sample || [])
                          .filter((entity) => (entity?.source_file || "") === selectedKbFile.path)
                          .slice(0, 12)
                          .map((entity, idx) => (
                            <div key={`${entity?.type || "entity"}-${idx}`} className="flex items-start justify-between gap-2 border-b border-slate-100 pb-1 last:border-b-0 last:pb-0">
                              <div className="min-w-0">
                                <div className="font-semibold text-slate-800 truncate">{entity?.name || entity?.path || "entity"}</div>
                                <div className="text-[10px] text-slate-500 uppercase tracking-wide">{entity?.type || "ENTITY"}</div>
                              </div>
                              <span className="text-[10px] text-slate-400 flex-shrink-0">{entity?.source_file ? "kb" : ""}</span>
                            </div>
                          ))}
                        {!(kb.entity_sample || []).some((entity) => (entity?.source_file || "") === selectedKbFile.path) && (
                          <div className="text-[11px] text-slate-500">No sampled entities were attached to this file.</div>
                        )}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="h-full min-h-[220px] flex items-center justify-center text-center text-slate-500 text-sm">
                    Select a file to inspect its KB signal.
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm p-5">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-violet-50 text-violet-600 flex items-center justify-center">
              {status === "running" ? <RefreshCw size={18} className="animate-spin" /> : <Database size={18} />}
            </div>
            <div>
              <div className="text-sm font-semibold text-slate-800">Knowledge Base {status === "running" ? "in progress" : "not loaded yet"}</div>
              <div className="text-[11px] text-slate-500">
                {status === "running"
                  ? "The first-pass KB summary will appear here as soon as the worker finishes indexing."
                  : "Run a transformation to populate KB statistics and indexed source files."}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );

  const renderTraceabilityBanner = () => {
    if (!traceability || status !== "awaiting_confirmation") return null;
    const mode = traceability.mode || "backend";
    const total =
      (traceability.backend_total || 0) + (traceability.frontend_total || 0);
    return (
      <div
        data-testid="traceability-gate"
        className="rounded-2xl border-2 border-amber-300 bg-amber-50/70 shadow-sm p-4 mb-3"
      >
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-amber-100 text-amber-700 flex items-center justify-center shrink-0">
            <AlertCircle size={18} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-amber-900 flex items-center gap-2">
              Traceability review required
              <span
                className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 bg-white border border-amber-300 text-amber-800 rounded font-semibold"
                data-testid="traceability-mode-badge"
              >
                {mode}
              </span>
            </div>
            <div className="text-[11px] text-amber-800 mt-0.5">
              {mode === "frontend"
                ? "Confirm the discovered UI screens → API mapping below. Planner will not start until you approve."
                : mode === "fullstack"
                ? "Confirm both the API → DB hop chain AND the UI → API mapping below. Planner will not start until you approve."
                : "Confirm the full API → Controller → Service → Repository → DB tables trace below. Planner will not start until you approve."}
              {" "}<strong>{total}</strong> item{total === 1 ? "" : "s"} discovered.
            </div>
          </div>
        </div>
      </div>
    );
  };

  const renderFrontendTraceability = () => {
    if (!traceability || status !== "awaiting_confirmation") return null;
    const rows = traceability.frontend_rows || [];
    if (rows.length === 0) return null;
    return (
      <div
        data-testid="ui-to-api-wireframes"
        className="rounded-2xl border border-sky-200 bg-white shadow-sm overflow-hidden mb-3"
      >
        <div className="px-5 py-3 border-b border-sky-100 bg-sky-50/60 flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-sky-100 text-sky-700 flex items-center justify-center">
            <Layers size={16} />
          </div>
          <div>
            <h4 className="text-sm font-semibold text-slate-800">
              UI → API Traceability ({rows.length} screen{rows.length === 1 ? "" : "s"})
            </h4>
            <p className="text-[11px] text-slate-500">
              Wireframe preview per screen with the APIs it calls. Confirm before Planner starts.
            </p>
          </div>
        </div>
        <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-3 max-h-[520px] overflow-y-auto">
          {rows.map((row, idx) => (
            <div
              key={`${row.screen_path}-${idx}`}
              data-testid={`ui-wireframe-${idx}`}
              className="border border-slate-200 rounded-lg overflow-hidden bg-slate-50/40"
            >
              {/* CSS-only wireframe: file header + rough element list */}
              <div className="px-3 py-2 bg-slate-100 border-b border-slate-200 flex items-center gap-2">
                <FileCode size={12} className="text-slate-500" />
                <span className="text-[11px] font-mono text-slate-700 truncate" title={row.screen_path}>
                  {row.screen}
                </span>
              </div>
              <div className="p-3 space-y-1.5 bg-white border-b border-slate-200">
                {(row.elements || []).slice(0, 6).map((el, i) => (
                  <div
                    key={i}
                    className={`text-[10px] flex items-center gap-2 ${
                      el.kind === "button" ? "px-2 py-1 bg-slate-100 rounded border border-slate-300 w-fit"
                      : el.kind === "input" || el.kind === "select"
                        ? "px-2 py-1 border border-slate-300 rounded bg-white text-slate-400"
                      : el.kind === "table" ? "border border-dashed border-slate-300 rounded px-2 py-2 text-slate-400"
                      : "text-slate-600"
                    }`}
                  >
                    <span className="uppercase text-[8px] tracking-wider text-slate-400">{el.kind}</span>
                    <span className="truncate">{el.label || "(unlabelled)"}</span>
                  </div>
                ))}
                {(!row.elements || row.elements.length === 0) && (
                  <div className="text-[10px] text-slate-400 italic">
                    No UI elements auto-detected.
                  </div>
                )}
              </div>
              <div className="px-3 py-2 bg-sky-50/50 space-y-1">
                <div className="text-[9px] uppercase tracking-wider text-sky-700 font-semibold">
                  Calls {row.api_calls.length} API{row.api_calls.length === 1 ? "" : "s"}
                </div>
                {row.api_calls.slice(0, 5).map((c, i) => (
                  <div key={i} className="text-[10px] font-mono text-slate-700 flex items-center gap-1 truncate">
                    <span className="text-sky-600">→</span>
                    <span className="truncate" title={c.url}>{c.url}</span>
                    {c.resolved_tables && c.resolved_tables.length > 0 && (
                      <span className="ml-1 text-emerald-700 text-[9px]">
                        ({c.resolved_tables.slice(0, 2).join(", ")})
                      </span>
                    )}
                  </div>
                ))}
                {row.api_calls.length > 5 && (
                  <div className="text-[9px] text-slate-500">+{row.api_calls.length - 5} more…</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderEnvelopeTable = () => (
    <div className="rounded-2xl border border-violet-200 bg-white shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-violet-100 bg-violet-50/40 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-100 text-violet-700 flex items-center justify-center">
            <Layers size={18} />
          </div>
          <div>
            <h4 className="text-sm font-semibold text-slate-800">Discovered Architecture</h4>
            <p className="text-[11px] text-slate-500">
              Context Manager identified {envelopes.length} endpoint and component envelopes for downstream planning.
            </p>
          </div>
        </div>
        {status === "awaiting_confirmation" && (
          <button
            data-testid="confirm-traceability-btn"
            onClick={handleConfirmPlan}
            disabled={confirming}
            className="px-4 py-2 bg-[#FFE600] text-[#2E2E38] font-semibold rounded-lg hover:bg-[#FFD500] transition-colors flex items-center gap-2 disabled:opacity-50"
          >
            {confirming ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {confirming ? "Starting..." : "Confirm Traceability & Start Planner"}
          </button>
        )}
      </div>
      <div className="overflow-y-auto">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Method</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Endpoint / Component</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Controller</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Service</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">DB Tables</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Action</th>
              <th className="text-left px-3 py-2 font-semibold text-slate-600">Risk</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {pagedEnvelopes.map((env, idx) => (
              <tr
                key={env.envelope_id || idx}
                className="hover:bg-violet-50/60 cursor-pointer transition-colors"
                onClick={() => setSelectedEnvelope(env)}
                data-testid={`envelope-row-${env.envelope_id || idx}`}
                title="Click to view full envelope detail"
              >
                <td className="px-3 py-2">
                  <span className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold uppercase ${
                    env.endpoint_method === "GET" ? "bg-blue-100 text-blue-700" :
                    env.endpoint_method === "POST" ? "bg-green-100 text-green-700" :
                    env.endpoint_method === "PUT" ? "bg-amber-100 text-amber-700" :
                    env.endpoint_method === "DELETE" ? "bg-red-100 text-red-700" :
                    "bg-slate-100 text-slate-700"
                  }`}>
                    {env.endpoint_method || env.layer || "INFRA"}
                  </span>
                  {env.is_outbound_client && (
                    <span
                      className="ml-1 inline-block px-1 py-0.5 rounded text-[8px] font-bold uppercase bg-sky-50 text-sky-700 border border-sky-200"
                      title="Outbound REST-client call — not part of this service's own API surface"
                    >
                      external
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 font-mono text-slate-800 truncate max-w-[200px]" title={env.endpoint_path}>
                  {env.endpoint_path || env.controller_class || "Infrastructure"}
                </td>
                <td className="px-3 py-2 text-slate-600 truncate max-w-[120px]">{env.controller_class || "-"}</td>
                <td className="px-3 py-2 text-slate-600 truncate max-w-[120px]">{env.service_class || "-"}</td>
                <td className="px-3 py-2">
                  {(env.db_tables || []).length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {env.db_tables.slice(0, 3).map((tableName, i) => (
                        <span key={i} className="px-1 py-0.5 bg-emerald-50 text-emerald-700 rounded text-[9px] border border-emerald-200">
                          {tableName}
                        </span>
                      ))}
                      {env.db_tables.length > 3 && (
                        <span className="text-[9px] text-slate-500">+{env.db_tables.length - 3}</span>
                      )}
                    </div>
                  ) : <span className="text-slate-400">-</span>}
                </td>
                <td className="px-3 py-2">
                  <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded ${
                    env.action === "TRANSFORM" ? "bg-blue-50 text-blue-700" :
                    env.action === "REWRITE" ? "bg-amber-50 text-amber-700" :
                    env.action === "DELETE" ? "bg-red-50 text-red-700" :
                    env.action === "NEW" ? "bg-green-50 text-green-700" :
                    env.action === "INTEGRATE" ? "bg-sky-50 text-sky-700" :
                    "bg-slate-50 text-slate-700"
                  }`}>
                    {env.action}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded ${
                    env.risk_level === "critical" ? "bg-red-100 text-red-700" :
                    env.risk_level === "high" ? "bg-amber-100 text-amber-700" :
                    env.risk_level === "medium" ? "bg-yellow-100 text-yellow-700" :
                    "bg-green-100 text-green-700"
                  }`}>
                    {env.risk_level}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {envelopes.length > ENVELOPES_PER_PAGE && (
        <div
          className="px-4 py-2 border-t border-violet-100 bg-white flex items-center justify-between text-[11px] text-slate-600"
          data-testid="envelope-pagination"
        >
          <span>
            Showing <b>{(envelopePage - 1) * ENVELOPES_PER_PAGE + 1}</b>-<b>{Math.min(envelopePage * ENVELOPES_PER_PAGE, envelopes.length)}</b> of <b>{envelopes.length}</b>
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setEnvelopePage((page) => Math.max(1, page - 1))}
              disabled={envelopePage <= 1}
              className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
              data-testid="envelope-pagination-prev"
              aria-label="Previous page"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="text-slate-500">Page <b>{envelopePage}</b> of <b>{totalEnvelopePages}</b></span>
            <button
              onClick={() => setEnvelopePage((page) => Math.min(totalEnvelopePages, page + 1))}
              disabled={envelopePage >= totalEnvelopePages}
              className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
              data-testid="envelope-pagination-next"
              aria-label="Next page"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
      <div className="px-4 py-3 border-t border-violet-100 bg-violet-50/30 flex items-center gap-4 flex-wrap text-[11px] text-slate-600">
        <span><b>{envelopes.filter((item) => item.endpoint_method && item.endpoint_method !== "INFRA" && !item.is_outbound_client).length}</b> API endpoints</span>
        <span><b>{envelopes.filter((item) => item.is_outbound_client).length}</b> external integrations</span>
        <span><b>{new Set(envelopes.flatMap((item) => item.db_tables || [])).size}</b> DB tables</span>
        <span><b>{envelopes.filter((item) => item.action === "TRANSFORM").length}</b> transforms</span>
        <span><b>{envelopes.filter((item) => item.action === "NEW").length}</b> new files</span>
        <span><b>{envelopes.filter((item) => item.risk_level === "high" || item.risk_level === "critical").length}</b> high-risk</span>
      </div>
    </div>
  );

  const renderPlannerPanel = () => (
    <div className="space-y-4">
      {status === "awaiting_task_confirmation" && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50/80 p-5 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <div className="w-11 h-11 rounded-xl bg-amber-100 text-amber-700 flex items-center justify-center">
              <AlertCircle size={18} />
            </div>
            <div>
              <h4 className="text-sm font-semibold text-amber-900">Planner review required</h4>
              <p className="text-[12px] text-amber-800 mt-1">
                Planner created <b>{taskStats.total || allTasks.length || 0}</b> tasks across <b>{taskWaves.length}</b> waves.
                Review the plan below, then confirm to start code generation.
              </p>
            </div>
          </div>
          <button
            onClick={handleConfirmTasks}
            disabled={confirmingTasks}
            className="px-4 py-2 bg-[#FFE600] text-[#2E2E38] font-semibold rounded-lg hover:bg-[#FFD500] transition-colors flex items-center gap-2 disabled:opacity-50"
          >
            {confirmingTasks ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {confirmingTasks ? "Starting..." : "Confirm & Start Coding"}
          </button>
        </div>
      )}

      {taskWaves.length > 0 && renderPlanChatWidget("planner")}

      <div className="grid grid-cols-2 xl:grid-cols-5 gap-3">
        {[
          ["Total", taskStats.total ?? allTasks.length ?? 0, "text-slate-800", "bg-white"],
          ["Done", taskStats.done ?? 0, "text-emerald-700", "bg-emerald-50/70"],
          ["Verified", taskStats.verified ?? 0, "text-sky-700", "bg-sky-50/70"],
          ["Blocked", taskStats.blocked ?? 0, "text-red-700", "bg-red-50/70"],
          ["In Progress", taskStats.in_progress ?? 0, "text-amber-700", "bg-amber-50/70"],
        ].map(([label, value, textClass, bgClass]) => (
          <div key={label} className={`rounded-2xl border border-slate-200 ${bgClass} px-4 py-3`}>
            <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
            <div className={`mt-1 text-2xl font-bold ${textClass} tabular-nums`}>{value}</div>
          </div>
        ))}
      </div>

      {taskWaves.length > 0 ? taskWaves.map((wave) => {
        const page = taskPages[wave.wave] || 1;
        const totalPages = Math.max(1, Math.ceil((wave.tasks || []).length / TASKS_PER_PAGE));
        const visibleTasks = (wave.tasks || []).slice((page - 1) * TASKS_PER_PAGE, page * TASKS_PER_PAGE);
        return (
          <details key={wave.wave} className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden" open>
            <summary className="list-none cursor-pointer px-5 py-4 border-b border-slate-100 bg-slate-50/80 flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-slate-800">Wave {wave.wave}: {wave.name || `Wave ${wave.wave}`}</div>
                <div className="text-[11px] text-slate-500">{(wave.tasks || []).length} task{(wave.tasks || []).length === 1 ? "" : "s"}</div>
              </div>
              <ChevronDown size={16} className="text-slate-400" />
            </summary>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead className="bg-slate-50 border-b border-slate-200">
                  <tr>
                    <th className="text-left px-3 py-2 font-semibold text-slate-600">Source Path</th>
                    <th className="text-left px-3 py-2 font-semibold text-slate-600">Target Path</th>
                    <th className="text-left px-3 py-2 font-semibold text-slate-600">Action</th>
                    <th className="text-left px-3 py-2 font-semibold text-slate-600">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {visibleTasks.map((task, idx) => (
                    <tr key={task.task_id || `${wave.wave}-${idx}`} className="hover:bg-slate-50/70">
                      <td className="px-3 py-2 font-mono text-slate-700 break-all">{task.source_path || "—"}</td>
                      <td className="px-3 py-2 font-mono text-slate-700 break-all">{task.target_path || "→ generated path"}</td>
                      <td className="px-3 py-2">
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${getTaskActionBadge(task.action)}`}>
                          {task.action || "TRANSFORM"}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${getTaskStatusBadge(task.status)}`}>
                          {task.status || "PENDING"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {(wave.tasks || []).length > TASKS_PER_PAGE && (
              <div className="px-4 py-2 border-t border-slate-100 bg-white flex items-center justify-between text-[11px] text-slate-600">
                <span>
                  Showing <b>{(page - 1) * TASKS_PER_PAGE + 1}</b>-<b>{Math.min(page * TASKS_PER_PAGE, (wave.tasks || []).length)}</b> of <b>{(wave.tasks || []).length}</b>
                </span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setTaskPages((prev) => ({ ...prev, [wave.wave]: Math.max(1, page - 1) }))}
                    disabled={page <= 1}
                    className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
                    aria-label={`Previous page for wave ${wave.wave}`}
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <span>Page <b>{page}</b> of <b>{totalPages}</b></span>
                  <button
                    onClick={() => setTaskPages((prev) => ({ ...prev, [wave.wave]: Math.min(totalPages, page + 1) }))}
                    disabled={page >= totalPages}
                    className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
                    aria-label={`Next page for wave ${wave.wave}`}
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            )}
          </details>
        );
      }) : (
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm p-6 text-center">
          <Bot size={22} className="mx-auto mb-2 text-slate-300" />
          <div className="text-sm font-semibold text-slate-700">Planner output will appear here</div>
          <div className="text-[11px] text-slate-500 mt-1">
            {status === "running" ? "Waiting for the Planner agent to finish the wave plan." : "No task list has been generated yet."}
          </div>
        </div>
      )}
    </div>
  );

  const renderCoderPanel = () => (
    <div className="space-y-4">
    {files.length > 0 && renderPlanChatWidget("coder")}
    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2 flex-wrap">
        <Code2 size={16} className="text-slate-500" />
        <h4 className="text-sm font-semibold text-gray-800">{status === "running" ? "Live Transformed Code" : "Generated Code"}</h4>
        <span
          className="text-[11px] text-slate-500"
          title="Generated output files persisted so far. This can exceed the header's Source count because one source file often produces multiple outputs (controller → DTO + service + test) and compile-fix rewrites add rows."
        >({files.length} generated files)</span>
        {status === "running" && (
          <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-violet-50 text-violet-700">Live</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="text-[11px] text-slate-500">Regenerate with:</label>
          <select
            value={regenModel}
            onChange={(e) => setRegenModel(e.target.value)}
            className="text-[11px] border border-slate-200 rounded px-2 py-1 bg-white focus:border-[#FFE600] focus:ring-1 focus:ring-[#FFE600] outline-none max-w-[220px]"
            title="Model used when clicking Regenerate on a file"
          >
            <option value="">{factoryEnabled ? "Auto (Factory Droid picks)" : "Auto (Console routing)"}</option>
            {effectiveModelOptions.map(({ id, label }) => (
              id ? <option key={id} value={id}>{label}</option> : null
            ))}
          </select>
        </div>
      </div>
      {factoryEnabled && (
        <div className="px-5 py-1.5 border-b border-slate-100 bg-violet-50/60 text-[10px] text-violet-700 flex items-center gap-1.5">
          <Bot size={12} /> Factory Droid is enabled for this project — model list below is Droid's catalogue.
        </div>
      )}
      {status === "running" && (
        <div className="px-5 py-3 border-b border-slate-100 bg-slate-50">
          <div className="flex items-center justify-between gap-3 text-[11px] text-slate-500">
            <div className="min-w-0">
              <div className="font-semibold text-slate-700 flex items-center gap-2">
                <RefreshCw size={12} className="text-violet-600 animate-spin" />
                {progress.phase === "building_kb" ? "Knowledge base building" : "Code generation running"}
              </div>
              <div className="truncate" title={progress.message || ""}>{progress.message || "Streaming transformed files…"}</div>
            </div>
            <div className="tabular-nums text-right">
              <div className="text-base font-semibold text-violet-700">{progress.percent || 0}%</div>
              {typeof progress.filesTotal === "number" && progress.filesTotal > 0 && (
                <div>{progress.filesDone || 0}/{progress.filesTotal}</div>
              )}
            </div>
          </div>
          <div className="mt-2 h-1.5 rounded-full bg-slate-200 overflow-hidden">
            <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-600 transition-all duration-300" style={{ width: `${progress.percent || 0}%` }} />
          </div>
        </div>
      )}
      {/* iter-16.x — was a fixed min-h-[560px], which left a large blank
          gutter under the Coder panel whenever the Planner Details drawer
          was collapsed (its own container fills the tall xl workspace
          panel but this grid never grew past its floor). Use a
          viewport-relative floor instead so the code viewer actually
          uses the vertical space the surrounding layout already grants
          it — still just a *minimum*, so shorter viewports aren't forced
          to overflow. */}
      <div className="grid xl:grid-cols-[minmax(0,320px)_minmax(0,1fr)] min-h-[560px] xl:min-h-[72vh]">
        <div className="border-r border-slate-100 overflow-y-auto">
          <div className="px-3 py-2 border-b border-slate-100 bg-slate-50 flex items-center justify-between">
            <div>
              <div className="text-[11px] font-semibold text-slate-700">Project Structure</div>
              <div className="text-[10px] text-slate-500 tabular-nums">{activeStructureRows.length} nodes</div>
            </div>
            {status === "running" && (
              <div className="text-[10px] text-emerald-600 flex items-center gap-1">
                <RefreshCw size={10} className="animate-spin" /> streaming
              </div>
            )}
          </div>
          <div className="overflow-y-auto max-h-[508px] xl:max-h-none py-1" data-testid="transformer-project-structure">
            {activeStructureRows.map((row) => {
              if (row.type === "dir") {
                const isOpen = !collapsedDirs[row.path];
                return (
                  <button
                    key={row.id}
                    onClick={() => toggleDir(row.path)}
                    data-testid={`transformer-dir-${row.path}`}
                    className="w-full flex items-center gap-1 text-[11px] py-0.5 hover:bg-[#F6F6FA]"
                    style={{ paddingLeft: 8 + row.depth * 10 }}
                  >
                    {isOpen
                      ? <ChevronDown className="w-3 h-3 text-[#747480] flex-shrink-0" />
                      : <ChevronRight className="w-3 h-3 text-[#747480] flex-shrink-0" />}
                    {isOpen
                      ? <FolderOpen className="w-3 h-3 text-[#FFE600] flex-shrink-0" />
                      : <Folder className="w-3 h-3 text-[#FFE600] flex-shrink-0" />}
                    <span className="text-[#2E2E38] truncate font-mono" title={row.path}>{row.label}</span>
                  </button>
                );
              }
              const isSelected = selectedFile?.id === row.id;
              return (
                <button
                  key={row.id}
                  onClick={() => {
                    if (row.sourceOnly || !row.raw) {
                      setSelectedFile({
                        id: row.id,
                        path: row.path,
                        content: `// Live generated class preview will appear here once ${row.path} is transformed.\n`,
                        loading: false,
                        confidence: null,
                        sourceOnly: true,
                      });
                      return;
                    }
                    loadFileContent(row.raw);
                  }}
                  data-testid={`transformer-file-${row.path}`}
                  className={`w-full flex items-center gap-1 text-[11px] py-0.5 ${isSelected ? "bg-[#FFFCE6] text-[#2E2E38] font-semibold" : "hover:bg-[#F6F6FA] text-[#2E2E38]"}`}
                  style={{ paddingLeft: 8 + row.depth * 10 + 14 }}
                >
                  <FileCode className="w-3 h-3 text-[#747480] flex-shrink-0" />
                  <span className="font-mono truncate" title={row.path}>{row.label}</span>
                  {row.sourceOnly && (
                    <span className="ml-auto shrink-0 text-[9px] font-semibold px-1 rounded-sm bg-[#F0F0F4] text-[#747480] border border-[#E6E6E6]">source</span>
                  )}
                  {typeof row.confidence === "number" && (
                    <span className={`ml-auto shrink-0 text-[9px] font-semibold px-1 rounded-sm ${formatConfidenceBadge(row.confidence > 1 ? row.confidence : row.confidence * 100)}`}>
                      {Math.round((row.confidence > 1 ? row.confidence : row.confidence * 100))}%
                    </span>
                  )}
                  {status === "running" && isSelected && !row.sourceOnly && (
                    <span className="text-[9px] text-violet-600 ml-1">live</span>
                  )}
                </button>
              );
            })}
          </div>
          <div className="px-3 py-2 border-t border-slate-100 bg-slate-50 text-[10px] text-slate-500">
            Click any generated file to preview the live class.
          </div>
        </div>

        <div className="flex flex-col min-h-0">
          {!selectedFile ? (
            <div className="flex-1 flex items-center justify-center text-sm text-gray-400">
              <div className="text-center max-w-sm px-4">
                <Eye size={28} className="mx-auto mb-2 opacity-50" />
                <div className="font-medium text-slate-600">
                  {files.length > 0 ? "Select a file from the structure to preview generated code" : "Source structure is loaded; generated files will appear here as the run progresses"}
                </div>
                <div className="mt-1 text-[11px] text-slate-400">
                  Use the kebab menu to download, push to GitHub, or reopen a past transform if the list should be populated.
                </div>
              </div>
            </div>
          ) : (
            <>
              <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-2 bg-slate-50">
                <FileCode size={13} className="text-slate-500" />
                <span className="text-[11px] font-mono truncate flex-1" title={selectedFile.path}>{selectedFile.path}</span>
                {progress.currentFile && !selectedFile.sourceOnly && (
                  <span className="text-[10px] text-violet-600 px-1.5 py-0.5 rounded bg-violet-50">
                    {status === "running" ? "Live" : "Preview"}
                  </span>
                )}
                {typeof selectedFile.confidence === "number" && (
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${formatConfidenceBadge(selectedFile.confidence > 1 ? selectedFile.confidence : selectedFile.confidence * 100)}`}>
                    Confidence: {Math.round((selectedFile.confidence > 1 ? selectedFile.confidence : selectedFile.confidence * 100))}%
                  </span>
                )}
                {!selectedFile.sourceOnly && (
                  <button
                    onClick={() => handleRegenerate(selectedFile)}
                    disabled={!!regeneratingIds[selectedFile.id]}
                    className="text-[11px] px-2 py-1 bg-violet-100 text-violet-700 rounded hover:bg-violet-200 flex items-center gap-1 disabled:opacity-60"
                    title="Re-run this file with the selected model"
                  >
                    {regeneratingIds[selectedFile.id]
                      ? <><RefreshCw size={11} className="animate-spin" /> Regenerating…</>
                      : <><Sparkles size={11} /> Regenerate</>}
                  </button>
                )}
              </div>
              <pre className="flex-1 overflow-auto text-[11px] font-mono p-4 bg-slate-900 text-slate-100 m-0 min-h-[520px] xl:min-h-[68vh]">{selectedFile.loading ? "// Loading…" : (selectedFile.content || "// (empty)")}</pre>
            </>
          )}
        </div>
      </div>
    </div>
    {/* iter-15.35 — Verification Console embedded directly under Code
        Generation so the user no longer has to switch to a separate
        Verifier tab to see per-file verification results. */}
    {renderVerificationConsole()}
    </div>
  );

  // iter-15.35 — "Verification Results" is no longer a separate tab the
  // user has to switch to. Renamed "Verification Console" and (a) embedded
  // directly under the Code Generation section in `renderCoderPanel()`
  // below, and (b) paginated 10 rows/page, matching the "Discovered
  // Architecture" table's pattern (iter-15.21/15.25). The Verifier tab
  // still exists in the agent-pipeline sidebar (so the agent icon/timeline
  // story stays intact) but now renders this exact same function — single
  // implementation, no duplicated markup.
  const renderVerificationConsole = () => (
    <div className="space-y-4">
    {verifierRows.length > 0 && renderPlanChatWidget("tester")}
    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden" data-testid="verification-console">
      <div className="px-5 py-3 border-b border-slate-100 bg-slate-900 flex items-center gap-2">
        <TerminalSquare size={15} className="text-emerald-400" />
        <div>
          <h4 className="text-sm font-semibold text-white">Verification Console</h4>
          <p className="text-[11px] text-slate-400">Per-file confidence, verdict, and issue counts produced by the Verifier agent.</p>
        </div>
        {verifierRows.length > 0 && (
          <span className="ml-auto text-[10px] font-mono text-emerald-400 tabular-nums">{verifierRows.length} file{verifierRows.length === 1 ? "" : "s"} verified</span>
        )}
      </div>
      {verifierRows.length > 0 ? (
        <>
          <div className="overflow-x-auto max-h-[420px] overflow-y-auto">
            <table className="w-full text-[11px]">
              <thead className="bg-slate-50 border-b border-slate-200 sticky top-0">
                <tr>
                  <th className="text-left px-3 py-2 font-semibold text-slate-600">Path</th>
                  <th className="text-left px-3 py-2 font-semibold text-slate-600">Confidence</th>
                  <th className="text-left px-3 py-2 font-semibold text-slate-600">Verdict</th>
                  <th className="text-left px-3 py-2 font-semibold text-slate-600">Issues</th>
                  <th className="text-left px-3 py-2 font-semibold text-slate-600">Summary</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {pagedVerifierRows.map((row) => (
                  <tr key={row.id} className="hover:bg-slate-50/70">
                    <td className="px-3 py-2">
                      <div className="font-mono text-slate-700 break-all">{row.path}</div>
                      {row.sourcePath && row.sourcePath !== row.path && (
                        <div className="text-[10px] text-slate-400 font-mono break-all">from {row.sourcePath}</div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold ${formatConfidenceBadge(row.confidence)}`}>
                        {typeof row.confidence === "number" ? `${Math.round(row.confidence)}%` : "Pending"}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${getTaskStatusBadge(row.verdict === "PASS" ? "VERIFIED" : row.verdict === "REJECT" ? "BLOCKED" : row.status)}`}>
                        {row.verdict || row.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-700 tabular-nums">{row.issueCount}</td>
                    <td className="px-3 py-2 text-slate-600">{row.summary || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {verifierRows.length > VERIFIER_PER_PAGE && (
            <div
              className="px-4 py-2 border-t border-slate-200 bg-white flex items-center justify-between text-[11px] text-slate-600"
              data-testid="verifier-pagination"
            >
              <span>
                Showing <b>{(verifierPage - 1) * VERIFIER_PER_PAGE + 1}</b>-<b>{Math.min(verifierPage * VERIFIER_PER_PAGE, verifierRows.length)}</b> of <b>{verifierRows.length}</b>
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setVerifierPage((page) => Math.max(1, page - 1))}
                  disabled={verifierPage <= 1}
                  className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
                  data-testid="verifier-pagination-prev"
                  aria-label="Previous page"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="text-slate-500">Page <b>{verifierPage}</b> of <b>{totalVerifierPages}</b></span>
                <button
                  onClick={() => setVerifierPage((page) => Math.min(totalVerifierPages, page + 1))}
                  disabled={verifierPage >= totalVerifierPages}
                  className="p-1 rounded border border-slate-200 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50"
                  data-testid="verifier-pagination-next"
                  aria-label="Next page"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="p-8 text-center">
          <CheckCircle size={24} className="mx-auto mb-2 text-slate-300" />
          <p className="text-[11px] font-medium text-slate-600">Verification data will appear here</p>
          <p className="text-[10px] text-slate-400 mt-1">
            {status === "running" ? "The Verifier agent has not published per-file scores yet." : "No per-file verification results are available yet."}
          </p>
        </div>
      )}
    </div>
    </div>
  );

  // Verifier tab reuses the exact same console — single implementation.

  // iter-15.43 — Stacked Coder+Planner workspace. Coder stays permanently
  // visible as the primary panel; Planner details render in a collapsible
  // section beneath it. Auto-opens when the pipeline is awaiting Planner
  // confirmation, and stays open once any tasks exist so the operator can
  // watch waves progress without leaving the Coder view. This replaces
  // the previous tab-swap between the two agents, which forced the
  // operator to context-switch mid-run (iter-15.42 UX review).
  const renderCoderPlannerStacked = () => {
    const hasTasks = (allTasks && allTasks.length > 0) || taskWaves.length > 0;
    const awaitingPlanner = status === "awaiting_task_confirmation";
    return (
      <div className="space-y-4">
        {renderCoderPanel()}

        <details
          className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden"
          open={awaitingPlanner || hasTasks}
          data-testid="planner-drawer"
        >
          <summary className="list-none cursor-pointer px-5 py-3 border-b border-slate-100 bg-slate-50/60 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Layers size={15} className="text-violet-500" />
              <span className="text-sm font-semibold text-slate-800">
                Planner Details
              </span>
              <span className="text-[11px] text-slate-500">
                {taskWaves.length} wave{taskWaves.length === 1 ? "" : "s"} · {taskStats.total ?? allTasks.length ?? 0} tasks
                {awaitingPlanner && <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800"><AlertCircle size={9} /> Review required</span>}
              </span>
            </div>
            <ChevronDown size={16} className="text-slate-400" />
          </summary>
          <div className="px-5 py-4 bg-slate-50/40">
            {renderPlannerPanel()}
          </div>
        </details>
      </div>
    );
  };

  const renderTesterPanel = () => {
    // iter-15.46 — Native compile console + test-gen summary + coverage.
    // Backward-compatible: when only the legacy LLM narrative is present
    // (older transformations, `mode === "static_only"`) we fall back to
    // the old view. When `mode === "native"` (iter-15.44+) we render the
    // real subprocess output.
    const cr = compilationResult || {};
    const nativeMode = cr.mode === "native";
    const staticNarrative = cr.static_analysis || (nativeMode ? null : cr);
    const testGen = cr.test_generation;
    const coverage = cr.coverage;
    const overallCov = coverage?.overall_coverage_pct;

    // iter-15.7x — Shared Compile Console body (log lines) so the inline
    // panel and the maximized full-screen overlay render identically.
    const consoleLines = compileConsole.lines.length > 0
      ? compileConsole.lines.map((line, i) => {
          const isErr = /error|exception|failed|failure|✘/i.test(line);
          const isOk = /BUILD SUCCESS|COMPILATION READY|✔/.test(line);
          return (
            <div key={i} className={isOk ? "text-emerald-400" : isErr ? "text-red-400" : undefined}>
              {line}
            </div>
          );
        })
      : <span className="text-slate-500">No console output yet — click "Rerun compile" to invoke the real build tool.</span>;

    // iter-15.7x — Shared toolbar: copy / clear / maximize-minimize.
    // Reused by both the inline console header and the maximized overlay.
    const consoleToolbar = (
      <div className="flex items-center gap-1">
        {compileConsole.updatedAt && (
          <span className="text-[10px] text-slate-500 mr-1.5 hidden sm:inline">
            updated {new Date(compileConsole.updatedAt).toLocaleTimeString()}
          </span>
        )}
        <button
          type="button"
          onClick={handleCopyConsoleLog}
          disabled={compileConsole.lines.length === 0}
          className="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-100 disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-slate-400"
          title="Copy console log"
          data-testid="console-copy-btn"
        >
          {consoleCopied ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
        </button>
        <button
          type="button"
          onClick={handleClearConsoleLog}
          disabled={compileConsole.lines.length === 0}
          className="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-red-400 disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-slate-400"
          title="Delete console log"
          data-testid="console-clear-btn"
        >
          <Trash2 size={12} />
        </button>
        <button
          type="button"
          onClick={() => setConsoleMaximized((v) => !v)}
          className="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-100"
          title={consoleMaximized ? "Restore console" : "Maximize console"}
          data-testid="console-maximize-btn"
        >
          {consoleMaximized ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        </button>
      </div>
    );

    return (
      <div className="space-y-4" data-testid="tester-panel">
        {/* ── Header ── */}
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
          <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <TerminalSquare size={16} className="text-blue-500" />
              <div>
                <h4 className="text-sm font-semibold text-gray-800">Compile Console</h4>
                <p className="text-[11px] text-slate-500">
                  {nativeMode
                    ? "Real subprocess build using the tool you picked at step 1. Per-component stdout/stderr tails below."
                    : "Static LLM-based compilation-readiness narrative."}
                </p>
              </div>
            </div>
            <button
              onClick={handleRunCompilation}
              disabled={compilationLoading}
              className="px-3 py-1.5 text-[11px] bg-blue-100 text-blue-700 rounded-lg hover:bg-blue-200 flex items-center gap-1.5 disabled:opacity-50"
              data-testid="rerun-compile-btn"
            >
              {compilationLoading ? <Loader2 size={12} className="animate-spin" /> : <Target size={12} />}
              {compilationLoading ? "Compiling..." : "Rerun compile"}
            </button>
          </div>

          {/* iter-15.6x — "Tester running" (left) + realistic live Console
              (right) split, so compilation progress and the actual
              subprocess output (real `mvn clean install` / `gradle clean
              build` / `npm run build` / etc.) are visible side-by-side
              while the Tester agent works, not just after it finishes. */}
          {(compileFixProgress || compilationLoading || compileConsole.lines.length > 0) && (
            <div
              className="border-b border-slate-100 grid grid-cols-1 lg:grid-cols-[minmax(0,320px)_1fr]"
              data-testid="tester-console-split"
            >
              {/* ── Left: Tester running / status ── */}
              <div
                className="min-w-0 px-5 py-4 border-b lg:border-b-0 lg:border-r border-slate-100 bg-violet-50/40 space-y-2"
                data-testid="tester-running-panel"
              >
                <div className="flex items-center gap-2">
                  {compileFixProgress?.phase === "passed" ? (
                    <div className="flex items-center gap-1.5 text-emerald-700 text-xs font-semibold">
                      <CheckCircle size={14} /> Build green
                    </div>
                  ) : ["exhausted", "unfixable", "infra_blocked", "stagnant", "stalled"].includes(compileFixProgress?.phase) ? (
                    <div className="flex items-center gap-1.5 text-amber-700 text-xs font-semibold">
                      <AlertTriangle size={14} /> Fix loop stopped
                    </div>
                  ) : compilationLoading || compileFixProgress ? (
                    <div className="flex items-center gap-1.5 text-violet-700 text-xs font-semibold">
                      <Loader2 size={14} className="animate-spin" /> Tester running
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 text-slate-500 text-xs font-semibold">
                      <TerminalSquare size={14} /> Tester idle
                    </div>
                  )}
                </div>
                {compileFixProgress && (
                  <>
                    <div className="text-[11px] text-slate-600">
                      {compileFixProgress.iteration
                        ? `Iteration ${compileFixProgress.iteration}${compileFixProgress.max_iterations ? ` / ${compileFixProgress.max_iterations}` : ""} · ${compileFixProgress.phase}`
                        : compileFixProgress.phase}
                    </div>
                    <div className="text-[11px] text-slate-600 break-words">{compileFixProgress.message || ""}</div>
                    {compileFixProgress.current_file && (
                      <div
                        className="text-[10px] text-slate-500 font-mono break-all"
                        title={compileFixProgress.current_file}
                      >
                        ↳ {compileFixProgress.current_file}
                      </div>
                    )}
                    {Array.isArray(compileFixProgress.failing_files) &&
                      compileFixProgress.failing_files.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {compileFixProgress.failing_files.slice(0, 8).map((p) => (
                          <span
                            key={p}
                            title={p}
                            className="text-[10px] px-1.5 py-0.5 rounded bg-white border border-red-200 text-red-700 font-mono break-all"
                          >
                            {p}
                          </span>
                        ))}
                        {compileFixProgress.failing_files.length > 8 && (
                          <span className="text-[10px] text-slate-500">
                            +{compileFixProgress.failing_files.length - 8} more
                          </span>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>

              {/* ── Right: realistic live Console (real subprocess stdout/
                  stderr, streamed line-by-line as the build actually runs) ── */}
              <div className="min-w-0 bg-slate-950" data-testid="tester-live-console">
                <div className="px-3 py-1.5 border-b border-slate-800 flex items-center justify-between gap-2">
                  <span className="text-[10px] uppercase tracking-wide text-slate-400 font-semibold shrink-0">Console</span>
                  {consoleToolbar}
                </div>
                <pre
                  ref={consoleMaximized ? null : compileConsoleRef}
                  className="text-[11px] font-mono leading-relaxed text-slate-100 p-3 h-64 overflow-y-auto overflow-x-auto whitespace-pre-wrap break-all"
                >
                  {consoleLines}
                </pre>
              </div>
            </div>
          )}

          {/* iter-15.7x — Maximized Compile Console: full-screen overlay so
              long class/package names and wide build output are fully
              readable without the 260-320px side-panel constraint. Esc or
              the minimize button restores the inline split view above. */}
          {consoleMaximized && (
            <div
              className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4 sm:p-8"
              data-testid="console-maximized-overlay"
              onClick={(e) => { if (e.target === e.currentTarget) setConsoleMaximized(false); }}
            >
              <div className="w-full h-full max-w-6xl bg-slate-950 rounded-xl shadow-2xl flex flex-col overflow-hidden border border-slate-800">
                <div className="px-4 py-2.5 border-b border-slate-800 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 text-slate-200">
                    <TerminalSquare size={14} className="text-blue-400" />
                    <span className="text-xs font-semibold uppercase tracking-wide">Compile Console</span>
                  </div>
                  {consoleToolbar}
                </div>
                <pre
                  ref={compileConsoleRef}
                  className="flex-1 text-[12px] font-mono leading-relaxed text-slate-100 p-4 overflow-y-auto overflow-x-auto whitespace-pre-wrap break-all"
                >
                  {consoleLines}
                </pre>
              </div>
            </div>
          )}



          {compilationResult ? (
            <div className="p-5 space-y-4">
              {/* ── Score + summary ── */}
              <div className="flex items-center gap-4 flex-wrap">
                <div
                  className={`w-16 h-16 rounded-full flex items-center justify-center text-lg font-bold ${
                    cr.compilation_ready ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"
                  }`}
                  data-testid="compile-score-badge"
                >
                  {cr.overall_score ?? 0}%
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-slate-800">
                    {cr.compilation_ready ? "Compilation ready" : "Issues found"}
                  </div>
                  <div className="text-[11px] text-slate-600">{cr.summary || ""}</div>
                  {nativeMode && (
                    <div className="text-[10px] text-slate-400 mt-1">
                      timeout={cr.timeout_sec}s · mode=native
                    </div>
                  )}
                </div>
                {/* ── Coverage widget ── */}
                {overallCov !== undefined && overallCov !== null && (
                  <div
                    className="flex flex-col items-center px-4 py-2 rounded-xl border border-sky-200 bg-sky-50"
                    data-testid="coverage-widget"
                  >
                    <div className="text-[10px] uppercase tracking-wide text-sky-700">Coverage</div>
                    <div className="text-lg font-bold text-sky-800 tabular-nums">{overallCov}%</div>
                  </div>
                )}
              </div>

              {/* ── Test generation summary ── */}
              {testGen?.total > 0 && (
                <div
                  className="rounded-xl border border-violet-200 bg-violet-50/60 p-3 flex items-center justify-between gap-3 flex-wrap"
                  data-testid="test-gen-summary"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="w-9 h-9 rounded-lg bg-violet-100 text-violet-700 flex items-center justify-center">
                      <TestTube2 size={16} />
                    </div>
                    <div className="min-w-0">
                      <div className="text-[12px] font-semibold text-slate-800">
                        {testGen.total} test file{testGen.total === 1 ? "" : "s"} generated
                      </div>
                      <div className="text-[11px] text-slate-600 truncate">{toSafeText(testGen.summary)}</div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {["business", "api", "integration"].map((tier) => (
                      <span key={tier} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-white border border-violet-200 text-[10px] font-semibold text-violet-700">
                        {tier}
                        <span className="text-slate-500 tabular-nums">{testGen.by_tier?.[tier] ?? 0}</span>
                      </span>
                    ))}
                    <a
                      href={transformId ? downloadTransformedTests(transformId) : "#"}
                      className={`inline-flex items-center gap-1 px-2 py-1 rounded-lg bg-violet-600 text-white text-[10px] font-semibold hover:bg-violet-700 ${!transformId ? "pointer-events-none opacity-40" : ""}`}
                      data-testid="tester-download-tests-btn"
                    >
                      <Download size={10} /> Tests ZIP
                    </a>
                  </div>
                </div>
              )}

              {/* ── Native per-component compile rows ── */}
              {nativeMode && Array.isArray(cr.components) && cr.components.length > 0 && (
                <div>
                  <div className="text-[11px] font-semibold text-slate-700 mb-2">Components</div>
                  <div className="space-y-2">
                    {cr.components.map((comp, i) => (
                      <details
                        key={comp.component || i}
                        className="rounded-lg border border-slate-200 bg-white overflow-hidden"
                        data-testid={`compile-component-${comp.component}`}
                      >
                        <summary className="list-none cursor-pointer px-3 py-2 flex items-center justify-between gap-3 hover:bg-slate-50/70">
                          <div className="flex items-center gap-2 min-w-0">
                            <span
                              className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase ${
                                comp.status === "passed" ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                                  : comp.status === "failed" ? "border-red-200 bg-red-50 text-red-700"
                                  : "border-slate-200 bg-slate-50 text-slate-600"
                              }`}
                            >
                              {comp.status}
                            </span>
                            <span className="text-[12px] font-semibold text-slate-800">{comp.component}</span>
                            <span className="text-[11px] text-slate-500 truncate">tool={comp.tool}</span>
                            {typeof comp.coverage_pct === "number" && (
                              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full bg-sky-50 border border-sky-200 text-sky-700 tabular-nums">
                                cov {comp.coverage_pct}%
                              </span>
                            )}
                          </div>
                          <ChevronDown size={14} className="text-slate-400" />
                        </summary>
                        {comp.reason && (
                          <div className="px-3 py-2 border-t border-slate-100 text-[11px] text-slate-600 bg-slate-50/40">
                            {toSafeText(comp.reason)}
                          </div>
                        )}
                        {(comp.invocations || []).map((inv, j) => (
                          <div key={j} className="px-3 py-2 border-t border-slate-100 space-y-1.5">
                            <div className="flex items-center gap-2 text-[11px] text-slate-600">
                              <span className="font-mono text-slate-500">{inv.cwd || "."}</span>
                              <span className="text-slate-300">·</span>
                              <span className="font-mono">{inv.label}</span>
                              {typeof inv.duration_ms === "number" && (
                                <span className="text-slate-400 tabular-nums">
                                  {(inv.duration_ms / 1000).toFixed(1)}s
                                </span>
                              )}
                              {typeof inv.exit_code === "number" && (
                                <span
                                  className={`ml-auto text-[10px] font-mono px-1.5 py-0.5 rounded ${
                                    inv.exit_code === 0 ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"
                                  }`}
                                >
                                  exit={inv.exit_code}
                                </span>
                              )}
                            </div>
                            {inv.stdout_tail && (
                              <pre className="text-[10px] font-mono whitespace-pre-wrap bg-slate-900 text-slate-100 rounded p-2 max-h-52 overflow-y-auto">
                                {inv.stdout_tail}
                              </pre>
                            )}
                            {inv.stderr_tail && (
                              <pre className="text-[10px] font-mono whitespace-pre-wrap bg-red-950 text-red-100 rounded p-2 max-h-52 overflow-y-auto">
                                {inv.stderr_tail}
                              </pre>
                            )}
                          </div>
                        ))}
                      </details>
                    ))}
                  </div>
                </div>
              )}

              {/* ── Coverage per component ── */}
              {Array.isArray(coverage?.components) && coverage.components.length > 0 && (
                <details className="rounded-lg border border-slate-200 bg-white overflow-hidden">
                  <summary className="list-none cursor-pointer px-3 py-2 flex items-center justify-between hover:bg-slate-50/70">
                    <div className="flex items-center gap-2">
                      <span className="text-[12px] font-semibold text-slate-800">Coverage details</span>
                      <span className="text-[11px] text-slate-500">
                        overall {overallCov ?? "n/a"}%
                      </span>
                    </div>
                    <ChevronDown size={14} className="text-slate-400" />
                  </summary>
                  <div className="px-3 py-2 border-t border-slate-100 space-y-1">
                    {coverage.components.map((c, i) => (
                      <div key={i} className="flex items-center justify-between text-[11px] text-slate-700">
                        <span className="font-semibold">{c.component}</span>
                        <span className="text-slate-500 font-mono">
                          {typeof c.coverage_pct === "number" ? `${c.coverage_pct}%` : (c.reason || c.status)}
                        </span>
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {/* ── Legacy static-analysis narrative ── */}
              {staticNarrative && (staticNarrative.checks?.length || staticNarrative.summary) && (
                <details className="rounded-lg border border-slate-200 bg-slate-50/40 overflow-hidden">
                  <summary className="list-none cursor-pointer px-3 py-2 flex items-center gap-2 hover:bg-slate-100/50">
                    <Sparkles size={12} className="text-slate-500" />
                    <span className="text-[11px] font-semibold text-slate-700">
                      Static analysis narrative
                    </span>
                    <span className="text-[10px] text-slate-500 truncate">
                      {staticNarrative.summary}
                    </span>
                    <ChevronDown size={13} className="text-slate-400 ml-auto" />
                  </summary>
                  <div className="px-3 py-2 border-t border-slate-100 space-y-2">
                    {staticNarrative.missing_dependencies?.length > 0 && (
                      <div className="text-[11px]">
                        <span className="font-semibold text-amber-800">Missing dependencies: </span>
                        <span className="text-slate-700">{staticNarrative.missing_dependencies.join(", ")}</span>
                      </div>
                    )}
                    {staticNarrative.checks?.length > 0 && (
                      <div className="rounded border border-slate-200 divide-y divide-slate-100">
                        {staticNarrative.checks.map((check, i) => (
                          <div key={i} className="px-2 py-1.5 flex items-start gap-2">
                            <span className={`mt-0.5 w-3.5 h-3.5 rounded-full flex items-center justify-center flex-shrink-0 ${
                              check.status === "PASS" ? "bg-emerald-100 text-emerald-600"
                                : check.status === "WARN" ? "bg-amber-100 text-amber-600"
                                : "bg-red-100 text-red-600"
                            }`}>
                              {check.status === "PASS" ? <Check size={8} /> : check.status === "WARN" ? <AlertTriangle size={8} /> : <AlertCircle size={8} />}
                            </span>
                            <div className="min-w-0 flex-1 text-[11px] text-slate-700">
                              {toSafeText(check.details)}
                              {check.fix_suggestion && (
                                <div className="text-[10px] text-blue-600 mt-0.5">Fix: {toSafeText(check.fix_suggestion)}</div>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </details>
              )}
            </div>
          ) : (
            <div className="p-8 text-center">
              {status === "running" && progress.phase === "tester"
                ? <Loader2 size={24} className="mx-auto mb-2 text-blue-400 animate-spin" />
                : <Target size={24} className="mx-auto mb-2 text-slate-300" />}
              <p className="text-[11px] font-medium text-slate-600">
                {status === "running" && progress.phase === "tester" ? "Tester is compiling the transformed code" : "No compile run yet"}
              </p>
              <p className="text-[10px] text-slate-400 mt-1">
                {status === "running" && progress.phase === "tester"
                  ? "Per-component pass/fail + coverage will populate automatically when the build finishes."
                  : "Click \"Rerun compile\" to invoke the build tool you picked at step 1."}
              </p>
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderSuperAgentPanel = () => (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate-200 bg-white shadow-sm p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-3">
            <div className="w-11 h-11 rounded-xl bg-violet-100 text-violet-700 flex items-center justify-center">
              <Bot size={18} />
            </div>
            <div>
              <h4 className="text-sm font-semibold text-slate-800">Pipeline Overview</h4>
              <p className="text-[11px] text-slate-500">Detected source stack, chosen targets, file counts, and run summary.</p>
            </div>
          </div>
          {(status === "completed" || status === "completed_with_errors" || status === "stopped") && (
            <button onClick={reset} className="px-3 py-1.5 text-sm font-medium text-gray-600 bg-gray-100 rounded-lg hover:bg-gray-200">
              New Transform
            </button>
          )}
        </div>
        <div className="mt-4 grid md:grid-cols-2 xl:grid-cols-4 gap-3">
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-[10px] uppercase tracking-wide text-slate-500">Status</div>
            <div className="mt-1 text-lg font-semibold text-slate-800">{paused ? "Paused" : status || "Draft"}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-[10px] uppercase tracking-wide text-slate-500">Source Files</div>
            <div className="mt-1 text-lg font-semibold text-slate-800 tabular-nums">{sourceFiles.length || progress.filesTotal || files.length || 0}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-[10px] uppercase tracking-wide text-slate-500">Generated Files</div>
            <div className="mt-1 text-lg font-semibold text-slate-800 tabular-nums">{files.length || 0}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-[10px] uppercase tracking-wide text-slate-500">Average Confidence</div>
            <div className="mt-1 text-lg font-semibold text-slate-800 tabular-nums">
              {typeof result?.avg_confidence === "number" ? `${Math.round(result.avg_confidence * 100)}%` : "—"}
            </div>
          </div>
        </div>
        <div className="mt-4 grid xl:grid-cols-2 gap-4">
          <div className="rounded-xl border border-slate-200 p-4">
            <div className="text-[10px] uppercase tracking-wide text-slate-500 font-semibold mb-2">Detected source stack</div>
            {detectedStack?.raw_detection?.summary && (
              <div className="text-[12px] text-slate-600 mb-2 pb-2 border-b border-slate-100">
                {toSafeText(detectedStack.raw_detection.summary)}
              </div>
            )}
            <div className="space-y-2">
              {Object.entries(detectedStack || {}).filter(([key]) => key !== "raw_detection" && TECH_CATEGORIES[key]).length > 0 ? Object.entries(detectedStack || {})
                .filter(([key]) => key !== "raw_detection" && TECH_CATEGORIES[key])
                .map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-3 text-sm">
                  <span className="text-slate-500">{TECH_CATEGORIES[key]?.label || key}</span>
                  <span className="font-semibold text-slate-800">{TECH_CATEGORIES[key]?.options.find((opt) => opt.id === value)?.name || value || "—"}</span>
                </div>
              )) : <div className="text-[11px] text-slate-500">No detected stack yet.</div>}
            </div>
          </div>
          <div className="rounded-xl border border-slate-200 p-4">
            <div className="text-[10px] uppercase tracking-wide text-slate-500 font-semibold mb-2">Selected target stack</div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(selectedTransforms).filter(([, value]) => value).length > 0 ? Object.entries(selectedTransforms)
                .filter(([, value]) => value)
                .map(([cat, target]) => (
                  <span key={cat} className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-green-50 text-green-700 rounded-lg text-sm font-medium border border-green-200">
                    <Check size={12} />
                    {TECH_CATEGORIES[cat]?.label}: {TECH_CATEGORIES[cat]?.options.find((opt) => opt.id === target)?.name || target}
                  </span>
                )) : <div className="text-[11px] text-slate-500">No target stack selected.</div>}
            </div>
          </div>
        </div>
      </div>
      {renderKbSignalPanel()}
    </div>
  );

  const renderContextManagerPanel = () => (
    envelopes.length > 0
      ? (
        <div>
          {renderTraceabilityBanner()}
          {renderFrontendTraceability()}
          {renderEnvelopeTable()}
        </div>
      )
      : (
        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-200 bg-white shadow-sm p-6 text-center">
            <Layers size={22} className="mx-auto mb-2 text-slate-300" />
            <div className="text-sm font-semibold text-slate-700">Discovered APIs will appear here</div>
            <div className="text-[11px] text-slate-500 mt-1">
              {status === "running"
                ? "The Context Manager is scanning the uploaded source to identify endpoints, controllers, services, and DB tables."
                : "No architecture envelopes are available yet."}
            </div>
            {kb?.stats?.api_routes > 0 && (
              <div className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 bg-blue-50 text-blue-700 rounded-full text-[11px] font-semibold border border-blue-200">
                <Layers size={11} />
                {kb.stats.api_routes} API route{kb.stats.api_routes === 1 ? "" : "s"} detected in KB — envelopes pending
              </div>
            )}
          </div>
        </div>
      )
  );

  // iter-15.43 — Second definition removed; the stacked workspace above
  // is the single source of truth for the workspace router.

  // ==========================================================================
  // iter-15.54 — Render helpers for the redesigned header, input wizard and
  // 3-column running workspace. All existing render helpers above are reused
  // verbatim (they carry the data-testids + behaviour); these functions only
  // reorganise layout.
  // ==========================================================================

  const formatElapsed = (ms) => {
    const total = Math.max(0, Math.floor((ms || 0) / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const buildOptionsPresent = Object.keys(buildToolOptions).length > 0;
  const canStart = !!name.trim()
    && sourceFiles.length > 0
    && hasSelections
    && !creating
    && (!buildOptionsPresent || buildToolsConfirmed);

  const avgConfidence = (() => {
    const cs = files
      .map((f) => (typeof f.confidence === "number" ? (f.confidence > 1 ? f.confidence : f.confidence * 100) : null))
      .filter((x) => x != null);
    return cs.length ? Math.round(cs.reduce((a, b) => a + b, 0) / cs.length) : null;
  })();

  const headerMetrics = showAgentWorkspace ? [
    { label: "Elapsed", value: formatElapsed(elapsedMs) },
    // iter-16.x — Was labeled "Files", which collided with the "Files"
    // center tab (whose badge counts `files.length` = generated output
    // rows). The header shows SOURCE-file progress (`s.files_done /
    // s.files_total`), which naturally diverges from generated-output
    // count once a source file fans out into controller + DTO + service
    // + test (or compile-fix rewrites add rows). Relabel to "Source" +
    // tooltip so the two numbers on the page are self-explanatory.
    { label: "Source", value: (typeof progress.filesTotal === "number" && progress.filesTotal > 0) ? `${progress.filesDone || 0}/${progress.filesTotal}` : String(files.length), title: "Source files transformed so far / total source files planned" },
    { label: "Envelopes", value: String(envelopes.length) },
    { label: "Confidence", value: avgConfidence != null ? `${avgConfidence}%` : "—" },
  ] : [];

  const centerTab = AGENT_TO_CENTER_TAB[selectedAgentTab] || "overview";

  const renderStatusPill = () => {
    if (!status) return null;
    // iter-16.x — When a compile-fix loop is actively iterating on top of
    // an otherwise-"completed" pipeline (operator clicked "Rerun compile"
    // after the pipeline first finished), the green "Completed" pill is a
    // lie — the run is genuinely still working. Override the pill to the
    // live/running tone + spinner so the header status matches the sidebar
    // (Coder/Verifier/Tester rendered as "running") and the Compile Console.
    const cfpActive = isCompileFixActive(compileFixProgress);
    if (cfpActive && (status === "completed" || status === "completed_with_errors" || status === "stopped")) {
      const cfpPhase = compileFixProgress?.phase || "fixing";
      return (
        <span
          className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-200 bg-violet-50 text-violet-700 border-violet-200"
          title={compileFixProgress?.message || `Compile-fix loop — ${cfpPhase}`}
          data-testid="transformer-status-pill-compile-fix"
        >
          <RefreshCw size={12} className="animate-spin" />
          Compile-fix {cfpPhase}
        </span>
      );
    }
    const tone = status === "completed" ? "bg-emerald-50 text-emerald-700 border-emerald-200"
      : status === "completed_with_errors" ? "bg-amber-50 text-amber-700 border-amber-200"
      : status === "failed" ? "bg-red-50 text-red-700 border-red-200"
        : status === "stopped" ? "bg-slate-100 text-slate-600 border-slate-200"
          : (status === "awaiting_confirmation" || status === "awaiting_task_confirmation" || paused) ? "bg-amber-50 text-amber-700 border-amber-200"
            : "bg-violet-50 text-violet-700 border-violet-200";
    const icon = status === "completed" ? <CheckCircle size={12} />
      : status === "completed_with_errors" ? <AlertTriangle size={12} />
      : status === "failed" ? <AlertTriangle size={12} />
        : status === "stopped" ? <Square size={12} />
          : (status === "awaiting_confirmation" || status === "awaiting_task_confirmation") ? <AlertCircle size={12} />
            : paused ? <Pause size={12} />
              : <RefreshCw size={12} className="animate-spin" />;
    const label = paused ? "Paused"
      : status === "awaiting_confirmation" ? "Context review"
        : status === "awaiting_task_confirmation" ? "Planner review"
          : status === "completed_with_errors" ? "Completed — errors remain"
          : status.charAt(0).toUpperCase() + status.slice(1);
    return (
      <span
        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-200 ${tone}`}
        title={progress.phase_label || progress.message || label}
      >
        {icon}
        {label}
      </span>
    );
  };

  const renderHeaderActions = () => {
    if (status === "running") {
      return (
        <>
          {paused ? (
            <button
              onClick={handleResume}
              disabled={busyControl === "resume"}
              data-testid="transformer-resume-btn"
              className={`${BTN_PRIMARY} h-9 px-3 text-sm`}
              aria-label="Resume transformation"
              title="Resume transformation"
            >
              {busyControl === "resume" ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Resume
            </button>
          ) : (
            <button
              onClick={handlePause}
              disabled={busyControl === "pause" || stopped}
              data-testid="transformer-pause-btn"
              className={`${BTN_PRIMARY} h-9 px-3 text-sm`}
              aria-label="Pause transformation"
              title="Pause after the current file"
            >
              {busyControl === "pause" ? <Loader2 size={14} className="animate-spin" /> : <Pause size={14} />}
              Pause
            </button>
          )}
          <button
            onClick={() => setShowStopConfirm(true)}
            disabled={busyControl === "stop" || stopped}
            data-testid="transformer-stop-btn"
            className={`inline-flex items-center gap-2 rounded-lg border border-red-300 bg-white text-red-600 font-medium hover:bg-red-50 h-9 px-3 text-sm transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed ${FOCUS_RING}`}
            aria-label="Stop transformation"
            title="Stop transformation (keeps completed files)"
          >
            {busyControl === "stop" ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
            Stop
          </button>
        </>
      );
    }
    if (!showAgentWorkspace) {
      return (
        <button
          onClick={handleCreate}
          disabled={!canStart}
          className={`${BTN_PRIMARY} h-9 px-4 text-sm`}
          aria-label="Start transformation"
          title={canStart ? "Start transformation" : "Complete steps 1–4 to start"}
        >
          {creating ? <Loader2 size={14} className="animate-spin" /> : <Rocket size={14} />}
          Start
        </button>
      );
    }
    return null;
  };

  const KEBAB_ITEM = "w-full text-left px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 flex items-center gap-2 rounded-md";
  const renderKebabMenu = () => (
    <>
      <button
        onClick={() => { setShowKebab(false); reset(); }}
        className={KEBAB_ITEM}
        data-testid="transformer-new-project-btn"
      >
        <FilePlus size={14} /> New project
      </button>
      <div className="my-1 border-t border-slate-100" />
      <a
        href={transformId ? downloadTransformedCode(transformId) : "#"}
        className={`${KEBAB_ITEM} ${!transformId ? "pointer-events-none opacity-40" : ""}`}
        onClick={() => setShowKebab(false)}
        data-testid="transformer-download-btn"
      >
        <Download size={14} /> Download code ZIP
      </a>
      <a
        href={transformId ? downloadTransformedTests(transformId) : "#"}
        className={`${KEBAB_ITEM} ${!transformId ? "pointer-events-none opacity-40" : ""}`}
        onClick={() => setShowKebab(false)}
        data-testid="transformer-download-tests-btn"
      >
        <TestTube2 size={14} /> Download tests ZIP
      </a>
      <a
        href={transformId ? downloadTransformedBundle(transformId) : "#"}
        className={`${KEBAB_ITEM} ${!transformId ? "pointer-events-none opacity-40" : ""}`}
        onClick={() => setShowKebab(false)}
        data-testid="transformer-download-bundle-btn"
      >
        <Package size={14} /> Download bundle (code + tests)
      </a>
      <button
        onClick={openGithubPush}
        className={`${KEBAB_ITEM} disabled:opacity-40 disabled:cursor-not-allowed`}
        disabled={!transformId || pushLoading}
        data-testid="transformer-github-push-btn"
      >
        {pushLoading ? <Loader2 size={14} className="animate-spin" /> : <GitBranch size={14} />}
        Push to GitHub
      </button>
      <button
        onClick={openHistory}
        className={KEBAB_ITEM}
        data-testid="transformer-history-btn"
      >
        <History size={14} /> History
      </button>
      <div className="my-1 border-t border-slate-100" />
      <button
        onClick={() => { setShowKebab(false); setShowRemoveConfirm(true); }}
        disabled={!transformId}
        className={`w-full text-left px-3 py-2 text-sm text-red-600 hover:bg-red-50 flex items-center gap-2 rounded-md disabled:opacity-40 disabled:cursor-not-allowed`}
        data-testid="transformer-remove-btn"
      >
        <Trash2 size={14} /> Remove
      </button>
    </>
  );

  const stepShell = (id, num, Icon, title, subtitle, badge, body) => (
    <section id={`transformer-step-${id}`} className={`${CARD_CLS} overflow-hidden scroll-mt-4`}>
      <div className={`${CARD_HEAD_CLS} justify-between`}>
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-violet-50 text-violet-700 flex items-center justify-center flex-shrink-0 text-sm font-semibold">
            {Icon ? <Icon size={16} /> : num}
          </div>
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-slate-800 truncate">{title}</h3>
            {subtitle && <p className="text-xs text-slate-500 truncate">{subtitle}</p>}
          </div>
        </div>
        {badge}
      </div>
      <div className="p-5">{body}</div>
    </section>
  );

  const renderInputFlow = () => {
    const doneMap = {
      upload: sourceFiles.length > 0,
      analyse: !!detectedStack && !analyzing,
      target: hasSelections,
      build: buildOptionsPresent ? buildToolsConfirmed : hasSelections,
      start: false,
    };
    let assignedCurrent = false;
    const stepperSteps = [
      { key: "upload", label: "Upload source", sublabel: "ZIP or files" },
      { key: "analyse", label: "Analyse", sublabel: "Build the KB" },
      { key: "target", label: "Choose target stack", sublabel: "Per component" },
      { key: "build", label: "Confirm build system", sublabel: "Tooling per component" },
      { key: "start", label: "Start transformation", sublabel: "Run the pipeline" },
    ].map((s) => {
      let st;
      if (doneMap[s.key]) st = "done";
      else if (!assignedCurrent) { st = "current"; assignedCurrent = true; }
      else st = "pending";
      return { ...s, status: st };
    });
    const currentStepKey = stepperSteps.find((s) => s.status === "current")?.key;
    const onStepClick = (key) => {
      if (typeof document !== "undefined") {
        document.getElementById(`transformer-step-${key}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    };
    const shownFiles = sourceFiles.slice(0, 5);
    const extraFiles = sourceFiles.length - shownFiles.length;

    return (
      <div className="flex flex-col lg:flex-row gap-6">
        <aside className="lg:w-56 lg:flex-shrink-0 lg:sticky lg:top-4 self-start w-full">
          <TransformerStepper steps={stepperSteps} currentStep={currentStepKey} onStepClick={onStepClick} />
        </aside>

        <div className="flex-1 min-w-0 space-y-4">
          {/* Step 1 — Upload */}
          {stepShell("upload", 1, Upload, "Upload source", "Individual files or ZIP/TAR archives",
            doneMap.upload ? (
              <span className={`${CHIP_CLS} bg-emerald-50 text-emerald-700`}>
                <Check size={12} /> {sourceFiles.length} file{sourceFiles.length !== 1 ? "s" : ""}
              </span>
            ) : null,
            (
              <div className="space-y-4">
                <div>
                  <label htmlFor="transformer-name-input" className="text-xs font-medium text-slate-600 mb-1.5 block">
                    Transformation name
                  </label>
                  <input
                    id="transformer-name-input"
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g., Helidon to Spring Boot"
                    className={`w-full text-sm px-3 py-2 rounded-lg border border-slate-200 bg-white placeholder-slate-400 ${FOCUS_RING} focus-visible:border-violet-400`}
                  />
                </div>
                <div ref={dropRef} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}>
                  <input
                    type="file"
                    multiple
                    onChange={(e) => handleFilesChange(e.target.files)}
                    className="hidden"
                    id="source-upload"
                    accept=".zip,.tar,.gz"
                  />
                  <label
                    htmlFor="source-upload"
                    className={`flex flex-col items-center justify-center min-h-40 py-8 border-2 border-dashed rounded-xl cursor-pointer transition-all duration-200 ${
                      isDragging ? "border-violet-400 bg-violet-50" : "border-slate-300 hover:border-violet-300 hover:bg-slate-50"
                    }`}
                  >
                    <div className={`w-12 h-12 rounded-full flex items-center justify-center mb-3 ${isDragging ? "bg-violet-100 text-violet-600" : "bg-slate-100 text-slate-400"}`}>
                      <Upload size={22} />
                    </div>
                    <p className="text-sm font-medium text-slate-700">
                      {isDragging ? "Drop the files here" : "Drop a ZIP or click to upload"}
                    </p>
                    <p className="text-xs text-slate-500 mt-1">ZIP / TAR archives or individual source files</p>
                  </label>
                </div>
                {sourceFiles.length > 0 && (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-medium text-slate-600">
                        {sourceFiles.length} file{sourceFiles.length !== 1 ? "s" : ""} uploaded
                      </span>
                      <button
                        onClick={() => { setSourceFiles([]); setDetectedStack(null); }}
                        className={`text-xs text-slate-500 hover:text-red-600 flex items-center gap-1 rounded ${FOCUS_RING}`}
                      >
                        <Trash2 size={12} /> Clear all
                      </button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {shownFiles.map((f, i) => (
                        <span key={i} className={`${CHIP_CLS} bg-slate-100 text-slate-700 max-w-[220px]`}>
                          <Archive size={12} className="text-slate-400 flex-shrink-0" />
                          <span className="truncate">{f.name}</span>
                          <button
                            onClick={() => {
                              const nf = sourceFiles.filter((_, idx) => idx !== i);
                              setSourceFiles(nf);
                              if (nf.length === 0) setDetectedStack(null);
                            }}
                            data-testid={`transformer-source-remove-${i}`}
                            className="text-slate-400 hover:text-red-600 flex-shrink-0"
                            aria-label={`Remove ${f.name}`}
                          >
                            <X size={12} />
                          </button>
                        </span>
                      ))}
                      {extraFiles > 0 && (
                        <span className={`${CHIP_CLS} bg-slate-50 text-slate-500`}>+{extraFiles} more</span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )
          )}

          {/* Step 2 — Analyse */}
          {stepShell("analyse", 2, Database, "Analyse", "Detect the source stack & build the KB",
            analyzing ? (
              <span className={`${CHIP_CLS} bg-violet-50 text-violet-700`}>
                <RefreshCw size={12} className="animate-spin" /> Analysing
              </span>
            ) : (doneMap.analyse ? (
              <span className={`${CHIP_CLS} bg-emerald-50 text-emerald-700`}><Check size={12} /> Detected</span>
            ) : null),
            (sourceFiles.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 text-center">
                <div className="w-12 h-12 rounded-full bg-slate-100 text-slate-400 flex items-center justify-center mb-3">
                  <Database size={22} />
                </div>
                <p className="text-sm font-medium text-slate-600">Upload files first</p>
                <p className="text-xs text-slate-500 mt-1">The knowledge base is built from your uploaded source.</p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center gap-3 flex-wrap">
                  <button
                    onClick={handleReanalyse}
                    disabled={analyzing}
                    className={`${BTN_OUTLINE} h-9 px-4 text-sm`}
                  >
                    {analyzing ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                    {detectedStack ? "Re-analyse" : "Analyse KB"}
                  </button>
                  {analyzing && <span className="text-xs text-slate-500">Analysing source code…</span>}
                  {kb?.stats?.entities != null && (
                    <span className={`${CHIP_CLS} bg-violet-50 text-violet-700`}>
                      <Database size={12} /> {kb.stats.entities} KB entities
                    </span>
                  )}
                </div>
                {detectedStack && detectedStack.raw_detection && (
                  <div className="rounded-xl bg-slate-50 border border-slate-200 p-4">
                    <div className="text-xs uppercase tracking-wide text-slate-500 font-semibold mb-2">Detected source stack</div>
                    {detectedStack.raw_detection.summary && (
                      <div className="text-sm text-slate-700 font-medium mb-2">{toSafeText(detectedStack.raw_detection.summary)}</div>
                    )}
                    <div className="flex flex-wrap gap-2">
                      {(detectedStack.raw_detection.languages || []).slice(0, 5).map(([lang, count], i) => (
                        <span key={`lang-${i}`} className={`${CHIP_CLS} bg-amber-50 text-amber-700`}>
                          {lang}{typeof count === "number" ? ` (${count})` : ""}
                        </span>
                      ))}
                      {(detectedStack.raw_detection.frameworks || []).map((fw, i) => (
                        <span key={`fw-${i}`} className={`${CHIP_CLS} bg-violet-50 text-violet-700`}>{fw}</span>
                      ))}
                      {(detectedStack.raw_detection.databases || []).map((db, i) => (
                        <span key={`db-${i}`} className={`${CHIP_CLS} bg-emerald-50 text-emerald-700`}>{db}</span>
                      ))}
                      {detectedStack.raw_detection.build && (
                        <span className={`${CHIP_CLS} bg-slate-100 text-slate-700`}>{detectedStack.raw_detection.build}</span>
                      )}
                    </div>
                  </div>
                )}
                {detectedStack && !detectedStack.raw_detection && !analyzing && (
                  <div className="text-sm text-slate-600">Stack analysed. Continue to choose your target stack below.</div>
                )}
              </div>
            ))
          )}

          {/* Step 3 — Choose target stack */}
          {stepShell("target", 3, Sparkles, "Choose target stack", "Pick one target per component · Others for the full catalog",
            doneMap.target ? (
              <span className={`${CHIP_CLS} bg-emerald-50 text-emerald-700`}><Check size={12} /> Selected</span>
            ) : null,
            ((!detectedStack || analyzing) ? (
              <div className="flex flex-col items-center justify-center py-8 text-center">
                <div className="w-12 h-12 rounded-full bg-slate-100 text-slate-400 flex items-center justify-center mb-3">
                  <Sparkles size={22} />
                </div>
                <p className="text-sm font-medium text-slate-600">Analyse the KB first</p>
                <p className="text-xs text-slate-500 mt-1">Target recommendations appear once the source stack is detected.</p>
              </div>
            ) : (
              <div className="-my-1">
                <div className="divide-y divide-slate-100">
                  {categoryKeysToShow.length === 0 && (
                    <div className="py-6 text-center text-sm text-slate-500">
                      No target-stack categories match the uploaded source. Add more files
                      (e.g. backend, DB scripts) to see additional transformation options.
                    </div>
                  )}
                  {categoryKeysToShow.map((catKey) => {
                    const cat = TECH_CATEGORIES[catKey];
                    const CatIcon = cat.icon;
                    const detected = detectedStack[catKey];
                    const selected = selectedTransforms[catKey];
                    const detectedName = cat.options.find(o => o.id === detected)?.name || detected;
                    const relevantOptions = getRelevantOptions(catKey, detected);
                    const recommendations = SMART_RECOMMENDATIONS[catKey]?.[detected] || [];
                    const suggestedIds = new Set(relevantOptions.slice(0, 3).map(o => o.id));
                    const otherOptions = cat.options.filter(o => !suggestedIds.has(o.id) && o.id !== detected);
                    const isOtherSelected = selected && !suggestedIds.has(selected);
                    const selectedOpt = isOtherSelected ? cat.options.find(o => o.id === selected) : null;

                    if (!detected && relevantOptions.length === 0) return null;

                    return (
                      <div key={catKey} className="py-3">
                        <div className="flex items-center gap-2 mb-2">
                          <CatIcon size={14} className={detected ? "text-violet-600" : "text-slate-400"} />
                          <span className="text-xs font-semibold text-slate-700 uppercase tracking-wide">{cat.label}</span>
                          {detected && (
                            <span className="text-xs font-mono px-1.5 py-0.5 bg-slate-100 text-slate-600 rounded border border-slate-200">
                              Detected: {detectedName}
                            </span>
                          )}
                          {selected && (
                            <button
                              onClick={() => handleTransformChange(catKey, null)}
                              className={`ml-auto text-xs text-slate-400 hover:text-red-600 flex items-center gap-1 rounded ${FOCUS_RING}`}
                              title="Clear selection"
                            >
                              <X size={12} /> Clear
                            </button>
                          )}
                        </div>

                        <div className="space-y-1.5">
                          {relevantOptions.slice(0, 3).map((opt, idx) => {
                            const checked = selected === opt.id;
                            const isRecommended = idx === 0 && recommendations.includes(opt.id);
                            return (
                              <label
                                key={opt.id}
                                className={`block border rounded-lg px-3 py-2 cursor-pointer transition-all duration-200 ${
                                  checked
                                    ? "border-violet-500 bg-violet-50 shadow-sm"
                                    : "border-slate-200 bg-white hover:border-violet-300"
                                }`}
                              >
                                <div className="flex items-center gap-2">
                                  <input
                                    type="radio"
                                    name={`target-${catKey}`}
                                    className="shrink-0 accent-violet-600"
                                    checked={checked}
                                    onChange={() => handleTransformChange(catKey, opt.id)}
                                  />
                                  <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: opt.color }} />
                                  <span className="text-sm font-medium text-slate-800 flex-1 truncate">{opt.name}</span>
                                  {isRecommended && (
                                    <span className="text-xs uppercase tracking-wide px-1.5 py-0.5 bg-amber-100 text-amber-800 border border-amber-200 rounded font-semibold">
                                      Top pick
                                    </span>
                                  )}
                                  {checked && <Check size={14} className="text-emerald-600" />}
                                </div>
                              </label>
                            );
                          })}

                          {isOtherSelected && selectedOpt && (
                            <label className="block border rounded-lg px-3 py-2 border-violet-500 bg-violet-50 shadow-sm">
                              <div className="flex items-center gap-2">
                                <input type="radio" name={`target-${catKey}`} checked readOnly className="shrink-0 accent-violet-600" />
                                <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: selectedOpt.color }} />
                                <span className="text-sm font-medium text-slate-800 flex-1 truncate">{selectedOpt.name}</span>
                                <span className="text-xs uppercase tracking-wide px-1.5 py-0.5 bg-slate-100 text-slate-700 border border-slate-200 rounded font-semibold">
                                  Custom
                                </span>
                                <Check size={14} className="text-emerald-600" />
                              </div>
                            </label>
                          )}
                        </div>

                        {otherOptions.length > 0 && (
                          <div className="mt-2 relative">
                            <button
                              onClick={(e) => {
                                const isOpen = !!showOthers[catKey];
                                if (!isOpen) {
                                  const r = e.currentTarget.getBoundingClientRect();
                                  const vh = window.innerHeight;
                                  const popH = 420;
                                  const openUp = (vh - r.bottom) < popH && r.top > popH;
                                  setOthersAnchor(prev => ({
                                    ...prev,
                                    [catKey]: {
                                      left: r.left,
                                      top: openUp ? Math.max(8, r.top - popH - 6) : r.bottom + 4,
                                      width: Math.max(288, r.width),
                                    },
                                  }));
                                }
                                setShowOthers(prev => ({ ...prev, [catKey]: !prev[catKey] }));
                              }}
                              className={`text-xs font-semibold text-slate-600 hover:text-slate-900 flex items-center gap-1 rounded ${FOCUS_RING}`}
                              data-testid={`transformer-others-${catKey}`}
                            >
                              <Layers size={12} />
                              Browse all {otherOptions.length} {cat.label} options
                              <ChevronDown size={12} className={`transition-transform ${showOthers[catKey] ? "rotate-180" : ""}`} />
                            </button>
                            {showOthers[catKey] && (
                              <>
                                <div className="fixed inset-0 z-[60]" onClick={() => setShowOthers(prev => ({ ...prev, [catKey]: false }))} />
                                <div
                                  className="fixed z-[61] bg-white border border-slate-200 rounded-xl shadow-xl flex flex-col"
                                  style={{
                                    top: othersAnchor[catKey]?.top ?? 0,
                                    left: othersAnchor[catKey]?.left ?? 0,
                                    width: othersAnchor[catKey]?.width ?? 288,
                                    maxHeight: 420,
                                  }}
                                >
                                  <div className="px-3 py-2 text-xs uppercase tracking-wide text-slate-500 font-semibold border-b border-slate-100 flex items-center justify-between bg-slate-50 flex-shrink-0">
                                    <span>All {cat.label} options ({otherOptions.length})</span>
                                    <button onClick={() => setShowOthers(prev => ({ ...prev, [catKey]: false }))} className="text-slate-400 hover:text-slate-700">
                                      <X size={12} />
                                    </button>
                                  </div>
                                  <div className="overflow-y-auto py-1 flex-1 min-h-0">
                                    {otherOptions.map((opt) => (
                                      <button
                                        key={opt.id}
                                        onClick={() => {
                                          handleTransformChange(catKey, opt.id);
                                          setShowOthers(prev => ({ ...prev, [catKey]: false }));
                                        }}
                                        className={`w-full text-left px-3 py-2 flex items-center gap-2 text-sm hover:bg-slate-50 ${
                                          selected === opt.id ? "bg-violet-50" : ""
                                        }`}
                                      >
                                        <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: opt.color }} />
                                        <span className="flex-1 truncate">{opt.name}</span>
                                        {selected === opt.id && <Check size={13} className="text-emerald-600" />}
                                      </button>
                                    ))}
                                  </div>
                                </div>
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))
          )}

          {/* Step 4 — Confirm build system */}
          {stepShell("build", 4, Cpu, "Confirm build system", "Used by the compiler at the final stage",
            buildOptionsPresent ? (buildToolsConfirmed ? (
              <span data-testid="build-system-confirmed-badge" className={`${CHIP_CLS} bg-emerald-50 text-emerald-700`}>
                <Check size={12} /> Confirmed
              </span>
            ) : (
              <span className={`${CHIP_CLS} bg-amber-50 text-amber-700`}>Awaiting confirmation</span>
            )) : null,
            (!buildOptionsPresent ? (
              <div className="text-sm text-slate-600">
                {hasSelections
                  ? "No build-tool choices are required for the selected target stack."
                  : "Choose a target stack first to see build-tool options."}
              </div>
            ) : (
              <div data-testid="build-system-picker" className="space-y-3">
                {Object.entries(buildToolOptions).map(([component, tools]) => (
                  <div key={component} data-testid={`build-tool-row-${component}`} className="flex items-center gap-3 flex-wrap">
                    <span className="text-sm font-medium text-slate-700 w-24 capitalize">{component}</span>
                    <span className="text-xs text-slate-400 font-mono">{selectedTransforms[component]}</span>
                    <span className="text-slate-300">→</span>
                    <select
                      data-testid={`build-tool-select-${component}`}
                      value={selectedBuildTools[component] || tools[0]}
                      onChange={(e) => {
                        const v = e.target.value;
                        setSelectedBuildTools((prev) => ({ ...prev, [component]: v }));
                        setBuildToolsConfirmed(false);
                      }}
                      className={`text-sm border border-slate-200 rounded-lg px-2 py-1.5 bg-white ${FOCUS_RING}`}
                    >
                      {tools.map((t, idx) => (
                        <option key={t} value={t}>{t}{idx === 0 ? " (recommended)" : ""}</option>
                      ))}
                    </select>
                  </div>
                ))}
                <div className="pt-1 flex items-center gap-3 flex-wrap">
                  <button
                    data-testid="build-system-confirm-btn"
                    onClick={() => setBuildToolsConfirmed(true)}
                    disabled={buildToolsConfirmed}
                    className={`${BTN_PRIMARY} h-9 px-4 text-sm`}
                  >
                    <Check size={14} />
                    {buildToolsConfirmed ? "Confirmed" : "Confirm build system"}
                  </button>
                  <span className="text-xs text-slate-500">Required — the Planner will not start until confirmed.</span>
                </div>
              </div>
            ))
          )}

          {/* Step 5 — Start transformation */}
          {stepShell("start", 5, Rocket, "Start transformation", "SA → Context Manager → Planner → Coder → Verifier → Tester", null,
            (
              <div className="space-y-4">
                <div className="flex items-center gap-3">
                  <span className="text-xs font-medium text-slate-600">Pipeline mode</span>
                  <div className="inline-flex rounded-lg border border-slate-200 p-0.5 bg-slate-50">
                    {[["multi_agent", "Multi-agent"], ["single", "Single agent"]].map(([val, lbl]) => (
                      <button
                        key={val}
                        onClick={() => setPipelineMode(val)}
                        className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all duration-200 ${
                          pipelineMode === val ? "bg-violet-600 text-white" : "text-slate-600 hover:text-slate-900"
                        }`}
                      >
                        {lbl}
                      </button>
                    ))}
                  </div>
                </div>
                <button
                  onClick={handleCreate}
                  disabled={!canStart}
                  className={`${BTN_PRIMARY} w-full h-11 text-base`}
                >
                  {creating ? <RefreshCw size={18} className="animate-spin" /> : <Network size={18} />}
                  {creating
                    ? "Starting multi-agent pipeline…"
                    : (pipelineMode === "multi_agent" ? "Run multi-agent transformation" : "Run transformation")}
                </button>
                {!name.trim() && (<p className="text-xs text-center text-amber-600">Give the transformation a name to start.</p>)}
                {name.trim() && sourceFiles.length === 0 && (<p className="text-xs text-center text-amber-600">Upload source files to start.</p>)}
                {sourceFiles.length > 0 && !hasSelections && (<p className="text-xs text-center text-amber-600">Select at least one transformation target.</p>)}
                {hasSelections && buildOptionsPresent && !buildToolsConfirmed && (
                  <p className="text-xs text-center text-amber-600">Confirm the build system above before starting.</p>
                )}
              </div>
            )
          )}
        </div>
      </div>
    );
  };

  const renderAgentNodeIcon = (st, size) => (
    st === "completed" ? <Check size={size} />
      : st === "running" ? <Loader2 size={size} className="animate-spin" />
        : st === "failed" ? <AlertTriangle size={size} />
          : st === "awaiting" ? <AlertCircle size={size} />
            : <Bot size={size} />
  );

  const renderAgentRail = () => (
    <div className="h-full flex flex-col">
      <div className="px-3 py-3 border-b border-slate-100 flex items-center gap-2 flex-shrink-0">
        <Network size={14} className="text-violet-500" />
        <span className="text-sm font-semibold text-slate-800">Pipeline</span>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-1.5 min-h-0">
        {AGENT_ORDER.map((agent) => {
          const st = getAgentNodeStatus(agent);
          const isSelected = selectedAgentTab === agent;
          const leftBorder = st === "completed" ? "border-l-emerald-500"
            : st === "running" ? "border-l-violet-500"
              : st === "failed" ? "border-l-red-500"
                : st === "awaiting" ? "border-l-amber-500"
                  : st === "stopped" ? "border-l-slate-400"
                    : "border-l-slate-200";
          const iconTone = st === "completed" ? "bg-emerald-500 text-white"
            : st === "running" ? "bg-violet-500 text-white"
              : st === "failed" ? "bg-red-500 text-white"
                : st === "awaiting" ? "bg-amber-500 text-white"
                  : "bg-slate-300 text-white";
          const bar = st === "completed" ? "bg-emerald-500 w-full"
            : st === "running" ? "bg-violet-500 w-2/3 motion-safe:animate-pulse"
              : st === "awaiting" ? "bg-amber-500 w-1/2"
                : st === "failed" ? "bg-red-500 w-full"
                  : st === "stopped" ? "bg-slate-400 w-1/3"
                    : "bg-slate-200 w-0";
          return (
            <div
              key={agent}
              className={`relative rounded-lg border border-l-4 ${leftBorder} transition-all duration-200 ${
                isSelected ? "bg-violet-50 border-violet-200" : "bg-white border-slate-200 hover:bg-slate-50"
              }`}
            >
              <button
                type="button"
                onClick={() => handleSelectAgentTab(agent)}
                data-testid={`agent-pipeline-node-${agent}`}
                className={`w-full text-left pl-2.5 pr-9 py-2 rounded-lg ${FOCUS_RING}`}
                title={`${AGENT_META[agent].label}: ${st}${agentOverrideFlags[agent] ? " (overridden)" : ""}`}
                aria-label={`Inspect ${AGENT_META[agent].label} (${st})`}
              >
                <div className="flex items-center gap-2">
                  <div className={`w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 ${iconTone}`}>
                    {renderAgentNodeIcon(st, 14)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-slate-800 truncate flex items-center gap-1.5">
                      {AGENT_META[agent].shortLabel}
                      {agentOverrideFlags[agent] && <span className="w-1.5 h-1.5 rounded-full bg-amber-400" title="Overridden for this run" />}
                    </div>
                    <div className="text-xs text-slate-500 capitalize truncate">
                      {st}{st === "awaiting" ? " · review" : ""}
                    </div>
                  </div>
                </div>
                <div className="mt-1.5 h-1 rounded-full bg-slate-100 overflow-hidden">
                  <div className={`h-full rounded-full transition-[width] duration-500 ${bar}`} />
                </div>
              </button>
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); openAgentConfig(agent); }}
                className={`absolute top-2 right-2 w-6 h-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 flex items-center justify-center ${FOCUS_RING}`}
                title={`Configure ${AGENT_META[agent].label}`}
                aria-label={`Configure ${AGENT_META[agent].label}`}
              >
                <Settings2 size={12} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );

  const renderAgentRailHorizontal = () => (
    <div className="p-2 flex gap-2 overflow-x-auto">
      {AGENT_ORDER.map((agent) => {
        const st = getAgentNodeStatus(agent);
        const isSelected = selectedAgentTab === agent;
        const iconTone = st === "completed" ? "bg-emerald-500 text-white"
          : st === "running" ? "bg-violet-500 text-white"
            : st === "failed" ? "bg-red-500 text-white"
              : st === "awaiting" ? "bg-amber-500 text-white"
                : "bg-slate-300 text-white";
        return (
          <button
            key={agent}
            onClick={() => handleSelectAgentTab(agent)}
            data-testid={`agent-pipeline-node-${agent}`}
            className={`flex items-center gap-2 rounded-lg border px-3 py-2 flex-shrink-0 ${FOCUS_RING} ${
              isSelected ? "border-violet-300 bg-violet-50" : "border-slate-200 bg-white hover:bg-slate-50"
            }`}
            title={AGENT_META[agent].label}
            aria-label={`Inspect ${AGENT_META[agent].label} (${st})`}
          >
            <span className={`w-6 h-6 rounded-md flex items-center justify-center ${iconTone}`}>
              {renderAgentNodeIcon(st, 12)}
            </span>
            <span className="text-sm font-medium text-slate-700">{AGENT_META[agent].shortLabel}</span>
            {agentOverrideFlags[agent] && <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />}
          </button>
        );
      })}
    </div>
  );

  const renderCenterWorkspace = () => (
    <div className="h-full flex flex-col bg-white min-h-0">
      <div className="border-b border-slate-100 px-3 flex items-center gap-1 overflow-x-auto flex-shrink-0">
        {CENTER_TABS.map((t) => {
          const activeTab = centerTab === t.key;
          const count = t.key === "envelopes" ? envelopes.length
            : t.key === "planner" ? allTasks.length
              : t.key === "files" ? files.length
                : null;
          return (
            <button
              key={t.key}
              onClick={() => handleSelectAgentTab(t.agent)}
              className={`relative px-3 py-3 text-sm font-medium whitespace-nowrap transition-colors rounded ${FOCUS_RING} ${
                activeTab ? "text-violet-700" : "text-slate-500 hover:text-slate-800"
              }`}
              aria-current={activeTab ? "page" : undefined}
              title={
                t.key === "files"
                  ? "Generated output files persisted so far (differs from the header's Source count: one source file often produces multiple outputs, and compile-fix rewrites add rows)."
                  : t.key === "planner"
                    ? "Planner tasks generated for this transformation."
                    : t.key === "envelopes"
                      ? "Context envelopes prepared by the Context Manager."
                      : undefined
              }
            >
              <span className="flex items-center gap-1.5">
                {t.label}
                {count != null && count > 0 && (
                  <span className={`rounded-full px-1.5 py-0.5 text-xs font-semibold ${activeTab ? "bg-violet-100 text-violet-700" : "bg-slate-100 text-slate-500"}`}>
                    {count}
                  </span>
                )}
              </span>
              {activeTab && <span className="absolute bottom-0 left-2 right-2 h-0.5 bg-violet-600 rounded-full" />}
            </button>
          );
        })}
        <button
          onClick={() => openAgentConfig(selectedAgentTab)}
          className={`ml-auto my-1.5 ${BTN_OUTLINE} h-8 px-3 text-xs flex-shrink-0`}
          aria-label="Configure agent"
        >
          <Settings2 size={13} /> Configure
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 bg-slate-50/40 min-h-0">
        {centerTab === "overview" && renderSuperAgentPanel()}
        {centerTab === "files" && renderCoderPlannerStacked()}
        {centerTab === "planner" && renderPlannerPanel()}
        {centerTab === "envelopes" && renderContextManagerPanel()}
        {centerTab === "compile" && renderTesterPanel()}
      </div>
    </div>
  );

  const renderActivityRail = () => {
    const st = getAgentNodeStatus(selectedAgentTab);
    const recent = [...agentTimeline].slice(-8).reverse();
    return (
      <div className="h-full flex flex-col bg-white min-h-0">
        <div className="px-3 py-3 border-b border-slate-100 flex items-center justify-between flex-shrink-0">
          <span className="text-sm font-semibold text-slate-800">Activity</span>
          <button
            onClick={() => setActivityCollapsed(true)}
            className={`w-7 h-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 flex items-center justify-center ${FOCUS_RING}`}
            aria-label="Collapse activity rail"
            title="Collapse"
          >
            <PanelRightClose size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-3 min-h-0">
          <div className={`${CARD_CLS} p-4`}>
            <div className="flex items-center gap-2 mb-2">
              <div className="w-8 h-8 rounded-lg bg-violet-100 text-violet-700 flex items-center justify-center flex-shrink-0">
                <Bot size={16} />
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold text-slate-800 truncate">{selectedAgentMeta.label}</div>
                <div className="text-xs text-slate-500 capitalize">{st}</div>
              </div>
            </div>
            <p className="text-xs text-slate-600">{selectedAgentMeta.description}</p>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div>
                <div className="text-slate-500">Elapsed</div>
                <div className="font-semibold text-slate-800 tabular-nums">{formatElapsed(elapsedMs)}</div>
              </div>
              <div>
                <div className="text-slate-500">Override</div>
                <div className="font-semibold text-slate-800">{agentOverrideFlags[selectedAgentTab] ? "Yes" : "No"}</div>
              </div>
            </div>
            <button
              onClick={() => openAgentConfig(selectedAgentTab)}
              className={`${BTN_OUTLINE} w-full h-8 px-3 text-xs mt-3`}
            >
              <Settings2 size={13} /> Configure agent
            </button>
          </div>

          {selectedAgentTab === "context_manager" && status === "awaiting_confirmation" && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
              <div className="text-sm font-semibold text-amber-800 flex items-center gap-1.5"><AlertCircle size={14} /> Context review</div>
              <p className="text-xs text-amber-700 mt-1">Review the discovered architecture, then confirm to start the Planner.</p>
              <button onClick={handleConfirmPlan} className={`${BTN_PRIMARY} w-full h-9 text-sm mt-2`}>
                <Check size={14} /> Confirm &amp; continue
              </button>
            </div>
          )}
          {selectedAgentTab === "planner" && status === "awaiting_task_confirmation" && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
              <div className="text-sm font-semibold text-amber-800 flex items-center gap-1.5"><AlertCircle size={14} /> Planner review</div>
              <p className="text-xs text-amber-700 mt-1">
                {taskWaves.length} wave{taskWaves.length !== 1 ? "s" : ""} · {allTasks.length} task{allTasks.length !== 1 ? "s" : ""} ready.
              </p>
              <button onClick={handleConfirmTasks} className={`${BTN_PRIMARY} w-full h-9 text-sm mt-2`}>
                <Check size={14} /> Confirm tasks
              </button>
            </div>
          )}

          {/* iter-15.55 — Live Tester detail so the slow test-gen phase is
              never a silent black box. Shows overall counter, per-tier
              progress, in-flight envelopes, and the most-recent completed
              tests. */}
          {selectedAgentTab === "tester" && testerProgress && (
            <div className={`${CARD_CLS} p-4`} data-testid="tester-live-panel">
              <div className="flex items-center justify-between gap-2 mb-3">
                <div className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
                  <FlaskConical size={14} className="text-violet-600" /> Test generation
                </div>
                <span className="text-xs tabular-nums text-slate-500">
                  {(testerProgress.done || 0)}/{(testerProgress.total || 0)}
                </span>
              </div>
              {/* Overall progress bar */}
              <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden mb-1">
                <div
                  className="h-full bg-violet-500 transition-[width] duration-500"
                  style={{
                    width: testerProgress.total
                      ? `${Math.min(100, Math.round((testerProgress.done / testerProgress.total) * 100))}%`
                      : "0%",
                  }}
                />
              </div>
              <div className="flex items-center justify-between text-[11px] text-slate-500 mb-3">
                <span>
                  {testerProgress.completed_at
                    ? `Completed in ${Math.round((testerProgress.duration_ms || 0) / 1000)}s`
                    : `Model: ${testerProgress.model || "auto"}`}
                </span>
                {!testerProgress.completed_at && (testerProgress.in_flight?.length || 0) > 0 && (
                  <span className="inline-flex items-center gap-1 text-violet-700">
                    <Loader2 size={11} className="animate-spin" />
                    {testerProgress.in_flight.length} in flight
                  </span>
                )}
              </div>

              {/* Per-tier bars */}
              <div className="space-y-2 mb-3">
                {Object.entries(testerProgress.per_tier || {}).map(([tier, tp]) => {
                  const done = tp?.done || 0;
                  const total = tp?.total || 0;
                  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
                  const tone = tier === "business" ? "bg-emerald-500"
                    : tier === "api" ? "bg-violet-500"
                    : "bg-amber-500";
                  return (
                    <div key={tier} data-testid={`tester-tier-${tier}`}>
                      <div className="flex items-center justify-between text-[11px] mb-0.5">
                        <span className="text-slate-600 capitalize font-medium">{tier}</span>
                        <span className="tabular-nums text-slate-500">{done}/{total}</span>
                      </div>
                      <div className="h-1 rounded-full bg-slate-100 overflow-hidden">
                        <div className={`h-full ${tone} transition-[width] duration-500`} style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* In-flight tests */}
              {(testerProgress.in_flight?.length || 0) > 0 && (
                <div className="mb-3">
                  <div className="text-[11px] uppercase tracking-wide text-slate-500 font-semibold mb-1.5">Generating now</div>
                  <div className="space-y-1">
                    {testerProgress.in_flight.slice(0, 6).map((slot, i) => (
                      <div key={slot.key || i} className="flex items-center gap-2 rounded-md bg-violet-50 border border-violet-100 px-2 py-1.5">
                        <Loader2 size={11} className="text-violet-600 animate-spin flex-shrink-0" />
                        <span className="inline-flex items-center h-4 px-1.5 rounded-full bg-white text-[10px] font-medium text-violet-700 border border-violet-200 uppercase tracking-wide">
                          {slot.tier}
                        </span>
                        <span className="text-xs text-slate-700 font-mono truncate">{slot.envelope}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Recent completions */}
              {(testerProgress.recent?.length || 0) > 0 && (
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-slate-500 font-semibold mb-1.5">Latest files</div>
                  <div className="space-y-1 max-h-56 overflow-y-auto">
                    {[...(testerProgress.recent || [])].reverse().slice(0, 12).map((row, i) => {
                      const tone = row.tier === "business" ? "text-emerald-700 bg-emerald-50 border-emerald-200"
                        : row.tier === "api" ? "text-violet-700 bg-violet-50 border-violet-200"
                        : "text-amber-700 bg-amber-50 border-amber-200";
                      return (
                        <div key={i} className="flex items-center gap-2 rounded-md border border-slate-100 bg-white px-2 py-1">
                          <Check size={11} className="text-emerald-500 flex-shrink-0" />
                          <span className={`inline-flex items-center h-4 px-1.5 rounded-full text-[10px] font-medium border uppercase tracking-wide ${tone}`}>
                            {row.tier}
                          </span>
                          <span className="text-xs text-slate-700 font-mono truncate flex-1" title={row.path}>{row.path.split("/").slice(-2).join("/")}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}
          {selectedAgentTab === "tester" && !testerProgress && status === "running" && (
            <div className={`${CARD_CLS} p-4`}>
              <div className="text-sm font-semibold text-slate-800 flex items-center gap-1.5 mb-1">
                <FlaskConical size={14} className="text-violet-600" /> Test generation
              </div>
              <div className="text-xs text-slate-500">Waiting for tester to start — no progress reported yet.</div>
            </div>
          )}

          {/* iter-15.59 — Tester-embedded Compile Console + Test Report */}
          {selectedAgentTab === "tester" && (compileFixProgress || compilationResult) && (
            <div className={`${CARD_CLS} p-4`} data-testid="tester-compile-panel">
              <div className="text-sm font-semibold text-slate-800 flex items-center gap-1.5 mb-2">
                <TerminalSquare size={14} className="text-blue-500" /> Compile Console
              </div>
              {compileFixProgress && (
                <div className="mb-2">
                  <div className="flex items-center gap-1.5 flex-wrap text-xs">
                    {compileFixProgress.phase === "passed" ? (
                      <span className="inline-flex items-center gap-1 text-emerald-700 font-semibold">
                        <CheckCircle size={12} /> Build green
                      </span>
                    ) : ["exhausted", "unfixable", "infra_blocked", "stagnant", "stalled"].includes(compileFixProgress.phase) ? (
                      <span className="inline-flex items-center gap-1 text-amber-700 font-semibold">
                        <AlertTriangle size={12} /> Fix loop stopped
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-violet-700 font-semibold">
                        <Loader2 size={12} className="animate-spin" />
                        Iter {compileFixProgress.iteration || 1}
                        {compileFixProgress.max_iterations ? `/${compileFixProgress.max_iterations}` : ""}
                        {" "}· {compileFixProgress.phase}
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] text-slate-600 mt-1 break-words">
                    {compileFixProgress.message || ""}
                  </div>
                  {compileFixProgress.current_file && (
                    <div className="text-[10px] text-slate-500 font-mono truncate mt-1">
                      ↳ {compileFixProgress.current_file}
                    </div>
                  )}
                </div>
              )}
              {compilationResult && (
                <div className="grid grid-cols-3 gap-2 text-center">
                  <div className={`rounded-lg border p-2 ${
                    compilationResult.compilation_ready
                      ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"
                  }`}>
                    <div className="text-[10px] uppercase tracking-wide text-slate-500">Score</div>
                    <div className={`text-sm font-bold ${
                      compilationResult.compilation_ready ? "text-emerald-700" : "text-red-700"
                    }`}>{compilationResult.overall_score ?? 0}%</div>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <div className="text-[10px] uppercase tracking-wide text-slate-500">Iterations</div>
                    <div className="text-sm font-bold text-slate-800">{compilationResult.iterations_used || 1}</div>
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-white p-2">
                    <div className="text-[10px] uppercase tracking-wide text-slate-500">Components</div>
                    <div className="text-sm font-bold text-slate-800">{(compilationResult.components || []).length}</div>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* iter-19 — DevOps gate. A green compile proves the code builds
              on this machine today; this says whether it will build the
              same way on a clean runner next month. Its verdict now
              decides the run's final status, so it has to be visible and
              it has to say what it wants changed. */}
          {selectedAgentTab === "tester" && dependencyAudit && (
            <div className="rounded-xl border border-[#E6E6E6] bg-white p-3 space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-xs font-display font-bold text-[#2E2E38]">DevOps — Dependency Audit</div>
                <span
                  data-testid="devops-audit-verdict"
                  className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded-sm ${
                    dependencyAudit.production_ready
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-red-100 text-red-700"
                  }`}
                >
                  {dependencyAudit.production_ready ? "Production ready" : "Not production ready"}
                </span>
              </div>

              {dependencyAudit.summary && (
                <div className="text-[11px] text-slate-600 break-words">{dependencyAudit.summary}</div>
              )}

              {/* What the agent actually did about it — the audit is no
                  longer a read-only opinion. */}
              {(dependencyAudit.remediation_rounds || []).length > 0 && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-2 space-y-1">
                  <div className="text-[10px] uppercase tracking-wide text-slate-500">
                    Remediation — {dependencyAudit.remediation_rounds_used || 0} round(s)
                  </div>
                  {(dependencyAudit.remediation_rounds || []).map((r) => (
                    <div key={r.round} className="text-[10px] text-slate-600 flex items-baseline gap-1.5">
                      <span className="font-mono text-slate-400">#{r.round}</span>
                      <span className="font-semibold">{r.status}</span>
                      {typeof r.findings_before === "number" && (
                        <span className="text-slate-500">
                          {r.findings_before} → {r.findings_after} finding(s)
                        </span>
                      )}
                      {r.summary && <span className="text-slate-500 break-words">{r.summary}</span>}
                    </div>
                  ))}
                </div>
              )}

              {(dependencyAudit.findings || []).length > 0 ? (
                <div className="space-y-1 max-h-64 overflow-y-auto">
                  {(dependencyAudit.findings || []).map((f, i) => {
                    const sev = String(f.severity || "").toLowerCase();
                    const tone =
                      sev === "critical" ? "border-red-200 bg-red-50 text-red-700"
                        : sev === "major" ? "border-amber-200 bg-amber-50 text-amber-700"
                          : "border-slate-200 bg-slate-50 text-slate-600";
                    return (
                      <div key={`${f.manifest}-${i}`} className={`rounded-md border p-2 ${tone}`}>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[9px] uppercase font-bold">{sev || "note"}</span>
                          {f.manifest && (
                            <span className="text-[10px] font-mono text-slate-500 truncate">{f.manifest}</span>
                          )}
                        </div>
                        <div className="text-[11px] mt-0.5 break-words">{f.issue}</div>
                        {f.fix && (
                          <div className="text-[10px] mt-0.5 text-slate-600 break-words">
                            <span className="font-semibold">Fix: </span>{f.fix}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="text-[11px] text-slate-500">
                  No outstanding manifest findings.
                </div>
              )}
            </div>
          )}

          {/* iter-15.59 — Graphical test-execution report. */}
          {selectedAgentTab === "tester" && (() => {
            const tr = compilationResult?.coverage?.test_report;
            if (!tr || !tr.totals || tr.totals.total === 0) return null;
            const totals = tr.totals || {};
            const perTier = tr.per_tier || {};
            const rate = tr.pass_rate ?? 0;
            const bar = (b) => {
              const t = b.total || 0;
              if (!t) return { p: 0, f: 0, s: 0 };
              return {
                p: (b.passed || 0) * 100 / t,
                f: (b.failed || 0) * 100 / t,
                s: (b.skipped || 0) * 100 / t,
              };
            };
            const tiers = [
              { key: "business",    label: "Business",    color: "emerald" },
              { key: "api",         label: "API",         color: "violet" },
              { key: "integration", label: "Integration", color: "amber" },
            ];
            return (
              <div className={`${CARD_CLS} p-4`} data-testid="tester-report-panel">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
                    <FlaskConical size={14} className="text-violet-600" /> Test Report
                  </div>
                  <div className={`text-xs font-bold px-2 py-0.5 rounded-full ${
                    rate >= 90 ? "bg-emerald-100 text-emerald-700"
                    : rate >= 70 ? "bg-amber-100 text-amber-700"
                    : "bg-red-100 text-red-700"
                  }`}>
                    {rate}% pass
                  </div>
                </div>

                <div className="grid grid-cols-4 gap-2 mb-3 text-center">
                  <div className="rounded-lg bg-slate-50 border border-slate-200 p-2">
                    <div className="text-[10px] uppercase text-slate-500">Total</div>
                    <div className="text-sm font-bold text-slate-800">{totals.total}</div>
                  </div>
                  <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-2">
                    <div className="text-[10px] uppercase text-emerald-700">Passed</div>
                    <div className="text-sm font-bold text-emerald-700">{totals.passed}</div>
                  </div>
                  <div className="rounded-lg bg-red-50 border border-red-200 p-2">
                    <div className="text-[10px] uppercase text-red-700">Failed</div>
                    <div className="text-sm font-bold text-red-700">{totals.failed}</div>
                  </div>
                  <div className="rounded-lg bg-slate-100 border border-slate-200 p-2">
                    <div className="text-[10px] uppercase text-slate-500">Skipped</div>
                    <div className="text-sm font-bold text-slate-600">{totals.skipped}</div>
                  </div>
                </div>

                <div className="space-y-2 mb-3">
                  {tiers.map((t) => {
                    const b = perTier[t.key] || { total: 0, passed: 0, failed: 0, skipped: 0 };
                    const w = bar(b);
                    return (
                      <div key={t.key} data-testid={`tester-report-tier-${t.key}`}>
                        <div className="flex items-center justify-between text-[11px] mb-1">
                          <span className="font-medium text-slate-700">{t.label}</span>
                          <span className="text-slate-500">
                            {b.passed}/{b.total} passed{b.failed ? ` · ${b.failed} failed` : ""}
                            {b.skipped ? ` · ${b.skipped} skipped` : ""}
                          </span>
                        </div>
                        <div className="w-full h-2.5 rounded-full bg-slate-100 overflow-hidden flex">
                          <div className="bg-emerald-500 h-full" style={{ width: `${w.p}%` }} />
                          <div className="bg-red-500 h-full" style={{ width: `${w.f}%` }} />
                          <div className="bg-slate-400 h-full" style={{ width: `${w.s}%` }} />
                        </div>
                      </div>
                    );
                  })}
                </div>

                {Array.isArray(tr.cases) && tr.cases.filter((c) => c.status === "failed").length > 0 && (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-red-700 font-semibold mb-1">
                      Failing tests ({tr.cases.filter((c) => c.status === "failed").length})
                    </div>
                    <div className="space-y-1 max-h-40 overflow-auto">
                      {tr.cases.filter((c) => c.status === "failed").slice(0, 12).map((c, i) => (
                        <div key={i} className="rounded border border-red-100 bg-red-50 px-2 py-1.5">
                          <div className="text-[11px] font-mono text-red-800 truncate">
                            {c.classname ? `${c.classname}.` : ""}{c.name}
                          </div>
                          {c.message && (
                            <div className="text-[10px] text-red-600 truncate mt-0.5">{c.message}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })()}

          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500 font-semibold mb-2 px-1">Recent activity</div>
            {recent.length === 0 ? (
              <div className="text-xs text-slate-500 px-1">No agent events yet.</div>
            ) : (
              <div className="space-y-1.5">
                {recent.map((row, i) => {
                  const rs = String(row.status || "").toLowerCase();
                  const dot = (rs === "completed" || rs === "done") ? "bg-emerald-500"
                    : (rs === "running" || rs === "in_progress") ? "bg-violet-500"
                      : rs === "failed" ? "bg-red-500"
                        : "bg-slate-300";
                  return (
                    <div key={i} className="flex items-start gap-2 rounded-lg border border-slate-100 bg-white px-2.5 py-2">
                      <span className={`w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${dot}`} />
                      <div className="min-w-0">
                        <div className="text-xs font-medium text-slate-700 truncate">{AGENT_META[row.agent]?.label || row.agent}</div>
                        {row.output_summary && <div className="text-xs text-slate-500 line-clamp-2">{toSafeText(row.output_summary)}</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  };

  const renderRunningWorkspace = () => {
    const orphanBanner = progress.phase === "paused_orphan" ? (
      <div className="mb-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-sm" role="status">
        <Pause size={14} className="text-amber-600" />
        <span>Interrupted by restart — Resume from file {progress.filesDone || 0}.</span>
      </div>
    ) : null;

    if (bp.xl) {
      return (
        <div>
          {orphanBanner}
          <div className="h-[calc(100vh-11rem)] min-h-[520px]">
            <PanelGroup
              key={activityCollapsed ? "collapsed" : "expanded"}
              direction="horizontal"
              className="h-full rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden"
            >
              <Panel defaultSize={18} minSize={13} maxSize={26}>
                <div className="h-full border-r border-slate-200">{renderAgentRail()}</div>
              </Panel>
              <PanelResizeHandle className="w-1 bg-slate-100 hover:bg-violet-300 transition-colors" />
              <Panel minSize={30}>{renderCenterWorkspace()}</Panel>
              {!activityCollapsed && (
                <>
                  <PanelResizeHandle className="w-1 bg-slate-100 hover:bg-violet-300 transition-colors" />
                  <Panel defaultSize={24} minSize={16} maxSize={34}>
                    <div className="h-full border-l border-slate-200">{renderActivityRail()}</div>
                  </Panel>
                </>
              )}
            </PanelGroup>
          </div>
          {activityCollapsed && (
            <button
              onClick={() => setActivityCollapsed(false)}
              className={`fixed right-4 bottom-24 z-40 ${BTN_OUTLINE} h-10 w-10 p-0 rounded-full shadow-md`}
              aria-label="Show activity rail"
              title="Show activity rail"
            >
              <PanelRightOpen size={18} />
            </button>
          )}
        </div>
      );
    }

    return (
      <div className="space-y-4">
        {orphanBanner}
        <div className={`${CARD_CLS} overflow-hidden`}>
          <div className="px-4 py-3 border-b border-slate-100 flex items-center gap-2">
            <Network size={14} className="text-violet-500" />
            <span className="text-sm font-semibold text-slate-800">Pipeline</span>
            <button
              onClick={() => setActivityCollapsed((v) => !v)}
              className={`ml-auto ${BTN_OUTLINE} h-8 px-3 text-xs`}
              aria-label={activityCollapsed ? "Show activity" : "Hide activity"}
            >
              {activityCollapsed ? <PanelRightOpen size={13} /> : <PanelRightClose size={13} />} Activity
            </button>
          </div>
          {renderAgentRailHorizontal()}
        </div>
        <div className={`${CARD_CLS} overflow-hidden`} style={{ minHeight: 480 }}>
          {renderCenterWorkspace()}
        </div>
        {!activityCollapsed && (
          <div className={`${CARD_CLS} overflow-hidden`} style={{ maxHeight: 480 }}>
            {renderActivityRail()}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-white">
      <TransformerHeader
        title={name || "Code Transformer"}
        subtitle="KB-driven cross-stack transformation"
        metrics={headerMetrics}
        statusPill={renderStatusPill()}
        actions={renderHeaderActions()}
        kebabOpen={showKebab}
        onKebabToggle={() => setShowKebab((v) => !v)}
        kebabContent={renderKebabMenu()}
      />
      {/* Main Content — iter-15.54 header + wizard / running workspace body */}
      <div className="flex-1 overflow-y-auto min-h-0">
        <div className="w-full px-6 py-6 pb-28">
          {status === "failed" && (
            <div role="alert" className="mb-4 rounded-xl border border-red-200 bg-red-50 p-4">
              <div className="flex items-start gap-3">
                <div className="w-9 h-9 rounded-lg bg-red-100 text-red-600 flex items-center justify-center flex-shrink-0">
                  <AlertCircle size={18} />
                </div>
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-semibold text-red-800">Transformation failed</h3>
                  <p className="text-sm text-red-700 mt-0.5 break-words">{error || "An unexpected error occurred"}</p>
                  <div className="mt-3 flex items-center gap-2 flex-wrap">
                    <button onClick={reset} className={`${BTN_PRIMARY} h-8 px-3 text-sm`} aria-label="Try again">
                      <RefreshCw size={13} /> Try again
                    </button>
                    <button onClick={copyError} className={`${BTN_OUTLINE} h-8 px-3 text-sm`} aria-label="Copy error to clipboard">
                      <Copy size={13} /> Copy error
                    </button>
                    <button onClick={() => handleSelectAgentTab("super_agent")} className={`${BTN_OUTLINE} h-8 px-3 text-sm`} aria-label="Open logs">
                      <Network size={13} /> Open logs
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {showAgentWorkspace ? renderRunningWorkspace() : renderInputFlow()}
          {/* iter-15.19 — Agent Pipeline config panel (click-through modal) */}
          {agentConfigAgent && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={closeAgentConfig}>
              <div
                className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="px-5 py-3 border-b border-gray-100 flex items-center gap-2">
                  <Settings2 size={16} className="text-violet-600" />
                  <h3 className="text-sm font-semibold text-gray-800">
                    {agentConfigData?.label || agentConfigAgent} — Prompt &amp; Model
                  </h3>
                  {agentConfigData?.is_overridden && (
                    <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200">
                      Overridden for this transformation
                    </span>
                  )}
                  <button onClick={closeAgentConfig} className="ml-auto text-slate-400 hover:text-slate-600">
                    <X size={18} />
                  </button>
                </div>

                <div className="px-5 py-4 overflow-y-auto flex-1 space-y-4">
                  {agentConfigLoading && (
                    <div className="flex items-center gap-2 text-sm text-slate-500 py-8 justify-center">
                      <Loader2 size={16} className="animate-spin" /> Loading agent config...
                    </div>
                  )}
                  {agentConfigError && (
                    <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                      {agentConfigError}
                    </div>
                  )}
                  {!agentConfigLoading && agentConfigData && (
                    <>
                      {agentConfigData.llm_backed === false && (
                        <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                          This agent is orchestration-only today — it does not yet call an LLM, so editing its
                          prompt/model here has no runtime effect. Saved for future-proofing.
                        </div>
                      )}
                      <div className="text-[11px] text-slate-500">
                        These changes apply <strong>only to this transformation</strong> ({transformId}) — the shared
                        Prompt Library default (<code className="text-slate-600">{agentConfigData.prompt_key}</code>) is never modified.
                      </div>

                      <div>
                        <label className="text-xs font-semibold text-slate-700 mb-1 block">System Prompt</label>
                        <textarea
                          value={agentConfigDraftPrompt}
                          onChange={(e) => setAgentConfigDraftPrompt(e.target.value)}
                          rows={12}
                          className="w-full text-[11px] font-mono border border-slate-200 rounded-lg px-3 py-2 focus:border-[#FFE600] focus:ring-1 focus:ring-[#FFE600] outline-none resize-y"
                        />
                      </div>

                      <div>
                        <label className="text-xs font-semibold text-slate-700 mb-1 block">Model</label>
                        <select
                          value={agentConfigDraftModel}
                          onChange={(e) => setAgentConfigDraftModel(e.target.value)}
                          className="text-[11px] border border-slate-200 rounded px-2 py-1.5 bg-white focus:border-[#FFE600] focus:ring-1 focus:ring-[#FFE600] outline-none w-full max-w-[320px]"
                        >
                          <option value="">
                            {agentConfigData.run_default_model
                              ? `Default for this run (${agentConfigData.run_default_model})`
                              : (factoryEnabled ? "Auto (Factory Droid picks)" : "Auto (Console routing)")}
                          </option>
                          {effectiveModelOptions.map(({ id, label }) => (
                            id ? <option key={id} value={id}>{label}</option> : null
                          ))}
                        </select>
                        {factoryEnabled && (
                          <div className="text-[10px] text-violet-700 bg-violet-50 border border-violet-200 rounded px-2 py-1 mt-1 flex items-center gap-1">
                            <Bot size={11} /> Factory Droid is enabled for this project — showing Droid's model catalogue.
                            Console/Ollama model picks are ignored while Factory routing is active.
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>

                <div className="px-5 py-3 border-t border-gray-100 flex items-center gap-2 flex-wrap">
                  <button
                    onClick={handleResetAgentConfig}
                    disabled={agentConfigSaving || agentConfigLoading || !agentConfigData?.is_overridden}
                    className="px-3 py-2 text-xs font-medium text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
                  >
                    <RotateCcw size={13} /> Reset to default
                  </button>
                  <button
                    onClick={handleSaveAgentConfig}
                    disabled={agentConfigSaving || agentConfigLoading}
                    className="px-3 py-2 text-xs font-semibold bg-[#2E2E38] text-white rounded-lg hover:bg-[#1f1f27] disabled:opacity-50 flex items-center gap-1.5"
                  >
                    {agentConfigSaving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                    Save
                  </button>
                  <button
                    onClick={handleRerunPipeline}
                    disabled={rerunning || status === "running" || agentConfigLoading}
                    title={status === "running" ? "Pipeline is currently running" : "Rerun the whole pipeline with saved overrides"}
                    className="px-3 py-2 text-xs font-semibold bg-[#FFE600] text-[#2E2E38] rounded-lg hover:bg-[#FFD500] disabled:opacity-50 flex items-center gap-1.5 ml-auto"
                  >
                    {rerunning ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                    Rerun Pipeline
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* iter-15.20 — Envelope detail panel: click any row in the
              "Discovered Architecture" table to see its full controller →
              service → repository → DB vertical slice, business logic
              summary, external calls, and acceptance criteria. */}
          {selectedEnvelope && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setSelectedEnvelope(null)}>
              <div
                className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}
                data-testid="envelope-detail-panel"
              >
                <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between gap-3 sticky top-0 bg-white">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold uppercase shrink-0 ${
                      selectedEnvelope.endpoint_method === "GET" ? "bg-blue-100 text-blue-700" :
                      selectedEnvelope.endpoint_method === "POST" ? "bg-green-100 text-green-700" :
                      selectedEnvelope.endpoint_method === "PUT" ? "bg-amber-100 text-amber-700" :
                      selectedEnvelope.endpoint_method === "DELETE" ? "bg-red-100 text-red-700" :
                      "bg-slate-100 text-slate-700"
                    }`}>
                      {selectedEnvelope.endpoint_method || selectedEnvelope.layer || "INFRA"}
                    </span>
                    <h3 className="text-sm font-mono font-semibold text-slate-800 truncate" title={selectedEnvelope.endpoint_path}>
                      {selectedEnvelope.endpoint_path || selectedEnvelope.controller_class || "Infrastructure component"}
                    </h3>
                  </div>
                  <button onClick={() => setSelectedEnvelope(null)} className="text-slate-400 hover:text-slate-600 shrink-0">
                    <X size={18} />
                  </button>
                </div>

                <div className="px-5 py-4 space-y-4 text-sm">
                  {selectedEnvelope.is_outbound_client && (
                    <div className="flex items-start gap-2 p-2.5 rounded-lg bg-sky-50 border border-sky-200 text-sky-800 text-xs">
                      <Network size={14} className="mt-0.5 shrink-0" />
                      <span>
                        This is an <b>outbound REST-CLIENT call</b> — a call this service MAKES to
                        another service. It is not part of this service's own exposed API surface.
                      </span>
                    </div>
                  )}

                  <div>
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Business logic</div>
                    <p className="text-slate-700">{selectedEnvelope.business_logic_summary || "—"}</p>
                  </div>

                  {/* iter-15.30 — API Contract: request/response payload details.
                      Backend already computes these (routes/tools.py::_row_to_envelope,
                      tools_kb_builder.build_traceability_map) but the modal never
                      rendered them — this was the "not showing proper payload/API
                      details" gap. */}
                  {(selectedEnvelope.request || selectedEnvelope.response) && (
                    <div className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-3">
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-indigo-500 mb-2 flex items-center gap-1.5">
                        <FileJson size={12} /> API Contract
                      </div>
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Request payload</div>
                          <div className="text-slate-800 font-mono text-xs">
                            {selectedEnvelope.request?.dto_class || "—"}
                          </div>
                          {selectedEnvelope.request?.framework && (
                            <div className="text-slate-400 text-[10px] mt-0.5">framework: {selectedEnvelope.request.framework}</div>
                          )}
                          {selectedEnvelope.request?.note && (
                            <div className="text-amber-600 text-[10px] mt-1">{selectedEnvelope.request.note}</div>
                          )}
                        </div>
                        <div>
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Response payload</div>
                          <div className="text-slate-800 font-mono text-xs">
                            {selectedEnvelope.response?.result_type || "—"}
                          </div>
                          {selectedEnvelope.response?.wrapper && (
                            <div className="text-slate-400 text-[10px] mt-0.5">wrapped in: {selectedEnvelope.response.wrapper}</div>
                          )}
                          {selectedEnvelope.response?.note && (
                            <div className="text-amber-600 text-[10px] mt-1">{selectedEnvelope.response.note}</div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {(selectedEnvelope.service_layer?.service_impl || selectedEnvelope.data_layer?.repository_class || (selectedEnvelope.data_layer?.table_trace || []).length > 0) && (
                    <div className="rounded-lg border border-slate-200 p-3">
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-2 flex items-center gap-1.5">
                        <Network size={12} /> Full API → DB trace
                      </div>
                      <div className="grid grid-cols-2 gap-4 mb-2">
                        <div>
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Service layer</div>
                          <div className="text-slate-700 font-mono text-xs">{selectedEnvelope.service_layer?.service_impl || "-"}</div>
                          {selectedEnvelope.service_layer?.use_case_interface && (
                            <div className="text-slate-400 text-[10px] font-mono truncate">implements {selectedEnvelope.service_layer.use_case_interface}</div>
                          )}
                        </div>
                        <div>
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Data layer</div>
                          <div className="text-slate-700 font-mono text-xs">{selectedEnvelope.data_layer?.repository_class || "-"}</div>
                          <div className="text-slate-400 text-[10px] font-mono truncate">{selectedEnvelope.data_layer?.repository_file || ""}</div>
                        </div>
                      </div>
                      {(selectedEnvelope.data_layer?.table_trace || []).length > 0 && (
                        <div className="space-y-1.5 mt-2 pt-2 border-t border-slate-100">
                          {selectedEnvelope.data_layer.table_trace.map((hop, i) => (
                            <div key={i} className="flex items-start gap-2 text-xs">
                              <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-semibold uppercase text-[9px] tracking-wide flex-shrink-0">
                                {hop.layer || `hop ${i + 1}`}
                              </span>
                              <span className="font-mono text-slate-600 truncate">{hop.class || ""}</span>
                              <span className="flex flex-wrap gap-1 ml-auto">
                                {(hop.tables || []).map((t, ti) => (
                                  <span key={ti} className="px-1.5 py-0.5 bg-emerald-50 text-emerald-700 rounded text-[9px] border border-emerald-200">{t}</span>
                                ))}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Controller</div>
                      <div className="text-slate-700 font-mono text-xs">{selectedEnvelope.controller_class || "-"}</div>
                      <div className="text-slate-400 text-[10px] font-mono truncate">{selectedEnvelope.controller_file || ""}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Service</div>
                      <div className="text-slate-700 font-mono text-xs">{selectedEnvelope.service_class || "-"}</div>
                      <div className="text-slate-400 text-[10px] font-mono truncate">{selectedEnvelope.service_file || selectedEnvelope.service_method || ""}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Repository</div>
                      <div className="text-slate-700 font-mono text-xs">{selectedEnvelope.repository_class || "-"}</div>
                      <div className="text-slate-400 text-[10px] font-mono truncate">{selectedEnvelope.repository_file || ""}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Layer</div>
                      <div className="text-slate-700 text-xs">{selectedEnvelope.layer || "-"}</div>
                    </div>
                  </div>

                  <div>
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">DB tables</div>
                    {(selectedEnvelope.db_tables || []).length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {selectedEnvelope.db_tables.map((t, i) => (
                          <span key={i} className="px-1.5 py-0.5 bg-emerald-50 text-emerald-700 rounded text-[10px] border border-emerald-200">{t}</span>
                        ))}
                      </div>
                    ) : <span className="text-slate-400 text-xs">None detected</span>}
                  </div>

                  {(selectedEnvelope.external_calls || []).length > 0 && (
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">External calls</div>
                      <div className="space-y-1">
                        {selectedEnvelope.external_calls.map((c, i) => (
                          <div key={i} className="text-xs font-mono text-slate-600">
                            {typeof c === "string" ? c : `${c.type || "rest"}: ${c.target || ""}`}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div>
                    <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Files affected</div>
                    {(selectedEnvelope.files_affected || []).length > 0 ? (
                      <div className="space-y-0.5">
                        {selectedEnvelope.files_affected.map((f, i) => (
                          <div key={i} className="text-xs font-mono text-slate-600 truncate">{f}</div>
                        ))}
                      </div>
                    ) : <span className="text-slate-400 text-xs">-</span>}
                  </div>

                  {(selectedEnvelope.acceptance_criteria || []).length > 0 && (
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1">Acceptance criteria</div>
                      <ul className="list-disc list-inside space-y-0.5 text-xs text-slate-600">
                        {selectedEnvelope.acceptance_criteria.map((c, i) => <li key={i}>{c}</li>)}
                      </ul>
                    </div>
                  )}

                  <div className="flex items-center gap-3 pt-2 border-t border-gray-100">
                    <span className={`text-[10px] font-semibold px-2 py-1 rounded ${
                      selectedEnvelope.action === "TRANSFORM" ? "bg-blue-50 text-blue-700" :
                      selectedEnvelope.action === "REWRITE" ? "bg-amber-50 text-amber-700" :
                      selectedEnvelope.action === "DELETE" ? "bg-red-50 text-red-700" :
                      selectedEnvelope.action === "NEW" ? "bg-green-50 text-green-700" :
                      selectedEnvelope.action === "INTEGRATE" ? "bg-sky-50 text-sky-700" :
                      "bg-slate-50 text-slate-700"
                    }`}>
                      {selectedEnvelope.action}
                    </span>
                    <span className={`text-[10px] font-semibold px-2 py-1 rounded ${
                      selectedEnvelope.risk_level === "critical" ? "bg-red-100 text-red-700" :
                      selectedEnvelope.risk_level === "high" ? "bg-amber-100 text-amber-700" :
                      selectedEnvelope.risk_level === "medium" ? "bg-yellow-100 text-yellow-700" :
                      "bg-green-100 text-green-700"
                    }`}>
                      {selectedEnvelope.risk_level} risk
                    </span>
                  </div>
                </div>
              </div>
            </div>
          )}

        </div>
      </div>

      {/* GitHub push modal */}
      {showGitHub && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => !pushing && setShowGitHub(false)}>
          <div
            className="bg-white rounded-2xl border border-slate-200 shadow-xl w-full max-w-md p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-4">
              <GitBranch size={18} className="text-violet-600" />
              <h3 className="text-base font-semibold text-slate-800">Push to GitHub</h3>
            </div>
            <div className="space-y-3">
              {[
                { key: "repo", label: "Repository (owner/repo)", ph: "acme-org/backend-transformed" },
                { key: "branch", label: "Branch", ph: "transformation-output" },
                { key: "commit_message", label: "Commit message", ph: "Transformed code from LAMA" },
                { key: "path_prefix", label: "Path prefix (optional)", ph: "generated/" },
              ].map(({ key, label, ph }) => (
                <div key={key}>
                  <label className="block text-xs font-medium text-slate-600 mb-1">{label}</label>
                  <input
                    type="text"
                    value={ghForm[key]}
                    onChange={(e) => setGhForm((prev) => ({ ...prev, [key]: e.target.value }))}
                    placeholder={ph}
                    className={`w-full text-sm border border-slate-200 rounded-lg px-3 py-1.5 outline-none ${FOCUS_RING}`}
                  />
                </div>
              ))}
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                onClick={() => setShowGitHub(false)}
                disabled={pushing}
                className={`px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 rounded-lg ${FOCUS_RING}`}
              >
                Cancel
              </button>
              <button
                onClick={handleGitPush}
                disabled={pushing || !ghForm.repo.trim()}
                className={`px-3 py-1.5 text-sm bg-violet-600 text-white rounded-lg hover:bg-violet-700 flex items-center gap-1.5 disabled:opacity-60 ${FOCUS_RING}`}
              >
                {pushing ? <><RefreshCw size={12} className="animate-spin" /> Pushing…</> : <><GitBranch size={12} /> Push</>}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* iter-15.14 — History side panel */}
      {showHistory && (
        <>
          <div className="fixed inset-0 z-[80] bg-black/30" onClick={() => setShowHistory(false)} />
          <div className="fixed top-0 right-0 z-[81] h-full w-[420px] bg-white border-l border-slate-200 shadow-2xl flex flex-col">
            <div className="h-11 px-4 flex items-center justify-between border-b border-slate-200 flex-shrink-0">
              <div className="flex items-center gap-2">
                <History size={14} className="text-slate-600" />
                <h3 className="text-sm font-semibold text-slate-800">Transformation History</h3>
                <span className="text-[10px] text-slate-400">{historyItems.length}</span>
              </div>
              <button onClick={() => setShowHistory(false)} className="w-7 h-7 rounded hover:bg-slate-100 flex items-center justify-center text-slate-500">
                <X size={14} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto min-h-0">
              {historyLoading && (
                <div className="p-6 text-center text-[12px] text-slate-500 flex items-center justify-center gap-2">
                  <Loader2 size={13} className="animate-spin" /> Loading history…
                </div>
              )}
              {!historyLoading && historyItems.length === 0 && (
                <div className="p-6 text-center text-[12px] text-slate-500">No transformations yet.</div>
              )}
              {!historyLoading && historyItems.map((h) => {
                const isCurrent = String(h.id || h._id) === String(transformId);
                const hid = h.id || h._id;
                return (
                  <button
                    key={hid}
                    onClick={() => loadTransformationFromHistory(hid)}
                    className={`w-full text-left px-4 py-3 border-b border-slate-100 hover:bg-slate-50 ${
                      isCurrent ? "bg-violet-50" : ""
                    }`}
                    data-testid={`transformer-history-item-${hid}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[12px] font-semibold text-slate-800 truncate flex-1">
                        {h.name || "(untitled)"}
                      </span>
                      <span className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded font-semibold ${
                        h.status === "completed" ? "bg-emerald-100 text-emerald-700" :
                        h.status === "failed" ? "bg-red-100 text-red-700" :
                        h.status === "stopped" ? "bg-slate-200 text-slate-600" :
                        h.status === "running" ? "bg-violet-100 text-violet-700" :
                        "bg-slate-100 text-slate-600"
                      }`}>
                        {h.status || "pending"}
                      </span>
                    </div>
                    <div className="text-[10px] text-slate-500 mt-1 flex items-center gap-2">
                      <span>{h.file_count || 0} files</span>
                      <span>·</span>
                      <span className="truncate">{h.created_at ? new Date(h.created_at).toLocaleString() : ""}</span>
                    </div>
                    {isCurrent && (
                      <span className="mt-1 inline-block text-[9px] uppercase tracking-wider text-violet-600 font-semibold">Current</span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        </>
      )}

      {/* iter-15.14 — Remove confirm */}
      {showRemoveConfirm && (
        <div className="fixed inset-0 z-[85] flex items-center justify-center bg-black/40" onClick={() => setShowRemoveConfirm(false)}>
          <div className="bg-white rounded-2xl border border-slate-200 shadow-xl w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-2">
              <Trash2 size={16} className="text-red-600" />
              <h3 className="text-base font-semibold text-slate-800">Remove transformation?</h3>
            </div>
            <p className="text-sm text-slate-600 mb-4">
              This permanently deletes the transformation record, all source files, and generated code.
              Matches Gap Analyzer's hard-delete behavior — cannot be undone.
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setShowRemoveConfirm(false)}
                className={`px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 rounded-lg ${FOCUS_RING}`}
              >
                Cancel
              </button>
              <button
                onClick={handleRemoveCurrent}
                className={`px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 flex items-center gap-1.5 ${FOCUS_RING}`}
                data-testid="transformer-remove-confirm"
              >
                <Trash2 size={12} /> Remove
              </button>
            </div>
          </div>
        </div>
      )}

      {/* iter-15.14 — Stop confirm */}
      {showStopConfirm && (
        <div className="fixed inset-0 z-[85] flex items-center justify-center bg-black/40" onClick={() => setShowStopConfirm(false)}>
          <div className="bg-white rounded-2xl border border-slate-200 shadow-xl w-full max-w-sm p-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-2">
              <Square size={16} className="text-red-600" />
              <h3 className="text-base font-semibold text-slate-800">Stop transformation?</h3>
            </div>
            <p className="text-sm text-slate-600 mb-4">
              The worker finishes the current file, then halts. Files transformed so far
              stay available under the Coder workspace. You can then remove the
              transformation or start a new one.
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setShowStopConfirm(false)}
                className={`px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 rounded-lg ${FOCUS_RING}`}
              >
                Cancel
              </button>
              <button
                onClick={handleStop}
                disabled={busyControl === "stop"}
                className={`px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 flex items-center gap-1.5 disabled:opacity-60 ${FOCUS_RING}`}
                data-testid="transformer-stop-confirm"
              >
                {busyControl === "stop" ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
                Stop
              </button>
            </div>
          </div>
        </div>
      )}

      <TransformerTelemetry
        transformId={transformId}
        isRunning={status === "running" || progress.phase === "transforming" || progress.phase === "paused" || progress.phase === "paused_orphan"}
      />
    </div>
  );
}
