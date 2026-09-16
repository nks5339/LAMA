import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Loader2,
  Sparkles,
  Trash2,
  Check,
  Plus,
  Cpu,
  Bot,
  FileText,
  Wand2,
  KeyRound,
  Terminal,
  Zap,
  ChevronDown,
  ChevronRight as ChevronRightIcon,
  RotateCcw,
  Play,
  TestTube,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import {
  setupProvider, listProviders, updateProvider, updateProviderKey,
  deleteProvider, testProvider, fetchProviderModels,
  listAgents, updateAgent, resetAgentBudget, testAgent,   listPrompts, previewPrompt, testPrompt, updateProjectPrompt,
  getFactoryOrchestratorConfig, updateFactoryOrchestratorConfig, testFactoryOrchestratorConfig,
  testFactoryOrchestratorCli,
  deleteFactoryOrchestratorConfig,
  ensureFactoryOrchestratorWorkspace, wakeFactoryOrchestratorDroid,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  Field as FormField,
  TextInput,
  SelectInput,
  FormErrorSummary,
} from "@/components/ui/form";
import { FACTORY_MODEL_OPTIONS } from "@/lib/factoryModels";

const STAGES = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"];
const COMPLEXITY_COLOR = {
  trivial: "bg-surface-2 text-fg-muted",
  low: "bg-emerald-100 text-emerald-700",
  medium: "bg-amber-100 text-amber-700",
  high: "bg-rose-100 text-rose-700",
  critical: "bg-red-200 text-red-900",
  reasoning: "bg-violet-100 text-violet-700",
};
// iter-19 — ordered cheapest → most capable. `reasoning` sits last as a
// sideways step, not a seventh rung: it is a different SHAPE of model
// (o-series) for diagnostic work, not a stronger one than `critical`.
// Leaving a tier unset is fine — the backend walks down the ladder.
const ROUTING_TIERS = ["trivial", "low", "medium", "high", "critical", "reasoning"];
const TIER_HINT = {
  trivial: "Labels, short summaries, one-line classifications",
  low: "Narrative passes, docs, cheap gates",
  medium: "Structural work — validation, envelopes, review",
  high: "Code generation and planning",
  critical: "DevOps, production-readiness, last-chance repairs",
  reasoning: "Diagnosing build failures from raw tool output",
};
const STATUS_COLOR = {
  enabled: "bg-emerald-100 text-emerald-700",
  disabled: "bg-surface-3 text-fg-muted",
  replaced: "bg-violet-100 text-violet-700",
  wrapped: "bg-blue-100 text-blue-700",
};

// iter-13.81 — per-pipeline-node model selection for Factory orchestrator.
// Each LAMA stage gets two operator-picked models: one for the first-time
// run (generate) and one for re-runs / regenerate / drift / gap-recovery.
// Backend buckets are keyed `{stage_key}.{mode}` — see factory_orchestrator
// FACTORY_STAGE_BUCKETS / FACTORY_MODE_BUCKETS.
//
// iter-14.94 — Tool project types (Gap Analyzer, Code/Technology
// Transformer) don't run the 5-stage legacy migration pipeline, so they
// must NOT show Discovery/DataModel/Architecture/CodeGen/Living here.
// Each project_type gets its own node list; see Sidebar.jsx STAGES_BY_TYPE
// for the matching pattern used on the sidebar pipeline nav.
const FACTORY_PIPELINE_NODES_LEGACY = [
  { key: "discovery",    label: "Discovery & SRS",     hint: "KB scan, SRS sections, chat, ontology" },
  { key: "datamodel",    label: "DataModel",           hint: "OLTP/OLAP DDL, bus matrix, migration scripts" },
  { key: "architecture", label: "Architecture",        hint: "Service map, HLD, LLD, sequence, API contracts" },
  { key: "codegen",      label: "CodeGen",             hint: "Per-service generation; regenerate = gap recovery" },
  { key: "living",       label: "Living",              hint: "Drift detection, SRS diff (always regenerate mode)" },
];
const FACTORY_PIPELINE_NODES_TRANSFORMER = [
  { key: "transformer",  label: "Code Transformer",    hint: "Pattern detection, planning, coding, verification, testing" },
];
const FACTORY_PIPELINE_NODES_GAP_ANALYSIS = [
  { key: "gap_analyzer", label: "Gap Analyzer",        hint: "UI→API→DB traceability + coverage gap detection" },
];
const FACTORY_PIPELINE_NODES_BY_TYPE = {
  legacy_migration: FACTORY_PIPELINE_NODES_LEGACY,
  tech_transformer: FACTORY_PIPELINE_NODES_TRANSFORMER,
  gap_analysis:     FACTORY_PIPELINE_NODES_GAP_ANALYSIS,
};
// Union of every node across all project types — used only to seed the
// default models map so a project's persisted config always has a value
// for every possible bucket key, regardless of which type it is.
const FACTORY_PIPELINE_NODES_ALL = [
  ...FACTORY_PIPELINE_NODES_LEGACY,
  ...FACTORY_PIPELINE_NODES_TRANSFORMER,
  ...FACTORY_PIPELINE_NODES_GAP_ANALYSIS,
];
// iter-13.129 — model IDs MUST match what `droid exec -m <id>` accepts.
// Probe with:  droid exec -m <bad-id> "test"   → prints the valid list.
// The short-name aliases (claude-opus-4, claude-sonnet-4, claude-haiku-4)
// that used to live here were REJECTED by droid CLI with "Invalid model",
// which then cascaded through the fabric fallback to the misleading
// "OPENROUTER_API_KEY not configured" abort. Backend normaliser also
// maps any legacy short-name still persisted in Mongo → its canonical
// droid ID at read-time (see _canonicalise_factory_model_id).
// iter-15.39 — FACTORY_MODEL_OPTIONS now lives in ../lib/factoryModels so the
// Code Transformer's Agent Pipeline "Prompt & Model" panel can reuse the
// exact same catalogue instead of drifting out of sync.
const FACTORY_DEFAULT_MODELS = (() => {
  const out = {};
  for (const n of FACTORY_PIPELINE_NODES_ALL) {
    out[`${n.key}.generate`] = "auto";
    out[`${n.key}.regenerate`] = "auto";
  }
  return out;
})();

