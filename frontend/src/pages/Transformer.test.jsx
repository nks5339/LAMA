import React, { useState, useCallback, useRef, useEffect, useMemo } from "react";
import {
  Play, Pause, Square, RefreshCw, Wand2, CheckCircle, AlertTriangle, Info,
  X, Cpu, Database, Globe, Server, Check, ChevronDown,
  AlertCircle, GitBranch, Upload, Archive, FileCode, Trash2,
  Network, Layers, Target, Download, Sparkles, Eye, Code2,
  MoreVertical, History, Loader2,
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
  listModels,
  pauseTransformation,
  resumeTransformation,
  stopTransformation,
  listTransformations,
  getTransformation,
  deleteTransformation,
} from "../lib/api";
import ZipFileRow from "../components/ZipFileRow";
import TransformerTelemetry from "../components/TransformerTelemetry";
import { useProjects } from "@/state/ProjectContext";

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

const buildSourceStructureRows = (sourceFiles) =>
  (Array.isArray(sourceFiles) ? sourceFiles : []).map((file, idx) => {
    const path = file?.webkitRelativePath || file?.name || `source-${idx}`;
    const parts = String(path).split("/").filter(Boolean);
    return {
      id: `source:${path}:${idx}`,
      type: "file",
      depth: Math.max(0, parts.length - 1),
      label: parts[parts.length - 1] || path,
      path,
      confidence: null,
      raw: null,
      sourceOnly: true,
      size: file?.size ?? null,
    };
  });

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

const TRANSFORMER_TABS = [
  {
    id: "input",
    label: "Input",
    description: "Source files, target stacks, and run controls.",
    hint: "Prepare or review the transformation request.",
    icon: Upload,
  },
  {
    id: "kb",
    label: "Knowledge Base",
    description: "Live entity graph, tech stack detection, and progress.",
    hint: "Watch the KB build and inspect intermediate results.",
    icon: Database,
  },
  {
    id: "code",
    label: "Transformed",
    description: "Generated structure, live file preview, and exports.",
    hint: "Review output, regenerate files, download, or push to GitHub.",
    icon: Code2,
  },
];

const TAB_HASH = {
  input: "#input",
  kb: "#kb",
  code: "#output",
};

const HASH_TO_TAB = {
  input: "input",
  kb: "kb",
  output: "code",
};

