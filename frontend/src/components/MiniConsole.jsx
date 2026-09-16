import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  Terminal, ChevronDown, ChevronUp, Cpu, Coins, Activity, RefreshCw,
  Plug, PlugZap, ScrollText, Play, Pause, Trash2, Copy, Check,
  Maximize2, X,
} from "lucide-react";
import { toast } from "sonner";
import {
  getUsageSummary, getFactoryOrchestratorConfig,
  testFactoryOrchestratorConfig, updateFactoryOrchestratorConfig,
  tailBackendLogs,
} from "@/lib/api";
import { useProjects } from "@/state/ProjectContext";
import { labelForAgentKey, colorForAgentKey, isAgentDrivenProjectType, projectTypeMeta } from "@/lib/agentLabels";
import { usePolling } from "@/hooks/usePolling";

// iter-13.31 — Global always-on status bar.
//
// Shows in a fixed footer on every page:
//   - Currently using <model>  (from the most recent LLM call, or the
//     Console default-tier model when no call has run yet).
//   - Cumulative tokens used in the last 7 days for this project,
//     plus running USD cost.
//   - The active stage (auto-derived from the route) and its share.
//
// Mounted ONCE inside <Shell> (App.js) so it appears everywhere.
// Per-page mounts have been removed to avoid duplicate footers.

const ROUTE_TO_STAGE = {
  "/": "Discovery",
  "/data-model": "DataModel",
  "/architecture": "Architecture",
  "/code-gen": "CodeGen",
  "/living": "Living",
  "/ontology-studio": "Discovery",
  "/console": "",
  "/prompts": "",
  "/audit": "",
  "/settings": "",
};

function fmt(n) {
  return (n || 0).toLocaleString();
}

function shortModel(m) {
  if (!m) return "—";
  return m.split("/").slice(-1)[0];
}

function ageLabel(iso) {
  if (!iso) return "idle";
  try {
    const t = new Date(iso).getTime();
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 5) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  } catch {
    return "—";
  }
}