// ============================================================
// Tab: Factory Orchestrator
// ============================================================
function FactoryOrchestratorTab() {
  const { active } = useProjects();
  const projectId = active?.id;
  // iter-14.94 — show only the pipeline node(s) relevant to this project's
  // type. Falls back to the legacy 5-stage list for older projects with no
  // project_type persisted yet (mirrors Sidebar.jsx's STAGES_BY_TYPE fallback).
  const pipelineNodes = FACTORY_PIPELINE_NODES_BY_TYPE[active?.project_type || "legacy_migration"]
    || FACTORY_PIPELINE_NODES_LEGACY;
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // iter-13.91.4 — busy flags + last-result memo for the explicit
  // "Create workspace" and "Wake droid" actions added to the Console.
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [wakeBusy, setWakeBusy] = useState(false);
  const [workspaceState, setWorkspaceState] = useState(null); // {ok, cwd, computer_id, error?}
  const [wakeState, setWakeState] = useState(null);           // {ok, computer_id, error?}
  // iter-13.127 — inline "Test connection" state. Works in both API and
  // CLI modes: in CLI mode it probes `droid --version` (fast, no network);
  // in API mode it delegates to the existing `/factory-orchestrator/test`
  // route (which also short-circuits to CLI when the project is set to
  // cli mode, so the same button always does the right thing).
  const [testBusy, setTestBusy] = useState(false);
  const [testState, setTestState] = useState(null);           // {ok, version?, binary?, error?}
  // iter-13.35 — typed-confirmation modal for destructive delete.
  const [showDelete, setShowDelete] = useState(false);
  const [deleteText, setDeleteText] = useState("");
  const [config, setConfig] = useState(null);
  const [form, setForm] = useState({
    enabled: false,
    app_key: "",
    computer_id: "",
    cwd: "",
    model: "auto",                       // iter-13.80 — project-level fallback default
    models: { ...FACTORY_DEFAULT_MODELS },// iter-13.81 — per-(stage, mode) bucket map
    host_anchored: true,                 // iter-13.91.16 — DEFAULT ON: workspace stays on LAMA host
    allow_fallback: false,               // iter-13.91.15 — route via OpenRouter when Factory is down
    // iter-13.125 — Per-project transport mode. "api" (default) routes
    // via Factory's HTTP API; "cli" shells out to the `droid` binary
    // on the LAMA host. The toggle exposes this in the UI; the cli_*
    // fields only apply when mode === "cli".
    mode: "api",
    cli_bin: "",
    cli_auto: "low",
  });

  const refresh = async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const r = await getFactoryOrchestratorConfig(projectId);
      const c = r?.config || {};
      setConfig(c);
      setForm({
        enabled: !!c.enabled,
        app_key: "",
        computer_id: c.computer_id || "",
        cwd: c.cwd || "",
        model: c.model || "auto",                          // iter-13.80
        models: { ...FACTORY_DEFAULT_MODELS, ...(c.models || {}) }, // iter-13.81
        host_anchored: !!c.host_anchored,                  // iter-13.91.12
        allow_fallback: !!c.allow_fallback,                // iter-13.91.15
        mode: (c.mode === "cli" ? "cli" : "api"),          // iter-13.125
        cli_bin: c.cli_bin || "",
        cli_auto: c.cli_auto || "low",
      });
    } catch (e) {
      toast.error("Could not load orchestrator config", { description: e?.response?.data?.detail || e.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSave = async () => {
    if (!projectId) return;
    const isCli = form.mode === "cli";
    const hasAppKey = !!(form.app_key.trim() || config?.has_app_key);
    const hasComputer = !!form.computer_id.trim();
    // iter-13.125 — CLI mode doesn't need app_key / computer_id.
    // API mode keeps the existing "must have both" contract.
    const targetEnabled = isCli
      ? !!form.enabled
      : !!(form.enabled || (hasAppKey && hasComputer));

    if (!isCli && targetEnabled && !hasComputer) {
      toast.error("computer_id is required when Factory orchestrator (API mode) is enabled.");
      return;
    }
    if (!isCli && targetEnabled && !hasAppKey) {
      toast.error("app key is required when Factory orchestrator (API mode) is enabled.");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        project_id: projectId,
        enabled: targetEnabled,
        computer_id: form.computer_id.trim(),
        cwd: form.cwd.trim(),
        model: (form.model || "auto").trim(),    // iter-13.80 — project-level default
        models: form.models || {},               // iter-13.81 — per-(stage, mode)
        host_anchored: !!form.host_anchored,     // iter-13.91.12 — host-anchored mode
        allow_fallback: !!form.allow_fallback,   // iter-13.91.15 — auto-route to OpenRouter when Factory is unreachable
        // iter-13.125 — transport mode + CLI overrides
        mode: isCli ? "cli" : "api",
        cli_bin: form.cli_bin.trim(),
        cli_auto: form.cli_auto || "low",
        ...(form.app_key.trim() ? { app_key: form.app_key.trim() } : {}),
      };
      const r = await updateFactoryOrchestratorConfig(payload);
      const c = r?.config || {};
      setConfig(c);
      setForm((prev) => ({ ...prev, app_key: "", enabled: !!c.enabled }));
      // iter-13.116 — broadcast so the Models-tab gate (and any other
      // listener) flips immediately without waiting for the polling
      // interval / tab switch. Before this, ticking Enable Factory
      // Orchestrator + Save left the Models tab's "Add Ollama" button
      // and provider editor live until the user manually switched tabs.
      try {
        window.dispatchEvent(new CustomEvent("lama:factory-config-changed", {
          detail: { enabled: !!c.enabled },
        }));
      } catch { /* */ }
      // iter-13.91.7 — DO NOT auto-fire `/factory-orchestrator/test` here.
      // Previously this kicked off a fresh Factory bootstrap session on
      // every Save. The MiniConsole already polls `/test` every 30s
      // (cheap by default since iter-13.91.7), and the explicit
      // "Wake droid" / "Create workspace" buttons exist for verification.
      if (c.enabled) {
        toast.success("Factory orchestrator enabled and saved", {
          description: "Use the Wake droid / Create workspace buttons below to verify connectivity.",
        });
      } else {
        toast.success("Factory orchestrator disabled");
      }
    } catch (e) {
      toast.error("Save failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async () => {
    if (!projectId) return;
    if (deleteText.trim().toUpperCase() !== "DELETE") {
      toast.error('Type DELETE to confirm.');
      return;
    }
    setDeleting(true);
    try {
      await deleteFactoryOrchestratorConfig(projectId);
      toast.success("Factory orchestrator config deleted", {
        description: "App key, computer ID and cached sessions removed. Project now routes through standard fabric.",
      });
      setShowDelete(false);
      setDeleteText("");
      setForm({ enabled: false, app_key: "", computer_id: "", cwd: "", model: "auto", models: { ...FACTORY_DEFAULT_MODELS }, host_anchored: false, allow_fallback: false, mode: "api", cli_bin: "", cli_auto: "low" });
      setWorkspaceState(null);
      setWakeState(null);
      // iter-13.116 — broadcast: Factory is gone, Models tab re-enables.
      try {
        window.dispatchEvent(new CustomEvent("lama:factory-config-changed", {
          detail: { enabled: false },
        }));
      } catch { /* */ }
      await refresh();
    } catch (e) {
      toast.error("Delete failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setDeleting(false);
    }
  };

  // iter-13.91.4 — explicit "Create / refresh workspace on Droid" action.
  // Backend forces a fresh `mkdir -p` (cache bypassed) so the user sees
  // the real filesystem outcome immediately instead of a stale cache hit.
  const onEnsureWorkspace = async () => {
    if (!projectId) return;
    setWorkspaceBusy(true);
    try {
      const r = await ensureFactoryOrchestratorWorkspace(projectId);
      setWorkspaceState(r);
      if (r?.ok) {
        if (r.relocated) {
          // iter-13.91.9 — original cwd was read-only; backend auto-fell-back
          // to a writable HOME-rooted path. Tell the user so they can update
          // mental model + confirm the new path appears in Factory's sidebar.
          toast.success("Workspace created (auto-relocated)", {
            description: `${r.relocated_from || "(default)"} → ${r.cwd} (writable, HOME-rooted)`,
          });
          // Refresh form so the new cwd shows up in the input.
          refresh();
        } else {
          toast.success("Workspace ready on Droid", { description: r.cwd || "(default)" });
        }
      } else {
        // iter-13.91.5 — backend now returns {reason, error, detail}. Show
        // the most actionable message we have. "droid_disconnected" is the
        // single most common failure mode (Factory Droid Computer offline).
        const msg = r?.detail || r?.error || r?.reason || "Unknown error";
        toast.error("Workspace bootstrap failed", { description: msg });
      }
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message;
      setWorkspaceState({ ok: false, error: msg });
      toast.error("Workspace bootstrap failed", { description: msg });
    } finally {
      setWorkspaceBusy(false);
    }
  };

  // iter-13.91.4 — explicit "Wake Droid Computer" action. Sends a
  // throttle-bypassed GET /computers/{id} (Factory's auto-resume trigger).
  const onWakeDroid = async () => {
    if (!projectId) return;
    setWakeBusy(true);
    try {
      const r = await wakeFactoryOrchestratorDroid(projectId);
      setWakeState(r);
      if (r?.ok) {
        toast.success("Droid woken", { description: r.computer_id || r.computer_ref || "" });
      } else {
        // iter-13.91.x — translate raw http_NNN reasons into actionable
        // guidance so operators don't have to grep Factory docs.
        const hintByStatus = {
          401: "Factory app key is invalid or expired. Re-issue a token in Factory → Settings → API Keys, then update it in the Factory Orchestrator panel below.",
          403: "Factory rejected the token (forbidden). The app key likely belongs to a different workspace than this Computer ID, lacks 'computers:read' scope, or was revoked. Verify the key and Computer ID are from the same Factory workspace.",
          404: "Computer ID not found in Factory. Double-check the ID (or paste the slug/name) in the Factory Orchestrator config.",
          429: "Factory rate-limited the wake call. Wait ~30s and retry.",
          424: "Factory says the Droid is unreachable (offline / paused / stopped). Open the Factory web UI and click Start on the computer, then retry.",
        };
        const parts = [];
        if (r?.reason) parts.push(r.reason);
        if (r?.error) parts.push(r.error);
        if (r?.http_status) parts.push(`HTTP ${r.http_status}`);
        const hint = hintByStatus[r?.http_status];
        const desc = (parts.join(" · ") || "Unknown error") + (hint ? `\n\n→ ${hint}` : "");
        toast.error("Wake failed", { description: desc, duration: 12000 });
      }
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message;
      setWakeState({ ok: false, error: msg });
      toast.error("Wake failed", { description: msg });
    } finally {
      setWakeBusy(false);
    }
  };

  // iter-13.127 — "Test connection" click handler. Two paths:
  //   • CLI mode: hit /factory-orchestrator/test-cli with the candidate
  //     `cli_bin` from the form (works even BEFORE Save so operators can
  //     validate a path they just typed).
  //   • API mode: hit /factory-orchestrator/test, which runs the full
  //     Factory HTTP handshake (resolve computer + connectivity check)
  //     using whatever config is currently saved in Mongo.
  // Result is surfaced inline (green/red pill) so users don't have to
  // hunt for it in toasts.
  const onTestConnection = async () => {
    if (!projectId) return;
    setTestBusy(true);
    setTestState(null);
    try {
      const r = form.mode === "cli"
        ? await testFactoryOrchestratorCli(form.cli_bin || "")
        : await testFactoryOrchestratorConfig(projectId);
      setTestState(r);
      if (r?.ok) {
        toast.success("Connection OK", {
          description: form.mode === "cli"
            ? `droid ${r.version || ""} @ ${r.binary || "PATH"}`
            : (r.computer_id ? `Factory computer ${r.computer_id}` : "Factory reachable"),
        });
      } else {
        toast.error("Connection failed", {
          description: r?.error || r?.reason || r?.detail || "Unknown error",
          duration: 10000,
        });
      }
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || "Unknown error";
      setTestState({ ok: false, error: msg });
      toast.error("Connection failed", { description: msg });
    } finally {
      setTestBusy(false);
    }
  };

  if (!projectId) {
    return (
      <div className="bg-surface border border-border rounded-sm p-6 text-[12px] text-fg-muted">
        Select an active project to configure Factory orchestrator.
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="tab-factory-orchestrator">
      <div className="bg-surface border border-border rounded-sm overflow-hidden">
        <div className="bg-brand-tint border-l-4 border-brand px-4 py-3">
          <div className="text-micro uppercase font-bold tracking-wider text-fg-muted">Factory-managed orchestration</div>
          <div className="text-sm font-display font-bold text-fg">
            Route all project LLM calls through Factory Sessions API
          </div>
          <div className="text-micro text-fg-muted mt-0.5">
            When enabled, LAMA sends prompts to Factory and renders returned responses in the same UI panels.
          </div>
        </div>

        <div className="p-4 space-y-3">
          <label className="flex items-center justify-between border border-border rounded-sm px-3 py-2">
            <div>
              <div className="text-[12px] font-semibold text-fg">Enable Factory Orchestrator</div>
              <div className="text-micro text-fg-muted">
                User-controlled on/off switch for this project.
                {(form.app_key.trim() || config?.has_app_key) && form.computer_id.trim() && !form.enabled && (
                  <span className="block mt-1 text-amber-700">
                    ⚠ App key + Computer are filled but Factory is OFF.
                    Tick this box (or click Save — auto-enabled when both
                    credentials are present) to route LLM calls via Factory.
                  </span>
                )}
              </div>
            </div>
            <input
              data-testid="factory-orch-enabled"
              type="checkbox"
              checked={!!form.enabled}
              onChange={(e) => {
                const v = e.target.checked;
                setForm((p) => ({ ...p, enabled: v }));
                // iter-13.116 — broadcast the INTENT immediately so the
                // Models-tab gate (and any other listener) reflects the
                // checkbox change without waiting for Save. The polling
                // refetch will eventually overwrite with the persisted
                // value if the user navigates away without saving.
                try {
                  window.dispatchEvent(new CustomEvent("lama:factory-config-changed", {
                    detail: { enabled: v, ephemeral: true },
                  }));
                } catch { /* */ }
              }}
              className="h-4 w-4"
            />
          </label>

          {/* iter-13.125 — Transport mode toggle. API (default) routes
              through Factory's HTTP API + Computer sandbox; CLI shells
              out to the `droid` binary on the LAMA host. Each mode
              shows only the fields it actually needs (API needs
              app_key + computer_id; CLI just needs the binary path). */}
          <div
            data-testid="factory-orch-mode-row"
            className="border-2 border-brand rounded-sm bg-brand-tint px-3 py-2"
          >
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[12px] font-bold text-fg uppercase tracking-wider">Transport mode</div>
                <div className="text-micro text-fg-muted mt-0.5">
                  Switch between Factory's <b>HTTP API</b> (managed Computer sandbox)
                  and the local <b>droid CLI</b> (subprocess on the LAMA host). Per-project — flip at any time.
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0 bg-surface border border-border rounded-sm p-0.5">
                <button
                  type="button"
                  data-testid="factory-orch-mode-api"
                  onClick={() => setForm((p) => ({ ...p, mode: "api" }))}
                  className={`text-micro font-bold px-3 py-1 rounded-sm transition-colors ${
                    form.mode === "api"
                      ? "bg-ink text-ink-fg"
                      : "text-fg-muted hover:text-fg"
                  }`}
                >
                  API
                </button>
                <button
                  type="button"
                  data-testid="factory-orch-mode-cli"
                  onClick={() => setForm((p) => ({ ...p, mode: "cli" }))}
                  className={`text-micro font-bold px-3 py-1 rounded-sm transition-colors ${
                    form.mode === "cli"
                      ? "bg-ink text-ink-fg"
                      : "text-fg-muted hover:text-fg"
                  }`}
                >
                  CLI
                </button>
              </div>
            </div>
            {form.mode === "cli" && (
              <div
                data-testid="factory-orch-cli-fields"
                className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-3"
              >
                <label className="block">
                  <span className="text-micro uppercase font-bold text-fg-muted">droid binary path (optional)</span>
                  <input
                    data-testid="factory-orch-cli-bin"
                    value={form.cli_bin}
                    onChange={(e) => setForm((p) => ({ ...p, cli_bin: e.target.value }))}
                    placeholder="droid  (or absolute path, e.g. /usr/local/bin/droid)"
                    className="mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5"
                  />
                  <span className="block mt-1 text-micro text-fg-muted">
                    Leave blank to fall back to the <code>LAMA_FACTORY_CLI_BIN</code> env var (default <code>droid</code> on PATH).
                  </span>
                </label>
                <label className="block">
                  <span className="text-micro uppercase font-bold text-fg-muted">Autonomy level</span>
                  <select
                    data-testid="factory-orch-cli-auto"
                    value={form.cli_auto || "low"}
                    onChange={(e) => setForm((p) => ({ ...p, cli_auto: e.target.value }))}
                    className="mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5 bg-surface"
                  >
                    <option value="low">low — file ops only, no installs / sudo / push</option>
                    <option value="medium">medium — local dev (installs, git commit, build)</option>
                    <option value="high">high — full autonomy (git push, sudo). USE WITH CARE.</option>
                  </select>
                </label>
                <div className="lg:col-span-2 text-micro text-fg bg-surface border border-border rounded-sm p-2">
                  <b>CLI mode notes:</b> droid auth is <b>host-wide</b> — every project shares the same Factory login.
                  No Computer sandbox; the agent has direct shell access on the LAMA host (use <code>low</code> autonomy unless you trust the prompts).
                  Run <code>droid auth login</code> on the host (or inside the container) once.
                  The <i>Working directory</i> field below still scopes the workspace.
                </div>
              </div>
            )}
          </div>

          {form.mode === "api" ? (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3" data-testid="factory-orch-api-fields">
              <label className="block">
                <span className="text-micro uppercase font-bold text-fg-muted">App key</span>
                <input
                  data-testid="factory-orch-app-key"
                  value={form.app_key}
                  onChange={(e) => setForm((p) => ({ ...p, app_key: e.target.value }))}
                  placeholder={config?.has_app_key ? `Stored: ${config.app_key_masked}` : "Paste Factory app key"}
                  type="password"
                  className="mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5"
                />
              </label>
              <label className="block">
                <span className="text-micro uppercase font-bold text-fg-muted">Computer ID or Name</span>
                <input
                  data-testid="factory-orch-computer-id"
                  value={form.computer_id}
                  onChange={(e) => setForm((p) => ({ ...p, computer_id: e.target.value }))}
                  placeholder="comp_... or my-droid-computer"
                  className="mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5"
                />
              </label>
            </div>
          ) : null}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <label className="block">
              <span className="text-micro uppercase font-bold text-fg-muted">Working directory (optional)</span>
              <input
                data-testid="factory-orch-cwd"
                value={form.cwd}
                onChange={(e) => setForm((p) => ({ ...p, cwd: e.target.value }))}
                placeholder={form.mode === "cli"
                  ? "(leave blank → auto: /tmp/lama-factory-cli/<project_id>)"
                  : (form.host_anchored
                    ? "(disabled — host-anchored mode uses droid's HOME)"
                    : "(leave blank → auto: /root/lama_<tenant>_<project>)")}
                // iter-13.125 — In CLI mode the cwd is the local droid `--cwd`;
                // host_anchored doesn't apply, so the field stays editable.
                disabled={form.mode === "api" && form.host_anchored}
                className={`mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5 ${(form.mode === "api" && form.host_anchored) ? "bg-bg text-fg-subtle" : ""}`}
              />
              {/* iter-13.91.6 / iter-13.91.7 / iter-13.91.8 — visibility hint.
                  Factory's left rail enumerates folders ONE LEVEL DEEP under
                  the Droid's HOME root. So:
                    • Anything nested deeper (e.g. /root/lama-workspaces/x/y)
                      only surfaces the wrapper dir, not the project.
                    • /tmp/... is writable but lives outside HOME → invisible.
                    • /root/<single-folder-name> is the sweet spot.
                  Leaving the field blank now gives /root/lama_<tenant>_<project>
                  which appears DIRECTLY in the sidebar. */}
              <span className="block mt-1 text-micro text-fg-muted">
                <b>Avoid</b> <span className="font-mono">/workspace</span> (read-only on most Droid images).
                For the folder to appear in Factory's left sidebar, use a
                <b> single-level path under the Droid's HOME</b>
                (e.g. <span className="font-mono">/root/my-project</span>).
                Nested paths (<span className="font-mono">/root/foo/bar</span>) and
                non-HOME paths (<span className="font-mono">/tmp/...</span>) work but
                won't show up in the sidebar.
              </span>
            </label>
            {/* iter-13.91.12 — Host-anchored mode. When ON, LAMA skips the
                on-droid workspace bootstrap entirely (no mkdir, no cross-OS
                path gymnastics, no Windows / PowerShell / cmd compatibility
                concerns). The droid's HOME folder is used as cwd; all
                inputs (legacy code slices, KB context, prompts) are pushed
                inline in the message body and outputs are persisted on the
                LAMA host (MongoDB + filesystem). Recommended for reasoning-
                only agents and for ANY droid running Windows. Leave OFF
                only when the agent must execute build / test commands
                that need files on the droid filesystem.
                iter-13.125 — Hidden entirely in CLI mode: there's no
                Factory Computer to anchor against; the CLI always runs
                on the LAMA host. */}
            {form.mode === "api" && (
              <label className="flex items-start gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  data-testid="factory-orch-host-anchored"
                  checked={!!form.host_anchored}
                  onChange={(e) => setForm((p) => ({ ...p, host_anchored: e.target.checked }))}
                  className="mt-0.5 h-3.5 w-3.5"
                />
                <span className="block">
                  <span className="text-micro font-bold text-fg">Host-anchored workspace</span>
                  <span className="block text-micro text-fg-muted mt-0.5">
                    Keep the workspace on the <b>LAMA host</b> and push inputs
                    inline to the droid. No on-droid <span className="font-mono">mkdir</span>,
                    no cross-OS path issues. Works on <b>any droid OS</b>
                    (Linux / macOS / Windows). Disable only for agents that
                    must run <span className="font-mono">build/test</span> commands on the droid.
                  </span>
                </span>
              </label>
            )}
            {/* iter-13.91.15 — Auto-fallback toggle. When ON and Factory
                returns 424 / 5xx / network errors, the LLM call routes
                through OpenRouter (or whichever provider is set as default
                in the Console → Models tab) instead of failing the user's
                stage. Use this when a specific Factory droid is broken
                upstream (e.g. "Computer disconnected during request" that
                persists across retries) so your work isn't blocked while
                you wait for Factory support. Leaving it OFF is strict mode
                — Factory failures surface honestly. */}
            <label className="flex items-start gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                data-testid="factory-orch-allow-fallback"
                checked={!!form.allow_fallback}
                onChange={(e) => setForm((p) => ({ ...p, allow_fallback: e.target.checked }))}
                className="mt-0.5 h-3.5 w-3.5"
              />
              <span className="block">
                <span className="text-micro font-bold text-fg">Auto-fallback to Ollama (then OpenRouter) when Factory is unreachable</span>
                <span className="block text-micro text-fg-muted mt-0.5">
                  When Factory returns <span className="font-mono">424</span> / <span className="font-mono">5xx</span> /
                  network errors, route the LLM call through a configured
                  <b> Ollama provider first</b> (iter-13.115 — operator preference for local /
                  free fallback over surprise cloud bills), and only if no
                  Ollama provider is configured, through the default provider
                  in the <b>Models tab</b> / the legacy <span className="font-mono">OPENROUTER_API_KEY</span>
                  env-var. Useful when a specific droid is broken upstream
                  (e.g. <span className="font-mono">"Computer disconnected during request"</span>) so your work
                  isn't blocked. Leave OFF for strict Factory-only mode.
                </span>
              </span>
            </label>
            <label className="block">
              <span className="text-micro uppercase font-bold text-fg-muted">Default fallback model</span>
              <select
                data-testid="factory-orch-model"
                value={form.model}
                onChange={(e) => setForm((p) => ({ ...p, model: e.target.value }))}
                className="mt-0.5 w-full text-micro font-mono border border-border rounded-sm px-2 py-1.5 bg-surface"
              >
                {/* "auto" = let Factory route to the computer's default model.
                    Other entries are the model IDs Factory exposes on POST
                    /sessions; values must match exactly what your Factory
                    plan supports for this Droid Computer. Per-node picks
                    below override this; this dropdown is the fallback when
                    a node is left on "auto". */}
                {FACTORY_MODEL_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              <span className="block mt-1 text-micro text-fg-muted">
                Used when a pipeline node below is left on "auto". Recorded in token-usage
                logs. <b>Factory's actual model</b> comes from your Droid Computer's
                Default Model setting in Factory Settings.
              </span>
            </label>
          </div>

          {/* iter-13.81 + iter-13.82 — Per-pipeline-node model selection.
              Each stage gets two dropdowns:
                • First-time run   → maps to backend `{stage}.generate`
                • Regeneration     → maps to backend `{stage}.regenerate`
              IMPORTANT: Factory's POST /sessions does NOT accept a per-session
              `model` field (it returns HTTP 400 "unrecognized_keys: [model]").
              The actual model is whatever the Droid Computer is configured for
              in Factory → Settings → Droid Computers → <computer> → Default
              Model. These dropdowns serve as LAMA-side metadata: token-usage
              reports show which pipeline node ran which model, and the value
              is forwarded to Factory if it ever exposes per-session model
              override. To actually change the model right now, change the
              Droid Computer's Default Model in Factory and pick a matching
              entry below so usage logs stay accurate. */}
          <div className="bg-surface-2 border border-border rounded-sm p-3"
               data-testid="factory-orch-models-grid">
            <div className="flex items-baseline justify-between mb-2">
              <div>
                <div className="text-micro font-bold uppercase tracking-wider text-fg">
                  Per-pipeline-node model selection
                </div>
                <div className="text-micro text-fg-muted mt-0.5">
                  One model for the first run, another for regeneration. Leave on "auto" to
                  inherit the default fallback above.
                </div>
                <div className="text-micro text-warn mt-1 leading-snug">
                  <b>Note:</b> Factory selects the actual model based on your{" "}
                  <span className="font-mono">Droid Computer → Default Model</span>{" "}
                  setting (Factory → Settings → Droid Computers). These picks are recorded in LAMA's
                  token-usage report and forwarded as a hint to Factory; if Factory rejects
                  the hint, the Droid Computer's default model is used.
                </div>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-micro">
                <thead>
                  <tr className="text-micro uppercase font-bold text-fg-muted border-b border-border">
                    <th className="text-left py-1.5 pr-3 font-bold w-1/3">Pipeline node</th>
                    <th className="text-left py-1.5 px-2 font-bold">First-time run</th>
                    <th className="text-left py-1.5 pl-2 font-bold">Regeneration</th>
                  </tr>
                </thead>
                <tbody>
                  {pipelineNodes.map((node) => {
                    const genKey   = `${node.key}.generate`;
                    const regenKey = `${node.key}.regenerate`;
                    return (
                      <tr key={node.key} className="border-b border-surface-2 last:border-b-0">
                        <td className="py-1.5 pr-3 align-top">
                          <div className="font-semibold text-fg">{node.label}</div>
                          <div className="text-micro text-fg-muted">{node.hint}</div>
                        </td>
                        <td className="py-1.5 px-2 align-top">
                          <select
                            data-testid={`factory-orch-model-${node.key}-generate`}
                            value={form.models?.[genKey] || "auto"}
                            onChange={(e) =>
                              setForm((p) => ({
                                ...p,
                                models: { ...(p.models || {}), [genKey]: e.target.value },
                              }))
                            }
                            className="w-full text-micro font-mono border border-border rounded-sm px-2 py-1 bg-surface"
                          >
                            {FACTORY_MODEL_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>{o.label}</option>
                            ))}
                          </select>
                        </td>
                        <td className="py-1.5 pl-2 align-top">
                          <select
                            data-testid={`factory-orch-model-${node.key}-regenerate`}
                            value={form.models?.[regenKey] || "auto"}
                            onChange={(e) =>
                              setForm((p) => ({
                                ...p,
                                models: { ...(p.models || {}), [regenKey]: e.target.value },
                              }))
                            }
                            className="w-full text-micro font-mono border border-border rounded-sm px-2 py-1 bg-surface"
                          >
                            {FACTORY_MODEL_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>{o.label}</option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              data-testid="factory-orch-save"
              onClick={onSave}
              disabled={saving || loading || deleting}
              className="bg-brand text-fg hover:bg-brand-hover font-bold h-8"
            >
              {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} Save
            </Button>
            {/* iter-13.35 — Delete button. Only meaningful when something is
                actually stored (has app key or computer id). Typed-confirm
                modal below prevents fat-fingered wipes. */}
            <Button
              data-testid="factory-orch-delete"
              onClick={() => { setDeleteText(""); setShowDelete(true); }}
              disabled={saving || loading || deleting || !(config?.has_app_key || config?.computer_id || config?.enabled)}
              variant="outline"
              className="border-red-500 text-red-600 hover:bg-red-50 hover:text-red-700 font-bold h-8"
              title="Permanently delete Factory orchestrator config for this project"
            >
              {deleting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />} Delete
            </Button>
            {loading && <span className="text-micro text-fg-muted">Loading…</span>}
            {/* iter-13.127 — inline Test connection button. Works in both
                API and CLI modes; in CLI mode it validates the `droid`
                binary path from the form BEFORE Save so operators get
                instant feedback on typos / stale paths. */}
            <Button
              data-testid="factory-orch-test"
              onClick={onTestConnection}
              disabled={testBusy || saving || loading || deleting}
              variant="outline"
              className="h-8 text-micro ml-1"
              title={form.mode === "cli"
                ? "Probe the droid binary (runs `droid --version`) — fast, no network."
                : "Handshake with Factory HTTP API to verify app_key + Computer ID."}
            >
              {testBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />} Test connection
            </Button>
            {testState && (
              <span
                data-testid="factory-orch-test-status"
                className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${testState.ok ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}
                title={testState.ok
                  ? (form.mode === "cli"
                      ? `droid ${testState.version || ""} @ ${testState.binary || "PATH"}${testState.resolved_via ? ` (resolved via ${testState.resolved_via})` : ""}`
                      : `computer=${testState.computer_id || "(unknown)"} · workspace_ready=${testState.workspace_ready ? "yes" : "no"}`)
                  : (testState.error || testState.detail || testState.reason || "failed")}
              >
                {testState.ok
                  ? (form.mode === "cli"
                      ? `CLI ✓ ${testState.version || ""}`
                      : `API ✓`)
                  : `✗ ${(testState.error || testState.reason || "failed").toString().slice(0, 60)}`}
              </span>
            )}
            {config?.updated_at && (
              <span className="text-micro text-fg-muted">Last updated: {new Date(config.updated_at).toLocaleString()}</span>
            )}
          </div>

          {/* iter-13.91.4 — explicit Droid-wake + workspace-create row.
              Both actions are idempotent and bypass the per-process
              cache so the user always sees fresh Droid state. Disabled
              when Factory isn't configured (no app key / computer).
              iter-13.127 — Only shown in API mode; CLI mode has no
              remote Droid Computer to wake / no workspace to mkdir on
              a Factory sandbox (the CLI just runs on the LAMA host). */}
          {form.mode === "api" && (config?.has_app_key && config?.computer_id) && (
            <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-surface-2"
                 data-testid="factory-orch-actions-row">
              <Button
                data-testid="factory-orch-wake"
                onClick={onWakeDroid}
                disabled={wakeBusy || workspaceBusy || saving || loading || deleting}
                variant="outline"
                className="h-8 text-micro"
                title="Send a GET /computers/{id} to Factory so the Droid auto-resumes from sleep"
              >
                {wakeBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />} Wake droid
              </Button>
              <Button
                data-testid="factory-orch-create-workspace"
                onClick={onEnsureWorkspace}
                disabled={workspaceBusy || saving || loading || deleting}
                variant="outline"
                className="h-8 text-micro"
                title="Force a fresh mkdir -p on the Droid for this project's workspace"
              >
                {workspaceBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Terminal className="w-3 h-3" />} Create / refresh workspace
              </Button>
              {wakeState && (
                <span
                  data-testid="factory-orch-wake-status"
                  className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${wakeState.ok ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}
                >
                  {wakeState.ok ? `Awake · ${wakeState.computer_id || ""}` : `Wake failed${wakeState.reason ? ` · ${wakeState.reason}` : ""}`}
                </span>
              )}
              {workspaceState && (
                <span
                  data-testid="factory-orch-workspace-status"
                  className={`text-micro font-mono px-1.5 py-0.5 rounded-sm ${workspaceState.ok ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}
                  /* iter-13.91.14 — surface the full diagnostic (Factory's
                     reported status, session-probe HTTP code, raw error)
                     so the operator can copy/paste the tooltip into a
                     Factory support ticket without diving into dev tools. */
                  title={[
                    workspaceState.detail || workspaceState.error || workspaceState.cwd || "",
                    workspaceState.session_probe_http ? `\nsession_probe_http=${workspaceState.session_probe_http}` : "",
                    workspaceState.diagnostic && Object.keys(workspaceState.diagnostic).length
                      ? `\ndiagnostic=${JSON.stringify(workspaceState.diagnostic)}`
                      : "",
                  ].join("")}
                >
                  {workspaceState.ok
                    ? `WS ✓ ${workspaceState.cwd || "(default)"}`
                    : `WS ✗ ${workspaceState.reason || workspaceState.error || "failed"}`}
                </span>
              )}
              {/* iter-13.91.15 — Copy diagnostic button. Appears next to
                  the red WS pill so the operator can paste a complete
                  Factory support ticket block (computer_id, requestId,
                  Factory verbatim message, full diagnostic) without
                  hunting through dev tools. */}
              {workspaceState && !workspaceState.ok && (
                <button
                  data-testid="factory-orch-copy-diag"
                  onClick={async () => {
                    const block = [
                      `LAMA Workspace Bootstrap — Factory failure report`,
                      `=================================================`,
                      `timestamp_local : ${new Date().toISOString()}`,
                      `project_id      : ${projectId || "(unknown)"}`,
                      `computer_id     : ${workspaceState.computer_id || config?.computer_id || "(unknown)"}`,
                      `reason          : ${workspaceState.reason || "(none)"}`,
                      `session_probe   : http=${workspaceState.session_probe_http ?? "(n/a)"}`,
                      `factory_request : ${workspaceState.factory_request_id || "(n/a)"}`,
                      `factory_message : ${workspaceState.factory_detail || "(n/a)"}`,
                      `diagnostic      : ${JSON.stringify(workspaceState.diagnostic || {})}`,
                      `stale_cleaned   : ${workspaceState.stale_sessions_cleaned ?? 0}`,
                      ``,
                      `Full detail:`,
                      workspaceState.detail || workspaceState.error || "(none)",
                    ].join("\n");
                    try {
                      await navigator.clipboard.writeText(block);
                      toast.success("Diagnostic copied — paste into Factory support ticket");
                    } catch {
                       
                      window.prompt("Copy the block below into your Factory support ticket:", block);
                    }
                  }}
                  title="Copy a complete diagnostic block for Factory support"
                  className="text-micro font-mono px-1.5 py-0.5 rounded-sm border border-rose-300 text-rose-700 hover:bg-rose-50"
                >
                  Copy diag
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* iter-13.40 — Configured Droids grid. Single-droid storage today
          (one Factory orchestrator config per project), so this renders
          at most one row. Visual confirmation that Save persisted, plus
          quick Edit/Delete affordances per row. */}
      {(config?.has_app_key || config?.computer_id) && (
        <div className="bg-surface border border-border rounded-sm overflow-hidden" data-testid="factory-orch-droids-grid">
          <div className="bg-bg px-4 py-2 border-b border-border">
            <div className="text-micro uppercase font-bold tracking-wider text-fg-muted">Configured droids</div>
            <div className="text-[12px] font-display font-semibold text-fg">
              Droids registered for this project
            </div>
          </div>
          <table className="w-full text-[12px]">
            <thead className="bg-surface-2 text-micro uppercase text-fg-muted">
              <tr>
                <th className="text-left px-4 py-2 font-bold">Droid name / computer ID</th>
                <th className="text-left px-4 py-2 font-bold">App key</th>
                <th className="text-left px-4 py-2 font-bold">CWD</th>
                <th className="text-left px-4 py-2 font-bold">Status</th>
                <th className="text-right px-4 py-2 font-bold">Actions</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-t border-surface-2" data-testid="factory-orch-droid-row">
                <td className="px-4 py-2 font-mono text-fg">{config?.computer_id || "—"}</td>
                <td className="px-4 py-2 font-mono text-fg-muted">{config?.app_key_masked || "—"}</td>
                <td className="px-4 py-2 font-mono text-fg-muted truncate max-w-[200px]">{config?.cwd || "(default)"}</td>
                <td className="px-4 py-2">
                  <span
                    className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${config?.routing_active ? "bg-emerald-100 text-emerald-700" : "bg-surface-3 text-fg-muted"}`}
                    title={config?.routing_reason || ""}
                  >
                    {config?.routing_active ? "Active" : "Disabled"}
                  </span>
                  {!config?.routing_active && config?.routing_reason && (
                    <div className="text-micro text-amber-700 mt-1">{config.routing_reason}</div>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
                  <button
                    data-testid="factory-orch-droid-edit"
                    onClick={() => {
                      // Hydrate the form for editing. App key stays masked —
                      // user must re-paste only if they want to rotate it.
                      setForm({
                        enabled: !!config?.enabled,
                        app_key: "",
                        computer_id: config?.computer_id || "",
                        cwd: config?.cwd || "",
                      });
                      // Scroll-into-view nicety.
                      try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch { /* */ }
                      toast.info("Loaded droid into the form above. Edit, then click Save.");
                    }}
                    className="text-micro px-2 py-1 border border-border rounded-sm hover:bg-bg mr-1"
                    title="Load this droid into the form for editing"
                  >
                    Edit
                  </button>
                  <button
                    data-testid="factory-orch-droid-delete"
                    onClick={() => { setDeleteText(""); setShowDelete(true); }}
                    disabled={deleting}
                    className="text-micro px-2 py-1 border border-red-300 text-red-600 rounded-sm hover:bg-red-50"
                    title="Delete this droid config"
                  >
                    <Trash2 className="w-3 h-3 inline" /> Delete
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {/* iter-13.35 — typed-confirm delete modal */}
      {showDelete && (
        <div
          className="fixed inset-0 z-50 bg-ink/40 flex items-center justify-center"
          data-testid="factory-orch-delete-modal"
        >
          <div className="bg-surface border border-red-300 rounded-sm p-5 w-[440px] shadow-2xl">
            <div className="flex items-center gap-2 mb-2">
              <Trash2 className="w-4 h-4 text-red-600" />
              <div className="text-sm font-display font-bold text-fg">
                Delete Factory Orchestrator config?
              </div>
            </div>
            <div className="text-micro text-fg-muted mb-3 leading-snug">
              This permanently removes the app key, computer ID, working directory,
              cached session IDs and the enabled flag for project <b>{active?.name || projectId}</b>.
              All future LLM calls for this project will route through the standard
              fabric (OpenRouter / Anthropic / etc.) until you re-configure Factory.
              <br /><br />
              Type <span className="font-mono font-bold text-red-600">DELETE</span> below to confirm.
            </div>
            <input
              data-testid="factory-orch-delete-confirm-input"
              autoFocus
              value={deleteText}
              onChange={(e) => setDeleteText(e.target.value)}
              placeholder="DELETE"
              className="w-full text-[12px] font-mono border border-border rounded-sm px-2 py-1.5 mb-3 focus:border-red-500 focus:ring-1 focus:ring-red-500 outline-none"
              onKeyDown={(e) => { if (e.key === "Enter") onDelete(); if (e.key === "Escape") setShowDelete(false); }}
            />
            <div className="flex items-center justify-end gap-2">
              <Button
                onClick={() => { setShowDelete(false); setDeleteText(""); }}
                disabled={deleting}
                variant="outline"
                className="h-8 text-micro"
              >
                Cancel
              </Button>
              <Button
                data-testid="factory-orch-delete-confirm"
                onClick={onDelete}
                disabled={deleting || deleteText.trim().toUpperCase() !== "DELETE"}
                className="h-8 text-micro bg-red-600 hover:bg-red-700 text-white font-bold"
              >
                {deleting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />} Delete config
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================
// Tab: Models
// ============================================================
function ModelsTab({ readOnly = false, onGoFactory }) {
  const [providers, setProviders] = useState([]);
  // Auto-detect reads the key prefix (sk-ant-, sk-or-, gsk_, AIza, sk-).
  // Azure keys are opaque 32-char strings with no prefix, so they are
  // indistinguishable from a custom provider and MUST be chosen explicitly.
  //
  // The six useState hooks this replaces validated nothing until submit,
  // and reported failures as a toast that named one problem at a time.
  const form = useForm({
    resolver: zodResolver(providerSchema),
    mode: "onBlur",
    reValidateMode: "onChange",
    defaultValues: {
      provider_type: "",
      api_key: "",
      name: "",
      base_url: "",
      azure_deployment: "",
      azure_api_version: "",
    },
  });
  const { register, handleSubmit, watch, reset, formState } = form;
  const { errors, isSubmitting } = formState;
  const providerType = watch("provider_type");
  const isAzure = providerType === "azure";
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try { const r = await listProviders(); setProviders(r.providers || []); } catch { /* */ }
  };
  useEffect(() => { refresh(); }, []);

  // Validation now lives in providerSchema, so this only has to submit.
  const onSetup = handleSubmit(async (v) => {
    if (readOnly) return;
    setBusy(true);
    try {
      // Empty key + base URL → Ollama (local runtime, no key required).
      // The backend route requires provider_type='ollama' to accept an
      // empty api_key, so send it explicitly.
      const payload = {
        api_key: v.api_key.trim(),
        name: v.name.trim(),
        base_url: v.base_url.trim(),
      };
      if (v.provider_type) payload.provider_type = v.provider_type;
      else if (!v.api_key.trim()) payload.provider_type = "ollama";
      if (v.provider_type === "azure") {
        payload.azure_deployment = v.azure_deployment.trim();
        payload.azure_api_version = v.azure_api_version.trim();
      }
      const r = await setupProvider(payload);
      toast.success(`Provider configured: ${r.provider?.name}`, {
        description: "Use Test connection on the new row to verify the key works.",
      });
      reset();
      await refresh();
    } catch (e) {
      toast.error("Setup failed", {
        description: e?.response?.data?.detail || e.message,
      });
    } finally {
      setBusy(false);
    }
  });

  const onAddOllama = async () => {
    if (readOnly) return;
    // One-click Ollama setup against the default local endpoint. User can
    // edit base URL afterwards if their daemon listens elsewhere.
    // iter-13.112 — when LAMA's backend is running in Docker, the backend
    // auto-rewrites localhost → host.docker.internal so the call actually
    // reaches the Mac/Windows/Linux host's Ollama daemon. The toast below
    // surfaces whichever URL the backend actually persisted so the user
    // can see the rewrite at-a-glance.
    setBusy(true);
    try {
      const r = await setupProvider({
        api_key: "",
        provider_type: "ollama",
        name: "Ollama (local)",
        base_url: "http://localhost:11434/v1",
      });
      const finalUrl = r?.provider?.base_url || "(unknown URL)";
      toast.success(
        `Provider configured: ${r.provider?.name}\nbase URL → ${finalUrl}\n\n` +
        `Click "Test connection" on the new row to verify the daemon is reachable.`,
        { duration: 8000 }
      );
      await refresh();
    } catch (e) {
      toast.error(
        "Ollama setup failed: " + (e?.response?.data?.detail || e.message) +
        "\n\nIf LAMA's backend runs in Docker, the daemon must be reachable " +
        "via host.docker.internal:11434. On Linux you may need to start " +
        "Ollama with OLLAMA_HOST=0.0.0.0:11434."
      );
    }
    finally { setBusy(false); }
  };

  return (
    <div className="space-y-5" data-testid="tab-models">
      {/* iter-13.116 — Factory-active read-only banner. Models content is
          disabled (every input/button greyed) while Factory Orchestrator
          handles routing. Banner tells the user how to re-enable. */}
      {readOnly && (
        <div
          data-testid="models-readonly-banner"
          className="bg-amber-50 border border-amber-300 rounded-sm px-4 py-3 flex items-start gap-3"
        >
          <Sparkles className="w-4 h-4 text-amber-600 mt-0.5 shrink-0" />
          <div className="flex-1">
            <div className="text-[12px] font-display font-bold text-amber-900">
              Models tab is disabled — Factory Orchestrator is handling routing
            </div>
            <div className="text-micro text-amber-800 mt-0.5">
              All LLM calls are routed via Factory.ai. Turn off Factory
              Orchestrator (or unmap its token) on the Factory tab to re-enable
              editing here.
            </div>
          </div>
          {onGoFactory && (
            <button
              onClick={onGoFactory}
              className="text-micro font-bold px-2 py-1 border border-amber-400 rounded-sm hover:bg-amber-100 text-amber-900"
            >
              Go to Factory tab
            </button>
          )}
        </div>
      )}

      {/* Quick setup */}
      <div className={`bg-surface border border-border rounded-sm overflow-hidden ${readOnly ? "opacity-60 pointer-events-none" : ""}`} aria-disabled={readOnly}>
        <div className="bg-brand-tint border-l-4 border-brand px-4 py-3">
          <div className="text-micro uppercase font-bold tracking-wider text-fg-muted">Quick Setup</div>
          <div className="text-sm font-display font-bold text-fg">Paste one API key to configure routing automatically</div>
          <div className="text-micro text-fg-muted mt-0.5">Auto-detects provider from key prefix (sk-or- → OpenRouter, sk-ant- → Anthropic, sk- → OpenAI, gsk_ → Groq, AIza → Gemini). Leave the key blank and fill the base URL to register an Ollama (local) provider. <strong>Azure has no key prefix</strong> — pick it explicitly below.</div>
        </div>
        <form onSubmit={onSetup} noValidate className="p-4 flex flex-col gap-3">
          <FormErrorSummary errors={errors} />

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <FormField label="Provider" htmlFor="setup-provider-type">
              {(a11y) => (
                <SelectInput
                  {...a11y}
                  {...register("provider_type")}
                  data-testid="setup-provider-type"
                  disabled={readOnly}
                >
                  <option value="">Auto-detect from key</option>
                  <option value="azure">Azure OpenAI</option>
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="gemini">Google Gemini</option>
                  <option value="groq">Groq</option>
                  <option value="openrouter">OpenRouter</option>
                  <option value="ollama">Ollama (local)</option>
                  <option value="custom">Custom (OpenAI-compatible)</option>
                </SelectInput>
              )}
            </FormField>

            <FormField
              label="API key"
              htmlFor="setup-api-key"
              className="lg:col-span-2"
              error={errors.api_key?.message}
              hint={
                isAzure
                  ? "Azure keys have no prefix, which is why the provider must be picked explicitly."
                  : "Detected from the prefix: sk-or- · sk-ant- · sk- · gsk_ · AIza"
              }
            >
              {(a11y) => (
                <TextInput
                  {...a11y}
                  {...register("api_key")}
                  data-testid="setup-api-key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  invalid={!!errors.api_key}
                  placeholder={isAzure ? "Azure API key" : "sk-or-… / sk-ant-… / sk-… / gsk_… / AIza…"}
                  disabled={readOnly}
                  className="font-mono"
                />
              )}
            </FormField>

            <FormField label="Name" htmlFor="setup-name" hint="Optional — shown in the provider list.">
              {(a11y) => (
                <TextInput
                  {...a11y}
                  {...register("name")}
                  data-testid="setup-name"
                  placeholder="e.g. Anthropic (prod)"
                  disabled={readOnly}
                />
              )}
            </FormField>

            <FormField
              label={isAzure ? "Azure endpoint" : "Base URL"}
              htmlFor="setup-base-url"
              className="lg:col-span-2"
              error={errors.base_url?.message}
              required={isAzure}
              hint={
                isAzure
                  ? "Account root only — LAMA appends /openai/deployments/<deployment>."
                  : "Optional. e.g. http://localhost:11434/v1 for Ollama."
              }
            >
              {(a11y) => (
                <TextInput
                  {...a11y}
                  {...register("base_url")}
                  data-testid="setup-base-url"
                  inputMode="url"
                  spellCheck={false}
                  invalid={!!errors.base_url}
                  placeholder={
                    isAzure
                      ? "https://my-resource.openai.azure.com"
                      : "https://…"
                  }
                  disabled={readOnly}
                  className="font-mono"
                />
              )}
            </FormField>

            {/* Progressive disclosure: the Azure fields only exist once
                Azure is chosen, rather than sitting empty for everyone. */}
            {isAzure && (
              <>
                <FormField
                  label="Deployment name"
                  htmlFor="setup-azure-deployment"
                  className="lg:col-span-2"
                  required
                  error={errors.azure_deployment?.message}
                  hint="The name you chose in the Azure portal, e.g. gpt-5.1"
                >
                  {(a11y) => (
                    <TextInput
                      {...a11y}
                      {...register("azure_deployment")}
                      data-testid="setup-azure-deployment"
                      invalid={!!errors.azure_deployment}
                      placeholder="gpt-5.1"
                      disabled={readOnly}
                      className="font-mono"
                    />
                  )}
                </FormField>

                <FormField
                  label="API version"
                  htmlFor="setup-azure-api-version"
                  hint="e.g. 2023-07-01-preview"
                >
                  {(a11y) => (
                    <TextInput
                      {...a11y}
                      {...register("azure_api_version")}
                      data-testid="setup-azure-api-version"
                      placeholder="2023-07-01-preview"
                      disabled={readOnly}
                      className="font-mono"
                    />
                  )}
                </FormField>

                <p className="lg:col-span-3 text-micro text-fg-muted bg-surface-2 border-l-2 border-brand px-3 py-2">
                  The endpoint is the <strong>account root</strong>. LAMA appends{" "}
                  <code className="font-mono">/openai/deployments/&lt;deployment&gt;</code>{" "}
                  and sends the API version as a query parameter, so do not paste
                  a full chat-completions URL. An enterprise gateway URL that
                  already contains <code className="font-mono">/deployments/</code>{" "}
                  is left as-is.
                </p>
              </>
            )}
          </div>

          <div className="flex flex-col sm:flex-row gap-2">
            <Button
              type="submit"
              variant="brand"
              data-testid="setup-btn"
              loading={busy || isSubmitting}
              disabled={readOnly}
            >
              <Wand2 className="size-3.5" aria-hidden /> Auto-configure
            </Button>
            <Button
              type="button"
              variant="outline"
              data-testid="setup-ollama-btn"
              onClick={onAddOllama}
              loading={busy}
              disabled={readOnly}
              className="flex-1"
            >
              <Cpu className="size-3.5" aria-hidden /> Add Ollama (local) —
              http://localhost:11434/v1
            </Button>
          </div>
        </form>
      </div>

      {/* Provider cards */}
      {providers.length === 0 && (
        <div className="text-center py-12 text-fg-muted border border-dashed border-border rounded-sm">
          <KeyRound className="w-8 h-8 mx-auto mb-2 text-brand" />
          <div className="text-sm">No provider configured yet — paste a key above.</div>
          <div className="text-micro mt-1">Without a provider, LAMA falls back to the legacy <code>OPENROUTER_API_KEY</code> env var.</div>
        </div>
      )}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {providers.map((p) => <ProviderCard key={p.id} provider={p} refresh={refresh} readOnly={readOnly} />)}
      </div>
    </div>
  );
}

/**
 * Provider setup schema.
 *
 * This is the highest-stakes form in the product: an invalid key here is
 * accepted, the provider row is written with is_active=true, and the
 * failure only surfaces as a cascade of 401s three stages later during
 * generation. Validating shape at the field is what turns that into an
 * error the user can act on immediately.
 *
 * The cross-field rules mirror what the backend actually requires:
 *   • either a key or a base URL (a bare base URL registers Ollama)
 *   • Azure needs an account-root endpoint AND a deployment name, because
 *     it cannot be auto-detected from a key prefix
 */
const providerSchema = z
  .object({
    provider_type: z.string(),
    api_key: z.string(),
    name: z.string(),
    base_url: z.string(),
    azure_deployment: z.string(),
    azure_api_version: z.string(),
  })
  .superRefine((v, ctx) => {
    const key = v.api_key.trim();
    const url = v.base_url.trim();

    if (!key && !url) {
      ctx.addIssue({
        path: ["api_key"],
        code: z.ZodIssueCode.custom,
        message: "Paste an API key, or a base URL to register a local Ollama provider.",
      });
    }

    if (key && key.length < 16) {
      ctx.addIssue({
        path: ["api_key"],
        code: z.ZodIssueCode.custom,
        message: "That looks too short for an API key — check for a truncated paste.",
      });
    }

    if (url && !/^https?:\/\//i.test(url)) {
      ctx.addIssue({
        path: ["base_url"],
        code: z.ZodIssueCode.custom,
        message: "Include the scheme, e.g. https://my-resource.openai.azure.com",
      });
    }

    if (v.provider_type === "azure") {
      if (!url) {
        ctx.addIssue({
          path: ["base_url"],
          code: z.ZodIssueCode.custom,
          message: "Azure needs the account-root endpoint.",
        });
      } else if (/\/chat\/completions/i.test(url)) {
        ctx.addIssue({
          path: ["base_url"],
          code: z.ZodIssueCode.custom,
          message: "Use the account root — LAMA appends /openai/deployments/<deployment> itself.",
        });
      }
      if (!v.azure_deployment.trim()) {
        ctx.addIssue({
          path: ["azure_deployment"],
          code: z.ZodIssueCode.custom,
          message: "Azure needs the deployment name you chose in the portal.",
        });
      }
    }
  });

function ProviderCard({ provider, refresh, readOnly = false }) {
  const [editingKey, setEditingKey] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const onDelete = async () => {
    if (readOnly) return;
    if (!window.confirm(`Delete provider "${provider.name}"?`)) return;
    try { await deleteProvider(provider.id); toast.success("Deleted"); refresh(); }
    catch (e) { toast.error(e?.response?.data?.detail || "Delete failed"); }
  };
  const onTest = async () => {
    setTesting(true); setTestResult(null);
    try { const r = await testProvider(provider.id); setTestResult(r); }
    catch (e) { setTestResult({ ok: false, error: e.message }); }
    finally { setTesting(false); }
  };
  const onFetch = async () => {
    if (readOnly) return;
    try { const r = await fetchProviderModels(provider.id); toast.success(`Fetched ${r.count || 0} models`); refresh(); }
    catch (e) { toast.error("Fetch failed"); }
  };
  const onSaveKey = async () => {
    if (readOnly) return;
    try { await updateProviderKey(provider.id, newKey); toast.success("Key updated"); setEditingKey(false); setNewKey(""); refresh(); }
    catch (e) { toast.error("Update failed"); }
  };
  const onSetDefault = async () => {
    if (readOnly) return;
    try { await updateProvider(provider.id, { is_default: true }); toast.success("Set as default"); refresh(); }
    catch { toast.error("Failed"); }
  };
  const onRoutingChange = async (tier, modelId) => {
    if (readOnly) return;
    const routing = { ...(provider.routing || {}), [tier]: modelId };
    try { await updateProvider(provider.id, { routing }); refresh(); }
    catch { toast.error("Routing update failed"); }
  };
  // iter-14.34 — API-key on/off toggle. Persists `key_enabled` on the
  // provider row. When OFF, backend skips the Bearer header AND the
  // "key present + localhost → cloud" auto-flip in _resolve_ollama_endpoint,
  // so local Ollama models actually resolve locally instead of 404-ing on ollama.com.
  const onKeyToggle = async (v) => {
    if (readOnly) return;
    try { await updateProvider(provider.id, { key_enabled: v }); toast.success(v ? "API key ON" : "API key OFF — stays local"); refresh(); }
    catch { toast.error("Toggle failed"); }
  };
  const keyOn = provider.key_enabled !== false;
  const looksLocal = /host\.docker\.internal|localhost|127\.0\.0\.1|0\.0\.0\.0/i.test(provider.base_url || "");
  const routingMode = provider.provider_type === "ollama"
    ? (keyOn && !looksLocal ? "Cloud" : keyOn && looksLocal ? "Local + key" : "Local")
    : (keyOn ? "Authenticated" : "Key OFF");

  return (
    <div className="bg-surface border border-border rounded-sm p-3" data-testid={`provider-${provider.id}`}>
      {/* Header row — name + type/default/mode badges + delete */}
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <Cpu className="w-3 h-3 text-fg-muted" />
          <span className="font-display font-bold text-fg truncate">{provider.name}</span>
          <span className="text-micro uppercase font-bold bg-bg px-1 py-0.5 rounded-sm">{provider.provider_type}</span>
          {provider.is_default && <span className="text-micro uppercase font-bold bg-brand text-fg px-1 py-0.5 rounded-sm">Default</span>}
          <span
            data-testid={`provider-${provider.id}-mode`}
            className={`text-micro uppercase font-bold px-1 py-0.5 rounded-sm ${keyOn ? "bg-emerald-100 text-emerald-700" : "bg-bg text-fg-muted"}`}
            title="Effective routing mode based on endpoint + key toggle"
          >
            {routingMode}
          </span>
        </div>
        <button onClick={onDelete} disabled={readOnly} data-testid={`delete-provider-${provider.id}`} title={readOnly ? "Read-only while Factory Orchestrator is active" : "Delete"} className="p-1 text-rose-500 hover:bg-rose-50 rounded-sm disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent">
          <Trash2 className="w-3 h-3" />
        </button>
      </div>

      {/* Detail grid — mirrors the Factory Orchestrator panel layout */}
      <div className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1.5 text-micro border border-surface-2 rounded-sm p-2 bg-surface-2">
        <div className="text-fg-muted uppercase font-bold text-micro self-center">Endpoint</div>
        <div className="font-mono text-fg truncate" title={provider.base_url}>{provider.base_url || "—"}</div>

        <div className="text-fg-muted uppercase font-bold text-micro self-center">API Key</div>
        <div className="flex items-center gap-2 min-w-0">
          <span className={`font-mono truncate ${keyOn ? "text-fg" : "text-fg-subtle line-through"}`}>
            {provider.api_key || "—"}
          </span>
          <label
            className={`ml-auto flex items-center gap-1 select-none ${readOnly ? "cursor-not-allowed opacity-50" : "cursor-pointer"}`}
            title={readOnly ? "Read-only while Factory Orchestrator is active" : "Send Authorization: Bearer on outbound LLM calls"}
          >
            <span className="text-micro uppercase font-bold text-fg-muted">Use key</span>
            <input
              data-testid={`key-toggle-${provider.id}`}
              type="checkbox"
              checked={keyOn}
              disabled={readOnly}
              onChange={(e) => onKeyToggle(e.target.checked)}
              className="h-4 w-4"
            />
          </label>
        </div>

        <div className="text-fg-muted uppercase font-bold text-micro self-center">Models</div>
        <div className="text-fg">{(provider.models || []).length} in catalogue</div>

        <div className="text-fg-muted uppercase font-bold text-micro self-center">Status</div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${provider.is_active ? "bg-emerald-100 text-emerald-700" : "bg-bg text-fg-muted"}`}>
            {provider.is_active ? "Active" : "Inactive"}
          </span>
          {!provider.is_default && (
            <button
              onClick={onSetDefault}
              disabled={readOnly}
              className="text-micro uppercase font-bold text-fg hover:underline disabled:opacity-40 disabled:cursor-not-allowed disabled:no-underline"
            >
              Set as default
            </button>
          )}
        </div>
      </div>

      {/* Routing table - Complexity based */}
      <div className="mt-3 border-t border-border pt-2">
        <div className="text-micro uppercase font-bold text-fg-muted mb-1">Complexity Routing</div>
        {ROUTING_TIERS.map((tier) => (
          <div key={tier} className="flex items-center gap-2 mb-1">
            <span
              className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm w-20 text-center ${COMPLEXITY_COLOR[tier]}`}
              title={TIER_HINT[tier]}
            >{tier}</span>
            <select
              data-testid={`routing-${provider.id}-${tier}`}
              value={provider.routing?.[tier] || ""}
              onChange={(e) => onRoutingChange(tier, e.target.value)}
              disabled={readOnly}
              title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined}
              className="flex-1 text-micro border border-border rounded-sm px-1 py-0.5 disabled:bg-bg disabled:cursor-not-allowed"
            >
              <option value="">— select —</option>
              {(provider.models || []).map((m) => <option key={m.id} value={m.id}>{m.label || m.id}</option>)}
            </select>
          </div>
        ))}
      </div>

      {/* Per-pipeline-node routing — mirrors Factory Orchestrator's
          "Pipeline node × {First-time run, Regeneration}" grid so Ollama
          (and every other provider) gets the same first-run / regen split
          the operator already knows from the Factory tab.
          Persists to backend `stage_routing_generate` / `stage_routing_regenerate`
          (models.py:317-322 · routes/console.py:92-95). Leaving a cell on
          "— use complexity routing —" falls through to the complexity tier
          dropdowns above, which in turn fall through to the catalogue. */}
      <div className="mt-3 border-t border-border pt-2">
        <div className="flex items-baseline justify-between mb-1">
          <div className="text-micro uppercase font-bold text-fg-muted">
            Per-pipeline-node model selection <span className="text-fg-subtle normal-case">(overrides complexity)</span>
          </div>
        </div>
        <div className="text-micro text-fg-muted mb-2 leading-snug">
          One model for the first run, another for regeneration. Leave on
          "— use complexity routing —" to inherit the tier picks above.
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-micro">
            <thead>
              <tr className="text-micro uppercase font-bold text-fg-muted border-b border-border">
                <th className="text-left py-1 pr-2 font-bold w-1/3">Pipeline node</th>
                <th className="text-left py-1 px-2 font-bold">First-time run</th>
                <th className="text-left py-1 pl-2 font-bold">Regeneration</th>
              </tr>
            </thead>
            <tbody>
              {STAGES.map((stage) => {
                const genVal   = provider.stage_routing_generate?.[stage]   || "";
                const regenVal = provider.stage_routing_regenerate?.[stage] || "";
                const onChangeMode = (mode, value) => {
                  if (readOnly) return;
                  const field = mode === "generate" ? "stage_routing_generate" : "stage_routing_regenerate";
                  const patch = { [field]: { ...(provider[field] || {}), [stage]: value } };
                  updateProvider(provider.id, patch)
                    .then(refresh)
                    .catch(() => toast.error(`${mode === "generate" ? "First-time" : "Regeneration"} routing update failed`));
                };
                return (
                  <tr key={stage} className="border-b border-surface-2 last:border-b-0">
                    <td className="py-1 pr-2 align-top">
                      <span className="text-micro font-bold px-1.5 py-0.5 rounded-sm bg-violet-100 text-violet-700 inline-block">{stage}</span>
                    </td>
                    <td className="py-1 px-2 align-top">
                      <select
                        data-testid={`stage-routing-generate-${provider.id}-${stage}`}
                        value={genVal}
                        onChange={(e) => onChangeMode("generate", e.target.value)}
                        disabled={readOnly}
                        title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined}
                        className="w-full text-micro font-mono border border-border rounded-sm px-1 py-0.5 bg-surface disabled:bg-bg disabled:cursor-not-allowed"
                      >
                        <option value="">— use complexity routing —</option>
                        {(provider.models || []).map((m) => <option key={m.id} value={m.id}>{m.label || m.id}</option>)}
                      </select>
                    </td>
                    <td className="py-1 pl-2 align-top">
                      <select
                        data-testid={`stage-routing-regenerate-${provider.id}-${stage}`}
                        value={regenVal}
                        onChange={(e) => onChangeMode("regenerate", e.target.value)}
                        disabled={readOnly}
                        title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined}
                        className="w-full text-micro font-mono border border-border rounded-sm px-1 py-0.5 bg-surface disabled:bg-bg disabled:cursor-not-allowed"
                      >
                        <option value="">— use complexity routing —</option>
                        {(provider.models || []).map((m) => <option key={m.id} value={m.id}>{m.label || m.id}</option>)}
                      </select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Buttons */}
      <div className="mt-3 flex flex-wrap gap-1">
        <button onClick={onTest} disabled={testing} data-testid={`test-provider-${provider.id}`} className="text-micro px-2 py-1 border border-border rounded-sm hover:bg-bg flex items-center gap-1">
          {testing ? <Loader2 className="w-3 h-3 animate-spin" /> : <TestTube className="w-3 h-3" />} Test
        </button>
        <button onClick={onFetch} disabled={readOnly} title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined} className="text-micro px-2 py-1 border border-border rounded-sm hover:bg-bg flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent">
          <Plus className="w-3 h-3" /> Fetch models
        </button>
        {!provider.is_default && (
          <button onClick={onSetDefault} disabled={readOnly} title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined} className="text-micro px-2 py-1 border border-border rounded-sm hover:bg-bg disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent">Set as default</button>
        )}
        <button onClick={() => setEditingKey(!editingKey)} disabled={readOnly} title={readOnly ? "Read-only while Factory Orchestrator is active" : undefined} className="text-micro px-2 py-1 border border-border rounded-sm hover:bg-bg disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent">Edit key</button>
      </div>
      {editingKey && !readOnly && (
        <div className="mt-2 flex gap-1">
          <input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="New API key" className="flex-1 text-micro border border-border rounded-sm px-2 py-1 font-mono" />
          <button onClick={onSaveKey} className="text-micro px-2 py-1 bg-ink text-ink-fg rounded-sm">Save</button>
        </div>
      )}
      {testResult && (
        <div className={`mt-2 text-micro p-2 rounded-sm ${testResult.ok ? "bg-emerald-50 border border-emerald-200 text-emerald-800" : "bg-rose-50 border border-rose-200 text-rose-700"}`}>
          {testResult.ok
            ? `✓ ${testResult.model_used} · ${testResult.latency_ms}ms · "${testResult.response}"`
            : `✗ ${testResult.error}`}
        </div>
      )}
      {(provider.models || []).length > 0 && (
        <details className="mt-2">
          <summary className="text-micro uppercase font-bold text-fg-muted cursor-pointer">Catalogue ({provider.models.length})</summary>
          <div className="mt-1 space-y-0.5">
            {provider.models.map((m) => (
              <div key={m.id} className="text-micro text-fg flex justify-between font-mono">
                <span className="truncate">{m.id}</span>
                {m.cost_per_1k_input != null && <span className="text-fg-muted">in ${m.cost_per_1k_input}/1k · out ${m.cost_per_1k_output}/1k</span>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

// ============================================================
// Tab: Agents
// ============================================================
function AgentsTab() {
  const { active } = useProjects();
  const projectId = active?.id;
  const [data, setData] = useState({});
  const [openStages, setOpenStages] = useState(() => Object.fromEntries(STAGES.map((s) => [s, true])));
  const [openAgent, setOpenAgent] = useState(null);

  const refresh = async () => { try { const r = await listAgents(); setData(r || {}); } catch { /* */ } };
  useEffect(() => { refresh(); }, []);

  return (
    <div className="space-y-3" data-testid="tab-agents">
      <div className="text-micro text-fg-muted">
        Every LLM call in LAMA flows through one of these agents. Override model, disable, wrap, or replace per agent — changes apply to the very next run.
      </div>
      {STAGES.map((stage) => {
        const bucket = data[stage] || { orchestrator: [], tasks: [] };
        const isOpen = openStages[stage];
        const all = [...(bucket.orchestrator || []), ...(bucket.tasks || [])];
        if (all.length === 0) return null;
        return (
          <div key={stage} className="bg-surface border border-border rounded-sm" data-testid={`stage-block-${stage}`}>
            <button onClick={() => setOpenStages((p) => ({ ...p, [stage]: !p[stage] }))} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bg">
              {isOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRightIcon className="w-3 h-3" />}
              <span className="font-display font-bold text-[13px] text-fg">{stage}</span>
              <span className="text-micro text-fg-muted">({all.length} agents)</span>
            </button>
            {isOpen && (
              <div className="border-t border-border">
                {all.map((a) => (
                  <AgentRow
                    key={a.key}
                    agent={a}
                    expanded={openAgent === a.key}
                    onToggle={() => setOpenAgent(openAgent === a.key ? null : a.key)}
                    projectId={projectId}
                    onChange={refresh}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AgentRow({ agent, expanded, onToggle, projectId, onChange }) {
  const [draft, setDraft] = useState(agent);
  const [testResult, setTestResult] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(agent); }, [agent.key, agent.updated_at]); // eslint-disable-line

  const save = async (patch) => {
    try { await updateAgent(agent.key, patch); toast.success("Saved"); onChange(); }
    catch (e) { toast.error("Save failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onTest = async () => {
    setBusy(true); setTestResult(null);
    try {
      const r = await testAgent(agent.key, projectId);
      setTestResult(r);
    } catch (e) { setTestResult({ ok: false, error: e.message }); }
    finally { setBusy(false); }
  };

  const onResetBudget = async () => {
    try { await resetAgentBudget(agent.key); toast.success("Budget reset"); onChange(); } catch { toast.error("Reset failed"); }
  };

  return (
    <div className="border-b border-surface-2 last:border-b-0" data-testid={`agent-row-${agent.key}`}>
      <button onClick={onToggle} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-surface-2 text-left">
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRightIcon className="w-3 h-3" />}
        {agent.agent_type === "orchestrator" ? <Zap className="w-3 h-3 text-brand" /> : <Bot className="w-3 h-3 text-fg-muted" />}
        <div className="flex-1 min-w-0">
          <div className="text-[12px] font-semibold text-fg truncate">{agent.label} <span className="text-fg-muted font-mono font-normal">· {agent.key}</span></div>
          <div className="text-micro text-fg-muted truncate">{agent.description}</div>
        </div>
        <span className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${COMPLEXITY_COLOR[agent.complexity] || ""}`}>{agent.complexity}</span>
        <span className={`text-micro uppercase font-bold px-1.5 py-0.5 rounded-sm ${STATUS_COLOR[agent.status] || ""}`}>{agent.status}</span>
        <span className="text-micro text-fg-muted font-mono whitespace-nowrap hidden md:inline">
          ↑ {(agent.tokens_used_last_run || 0).toLocaleString()} · ${(agent.last_run_cost_usd || 0).toFixed(4)}
        </span>
      </button>
      {expanded && (
        <div className="px-4 pb-3 pt-1 bg-surface-2">
          <div className="text-micro text-fg-muted mb-2">Resolved: <span className="font-mono text-fg">{agent.resolved_model || "(no model)"}</span> via {agent.resolved_provider}</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <Field label="Complexity">
              <select data-testid={`agent-${agent.key}-complexity`} value={draft.complexity || "medium"} onChange={(e) => save({ complexity: e.target.value })} className="w-full text-micro border border-border rounded-sm px-1 py-1">
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </Field>
            <Field label="Model override">
              <input
                data-testid={`agent-${agent.key}-override`}
                value={draft.model_override || ""}
                onChange={(e) => setDraft({ ...draft, model_override: e.target.value })}
                onBlur={() => draft.model_override !== agent.model_override && save({ model_override: draft.model_override })}
                placeholder="(routing-based)"
                className="w-full text-micro font-mono border border-border rounded-sm px-1 py-1"
              />
            </Field>
            <Field label="Status">
              <select data-testid={`agent-${agent.key}-status`} value={draft.status} onChange={(e) => save({ status: e.target.value })} className="w-full text-micro border border-border rounded-sm px-1 py-1">
                <option value="enabled">Enabled</option>
                <option value="disabled">Disabled</option>
                <option value="wrapped">Wrapped</option>
                <option value="replaced">Replaced</option>
              </select>
            </Field>
            <Field label="Max tokens">
              <input
                type="number" min={128}
                value={draft.max_tokens}
                onChange={(e) => setDraft({ ...draft, max_tokens: parseInt(e.target.value) || 0 })}
                onBlur={() => draft.max_tokens !== agent.max_tokens && save({ max_tokens: draft.max_tokens })}
                className="w-full text-micro border border-border rounded-sm px-1 py-1"
              />
            </Field>
            <Field label="Temperature">
              <input
                type="number" step={0.05} min={0} max={2}
                value={draft.temperature}
                onChange={(e) => setDraft({ ...draft, temperature: parseFloat(e.target.value) || 0 })}
                onBlur={() => draft.temperature !== agent.temperature && save({ temperature: draft.temperature })}
                className="w-full text-micro border border-border rounded-sm px-1 py-1"
              />
            </Field>
            <Field label="Total budget (0 = unlimited)">
              <div className="flex gap-1">
                <input
                  type="number" min={0}
                  value={draft.token_budget_total}
                  onChange={(e) => setDraft({ ...draft, token_budget_total: parseInt(e.target.value) || 0 })}
                  onBlur={() => draft.token_budget_total !== agent.token_budget_total && save({ token_budget_total: draft.token_budget_total })}
                  className="flex-1 text-micro border border-border rounded-sm px-1 py-1"
                />
                <button onClick={onResetBudget} title="Reset usage counter" className="text-micro px-1 border border-border rounded-sm hover:bg-surface">
                  <RotateCcw className="w-3 h-3" />
                </button>
              </div>
            </Field>
          </div>

          {draft.status === "wrapped" && (
            <div className="mt-2 space-y-1">
              <Field label="Wrap prefix">
                <textarea rows={2} value={draft.wrap_prefix || ""} onChange={(e) => setDraft({ ...draft, wrap_prefix: e.target.value })} onBlur={() => save({ wrap_prefix: draft.wrap_prefix })} className="w-full text-micro font-mono border border-border rounded-sm px-1 py-1" />
              </Field>
              <Field label="Wrap suffix">
                <textarea rows={2} value={draft.wrap_suffix || ""} onChange={(e) => setDraft({ ...draft, wrap_suffix: e.target.value })} onBlur={() => save({ wrap_suffix: draft.wrap_suffix })} className="w-full text-micro font-mono border border-border rounded-sm px-1 py-1" />
              </Field>
            </div>
          )}
          {draft.status === "replaced" && (
            <div className="mt-2">
              <Field label="Replacement template">
                <textarea rows={4} value={draft.replaced_template || ""} onChange={(e) => setDraft({ ...draft, replaced_template: e.target.value })} onBlur={() => save({ replaced_template: draft.replaced_template })} className="w-full text-micro font-mono border border-border rounded-sm px-1 py-1" />
              </Field>
            </div>
          )}
          {draft.status === "disabled" && (
            <div className="mt-2 text-micro bg-amber-50 border border-amber-200 rounded-sm p-2 text-amber-800">
              <strong>Warning:</strong> This agent will be skipped. The pipeline step it performs will not execute.
            </div>
          )}

          <div className="mt-3 flex items-center gap-2">
            <Button onClick={onTest} disabled={busy} data-testid={`agent-${agent.key}-test`} className="h-7 text-micro bg-brand text-fg hover:bg-brand-hover">
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />} Test
            </Button>
            <span className="text-micro text-fg-muted">Tokens all-time: {(agent.tokens_used_all_time || 0).toLocaleString()}</span>
          </div>
          {testResult && (
            <div className={`mt-2 text-micro p-2 rounded-sm ${testResult.ok ? "bg-emerald-50 border border-emerald-200" : "bg-rose-50 border border-rose-200 text-rose-700"}`}>
              {testResult.ok ? (
                <>
                  <div><strong>{testResult.model_used}</strong> · ↑ {testResult.usage?.prompt_tokens} ↓ {testResult.usage?.completion_tokens} · ${testResult.cost_usd?.toFixed?.(4)}</div>
                  <pre className="mt-1 whitespace-pre-wrap text-fg">{testResult.content_preview}</pre>
                </>
              ) : `✗ ${testResult.error}`}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="text-micro uppercase font-bold text-fg-muted">{label}</span>
      <div className="mt-0.5">{children}</div>
    </label>
  );
}

// ============================================================
// Tab: Prompts (engineering — preview + test)
// ============================================================
function PromptsTab() {
  const { active } = useProjects();
  const projectId = active?.id;
  const [prompts, setPrompts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [template, setTemplate] = useState("");
  const [preview, setPreview] = useState(null);
  const [running, setRunning] = useState(false);
  const [testOut, setTestOut] = useState(null);

  const refresh = async () => { try { const r = await listPrompts(); setPrompts(r.prompts || r || []); } catch { /* */ } };
  useEffect(() => { refresh(); }, []);

  const onSelect = async (p) => {
    setSelected(p); setTemplate(p.template || ""); setPreview(null); setTestOut(null);
    if (projectId) {
      try { const pr = await previewPrompt(p.key, projectId); setPreview(pr); } catch { /* */ }
    }
  };

  const grouped = useMemo(() => {
    const out = {};
    for (const p of prompts) {
      const stage = (p.key || "").split(".")[0];
      const stageMap = { srs: "Discovery", datamodel: "DataModel", arch: "Architecture", codegen: "CodeGen" };
      const s = stageMap[stage] || "Other";
      out[s] = out[s] || [];
      out[s].push(p);
    }
    return out;
  }, [prompts]);

  const onSave = async () => {
    if (!selected || !projectId) return;
    try {
      await updateProjectPrompt(projectId, selected.key, { template });
      toast.success("Project override saved");
      refresh();
    } catch { toast.error("Save failed"); }
  };

  const onTest = async () => {
    if (!selected) return;
    setRunning(true); setTestOut(null);
    try { const r = await testPrompt(selected.key, projectId); setTestOut(r); }
    catch (e) { setTestOut({ ok: false, error: e.message }); }
    finally { setRunning(false); }
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 min-h-[600px]" data-testid="tab-prompts">
      {/* List */}
      <div className="lg:col-span-3 bg-surface border border-border rounded-sm overflow-hidden">
        <div className="px-3 py-2 border-b border-border text-micro uppercase font-bold text-fg-muted">Prompts</div>
        <div className="overflow-y-auto max-h-[600px]">
          {Object.entries(grouped).map(([stage, list]) => (
            <div key={stage}>
              <div className="text-micro uppercase text-fg-muted bg-bg px-3 py-1 font-bold">{stage}</div>
              {list.map((p) => (
                <button
                  key={p.key}
                  data-testid={`prompt-${p.key}`}
                  onClick={() => onSelect(p)}
                  className={`w-full text-left px-3 py-1.5 text-micro font-mono border-l-2 ${selected?.key === p.key ? "border-brand bg-brand-tint" : "border-transparent hover:bg-bg"}`}
                >
                  {p.key}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>

      {/* Editor */}
      <div className="lg:col-span-5 bg-surface border border-border rounded-sm flex flex-col">
        <div className="px-3 py-2 border-b border-border flex items-center gap-2">
          <FileText className="w-3 h-3 text-fg-muted" />
          <span className="text-[12px] font-mono">{selected?.key || "Select a prompt"}</span>
          {selected && (
            <span className="text-micro uppercase bg-bg px-1 py-0.5 rounded-sm ml-auto">v{selected.version || 1}</span>
          )}
        </div>
        <textarea
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          data-testid="prompt-editor"
          placeholder="Pick a prompt on the left to edit its project override…"
          className="flex-1 text-micro font-mono p-3 outline-none resize-none min-h-[400px]"
        />
        <div className="border-t border-border px-3 py-2 flex items-center gap-2">
          <span className="text-micro text-fg-muted">{template.length} chars · ~{Math.max(1, Math.floor(template.length / 4))} tokens</span>
          <div className="ml-auto flex gap-1">
            <Button data-testid="prompt-test" onClick={onTest} disabled={!selected || running} className="h-7 text-micro" variant="outline">
              {running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />} Test
            </Button>
            <Button data-testid="prompt-save" onClick={onSave} disabled={!selected || !projectId} className="h-7 text-micro bg-ink text-ink-fg">Save override</Button>
          </div>
        </div>
      </div>

      {/* Preview */}
      <div className="lg:col-span-4 bg-surface border border-border rounded-sm overflow-hidden">
        <div className="px-3 py-2 border-b border-border text-micro uppercase font-bold text-fg-muted">Live preview (current KB)</div>
        <div className="p-3 space-y-2 overflow-y-auto max-h-[600px]">
          {!preview && <div className="text-micro text-fg-muted">Select a prompt to resolve variables against the current project.</div>}
          {preview && (
            <>
              <div className="text-micro grid grid-cols-2 gap-1">
                <div><span className="text-fg-muted">Tokens:</span> <strong>{preview.total_token_estimate.toLocaleString()}</strong></div>
                <div><span className="text-fg-muted">Cost:</span> <strong>${preview.cost_estimate_usd.toFixed(6)}</strong></div>
                <div className="col-span-2"><span className="text-fg-muted">Model:</span> <strong className="font-mono">{preview.model_that_will_run || "(no provider)"}</strong></div>
              </div>
              <div className="border-t border-border pt-2 space-y-1">
                <div className="text-micro uppercase font-bold text-fg-muted">Variables</div>
                {preview.variables.map((v) => (
                  <div key={v.name} className="text-micro border border-surface-2 rounded-sm p-1.5">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-fg">{`{${v.name}}`}</span>
                      <span className="text-micro text-fg-muted">~{v.token_estimate} tok</span>
                    </div>
                    <div className="text-micro text-fg-muted mt-0.5 truncate">{v.resolved}</div>
                  </div>
                ))}
              </div>
              {testOut && (
                <div className={`mt-2 text-micro p-2 rounded-sm ${testOut.ok ? "bg-emerald-50 border border-emerald-200" : "bg-rose-50 border border-rose-200 text-rose-700"}`}>
                  {testOut.ok ? (
                    <>
                      <div><strong>{testOut.model_used}</strong> · ↑ {testOut.usage?.prompt_tokens} ↓ {testOut.usage?.completion_tokens} · ${testOut.cost_usd?.toFixed?.(4)} · {testOut.duration_ms}ms</div>
                      <pre className="mt-1 whitespace-pre-wrap text-fg max-h-40 overflow-y-auto">{testOut.content}</pre>
                    </>
                  ) : `✗ ${testOut.error}`}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================
// Main page
// ============================================================
export default function ConsolePage() {
  const [params] = useSearchParams();
  const [tab, setTab] = useState(params.get("tab") || "models");
  const { active } = useProjects();
  const projectId = active?.id;
  const [hasFactoryDetails, setHasFactoryDetails] = useState(false);
  // iter-13.116 — Ephemeral override: when the user ticks the Enable
  // Factory checkbox on the Factory tab WITHOUT saving, the polling
  // refetch would immediately overwrite our state with the persisted
  // (false) value. The override sticks until the user navigates to a
  // different tab and back, or saves/deletes (both cancel the override).
  const ephemeralEnabledRef = useRef(null);

  // iter-13.65 — Read-only gate for Models tab.
  // iter-13.116 — Gate widened to the OPERATOR INTENT (`config.enabled`)
  // rather than the full `routing_active` triple (enabled + app_key +
  // computer_id). User feedback: "I ticked Enable Factory Orchestrator
  // but Models tab is still editable" — the previous gate required the
  // token to ALSO be mapped, which meant the box state alone did nothing.
  // Now the moment Enable Factory Orchestrator is saved as ON, the
  // Models tab content is disabled. The Factory tab itself is where the
  // user finishes wiring the app key / computer_id; until then the
  // banner inside Models warns them clearly.
  useEffect(() => {
    if (!projectId) {
      setHasFactoryDetails(false);
      return;
    }
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const r = await getFactoryOrchestratorConfig(projectId);
        const c = r?.config || {};
        if (cancelled) return;
        // iter-13.116 — honour ephemeral override (user toggled
        // checkbox but hasn't saved). The override is cleared on
        // save/delete (those events have `ephemeral:false`).
        if (ephemeralEnabledRef.current !== null) {
          setHasFactoryDetails(!!ephemeralEnabledRef.current);
        } else {
          setHasFactoryDetails(!!c.enabled);
        }
      } catch {
        if (!cancelled) setHasFactoryDetails(false);
      }
    };
    fetchOnce();
    // iter-13.116 — Poll every 5s so the Models-tab read-only gate
    // stays in sync even when the user sits on the Models tab while
    // editing Factory config in another window / via API. Cheap (one
    // /factory-orchestrator/{pid} GET).
    const t = setInterval(() => { if (!document.hidden) fetchOnce(); }, 5000);
    // iter-13.116 — Instant flip on save/delete/checkbox-toggle from
    // the Factory tab in THIS window — same-window CustomEvent (no
    // need for storage event). When the event carries `ephemeral:true`
    // (user toggled the checkbox but hasn't saved yet) we honour the
    // payload directly AND remember it via the ref so subsequent
    // polling refetches don't overwrite it. Save/delete fire with
    // ephemeral:false (or omitted) which clears the override.
    const onFactoryChanged = (e) => {
      const d = (e && e.detail) || {};
      if (d.ephemeral) {
        ephemeralEnabledRef.current = !!d.enabled;
        setHasFactoryDetails(!!d.enabled);
      } else {
        ephemeralEnabledRef.current = null;
        fetchOnce();
      }
    };
    window.addEventListener("lama:factory-config-changed", onFactoryChanged);
    return () => {
      cancelled = true;
      clearInterval(t);
      window.removeEventListener("lama:factory-config-changed", onFactoryChanged);
    };
  }, [projectId, tab]);

  // iter-13.64 — One-shot auto-jump to Factory tab on FIRST load (only when
  // ?tab= wasn't pinned in the URL). Previously this fired on every render
  // and bounced the user away whenever they clicked Models manually. Now
  // the Models tab is always clickable; it just renders read-only (see
  // `readOnly` prop below) while Factory is active.
  const didAutoJump = useRef(false);
  useEffect(() => {
    if (didAutoJump.current) return;
    if (params.get("tab")) { didAutoJump.current = true; return; }
    if (hasFactoryDetails && tab === "models") {
      didAutoJump.current = true;
      setTab("factory");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasFactoryDetails]);

  // iter-13.116 — OPERATOR REQUEST: when Factory Orchestrator routing
  // is ON, the Models TAB stays CLICKABLE but its CONTENT is disabled
  // (read-only — every input/button greyed, banner explains why).
  // Turning Factory off re-enables the Models content automatically.
  const tabs = [
    { id: "factory", label: "Factory Orchestrator", icon: Sparkles },
    { id: "models",  label: "Models",  icon: Cpu },
    { id: "agents",  label: "Agents",  icon: Bot },
    { id: "prompts", label: "Prompts", icon: FileText },
  ];

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-bg" data-testid="console-page">
      <header className="bg-surface border-b-2 border-brand px-6 py-3">
        <div className="text-micro uppercase tracking-widest text-fg-muted">LAMA Console</div>
        <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center gap-2">
          <Terminal className="w-4 h-4 text-brand" /> Model Fabric · Agent Fabric · Prompt Engineering
        </h1>
        <div className="mt-3 flex gap-1">
          {tabs.map((t) => {
            const isActive = tab === t.id;
            // iter-13.116 — Models tab stays CLICKABLE while Factory
            // Orchestrator is active; the CONTENT inside renders read-only
            // (see ModelsTab's readOnly prop). A small "Disabled" pill on
            // the tab tells the user what to expect before they click.
            const isContentDisabled = t.id === "models" && hasFactoryDetails;
            return (
              <button
                key={t.id}
                data-testid={`console-tab-${t.id}`}
                onClick={() => setTab(t.id)}
                title={
                  isContentDisabled
                    ? "Factory Orchestrator is active — Models tab content is disabled (read-only). Turn off Factory (or unmap its token) on the Factory tab to re-enable editing."
                    : t.label
                }
                className={[
                  "flex items-center gap-1 text-[12px] px-3 py-1.5 border-b-2 transition-colors",
                  isActive ? "border-brand text-fg font-bold" : "border-transparent text-fg-muted hover:text-fg",
                ].join(" ")}
              >
                <t.icon className="w-3 h-3" /> {t.label}
                {isContentDisabled && (
                  <span className="ml-1 text-micro uppercase tracking-wider bg-surface-3 text-fg-muted border border-border-strong px-1 rounded-sm">
                    Disabled
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </header>
      <div className="flex-1 overflow-y-auto mos-scroll p-6">
        {tab === "factory" && <FactoryOrchestratorTab />}
        {tab === "models"  && <ModelsTab readOnly={hasFactoryDetails} onGoFactory={() => setTab("factory")} />}
        {tab === "agents"  && <AgentsTab />}
        {tab === "prompts" && <PromptsTab />}
      </div>
    </div>
  );
}