const getTabFromHash = (hash) => {
  const raw = String(hash || "").replace(/^#/, "").toLowerCase();
  return HASH_TO_TAB[raw] || null;
};

/* ─────────────── Main Component ─────────────── */

export default function TransformerPage() {
  const { active } = useProjects();
  const navigate = useNavigate();
  const location = useLocation();
  const [sourceFiles, setSourceFiles] = useState([]);
  const [name, setName] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [detectedStack, setDetectedStack] = useState(null);
  const [selectedTransforms, setSelectedTransforms] = useState({});
  const [creating, setCreating] = useState(false);
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [files, setFiles] = useState([]);
  const [error, setError] = useState(null);
  const [transformId, setTransformId] = useState(null);
  const [progress, setProgress] = useState({ phase: null, percent: 0, message: "" });
  const [isDragging, setIsDragging] = useState(false);
  const [kb, setKb] = useState(null);
  const [selectedKbFile, setSelectedKbFile] = useState(null);
  // iter-15.10 additions
  const [selectedFile, setSelectedFile] = useState(null);   // { id, path, content, confidence }
  const [regeneratingIds, setRegeneratingIds] = useState({});
  const [regenModel, setRegenModel] = useState("");
  const [availableModels, setAvailableModels] = useState([]);
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
  const pollRef = useRef(null);
  const lastFilesDoneRef = useRef(0);
  const [activeTab, setActiveTab] = useState(() => getTabFromHash(window.location.hash) || "input");

  const changeTab = useCallback((tab) => {
    if (!["input", "kb", "code"].includes(tab)) return;
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

  useEffect(() => {
    if (!location.hash) {
      const fallback = status === "running"
        ? "code"
        : status === "completed"
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

  // Cleanup polling on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const dropRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const pollStatus = useCallback(async (tid) => {
    try {
      const s = await getTransformationStatus(tid);
      const pct = s.progress_pct ?? 0;
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
      // iter-15.14 — broadcast to sidebar + top stage progress
      broadcastTransformerPhase(s.phase || null);
      const filesDone = Number(s.files_done || 0);
      const shouldRefreshFiles =
        s.status === "running" || s.status === "completed" || s.status === "stopped";
      if (shouldRefreshFiles && (filesDone !== lastFilesDoneRef.current || s.status !== "running")) {
        lastFilesDoneRef.current = filesDone;
        let loadedFiles = [];
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          loadedFiles = Array.isArray(arr) ? arr : [];
          setFiles(loadedFiles);
          if (loadedFiles.length > 0 && s.status === "running") {
            changeTab("code");
          }
        } catch {}
      }
      if (s.status === "completed") {
        stopPolling();
        setResult(s.result || {});
        setStatus("completed");
        broadcastTransformerPhase("completed");
        changeTab("code");
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          setFiles(Array.isArray(arr) ? arr : []);
        } catch {}
      } else if (s.status === "failed") {
        stopPolling();
        setError(s.error || "Transformation failed");
        setStatus("failed");
        broadcastTransformerPhase("failed");
      } else if (s.status === "stopped") {
        stopPolling();
        setStatus("stopped");
        broadcastTransformerPhase("stopped");
        try {
          const genFiles = await getTransformationFiles(tid);
          const arr = genFiles?.files || genFiles || [];
          setFiles(Array.isArray(arr) ? arr : []);
        } catch {}
      }
    } catch (e) {
      // transient poll errors are OK — keep the interval alive
    }
  }, [stopPolling, changeTab]);

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
  const openHistory = async () => {
    setShowKebab(false);
    setShowHistory(true);
    setHistoryLoading(true);
    try {
      const items = await listTransformations();
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
  const loadTransformationFromHistory = async (tid) => {
    if (!tid) return;
    setShowHistory(false);
    try {
      const t = await getTransformation(tid);
      setTransformId(tid);
      setName(t.name || "");
      setDetectedStack(t.source_stack || {});
      setSelectedTransforms(t.transforms || {});
      setStatus(t.status || null);
      setResult(t.result || null);
      setError(t.error || null);
      setPaused(!!t.paused);
      setStopped(!!t.stopped);
      lastFilesDoneRef.current = Number(t.files_done || 0);
      setKb(null);
      setSelectedFile(null);
      setSourceFiles([]);
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
      // Resume polling if still running
      stopPolling();
      if (t.status === "running" || t.status === "pending") {
        changeTab("kb");
        pollRef.current = setInterval(() => pollStatus(tid), 2000);
        pollStatus(tid);
      } else if (t.status === "completed" || loadedFiles.length > 0) {
        changeTab("code");
      } else {
        changeTab("input");
      }
    } catch (e) {
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

  const handleTransformChange = (cat, val) => {
    setSelectedTransforms(prev => ({ ...prev, [cat]: prev[cat] === val ? null : val }));
  };

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
    setProgress({ phase: "uploading", percent: 0, message: "Preparing upload..." });
    
    try {
      const fd = new FormData();
      fd.append("name", name);
      fd.append("detected_stack", JSON.stringify(detectedStack || {}));
      fd.append("transforms", JSON.stringify(selectedTransforms));
      sourceFiles.forEach(f => fd.append("source_files", f));
      
      const created = await createTransformation(fd, { onProgress: (p) => setProgress(p) });
      const tid = created?._id;
      if (!tid) throw new Error("Server did not return a transformation id");
      setTransformId(tid);
      setProgress({ phase: "queued", percent: 5, message: "Queued — starting transformation..." });
      lastFilesDoneRef.current = 0;
      changeTab("code");

      // Fire-and-poll — /run returns 202 immediately
      await runTransformation(tid);
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
    setSourceFiles([]);
    setName("");
    setDetectedStack(null);
    setSelectedTransforms({});
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
    lastFilesDoneRef.current = 0;
    broadcastTransformerPhase(null);
    changeTab("input");
  };

  const hasSelections = Object.values(selectedTransforms).some(v => v);
  const structureRows = useMemo(() => buildStructureRows(files), [files]);
  const sourceStructureRows = useMemo(() => buildSourceStructureRows(sourceFiles), [sourceFiles]);
  const activeStructureRows = useMemo(
    () => (files.length > 0 ? structureRows : sourceStructureRows),
    [files.length, structureRows, sourceStructureRows],
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
    const firstSource = sourceStructureRows[0];
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
  }, [files, loadFileContent, selectedFile?.id, sourceStructureRows]);

  // iter-15.10 — only show target-stack categories that were actually detected
  // in the uploaded source. If the user uploaded only a React FE, don't ask
  // them to pick a backend / database target.
  const categoryKeysToShow = Object.keys(TECH_CATEGORIES).filter((catKey) => {
    const detected = detectedStack?.[catKey];
    if (!detected) return false;
    const val = String(detected).toLowerCase();
    return val && val !== "unknown" && val !== "none" && val !== "n/a";
  });

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-white">
      {/* Compact Header (matches Gap Analyzer) */}
      <div className="h-11 px-4 flex items-center justify-between border-b border-slate-200 bg-white flex-shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-7 h-7 rounded bg-gradient-to-br from-violet-500 to-violet-600 flex items-center justify-center flex-shrink-0">
            <Wand2 size={14} className="text-white" />
          </div>
          <div className="flex items-center gap-2 min-w-0">
            <h1 className="text-sm font-semibold text-slate-800 truncate">
              {name || "Code Transformer"}
            </h1>
            <span className="text-[10px] text-slate-400">·</span>
            <span className="text-[11px] text-slate-500 truncate">KB-driven cross-stack transformation</span>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {status && (
            <div className={`px-2 py-0.5 rounded-full text-[10px] font-medium flex items-center gap-1 ${
              status === "completed" ? "bg-emerald-50 text-emerald-700" :
              status === "failed" ? "bg-red-50 text-red-700" :
              status === "stopped" ? "bg-slate-100 text-slate-600" :
              paused ? "bg-amber-50 text-amber-700" :
              "bg-violet-50 text-violet-700"
            }`}>
              {status === "completed" ? <CheckCircle size={10} /> :
               status === "failed" ? <AlertTriangle size={10} /> :
               status === "stopped" ? <Square size={10} /> :
               paused ? <Pause size={10} /> :
               <RefreshCw size={10} className="animate-spin" />}
              {paused ? "Paused" : status.charAt(0).toUpperCase() + status.slice(1)}
            </div>
          )}

          {/* iter-15.14 — Kebab menu (History + Remove) */}
          <div className="relative">
            <button
              onClick={() => setShowKebab((v) => !v)}
              className="w-7 h-7 rounded hover:bg-slate-100 flex items-center justify-center text-slate-500 hover:text-slate-800"
              data-testid="transformer-kebab-btn"
              title="More actions"
            >
              <MoreVertical size={15} />
            </button>
            {showKebab && (
              <>
                <div className="fixed inset-0 z-[60]" onClick={() => setShowKebab(false)} />
                  <div className="absolute right-0 top-8 z-[61] w-52 bg-white border border-slate-200 rounded-lg shadow-2xl py-1">
                    <a
                      href={transformId ? downloadTransformedCode(transformId) : "#"}
                      className={`w-full text-left px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50 flex items-center gap-2 ${
                        !transformId ? "pointer-events-none opacity-40" : ""
                      }`}
                      onClick={() => setShowKebab(false)}
                      data-testid="transformer-download-btn"
                    >
                      <Download size={13} /> Download ZIP
                    </a>
                    <button
                      onClick={openGithubPush}
                      className="w-full text-left px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50 flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed"
                      disabled={!transformId || pushLoading}
                      data-testid="transformer-github-push-btn"
                    >
                      {pushLoading ? <Loader2 size={13} className="animate-spin" /> : <GitBranch size={13} />}
                      Push to GitHub
                    </button>
                    <button
                      onClick={openHistory}
                      className="w-full text-left px-3 py-1.5 text-[12px] text-slate-700 hover:bg-slate-50 flex items-center gap-2"
                    data-testid="transformer-history-btn"
                  >
                    <History size={13} /> History
                  </button>
                  <button
                    onClick={() => { setShowKebab(false); setShowRemoveConfirm(true); }}
                    disabled={!transformId}
                    className="w-full text-left px-3 py-1.5 text-[12px] text-red-600 hover:bg-red-50 flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed"
                    data-testid="transformer-remove-btn"
                  >
                    <Trash2 size={13} /> Remove
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Compact status + controls — iter-15.25 */}
      <div className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur-sm flex-shrink-0">
        <div className="px-4 py-2 flex items-center justify-between gap-3 flex-wrap">
          {/* Status indicator */}
          <div className="flex items-center gap-3 min-w-0">
            {status === "running" ? (
              <>
                <RefreshCw size={14} className="text-violet-600 animate-spin flex-shrink-0" />
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-slate-800 truncate">
                    {paused ? "Paused" : (progress.phase_label || progress.message || "Transforming...")}
                  </div>
                  <div className="text-[11px] text-slate-500 flex items-center gap-2 tabular-nums">
                    <span className="text-violet-700 font-semibold">{progress.percent || 0}%</span>
                    {typeof progress.filesTotal === "number" && progress.filesTotal > 0 && (
                      <span>· {progress.filesDone || 0}/{progress.filesTotal} files</span>
                    )}
                  </div>
                </div>
              </>
            ) : status === "completed" ? (
              <>
                <CheckCircle size={14} className="text-green-600 flex-shrink-0" />
                <div className="text-sm font-semibold text-slate-800">Transformation Complete</div>
              </>
            ) : status === "stopped" ? (
              <>
                <Square size={14} className="text-slate-600 flex-shrink-0" />
                <div className="text-sm font-semibold text-slate-800">Transformation Stopped</div>
              </>
            ) : (
              <>
                <FileInput size={14} className="text-slate-400 flex-shrink-0" />
                <div className="text-sm font-medium text-slate-600">Draft workspace</div>
              </>
            )}
          </div>

          {/* Quick stats */}
          <div className="flex items-center gap-3 text-[11px] text-slate-600">
            {sourceFiles.length > 0 && (
              <span className="px-2 py-1 rounded bg-slate-100">
                <Archive size={10} className="inline mr-1" />
                {sourceFiles.length} source
              </span>
            )}
            {kb?.stats?.entities && (
              <span className="px-2 py-1 rounded bg-violet-50 text-violet-700">
                <Database size={10} className="inline mr-1" />
                {kb.stats.entities} KB
              </span>
            )}
            {files.length > 0 && (
              <span className="px-2 py-1 rounded bg-emerald-50 text-emerald-700">
                <Code2 size={10} className="inline mr-1" />
                {files.length} out
              </span>
            )}
          </div>
        </div>

        {/* Control buttons — iter-15.25 */}
        {status === "running" && (
          <div className="px-4 pb-2 flex items-center justify-end gap-1.5">
            {progress.phase === "paused_orphan" && (
              <div className="mr-auto flex items-center gap-2 px-2 py-1 rounded bg-amber-50 border border-amber-200 text-amber-800 text-[11px]">
                <Pause size={11} className="text-amber-600" />
                <span>Interrupted by restart — Resume from file {progress.filesDone || 0}</span>
              </div>
            )}
            {paused ? (
              <button
                onClick={handleResume}
                disabled={busyControl === "resume"}
                className="h-6 px-2 rounded bg-emerald-100 text-emerald-700 hover:bg-emerald-200 flex items-center gap-1 disabled:opacity-50"
                data-testid="transformer-resume-btn"
                title="Resume transformation"
              >
                {busyControl === "resume" ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}
                <span className="text-[10px] font-semibold">Resume</span>
              </button>
            ) : (
              <button
                onClick={handlePause}
                disabled={busyControl === "pause" || stopped}
                className="h-6 px-2 rounded bg-amber-100 text-amber-700 hover:bg-amber-200 flex items-center gap-1 disabled:opacity-50"
                data-testid="transformer-pause-btn"
                title="Pause after the current file"
              >
                {busyControl === "pause" ? <Loader2 size={10} className="animate-spin" /> : <Pause size={10} />}
                <span className="text-[10px] font-semibold">Pause</span>
              </button>
            )}
            <button
              onClick={() => setShowStopConfirm(true)}
              disabled={busyControl === "stop" || stopped}
              className="h-6 px-2 rounded bg-red-100 text-red-700 hover:bg-red-200 flex items-center gap-1 disabled:opacity-50"
              data-testid="transformer-stop-btn"
              title="Stop transformation (keeps completed files)"
            >
              {busyControl === "stop" ? <Loader2 size={10} className="animate-spin" /> : <Square size={10} />}
              <span className="text-[10px] font-semibold">Stop</span>
            </button>
          </div>
        )}
      </div>

      {/* Main Content — iter-15.25 compact side-by-side layout */}
      <div className="flex-1 overflow-y-auto min-h-0">
        <div className="w-full px-4 py-4 pb-28 space-y-3">
          {/* Top Row: Input + KB side-by-side when not running */}
          {status !== "running" && (status !== "completed" && status !== "stopped") && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {/* Input Section */}
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                <div className="px-4 py-2 border-b border-gray-100 flex items-center gap-2">
                  <FileInput size={14} className="text-slate-500" />
                  <h3 className="text-sm font-semibold text-gray-800">Input</h3>
                  {sourceFiles.length > 0 && (
                    <span className="text-[11px] text-slate-500">({sourceFiles.length} files)</span>
                  )}
                </div>
                {/* Name Input Bar */}
                <div className="px-4 py-2 border-b border-gray-100">
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Transformation name (e.g., Helidon to Spring Boot)"
                    className="w-full px-0 py-1 text-sm font-medium border-0 border-b border-transparent focus:border-[#FFE600] focus:ring-0 bg-transparent placeholder-gray-400"
                  />
                </div>
                {/* Drop Zone */}
                <div
                  ref={dropRef}
                  onDragOver={handleDragOver}
                  onDragLeave={handleDragLeave}
                  onDrop={handleDrop}
                  className={`p-4 transition-colors ${isDragging ? "bg-purple-50" : "bg-gray-50"}`}
                >
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
                    className={`flex flex-col items-center justify-center py-6 border-2 border-dashed rounded-lg cursor-pointer transition-colors ${
                      isDragging ? "border-purple-400 bg-white" : "border-gray-300 hover:border-gray-400"
                    }`}
                  >
                    <Upload size={28} className={`mb-2 ${isDragging ? "text-purple-500" : "text-gray-400"}`} />
                    <p className="text-sm font-medium text-gray-700">
                      {isDragging ? "Drop source files here" : "Drop source code or click to upload"}
                    </p>
                    <p className="text-xs text-gray-500 mt-1">ZIP/TAR archives</p>
                  </label>
                </div>
                {/* File List */}
                {sourceFiles.length > 0 && (
                  <div className="px-4 py-2 border-t border-gray-100">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[11px] font-medium text-gray-700">{sourceFiles.length} file{sourceFiles.length !== 1 ? 's' : ''}</span>
                      <button onClick={() => { setSourceFiles([]); setDetectedStack(null); }} className="text-[10px] text-gray-500 hover:text-red-500 flex items-center gap-1">
                        <Trash2 size={11} /> Clear
                      </button>
                    </div>
                    <div className="flex flex-col gap-1.5 max-h-32 overflow-y-auto">
                      {sourceFiles.map((f, i) => (
                        <ZipFileRow
                          key={i}
                          file={f}
                          accentColor="purple"
                          RowIcon={Archive}
                          testId={`transformer-source-remove-${i}`}
                          onRemove={() => {
                            const newFiles = sourceFiles.filter((_, idx) => idx !== i);
                            setSourceFiles(newFiles);
                            if (newFiles.length === 0) setDetectedStack(null);
                          }}
                        />
                      ))}
                    </div>
                  </div>
                )}
                {/* Analyzing State */}
                {analyzing && (
                  <div className="px-4 py-2 border-t border-gray-100 bg-purple-50">
                    <div className="flex items-center gap-2 text-purple-700">
                      <RefreshCw size={14} className="animate-spin" />
                      <span className="text-[11px] font-medium">Analyzing source code...</span>
                    </div>
                  </div>
                )}
              </div>

              {/* KB Section */}
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                <div className="px-4 py-2 border-b border-gray-100 flex items-center gap-2">
                  <Database size={14} className="text-violet-500" />
                  <h3 className="text-sm font-semibold text-gray-800">Knowledge Base</h3>
                  {kb?.stats?.entities && (
                    <span className="text-[11px] text-slate-500">({kb.stats.entities} entities)</span>
                  )}
                </div>
                {!kb && (
                  <div className="p-4 text-center">
                    <Database size={20} className="mx-auto mb-2 text-slate-300" />
                    <p className="text-[11px] font-medium text-slate-600">KB will be built on run</p>
                    <p className="text-[10px] text-slate-400 mt-1">Upload source files and transform to see KB stats</p>
                  </div>
                )}
                {kb?.stats && (
                  <div className="px-4 py-3 grid grid-cols-2 gap-2 max-h-48 overflow-y-auto">
                    {[
                      { label: "Entities", value: kb.stats.entities ?? 0, bg: "bg-violet-50/60 border-violet-100", tx: "text-violet-700" },
                      { label: "API Routes", value: kb.stats.api_routes ?? 0, bg: "bg-blue-50/60 border-blue-100", tx: "text-blue-700" },
                      { label: "DB Tables", value: kb.stats.db_tables ?? 0, bg: "bg-emerald-50/60 border-emerald-100", tx: "text-emerald-700" },
                      { label: "UI Files", value: kb.stats.ui_files ?? 0, bg: "bg-amber-50/60 border-amber-100", tx: "text-amber-700" },
                    ].map((s) => (
                      <div key={s.label} className={`rounded-lg border ${s.bg} px-2 py-1.5`}>
                        <div className={`text-[10px] font-medium ${s.tx}`}>{s.label}</div>
                        <div className={`text-base font-semibold ${s.tx} tabular-nums`}>{s.value.toLocaleString()}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Stack Detection & Selection - iter-15.14 Discovery-style card list */}
          {detectedStack && !analyzing && status !== "running" && status !== "completed" && status !== "stopped" && (
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2">
                  <div className="w-7 h-7 rounded bg-[#FFE600] flex items-center justify-center">
                    <Sparkles size={13} className="text-[#2E2E38]" />
                  </div>
                  <div>
                    <h3 className="text-[13px] font-semibold text-slate-800">Suggested Target Stack</h3>
                    <p className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">
                      Detected → recommended per category
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-1.5 text-[10px] text-slate-500">
                  <Info size={11} />
                  Pick one target per category · <b>Others</b> for the full catalog
                </div>
              </div>

              <div className="divide-y divide-gray-100">
                {categoryKeysToShow.length === 0 && (
                  <div className="px-4 py-6 text-center text-sm text-gray-500">
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
                    <div key={catKey} className="px-4 py-3">
                      {/* Category header */}
                      <div className="flex items-center gap-2 mb-2">
                        <CatIcon size={14} className={detected ? `text-${cat.color}-600` : "text-gray-400"} />
                        <span className="text-[11px] font-semibold text-slate-700 uppercase tracking-wider">{cat.label}</span>
                        {detected && (
                          <span className="text-[10px] font-mono px-1.5 py-0.5 bg-blue-50 text-blue-700 rounded border border-blue-100">
                            Detected: {detectedName}
                          </span>
                        )}
                        {selected && (
                          <button
                            onClick={() => handleTransformChange(catKey, null)}
                            className="ml-auto text-[10px] text-slate-400 hover:text-red-500 flex items-center gap-1"
                            title="Clear selection"
                          >
                            <X size={10} /> Clear
                          </button>
                        )}
                      </div>

                      {/* Discovery-style radio-card list — top 3 recommendations */}
                      <div className="space-y-1.5">
                        {relevantOptions.slice(0, 3).map((opt, idx) => {
                          const checked = selected === opt.id;
                          const isRecommended = idx === 0 && recommendations.includes(opt.id);
                          return (
                            <label
                              key={opt.id}
                              className={`block border rounded-md px-2.5 py-2 cursor-pointer transition-colors ${
                                checked
                                  ? "border-[#2E2E38] bg-[#FFFCE0] shadow-sm"
                                  : "border-slate-200 bg-white hover:border-slate-400"
                              }`}
                            >
                              <div className="flex items-center gap-2">
                                <input
                                  type="radio"
                                  name={`target-${catKey}`}
                                  className="shrink-0"
                                  checked={checked}
                                  onChange={() => handleTransformChange(catKey, opt.id)}
                                />
                                <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: opt.color }} />
                                <span className="text-[12px] font-semibold text-slate-800 flex-1 truncate">{opt.name}</span>
                                {isRecommended && (
                                  <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 bg-amber-100 text-amber-800 border border-amber-200 rounded font-semibold">
                                    ⭐ Top pick
                                  </span>
                                )}
                                <span className="text-[9px] text-slate-400 uppercase tracking-wider">
                                  {cat.label}
                                </span>
                                {checked && <Check size={13} className="text-emerald-600" />}
                              </div>
                            </label>
                          );
                        })}

                        {/* Custom-selected "Other" row when the pick is outside top-3 */}
                        {isOtherSelected && selectedOpt && (
                          <label className="block border rounded-md px-2.5 py-2 border-[#2E2E38] bg-[#FFFCE0] shadow-sm">
                            <div className="flex items-center gap-2">
                              <input type="radio" name={`target-${catKey}`} checked readOnly className="shrink-0" />
                              <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: selectedOpt.color }} />
                              <span className="text-[12px] font-semibold text-slate-800 flex-1 truncate">{selectedOpt.name}</span>
                              <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 bg-slate-100 text-slate-700 border border-slate-200 rounded font-semibold">
                                Custom
                              </span>
                              <Check size={13} className="text-emerald-600" />
                            </div>
                          </label>
                        )}
                      </div>

                      {/* "Others" button + popover — testid preserved */}
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
                            className="text-[11px] font-semibold text-slate-600 hover:text-slate-900 flex items-center gap-1"
                            data-testid={`transformer-others-${catKey}`}
                          >
                            <Layers size={11} />
                            Browse all {otherOptions.length} {cat.label} options
                            <ChevronDown size={11} className={`transition-transform ${showOthers[catKey] ? "rotate-180" : ""}`} />
                          </button>
                          {showOthers[catKey] && (
                            <>
                              <div className="fixed inset-0 z-[60]" onClick={() => setShowOthers(prev => ({ ...prev, [catKey]: false }))} />
                              <div
                                className="fixed z-[61] bg-white border border-gray-200 rounded-lg shadow-2xl flex flex-col"
                                style={{
                                  top: othersAnchor[catKey]?.top ?? 0,
                                  left: othersAnchor[catKey]?.left ?? 0,
                                  width: othersAnchor[catKey]?.width ?? 288,
                                  maxHeight: 420,
                                }}
                              >
                                <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-slate-500 font-semibold border-b border-gray-100 flex items-center justify-between bg-slate-50/70 flex-shrink-0">
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
                                        selected === opt.id ? "bg-[#FFFCE6]" : ""
                                      }`}
                                    >
                                      <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: opt.color }} />
                                      <span className="flex-1 truncate">{opt.name}</span>
                                      {selected === opt.id && <Check size={13} className="text-green-600" />}
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

              {/* Action Button */}
              <div className="px-4 py-3 bg-gray-50 border-t border-gray-100">
                <button
                  onClick={handleCreate}
                  disabled={!name.trim() || sourceFiles.length === 0 || !hasSelections || creating}
                  className="w-full py-2.5 bg-[#FFE600] text-[#2E2E38] font-semibold rounded-lg hover:bg-[#FFD500] transition-colors flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {creating ? <RefreshCw size={16} className="animate-spin" /> : <Wand2 size={16} />}
                  {creating ? "Transforming..." : "Run Transformation"}
                </button>
                {!hasSelections && sourceFiles.length > 0 && (
                  <p className="text-xs text-center text-amber-600 mt-2">⚠ Select at least one transformation target</p>
                )}
              </div>
            </div>
          )}

          {/* Error Section */}
          {status === "failed" && (
            <div className="bg-white rounded-xl border border-red-200 shadow-sm p-6">
              <div className="flex items-start gap-4">
                <div className="w-10 h-10 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
                  <AlertCircle size={20} className="text-red-600" />
                </div>
                <div className="flex-1">
                  <h3 className="text-base font-semibold text-red-800 mb-1">Transformation Failed</h3>
                  <p className="text-sm text-red-600">{error || "An unexpected error occurred"}</p>
                </div>
                <button onClick={reset} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200">
                  Try Again
                </button>
              </div>
            </div>
          )}

          {/* Running/Completed Transformed Code Workspace — iter-15.25 always visible, full-width */}
          {(status === "running" || status === "completed" || status === "stopped") && (files.length > 0 || status === "running") && (
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              {/* Slim progress bar at top */}
              {status === "running" && (
                <div className="px-4 py-2 border-b border-gray-100 bg-slate-50/50">
                  <div className="flex items-center justify-between gap-3 text-[11px]">
                    <div className="flex items-center gap-2 min-w-0">
                      <RefreshCw size={11} className="text-violet-600 animate-spin flex-shrink-0" />
                      <span className="text-slate-700 font-medium truncate" title={progress.message || ""}>
                        {progress.phase === "building_kb" ? "Building KB" : progress.message || "Transforming..."}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 tabular-nums text-slate-600 flex-shrink-0">
                      <span className="text-violet-700 font-semibold">{progress.percent || 0}%</span>
                      {typeof progress.filesTotal === "number" && progress.filesTotal > 0 && (
                        <span>{progress.filesDone || 0}/{progress.filesTotal}</span>
                      )}
                    </div>
                  </div>
                  <div className="mt-1.5 h-1 rounded-full bg-slate-200 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-600 transition-all duration-300"
                      style={{ width: `${progress.percent || 0}%` }}
                    />
                  </div>
                </div>
              )}
              
              {/* Header bar */}
              <div className="px-4 py-2 border-b border-gray-100 flex items-center gap-2 flex-wrap">
                <Code2 size={14} className="text-slate-500" />
                <h3 className="text-sm font-semibold text-gray-800">
                  {status === "running" ? "Live Transformed Code" : status === "stopped" ? "Stopped Transform Output" : "Generated Code"}
                </h3>
                <span className="text-[11px] text-slate-500">({files.length} files)</span>
                {status === "running" && (
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-violet-50 text-violet-700">
                    Live
                  </span>
                )}
                <div className="ml-auto flex items-center gap-2">
                  {(status === "completed" || status === "stopped") && (
                    <button onClick={reset} className="px-2 py-1 text-[11px] font-medium text-gray-600 bg-gray-100 rounded hover:bg-gray-200">
                      New Transform
                    </button>
                  )}
                  <label className="text-[11px] text-slate-500">Regenerate with:</label>
                  <select
                    value={regenModel}
                    onChange={(e) => setRegenModel(e.target.value)}
                    className="text-[11px] border border-slate-200 rounded px-2 py-1 bg-white focus:border-[#FFE600] focus:ring-1 focus:ring-[#FFE600] outline-none max-w-[180px]"
                    title="Model used when clicking Regenerate on a file"
                  >
                    <option value="">Auto (Console routing)</option>
                    {availableModels.map((m) => {
                      const id = typeof m === "string" ? m : (m.id || m.model || "");
                      const label = typeof m === "string" ? m : (m.label || m.name || id);
                      return id ? <option key={id} value={id}>{label}</option> : null;
                    })}
                  </select>
                </div>
              </div>

              {/* Split layout: tree left, editor right */}
              <div className="grid grid-cols-[minmax(0,280px)_minmax(0,1fr)] flex-1 min-h-0" style={{ height: "calc(100vh - 200px)", maxHeight: "calc(100vh - 180px)" }}>
                {/* File list */}
                <div className="border-r border-gray-100 overflow-y-auto">
                      <div className="px-3 py-2 border-b border-gray-100 bg-slate-50 flex items-center justify-between">
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
                      <div className="overflow-y-auto">
                        {activeStructureRows.map((row) => {
                          const isFile = row.type === "file";
                          const selected = isFile && selectedFile?.id === row.id;
                          const rowKey = row.id;
                          return (
                            <button
                              key={rowKey}
                              onClick={() => {
                                if (!isFile) return;
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
                              className={`w-full text-left px-3 py-2 flex items-start gap-2 border-b border-gray-50 text-[11px] group ${
                                isFile && selected ? "bg-violet-50" : "hover:bg-gray-50"
                              }`}
                              style={{ paddingLeft: `${12 + row.depth * 12}px` }}
                            >
                              {isFile ? (
                                <FileCode size={12} className="text-gray-400 mt-0.5 flex-shrink-0" />
                              ) : (
                                <Archive size={12} className="text-amber-500 mt-0.5 flex-shrink-0" />
                              )}
                              <div className="flex-1 min-w-0">
                                <div className="font-mono truncate text-slate-700" title={row.path}>
                                  {row.label}
                                </div>
                                {isFile && (
                                  <div className="flex items-center gap-1 mt-0.5">
                                    {row.sourceOnly && (
                                      <span className="text-[9px] font-semibold px-1.5 py-0 rounded bg-slate-100 text-slate-600">
                                        source
                                      </span>
                                    )}
                                    {typeof row.confidence === "number" && (
                                      <span
                                        className={`text-[9px] font-semibold px-1.5 py-0 rounded ${
                                          row.confidence >= 0.75 ? "bg-emerald-100 text-emerald-700" :
                                          row.confidence >= 0.5 ? "bg-amber-100 text-amber-700" :
                                          "bg-red-100 text-red-700"
                                        }`}
                                      >
                                        {Math.round(row.confidence * 100)}%
                                      </span>
                                    )}
                                    {status === "running" && selected && !row.sourceOnly && (
                                      <span className="text-[9px] text-violet-600">live</span>
                                    )}
                                  </div>
                                )}
                              </div>
                            </button>
                          );
                        })}
                      </div>
                      <div className="px-3 py-2 border-t border-gray-100 bg-slate-50 text-[10px] text-slate-500">
                        Click any generated file to preview the live class.
                      </div>
                    </div>

                    {/* Content pane */}
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
                          <div className="px-3 py-2 border-b border-gray-100 flex items-center gap-2 bg-slate-50">
                            <FileCode size={13} className="text-slate-500" />
                            <span className="text-[11px] font-mono truncate flex-1" title={selectedFile.path}>
                              {selectedFile.path}
                            </span>
                            {progress.currentFile && !selectedFile.sourceOnly && (
                              <span className="text-[10px] text-violet-600 px-1.5 py-0.5 rounded bg-violet-50">
                                {status === "running" ? "Live" : "Preview"}
                              </span>
                            )}
                            {typeof selectedFile.confidence === "number" && (
                              <span
                                className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
                                  selectedFile.confidence >= 0.75 ? "bg-emerald-100 text-emerald-700" :
                                  selectedFile.confidence >= 0.5 ? "bg-amber-100 text-amber-700" :
                                  "bg-red-100 text-red-700"
                                }`}
                              >
                                Confidence: {Math.round(selectedFile.confidence * 100)}%
                              </span>
                            )}
                            {!selectedFile.sourceOnly && (
                              <button
                                onClick={() => handleRegenerate(selectedFile)}
                                disabled={!!regeneratingIds[selectedFile.id]}
                                className="text-[11px] px-2 py-1 bg-violet-100 text-violet-700 rounded hover:bg-violet-200 flex items-center gap-1 disabled:opacity-60"
                                title="Re-run this file with the selected model"
                              >
                                {regeneratingIds[selectedFile.id] ? (
                                  <><RefreshCw size={11} className="animate-spin" /> Regenerating…</>
                                ) : (
                                  <><Sparkles size={11} /> Regenerate</>
                                )}
                              </button>
                            )}
                          </div>
                          <pre className="flex-1 overflow-auto text-[11px] font-mono p-3 bg-slate-900 text-slate-100 m-0">
                            {selectedFile.loading ? "// Loading…" : (selectedFile.content || "// (empty)")}
                          </pre>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