export default function MiniConsole() {
  const { active } = useProjects();
  const projectId = active?.id || "";
  const projectType = active?.project_type || "legacy_migration";
  const agentDriven = isAgentDrivenProjectType(projectType);
  const location = useLocation();
  // iter-16.x — Gap Analyzer / Code Transformer projects aren't routed
  // through the 5-stage legacy pipeline paths, so ROUTE_TO_STAGE has
  // nothing to match and the header used to fall back to a bare
  // "Console". Show the project-type label instead so Live Telemetry
  // always identifies what it's actually measuring.
  const stage = agentDriven
    ? projectTypeMeta(projectType).label
    : (ROUTE_TO_STAGE[location.pathname] ?? "");

  const lsKey = "lama:miniconsole:open";
  const [open, setOpen] = useState(() => localStorage.getItem(lsKey) !== "0");
  const [summary, setSummary] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [factoryConnected, setFactoryConnected] = useState(false);
  const [factoryComputer, setFactoryComputer] = useState("");
  const [factoryModel, setFactoryModel] = useState("auto");          // iter-13.80
  // iter-13.35 — live factory.ai reachability ping.
  //   health = "unknown" | "up" | "down" | "disabled" | "misconfigured"
  // iter-13.116 — "misconfigured" added for the case where the user
  // ticked "Enable Factory Orchestrator" but hasn't yet pasted an app
  // key / computer_id. Previously this fell back to the Fabric label
  // and Live Telemetry started reporting Ollama as the active route,
  // which was confusing — the operator's INTENT is clearly Factory.
  const [factoryHealth, setFactoryHealth] = useState("unknown");
  const [factoryHealthErr, setFactoryHealthErr] = useState("");
  const [factoryHealthAt, setFactoryHealthAt] = useState("");
  // iter-14.16 — inline connect/disconnect toggle so operators can
  // suspend the Factory-AI droid mid-run (e.g. during confidence
  // generation) without leaving the current page for Console →
  // Factory tab. `factoryConfigured` tells us whether the project
  // has *any* Factory orchestrator config saved — the toggle is
  // only meaningful when it does; otherwise we link to Console.
  const [factoryConfigured, setFactoryConfigured] = useState(false);
  const [factoryToggling, setFactoryToggling] = useState(false);

  // iter-14.17 — Live backend log tail.
  // `logsOpen`      — the pane is expanded (toggled with the ScrollText button).
  // `logsFollow`    — auto-scroll to bottom when new records arrive.
  // `logs`          — bounded array of last ~500 rendered records.
  // `logSinceSeq`   — highest seq we've fetched so far; sent in next poll.
  // `logFilter`     — case-insensitive substring; empty = no filter.
  // `logLevel`      — INFO / WARNING / ERROR / "" (all).
  // Polling only runs while the pane is OPEN so the closed state is
  // zero-cost.
  const lsLogOpen = "lama:miniconsole:logsopen";
  const [logsOpen, setLogsOpen] = useState(() => localStorage.getItem(lsLogOpen) === "1");
  const [logsFollow, setLogsFollow] = useState(true);
  const [logs, setLogs] = useState([]);
  const [logSinceSeq, setLogSinceSeq] = useState(0);
  const [logFilter, setLogFilter] = useState("");
  const [logLevel, setLogLevel] = useState("INFO");
  const [logsDropped, setLogsDropped] = useState(0);
  const [logsMax, setLogsMax] = useState(false);
  const logsBoxRef = useRef(null);
  const logsBoxMaxRef = useRef(null);
  // iter-15.5 — track latest since_seq via ref so the polling closure
  // always reads the fresh value (avoids stale-closure freezing at 0).
  const logSinceSeqRef = useRef(0);
  useEffect(() => { logSinceSeqRef.current = logSinceSeq; }, [logSinceSeq]);

  const refresh = async () => {
    if (!projectId) return;
    setRefreshing(true);
    try {
      const r = await getUsageSummary(projectId, 7);
      setSummary(r);
    } catch {
      /* keep last successful summary */
    } finally {
      setRefreshing(false);
    }
  };

  const refreshFactoryStatus = async () => {
    if (!projectId) {
      setFactoryConnected(false);
      setFactoryComputer("");
      setFactoryModel("auto");
      setFactoryHealth("unknown");
      return;
    }
    try {
      const r = await getFactoryOrchestratorConfig(projectId);
      const c = r?.config || {};
      // iter-13.116 — Gate Live Telemetry on the OPERATOR INTENT
      // (`config.enabled`) rather than the strict `routing_active` triple.
      // When the user has ticked "Enable Factory Orchestrator" but
      // hasn't yet wired an app key / computer_id, we still want the
      // banner + Currently-using tile to reflect Factory mode — flagged
      // as MISCONFIGURED so they know to finish the setup — instead of
      // falsely reporting Ollama / fabric as the route.
      const intent = !!c.enabled;
      const fullyWired = !!(c.routing_active ?? (c.enabled && c.has_app_key && (c.computer_id || "").trim()));
      const isCliMode = (c.mode || "").toLowerCase() === "cli";
      setFactoryConnected(intent);
      // iter-14.16 — a config counts as "configured" if the project
      // has *ever* saved a Factory orchestrator record — either
      // currently enabled OR it's disabled but the credentials
      // (app_key / computer_id) are still present. Toggling in this
      // case is safe because a re-enable will pick up the saved
      // credentials rather than land in the misconfigured state.
      // iter-16.x — CLI-mode droids don't use app_key/computer_id at
      // all (only `droid` on PATH), so those two fields are always
      // empty for them. Without this branch, disconnecting a CLI-mode
      // droid (enabled → false) made `factoryConfigured` fall to
      // false too — the Reconnect button vanished entirely and there
      // was no way back short of visiting Console → Factory.
      setFactoryConfigured(
        !!(intent || isCliMode || (c.has_app_key || (c.computer_id || "").trim()))
      );
      setFactoryComputer((c.computer_id || "").trim());
      setFactoryModel(((c.model || "auto").trim()) || "auto");
      if (intent && !fullyWired) {
        setFactoryHealth("misconfigured");
        setFactoryHealthErr(c.routing_reason || "app_key / computer_id not yet mapped");
      } else if (!intent) {
        setFactoryHealth("disabled");
        setFactoryHealthErr(c.routing_reason || "");
      }
    } catch {
      setFactoryConnected(false);
      setFactoryConfigured(false);
      setFactoryComputer("");
      setFactoryModel("auto");
      setFactoryHealth("unknown");
    }
  };

  // iter-14.16 — inline connect/disconnect. Sends the minimum
  // partial payload `{project_id, enabled}` — the backend upsert
  // treats absent keys as "no change" so all other Factory settings
  // (app_key, computer_id, cwd, mode, model, models, host_anchored,
  // allow_fallback, cli_*) are preserved. This lets an operator
  // suspend the droid mid-confidence-run and re-enable it later
  // without losing configuration.
  const toggleFactoryEnabled = async () => {
    if (!projectId || factoryToggling) return;
    const next = !factoryConnected;
    setFactoryToggling(true);
    // Optimistic UI so the label flips immediately.
    setFactoryConnected(next);
    if (!next) {
      setFactoryHealth("disabled");
      setFactoryHealthErr("");
    } else {
      setFactoryHealth("unknown");
    }
    try {
      const r = await updateFactoryOrchestratorConfig({
        project_id: projectId,
        enabled: next,
      });
      const c = r?.config || {};
      // Reconcile with server truth.
      setFactoryConnected(!!c.enabled);
      setFactoryConfigured(
        !!(c.enabled || (c.mode || "").toLowerCase() === "cli" || c.has_app_key || (c.computer_id || "").trim())
      );
      // iter-13.116 — broadcast so Console/Models tabs stay in sync
      // if they're open in another tab.
      try {
        window.dispatchEvent(new CustomEvent("lama:factory-config-changed", {
          detail: { enabled: !!c.enabled },
        }));
      } catch { /* */ }
      toast.success(
        next ? "Factory droid reconnected" : "Factory droid disconnected",
        {
          description: next
            ? "LLM calls will route through Factory again."
            : "LLM calls now fall through to the Fabric provider. "
              + "Use this during confidence generation to skip Factory.",
        },
      );
      // Re-ping health so the banner reflects the new state.
      refreshFactoryHealth();
    } catch (e) {
      // Roll back optimistic flip.
      setFactoryConnected(!next);
      toast.error(
        next ? "Failed to reconnect droid" : "Failed to disconnect droid",
        { description: e?.response?.data?.detail || e?.message || "unknown error" },
      );
    } finally {
      setFactoryToggling(false);
    }
  };

  const refreshFactoryHealth = async () => {
    if (!projectId) return;
    try {
      const cfg = await getFactoryOrchestratorConfig(projectId);
      const c = cfg?.config || {};
      const intent = !!c.enabled;
      const fullyWired = !!(c.routing_active ?? (c.enabled && c.has_app_key && (c.computer_id || "").trim()));
      if (!intent) {
        setFactoryHealth("disabled");
        setFactoryHealthErr(c.routing_reason || "");
        return;
      }
      if (!fullyWired) {
        // iter-13.116 — Don't bother pinging /test when the config is
        // incomplete; surface the misconfiguration directly.
        setFactoryHealth("misconfigured");
        setFactoryHealthErr(c.routing_reason || "app_key / computer_id not yet mapped");
        return;
      }
      const r = await testFactoryOrchestratorConfig(projectId);
      if (r?.ok) {
        setFactoryHealth("up");
        setFactoryHealthErr("");
      } else {
        setFactoryHealth("down");
        setFactoryHealthErr((r?.error || "unreachable").slice(0, 200));
      }
    } catch (e) {
      setFactoryHealth("down");
      setFactoryHealthErr((e?.message || "unreachable").slice(0, 200));
    } finally {
      setFactoryHealthAt(new Date().toISOString());
    }
  };

  // Initial fetch on mount / project change. The three recurring polls it
  // used to own are now usePolling calls below, which pause while the tab
  // is hidden — this component sits in the shell on every route, so its
  // timers previously ran forever in every backgrounded LAMA tab.
  useEffect(() => {
    refresh();
    refreshFactoryStatus();
    refreshFactoryHealth();
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  usePolling(refresh, 10000);
  usePolling(refreshFactoryStatus, 15000);
  usePolling(refreshFactoryHealth, 30000);

  // iter-14.59 — auto-open state moved below to sit next to the
  // `current` derivation it depends on (see line ~485).
  const [autoOpenedStage, setAutoOpenedStage] = useState("");


  const toggle = () => {
    const v = !open;
    setOpen(v);
    localStorage.setItem(lsKey, v ? "1" : "0");
  };

  // iter-14.17 — Log-tail poller. Runs only while the pane is open AND
  // the parent popup is expanded so we don't churn XHRs when the console
  // is collapsed. Batches new records at ~1.5s cadence. Trims local
  // buffer to LOG_KEEP records so a long-running session doesn't grow
  // unbounded in memory.
  const LOG_KEEP = 500;
  const toggleLogsPane = () => {
    const v = !logsOpen;
    setLogsOpen(v);
    localStorage.setItem(lsLogOpen, v ? "1" : "0");
    // iter-15.5 — Opening the pane must always show the full ring, not
    // just records that landed AFTER the poll last stopped. Reset the
    // sinceSeq baseline so the next poll fetches from seq 0.
    if (v) {
      setLogSinceSeq(0);
      setLogs([]);
      setLogsDropped(0);
    }
  };
  const clearLogs = () => {
    setLogs([]);
    setLogsDropped(0);
    // iter-15.5 — Also reset the seq baseline so the next poll re-fetches
    // the whole ring. Without this, Clear would leave the pane
    // permanently empty until brand-new records arrived on the server.
    setLogSinceSeq(0);
  };
  // iter-14.20 — copy currently-visible log records as plain text.
  // Uses the modern Clipboard API in secure contexts and falls back to a
  // hidden textarea + `document.execCommand("copy")` when clipboard is
  // unavailable (Safari file://, corporate policies, etc). Never throws:
  // the button flips to a check-mark on success or shows a toast on
  // failure. Format matches what the UI already renders, so operators can
  // paste straight into a chat / ticket.
  const [logsCopied, setLogsCopied] = useState(false);
  const copyLogs = async () => {
    if (!logs.length) {
      toast.info("No logs to copy");
      return;
    }
    const text = logs
      .map((r) => {
        const ts = new Date((r.ts || 0) * 1000).toISOString();
        const level = (r.level || "").padEnd(5).slice(0, 5);
        const suffix = r.repeat > 1 ? ` ×${r.repeat}` : "";
        return `${ts} ${level} ${r.name} ${r.msg}${suffix}`;
      })
      .join("\n");
    const succeed = () => {
      setLogsCopied(true);
      toast.success(`Copied ${logs.length} log line(s)`);
      setTimeout(() => setLogsCopied(false), 1500);
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        succeed();
        return;
      }
    } catch (_err) {
      // fall through to the textarea path below
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) {
        succeed();
      } else {
        toast.error("Clipboard blocked — copy manually from the pane");
      }
    } catch (_err) {
      toast.error("Clipboard blocked — copy manually from the pane");
    }
  };
  useEffect(() => {
    if (!open || !logsOpen) return undefined;
    let cancelled = false;
    let inFlight = false;
    // iter-15.5 — Ensure the FIRST poll under the current effect fetches
    // the ring from seq 0 so the pane hydrates with historic records
    // even if the user has been idle for a while.
    if (logs.length === 0) {
      logSinceSeqRef.current = 0;
    }
    const tick = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const r = await tailBackendLogs({
          sinceSeq: logSinceSeqRef.current,
          limit:    300,
          minLevel: logLevel || "",
          contains: logFilter || "",
        });
        if (cancelled) return;
        const recs = r?.records || [];
        if (recs.length) {
          setLogs((prev) => {
            // iter-14.17.1 — Server-side collapses consecutive
            // identical records into one entry with a bumped seq and
            // incremented `repeat` counter. Mirror that on append so
            // (a) already-rendered rows update in place instead of
            // duplicating, and (b) collapses that span a poll boundary
            // still merge correctly.
            let out = prev.slice();
            for (const rec of recs) {
              const tail = out.length ? out[out.length - 1] : null;
              if (tail
                  && tail.level === rec.level
                  && tail.name  === rec.name
                  && tail.msg   === rec.msg) {
                // Replace the tail entry with the fresher rec — keep
                // the higher seq, higher repeat, latest ts.
                out[out.length - 1] = {
                  ...rec,
                  repeat: Math.max(rec.repeat || 1, (tail.repeat || 1) + 1),
                };
              } else {
                out.push(rec);
              }
            }
            return out.length > LOG_KEEP ? out.slice(out.length - LOG_KEEP) : out;
          });
          setLogSinceSeq(r.next_seq || logSinceSeqRef.current);
        }
        if (r?.dropped) setLogsDropped((n) => n + r.dropped);
      } catch {
        /* keep last records; next tick will retry */
      } finally {
        inFlight = false;
      }
    };
    // Fire immediately so the pane never shows an empty state briefly.
    tick();
    const iv = setInterval(() => { if (!document.hidden) tick(); }, 1500);
    return () => { cancelled = true; clearInterval(iv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, logsOpen, logLevel, logFilter]);

  // Reset the seq baseline (but keep buffer visible) when filters change
  // so the next poll picks up FROM NOW under the new filter — otherwise
  // switching from WARNING to INFO would silently skip a chunk.
  useEffect(() => {
    setLogSinceSeq(0);
    setLogs([]);
    setLogsDropped(0);
  }, [logLevel, logFilter]);

  // Auto-scroll to bottom when new records land (unless follow is off).
  useEffect(() => {
    if (!logsFollow) return;
    const el = logsBoxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    const elMax = logsBoxMaxRef.current;
    if (elMax) elMax.scrollTop = elMax.scrollHeight;
  }, [logs, logsFollow]);

  // ESC closes the maximized log viewer.
  useEffect(() => {
    if (!logsMax) return;
    const onKey = (e) => { if (e.key === "Escape") setLogsMax(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [logsMax]);

  const current = summary?.current || {};
  // iter-16.x — For agent-driven project types (Gap Analyzer / Code
  // Transformer), every LLM call is tagged `stage: "Tools"` server-side
  // (there's no pipeline stage), while `stage` here holds the friendly
  // project-type label used for the header/section titles. Look up the
  // "Tools" bucket directly so this tile still shows real numbers
  // instead of always reading zero.
  const stageRow = summary?.by_stage?.find((r) => r.stage === (agentDriven ? "Tools" : stage)) || null;
  const totalTokens = summary?.total_tokens || 0;
  const totalCost   = summary?.total_cost_usd || 0;
  const stageTokens = stageRow?.tokens || 0;
  const stageCost   = stageRow?.cost   || 0;

  // iter-14.59 — Auto-open the Backend Logs pane whenever an activity
  // is running in the current stage. "Running" = the last LLM call for
  // this project happened in the current stage AND is < 30s old. This
  // gives operators the "logs stream while my job runs" experience
  // without having to remember to click the pane open. Also flips the
  // parent popup open if it's collapsed so the stream is actually
  // visible. Once the operator manually collapses the pane the flip
  // won't fire again for the same stage until activity resumes after a
  // 60s quiet window. Declared HERE (not near the other effects at the
  // top of the component) because it reads `current` which is derived
  // from `summary` on the render path; declaring earlier put the hook
  // into a temporal dead zone (Uncaught ReferenceError 'Ze').
  useEffect(() => {
    if (!stage) return;
    if (!current?.at) return;
    if ((current.stage || "") !== stage) return;
    const ageSec = (Date.now() - new Date(current.at).getTime()) / 1000;
    if (!(ageSec >= 0 && ageSec < 30)) return;
    if (autoOpenedStage === stage) return;
    if (!open) {
      setOpen(true);
      localStorage.setItem(lsKey, "1");
    }
    if (!logsOpen) {
      setLogsOpen(true);
      localStorage.setItem(lsLogOpen, "1");
    }
    setAutoOpenedStage(stage);
  }, [current?.at, current?.stage, stage]); // eslint-disable-line

  useEffect(() => {
    if (!stage) { setAutoOpenedStage(""); return; }
    const at = current?.at ? new Date(current.at).getTime() : 0;
    const ageSec = at ? (Date.now() - at) / 1000 : Infinity;
    if (ageSec > 60 && autoOpenedStage) setAutoOpenedStage("");
  }, [stage, current?.at]); // eslint-disable-line

  // iter-14.59 — Project-wise breakdown for the "Live Telemetry" tile.
  // `summary` was requested with the active `project_id`, so `by_project`
  // holds a single row (this project); the sparse `by_project_stage`
  // cross-tab lets us render a "Tokens by stage — this project" mini
  // chart so operators can see Living's share vs Discovery / DataModel /
  // Architecture / CodeGen at a glance. When no project is active we
  // fall back to the multi-project rollup for the same view.
  //
  // iter-16.x — Gap Analyzer / Code Transformer projects tag every LLM
  // call `stage: "Tools"` (they don't have pipeline stages), so the
  // stage breakdown used to render four permanently-zero legacy rows
  // plus one lump "Tools" bucket. For those project types we instead
  // group by `by_agent` (agent_key), which the backend already
  // aggregates, and label it with human-friendly names.
  const projectRow =
    (summary?.by_project || []).find((r) => r.project_id === projectId)
    || (summary?.by_project || [])[0]
    || null;
  const projectStagesEntry =
    (summary?.by_project_stage || []).find((r) => r.project_id === projectId)
    || (summary?.by_project_stage || [])[0]
    || null;
  const STAGE_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"];
  const projectStageRows = (() => {
    if (agentDriven) {
      const rows = (summary?.by_agent || [])
        .map((r) => ({
          stage: labelForAgentKey(r.agent_key),
          tokens: Number(r.tokens || 0),
          color: colorForAgentKey(r.agent_key),
        }))
        .filter((r) => r.tokens > 0)
        .sort((a, b) => b.tokens - a.tokens)
        .slice(0, 6);
      return rows;
    }
    const stages = projectStagesEntry?.stages || {};
    const rows = STAGE_ORDER.map((s) => ({ stage: s, tokens: Number(stages[s] || 0) }));
    const known = new Set(STAGE_ORDER);
    Object.entries(stages).forEach(([s, t]) => {
      if (!known.has(s) && s) rows.push({ stage: s, tokens: Number(t || 0) });
    });
    return rows;
  })();
  const projectStageMax = Math.max(1, ...projectStageRows.map((r) => r.tokens || 0));
  const projectTokens = projectRow?.tokens || 0;
  const projectRuns   = projectRow?.runs || 0;
  const projectCost   = projectRow?.cost || 0;
  const projectName   = projectRow?.project_name
    || (active?.name || (projectId ? `${projectId.slice(0, 8)}…` : ""));

  const statusDot =
    current.status === "error"   ? "bg-red-500"
    : current.status === "idle"  ? "bg-fg-subtle"
    : current.status === "timeout" ? "bg-orange-400"
    : "bg-emerald-500";

  if (!open) {
    // iter-13.35 — collapsed pill shows factory.ai status when enabled.
    const factoryPill = factoryConnected ? (
      factoryHealth === "up" ? (
        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-emerald-500 text-white text-micro font-bold">
          <span className="w-1.5 h-1.5 rounded-full bg-surface" />FACTORY UP
        </span>
      ) : factoryHealth === "down" ? (
        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-red-500 text-white text-micro font-bold animate-pulse">
          <span className="w-1.5 h-1.5 rounded-full bg-surface" />FACTORY DOWN
        </span>
      ) : factoryHealth === "misconfigured" ? (
        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-orange-500 text-white text-micro font-bold">
          <span className="w-1.5 h-1.5 rounded-full bg-surface" />FACTORY · SETUP
        </span>
      ) : (
        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-500 text-white text-micro font-bold">
          <span className="w-1.5 h-1.5 rounded-full bg-surface" />FACTORY …
        </span>
      )
    ) : null;
    return (
      <button
        onClick={toggle}
        data-testid="mini-console-collapsed"
        title={
          factoryConnected
            ? `Factory.ai (${factoryHealth})${factoryComputer ? ` · ${factoryComputer}` : ""}${factoryHealthErr ? ` · ${factoryHealthErr}` : ""}`
            : `Model: ${current.model || "—"} · ${fmt(totalTokens)} tokens · $${totalCost.toFixed(4)}`
        }
        className="fixed bottom-0 right-0 z-40 h-8 px-3 bg-ink text-ink-fg text-micro flex items-center gap-3 rounded-tl-md hover:bg-brand hover:text-fg shadow-lg border-t border-l border-brand/30"
      >
        <span className={`w-2 h-2 rounded-full ${statusDot}`} />
        <Cpu className="w-3 h-3" />
        {/* iter-13.36 / iter-13.80 — when Factory.ai is active, the pill
            shows the operator-picked Factory model (or `factory/auto` when
            no explicit selection was made). Falls back to the fabric
            current.model when Factory is not active. */}
        <span className="font-mono" data-testid="mini-console-current-model">
          {factoryConnected
            ? (factoryModel && factoryModel.toLowerCase() !== "auto"
                ? `factory/${factoryModel}`
                : "factory/auto")
            : shortModel(current.model)}
        </span>
        {factoryPill}
        <span className="opacity-60">·</span>
        <Coins className="w-3 h-3" />
        <span className="font-mono text-brand group-hover:text-fg" data-testid="mini-console-total-tokens">
          {fmt(totalTokens)}
        </span>
        <span className="opacity-60">tokens</span>
        <ChevronUp className="w-3 h-3" />
      </button>
    );
  }

  return (
    <div
      data-testid="mini-console-expanded"
      className="fixed bottom-0 right-0 z-40 w-[420px] max-w-[100vw] sm:max-w-[95vw] bg-surface border-t border-l border-border rounded-tl-md shadow-2xl"
    >
      <div className="bg-ink text-ink-fg px-3 py-1.5 flex items-center justify-between">
        <div className="flex items-center gap-2 text-micro font-bold">
          <Terminal className="w-3 h-3 text-brand" />
          <span>{stage || "Console"} · Live Telemetry</span>
        </div>
        <div className="flex items-center gap-2">
          {/* iter-14.16 — Connect / Disconnect Factory droid.
              Shown whenever the project has any Factory config saved
              (currently enabled OR previously configured but toggled
              off). Missing altogether → nothing to toggle; the user
              needs to configure Factory on the Console page first. */}
          {factoryConfigured && (
            <button
              onClick={toggleFactoryEnabled}
              disabled={factoryToggling}
              data-testid="mini-console-factory-toggle"
              title={
                factoryConnected
                  ? "Disconnect Factory droid — LLM calls will fall back to the Fabric provider. Use during confidence generation to skip Factory."
                  : "Reconnect Factory droid — resume routing LLM calls through Factory."
              }
              className={
                "flex items-center gap-1 px-1.5 py-0.5 rounded-sm text-micro font-bold border transition-colors disabled:opacity-50 "
                + (factoryConnected
                  ? "bg-emerald-500/20 border-emerald-400/60 text-emerald-100 hover:bg-emerald-500/40"
                  : "bg-fg-subtle/20 border-border-strong/60 text-fg-onDark hover:bg-fg-subtle/40")
              }
            >
              {factoryConnected
                ? <PlugZap className="w-3 h-3" />
                : <Plug   className="w-3 h-3" />}
              <span>{factoryToggling
                ? "…"
                : (factoryConnected ? "DISCONNECT" : "RECONNECT")}</span>
            </button>
          )}
          <button
            onClick={refresh}
            title="Refresh now"
            className="hover:text-brand disabled:opacity-50"
            disabled={refreshing}
            data-testid="mini-console-refresh"
          >
            <RefreshCw className={`w-3 h-3 ${refreshing ? "animate-spin" : ""}`} />
          </button>
          <button
            onClick={toggle}
            data-testid="mini-console-toggle"
            className="hover:text-brand"
          >
            <ChevronDown className="w-3 h-3" />
          </button>
        </div>
      </div>
      {/* iter-13.35 — live routing/health banner */}
      {(() => {
        // Decide which provider is actually being tried:
        //   - If Factory orchestrator is enabled, every LLM call is routed
        //     through Factory regardless of fabric providers.
        //   - Otherwise the last call's `current.provider` shows the
        //     fabric provider in use.
        let bg = "bg-surface-2 border-border-strong text-fg-muted";
        let dot = "bg-fg-subtle";
        let label = "";
        if (factoryConnected) {
          if (factoryHealth === "up") {
            bg = "bg-emerald-100 border-emerald-300 text-emerald-800";
            dot = "bg-emerald-500";
            label = `factory.ai · UP · routing → ${factoryComputer || "computer"}`;
          } else if (factoryHealth === "down") {
            bg = "bg-red-100 border-red-300 text-red-800";
            dot = "bg-red-500 animate-pulse";
            label = `factory.ai · DOWN${factoryComputer ? ` · ${factoryComputer}` : ""}${factoryHealthErr ? ` · ${factoryHealthErr}` : ""}`;
          } else if (factoryHealth === "misconfigured") {
            // iter-13.116 — user ticked Enable Factory but hasn't pasted
            // the app_key / computer_id yet. Make the missing piece
            // visible right here so they don't waste time wondering why
            // the route still falls through to Ollama/fabric.
            bg = "bg-orange-100 border-orange-300 text-orange-900";
            dot = "bg-orange-500 animate-pulse";
            label = `factory.ai · SETUP INCOMPLETE${factoryHealthErr ? ` · ${factoryHealthErr}` : " · finish on Factory tab"}`;
          } else {
            bg = "bg-amber-100 border-amber-300 text-amber-800";
            dot = "bg-amber-500 animate-pulse";
            label = `factory.ai · checking… · ${factoryComputer || "computer"}`;
          }
        } else {
          bg = "bg-surface-2 border-border-strong text-fg-muted";
          dot = "bg-fg-subtle";
          label = `Fabric · routing → ${current.provider || "—"}${current.model ? ` · ${shortModel(current.model)}` : ""}`;
        }
        return (
          <div
            data-testid="mini-console-routing-banner"
            className={`px-3 py-1.5 border-b text-micro font-semibold flex items-center gap-2 ${bg}`}
            title={factoryConnected && factoryHealthAt ? `last ping ${ageLabel(factoryHealthAt)}` : ""}
          >
            <span className={`w-2 h-2 rounded-full ${dot}`} />
            <span className="truncate">{label}</span>
            {factoryConnected && (
              <button
                onClick={refreshFactoryHealth}
                title="Ping factory.ai now"
                className="ml-auto hover:opacity-70"
                data-testid="mini-console-factory-ping"
              >
                <RefreshCw className="w-3 h-3" />
              </button>
            )}
          </div>
        );
      })()}

      <div className="p-3 border-b border-border">
        <div className="flex items-center justify-between mb-1">
          <div className="text-micro uppercase text-fg-muted font-bold flex items-center gap-1">
            <Activity className="w-3 h-3" /> Currently using
          </div>
          <span className={`w-2 h-2 rounded-full ${statusDot}`} title={current.status || "—"} />
        </div>
        {(() => {
          // iter-13.36 — when Factory.ai is the active route, the
          // "Currently using" tile MUST reflect that — not the stale
          // claude-sonnet entry from a fabric call before Factory was
          // turned on. The usage log keeps history; the tile shows intent.
          const factoryActive = factoryConnected;
          const displayModel = factoryActive
            ? "factory/auto"
            : current.model;
          const displayProvider = factoryActive
            ? `factory.ai${factoryComputer ? ` · ${factoryComputer}` : ""}`
            : (current.provider || "—");
          return (
            <>
              <div
                className="font-mono text-[12px] text-fg font-bold leading-tight"
                data-testid="mini-console-current-model-expanded"
                title={factoryActive
                  ? "Factory.ai auto-routes each call; the actual model is chosen by Factory and not reported to LAMA."
                  : (current.model || "")}
              >
                {factoryActive ? "factory/auto" : shortModel(displayModel)}
                {factoryActive && (
                  <span className={`ml-2 text-micro px-1.5 py-0.5 rounded font-normal ${
                    factoryHealth === "up" ? "bg-emerald-100 text-emerald-700"
                    : factoryHealth === "down" ? "bg-red-100 text-red-700"
                    : factoryHealth === "misconfigured" ? "bg-orange-100 text-orange-800"
                    : "bg-amber-100 text-amber-700"
                  }`}>
                    {factoryHealth === "up" ? "STRICT"
                      : factoryHealth === "misconfigured" ? "SETUP"
                      : factoryHealth.toUpperCase()}
                  </span>
                )}
              </div>
              <div className="text-micro text-fg-muted flex items-center gap-2 mt-0.5">
                <span className="truncate">{displayProvider}</span>
                {current.agent_key && (
                  <>
                    <span>·</span>
                    <span className="font-mono">{current.agent_key}</span>
                  </>
                )}
                <span>·</span>
                <span>{ageLabel(current.at)}</span>
              </div>
              {factoryActive && current.model && !/^factory/i.test(current.model || "") && (
                // Honesty: last-logged call was NOT through Factory. Show
                // it as historical so the user isn't confused by stale data.
                <div className="text-micro text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5 mt-1 font-mono">
                  last log entry was <b>{shortModel(current.model)}</b> ({ageLabel(current.at)}) — before Factory was the active route
                </div>
              )}
            </>
          );
        })()}
        {current.tokens > 0 && (
          <div className="text-micro text-fg-muted mt-0.5 font-mono">
            last call: {fmt(current.tokens)} tokens · ${(current.cost_usd || 0).toFixed(4)} · {current.duration_ms || 0}ms
          </div>
        )}
      </div>

      <div className="p-3 grid grid-cols-2 gap-3 text-micro">
        <div>
          <div className="text-micro uppercase text-fg-muted font-bold">
            {stage ? `${stage} · 7d` : "This stage · 7d"}
          </div>
          <div className="font-mono text-fg text-base font-bold" data-testid="mini-console-stage-tokens">
            {fmt(stageTokens)}
            <span className="text-micro text-fg-muted font-normal"> tokens</span>
          </div>
          <div className="font-mono text-micro text-fg-muted">
            ${stageCost.toFixed(4)}
          </div>
        </div>
        <div>
          <div className="text-micro uppercase text-fg-muted font-bold">
            All stages · 7d
          </div>
          <div className="font-mono text-fg text-base font-bold" data-testid="mini-console-total-tokens-expanded">
            {fmt(totalTokens)}
            <span className="text-micro text-fg-muted font-normal"> tokens</span>
          </div>
          <div className="font-mono text-micro text-fg-muted">
            ${totalCost.toFixed(4)} · {summary?.total_runs || 0} runs
          </div>
        </div>
      </div>

      {summary?.by_model?.length > 0 && (
        <div className="px-3 pb-2 text-micro">
          <div className="text-micro uppercase text-fg-muted font-bold mb-1">
            Tokens by model
          </div>
          <div className="space-y-0.5 max-h-[80px] overflow-auto">
            {[...summary.by_model]
              .sort((a, b) => (b.tokens || 0) - (a.tokens || 0))
              .slice(0, 4)
              .map((m) => (
                <div
                  key={m.model}
                  className="flex items-center justify-between font-mono"
                >
                  <span className="text-fg truncate">{shortModel(m.model)}</span>
                  <span className="text-fg-muted">{fmt(m.tokens)}t</span>
                </div>
              ))}
          </div>
        </div>
      )}

      {/* iter-14.59 — Project-wise token utilization. Renders whenever a
          project is active so operators can see this project's total,
          run-count, and the per-stage split (incl. Living, which the
          old to_list(5000) cap was silently dropping). */}
      {projectId && projectRow && (
        <div
          className="px-3 pb-2 text-micro border-t border-surface-2 pt-2"
          data-testid="mini-console-project-utilization"
        >
          <div className="flex items-center justify-between mb-1">
            <div className="text-micro uppercase text-fg-muted font-bold flex items-center gap-1">
              <Activity className="w-3 h-3" /> Project · 7d
            </div>
            <span
              className="font-mono text-micro text-fg-muted truncate max-w-[180px]"
              title={projectId}
            >
              {projectName}
            </span>
          </div>
          <div className="flex items-baseline justify-between mb-1.5">
            <span
              className="font-mono text-fg text-sm font-bold"
              data-testid="mini-console-project-tokens"
            >
              {fmt(projectTokens)}
              <span className="text-micro text-fg-muted font-normal"> tokens</span>
            </span>
            <span className="font-mono text-micro text-fg-muted">
              ${projectCost.toFixed(4)} · {fmt(projectRuns)} runs
            </span>
          </div>
          <div className="text-micro uppercase tracking-wide text-fg-subtle font-bold mb-0.5">
            {agentDriven ? "Tokens by agent" : "Tokens by stage"}
          </div>
          {projectStageRows.length === 0 && (
            <div className="text-micro text-fg-subtle italic py-1">No activity yet in the last 7 days.</div>
          )}
          <div className="space-y-0.5">
            {projectStageRows.map((r) => {
              const pct = Math.round((r.tokens / projectStageMax) * 100);
              const isActive = r.stage === stage;
              return (
                <div
                  key={r.stage}
                  className="flex items-center gap-2 font-mono"
                  data-testid={`mini-console-project-stage-${r.stage}`}
                >
                  <span
                    className={
                      "w-[68px] truncate text-micro " +
                      (isActive ? "text-fg font-bold" : "text-fg-muted")
                    }
                    title={r.stage}
                  >
                    {r.stage}
                  </span>
                  <div className="flex-1 h-1.5 rounded bg-surface-2 overflow-hidden">
                    <div
                      className={
                        "h-full transition-all " +
                        (agentDriven ? "" : (isActive ? "bg-brand" : "bg-fg-subtle"))
                      }
                      style={{ width: `${pct}%`, backgroundColor: agentDriven ? (r.color || "#B3B3BC") : undefined }}
                    />
                  </div>
                  <span className="w-14 text-right text-micro text-fg-muted">
                    {fmt(r.tokens)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* iter-14.17 — Live backend log tail.
          Header row is always visible (a "Logs" button toggles the body)
          so the operator knows the feed exists and can open it during
          long-running work (Confidence, SRS regen, HF cold-start). */}
      <div className="border-t border-border">
        <button
          onClick={toggleLogsPane}
          data-testid="mini-console-logs-toggle"
          className="w-full px-3 py-1.5 flex items-center justify-between text-micro font-bold uppercase text-fg-muted hover:bg-surface-2"
          title={logsOpen ? "Hide backend logs" : "Show live backend logs"}
        >
          <span className="flex items-center gap-1">
            <ScrollText className="w-3 h-3" />
            Backend logs
            {logsOpen && logs.length > 0 && (
              <span className="ml-1 font-mono text-micro text-fg-muted">
                · {logs.length} lines
              </span>
            )}
            {logsDropped > 0 && (
              <span className="ml-1 font-mono text-micro text-orange-600">
                · {logsDropped} dropped
              </span>
            )}
          </span>
          {logsOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
        </button>

        {logsOpen && (
          <div data-testid="mini-console-logs-pane" className="px-3 pb-2">
            <div className="flex items-center gap-1 mb-1">
              <select
                value={logLevel}
                onChange={(e) => setLogLevel(e.target.value)}
                className="text-micro border border-border rounded px-1 py-0.5 bg-surface focus:outline-none focus:ring-1 focus:ring-brand"
                data-testid="mini-console-logs-level"
                title="Minimum log level"
              >
                <option value="">ALL</option>
                <option value="INFO">INFO</option>
                <option value="WARNING">WARN</option>
                <option value="ERROR">ERROR</option>
              </select>
              <input
                type="text"
                value={logFilter}
                onChange={(e) => setLogFilter(e.target.value)}
                placeholder="filter (e.g. confidence, factory, 401)"
                className="flex-1 text-micro border border-border rounded px-1.5 py-0.5 font-mono focus:outline-none focus:ring-1 focus:ring-brand"
                data-testid="mini-console-logs-filter"
              />
              <button
                onClick={() => setLogsFollow((v) => !v)}
                title={logsFollow ? "Pause auto-scroll" : "Follow (auto-scroll to bottom)"}
                className={"p-0.5 rounded " + (logsFollow ? "text-emerald-600 hover:bg-emerald-50" : "text-fg-muted hover:bg-surface-2")}
                data-testid="mini-console-logs-follow"
              >
                {logsFollow ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
              </button>
              <button
                onClick={copyLogs}
                title={
                  logs.length
                    ? `Copy ${logs.length} visible log line(s) to clipboard`
                    : "No logs to copy"
                }
                disabled={!logs.length}
                className={
                  "p-0.5 rounded " +
                  (logsCopied
                    ? "text-emerald-600 hover:bg-emerald-50"
                    : logs.length
                    ? "text-fg-muted hover:bg-surface-2"
                    : "text-fg-subtle cursor-not-allowed")
                }
                data-testid="mini-console-logs-copy"
              >
                {logsCopied ? (
                  <Check className="w-3 h-3" />
                ) : (
                  <Copy className="w-3 h-3" />
                )}
              </button>
              <button
                onClick={() => setLogsMax(true)}
                title="Maximize log viewer (fullscreen)"
                className="p-0.5 rounded text-fg-muted hover:bg-surface-2"
                data-testid="mini-console-logs-maximize"
              >
                <Maximize2 className="w-3 h-3" />
              </button>
              <button
                onClick={clearLogs}
                title="Clear visible logs (server buffer keeps history)"
                className="p-0.5 rounded text-fg-muted hover:bg-surface-2"
                data-testid="mini-console-logs-clear"
              >
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
            <div
              ref={logsBoxRef}
              className="h-40 overflow-y-auto bg-ink text-border rounded-sm p-2 font-mono text-micro leading-tight border border-fg"
            >
              {logs.length === 0 ? (
                <div className="text-fg-muted italic">
                  waiting for log records…
                </div>
              ) : (
                logs.map((r) => {
                  let cls = "text-fg-subtle";
                  if (r.level === "ERROR" || r.level === "CRITICAL") cls = "text-red-400";
                  else if (r.level === "WARNING") cls = "text-amber-300";
                  else if (r.level === "DEBUG") cls = "text-fg-muted";
                  else cls = "text-fg-subtle";
                  const ts = new Date((r.ts || 0) * 1000).toLocaleTimeString();
                  return (
                    <div key={r.seq} className="whitespace-pre-wrap break-words">
                      <span className="text-fg-muted">{ts}</span>{" "}
                      <span className={"font-bold " + cls}>{r.level.padEnd(5).slice(0, 5)}</span>{" "}
                      <span className="text-info">{r.name}</span>{" "}
                      <span className={cls}>{r.msg}</span>
                      {r.repeat > 1 && (
                        <span className="ml-1 text-micro text-amber-300 font-bold">
                          ×{r.repeat}
                        </span>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}
      </div>

      <div className="border-t border-border px-3 py-1.5 flex items-center justify-between text-micro">
        <Link
          to="/console?tab=agents"
          className="text-fg hover:text-brand underline"
          data-testid="mini-console-open-agents"
        >
          Open agents →
        </Link>
        {factoryConnected ? (
          <Link
            to="/console?tab=factory"
            className="text-fg hover:text-brand underline"
          >
            Factory →
          </Link>
        ) : (
          <Link
            to="/console?tab=models"
            className="text-fg hover:text-brand underline"
          >
            Models →
          </Link>
        )}
      </div>

      {logsMax && (
        <div aria-hidden="true"
          data-testid="mini-console-logs-max-overlay"
          className="fixed inset-0 z-[100] bg-ink/70 flex items-center justify-center p-4"
          onClick={(e) => { if (e.target === e.currentTarget) setLogsMax(false); }}
        >
          <div className="w-full h-full max-w-[1400px] max-h-[92vh] bg-ink border border-fg rounded-md shadow-2xl flex flex-col">
            <div className="bg-ink text-ink-fg px-4 py-2 flex items-center justify-between rounded-t-md">
              <div className="flex items-center gap-2 text-[13px] font-bold">
                <ScrollText className="w-4 h-4 text-brand" />
                Backend logs
                <span className="ml-2 font-mono text-micro text-fg-subtle">
                  · {logs.length} lines
                </span>
                {logsDropped > 0 && (
                  <span className="font-mono text-micro text-orange-400">
                    · {logsDropped} dropped
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={logLevel}
                  onChange={(e) => setLogLevel(e.target.value)}
                  className="text-micro border border-border-strong rounded px-1.5 py-0.5 bg-ink-hover text-white focus:outline-none focus:ring-1 focus:ring-brand"
                  title="Minimum log level"
                >
                  <option value="">ALL</option>
                  <option value="INFO">INFO</option>
                  <option value="WARNING">WARN</option>
                  <option value="ERROR">ERROR</option>
                </select>
                <input
                  type="text"
                  value={logFilter}
                  onChange={(e) => setLogFilter(e.target.value)}
                  placeholder="filter (e.g. confidence, factory, 401)"
                  className="w-64 text-micro border border-border-strong rounded px-2 py-0.5 bg-ink-hover text-white font-mono focus:outline-none focus:ring-1 focus:ring-brand"
                />
                <button
                  onClick={() => setLogsFollow((v) => !v)}
                  title={logsFollow ? "Pause auto-scroll" : "Follow (auto-scroll to bottom)"}
                  className={"p-1 rounded " + (logsFollow ? "text-emerald-400 hover:bg-ink-hover" : "text-fg-onDarkMuted hover:text-fg-onDark hover:bg-ink-hover")}
                >
                  {logsFollow ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
                </button>
                <button
                  onClick={copyLogs}
                  disabled={!logs.length}
                  title={logs.length ? `Copy ${logs.length} log line(s)` : "No logs to copy"}
                  className={"p-1 rounded " + (logsCopied ? "text-emerald-400" : logs.length ? "text-fg-onDarkMuted hover:text-fg-onDark hover:bg-ink-hover" : "text-fg-onDarkSubtle cursor-not-allowed")}
                >
                  {logsCopied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                </button>
                <button
                  onClick={clearLogs}
                  title="Clear visible logs"
                  className="p-1 rounded text-fg-onDarkMuted hover:text-fg-onDark hover:bg-ink-hover"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
                <button
                  onClick={() => setLogsMax(false)}
                  title="Close (Esc)"
                  className="p-1 rounded text-fg-onDarkMuted hover:text-fg-onDark hover:bg-ink-hover"
                  data-testid="mini-console-logs-max-close"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
            <div
              ref={logsBoxMaxRef}
              className="flex-1 overflow-y-auto bg-ink text-border p-4 font-mono text-[12px] leading-relaxed"
            >
              {logs.length === 0 ? (
                <div className="text-fg-muted italic">waiting for log records…</div>
              ) : (
                logs.map((r) => {
                  let cls = "text-fg-subtle";
                  if (r.level === "ERROR" || r.level === "CRITICAL") cls = "text-red-400";
                  else if (r.level === "WARNING") cls = "text-amber-300";
                  else if (r.level === "DEBUG") cls = "text-fg-muted";
                  else cls = "text-fg-subtle";
                  const ts = new Date((r.ts || 0) * 1000).toLocaleTimeString();
                  return (
                    <div key={r.seq} className="whitespace-pre-wrap break-words">
                      <span className="text-fg-muted">{ts}</span>{" "}
                      <span className={"font-bold " + cls}>{r.level.padEnd(5).slice(0, 5)}</span>{" "}
                      <span className="text-info">{r.name}</span>{" "}
                      <span className={cls}>{r.msg}</span>
                      {r.repeat > 1 && (
                        <span className="ml-1 text-micro text-amber-300 font-bold">×{r.repeat}</span>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

