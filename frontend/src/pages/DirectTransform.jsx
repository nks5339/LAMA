// Direct Transform — the fourth Tools section, after Console,
// Integrations and Prompt Library.
//
// iter-18's Direct Code Transformation Engine (DCTE). Folder-path driven
// and plugin-based: no project_id, no KB, no SRS, no stage_context. It
// sits beside the Transformer (multi-agent LLM) and the Gap Analyzer as a
// third standalone track, and shares no state with either.
//
// Three panes: configure services on the left, watch the job and its
// event stream in the middle, read reports and per-file transforms on
// the right. Everything talks to /api/dcte via the dcte* helpers in
// lib/api.js.
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { ArrowRightLeft, Trash2 } from "lucide-react";
import {
  dcteListPlugins,
  dcteListStacks,
  dcteDetectProject,
  dcteCreateJob,
  dcteListJobs,
  dcteGetJob,
  dcteStartJob,
  dctePauseJob,
  dcteResumeJob,
  dcteRollbackJob,
  dcteDeleteJob,
  dcteGetReports,
  dcteGetEvents,
  dcteGetTransforms,
  dcteBrowseFs,
} from "@/lib/api";
import { FACTORY_MODEL_OPTIONS } from "@/lib/factoryModels";
import HelpIcon from "@/components/HelpIcon";

// iter-18.2 — server-side folder picker. The browser can only see the
// backend filesystem (inside the container for Docker deploys), so we
// browse via /api/dcte/fs/browse and let the user click into subdirs.
function FolderPicker({ initialPath = "", onSelect, onClose }) {
  const [cwd, setCwd] = useState(initialPath);
  const [data, setData] = useState({ path: "", parent: null, entries: [] });
  const [err, setErr] = useState(null);
  const [manual, setManual] = useState("");

  // useCallback so the mount effect can depend on it honestly instead of
  // silencing exhaustive-deps: `load` closes over nothing but setState,
  // which React guarantees is stable, so the identity never changes and
  // the effect still runs exactly once per open.
  const load = useCallback(async (p) => {
    setErr(null);
    try {
      const d = await dcteBrowseFs(p || "");
      setData(d);
      setCwd(d.path);
      setManual(d.path);
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message || "Browse failed");
    }
  }, []);
  useEffect(() => { load(initialPath); }, [load, initialPath]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm" data-testid="dcte-picker">
      <div className="bg-surface w-[560px] max-h-[80vh] rounded-sm border border-border flex flex-col">
        <div className="px-3 py-2 border-b border-border flex items-center gap-2">
          <b className="text-xs">Select a folder (server-side)</b>
          <button onClick={onClose} className="ml-auto text-micro text-fg-muted" data-testid="dcte-picker-close">✕</button>
        </div>
        <div className="px-3 py-2 border-b border-border flex gap-1">
          <input value={manual} onChange={(e) => setManual(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") load(manual); }}
                 data-testid="dcte-picker-input"
                 className="flex-1 border border-border rounded-sm px-2 py-1 text-micro font-mono" />
          <button onClick={() => load(manual)} className="px-2 py-1 text-micro bg-fg text-white rounded-sm">Go</button>
        </div>
        {err && <div className="px-3 py-2 text-micro text-red-600">{err}</div>}
        {data.warning && <div className="px-3 py-2 text-micro text-amber-700 bg-amber-50">{data.warning}</div>}
        <div className="overflow-y-auto flex-1">
          {data.parent && (
            <button onClick={() => load(data.parent)}
                    className="w-full text-left px-3 py-1.5 text-micro hover:bg-bg">
              📁 ..
            </button>
          )}
          {data.entries.map((e) => (
            <button key={e.path} disabled={!e.is_dir}
                    onClick={() => e.is_dir && load(e.path)}
                    data-testid={`dcte-picker-entry-${e.name}`}
                    className={`w-full text-left px-3 py-1.5 text-micro hover:bg-bg ${e.is_dir ? "" : "text-fg-subtle"}`}>
              {e.is_dir ? "📁" : "📄"} {e.name}
            </button>
          ))}
          {data.entries.length === 0 && !err && (
            <div className="px-3 py-2 text-micro text-fg-muted">Empty folder.</div>
          )}
        </div>
        <div className="px-3 py-2 border-t border-border flex gap-2">
          <div className="text-micro text-fg-muted flex-1 truncate">Current: <code>{cwd}</code></div>
          <button onClick={() => { onSelect(cwd); onClose(); }}
                  data-testid="dcte-picker-select"
                  className="px-3 py-1 text-micro bg-brand text-fg font-semibold rounded-sm">
            Use this folder
          </button>
        </div>
      </div>
    </div>
  );
}

// iter-21 — One stack dropdown, grouped by family so a 14-entry list stays
// scannable. Backend / frontend / database come from the server catalogue
// (dcte/stacks.py), which is also what builds the migration brief — so a
// stack can never be offered here without the prompt knowing what it is.
const FAMILY_LABELS = {
  backend: "Backend",
  frontend: "Frontend",
  database: "Database",
  platform: "Platform",
};

function StackSelect({ value, options, onChange, testId, loading, error }) {
  const grouped = useMemo(() => {
    const byFamily = new Map();
    for (const o of options || []) {
      if (!byFamily.has(o.family)) byFamily.set(o.family, []);
      byFamily.get(o.family).push(o);
    }
    return [...byFamily.entries()];
  }, [options]);

  const known = (options || []).some((o) => o.id === value);

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      data-testid={testId}
      className="mt-0.5 w-full border border-border rounded-sm px-2 py-1 text-micro"
    >
      {(options || []).length === 0 && (
        <option value="">{error ? "Stacks unavailable — retry" : loading ? "Loading stacks…" : "No stacks"}</option>
      )}
      {/* A job saved with a stack that has since left the catalogue must
          still render its own value, or reopening it would silently
          re-point the job at whatever happened to be first in the list. */}
      {!known && value && <option value={value}>{value} (not in catalogue)</option>}
      {grouped.map(([family, items]) => (
        <optgroup key={family} label={FAMILY_LABELS[family] || family}>
          {items.map((o) => (
            <option key={o.id} value={o.id}>{o.label}</option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

const DEFAULT_SERVICE = () => ({
  name: "service-1",
  source_path: "",
  module_path: "",
  destination_path: "",
  source_stack: "helidon-mp",
  target_stack: "spring-boot-3",
});

export default function DirectTransformPage() {
  const [plugins, setPlugins] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [activeJobId, setActiveJobId] = useState(null);
  const [activeJob, setActiveJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [reports, setReports] = useState([]);
  const [transforms, setTransforms] = useState([]);

  const [name, setName] = useState("Direct Transform run 1");
  const [services, setServices] = useState([DEFAULT_SERVICE()]);
  const [aiRefactor, setAiRefactor] = useState(true);
  // iter-19 — Autonomous droid mode toggle. When ON, DCTE calls
  // `droid exec --auto medium` once per service (agentic) INSTEAD of
  // the per-file LLM sweep + Python build-fix loop, so droid can browse
  // siblings, run mvn, and iterate to a green build the way it does in
  // Factory AI directly. Persisted per-browser so operators don't have
  // to re-tick it on every DCTE run.
  const [useDroidAgent, setUseDroidAgent] = useState(
    () => (typeof window !== "undefined" && window.localStorage.getItem("lama:dcte:droidAgent") === "1") || false
  );
  useEffect(() => {
    try { window.localStorage.setItem("lama:dcte:droidAgent", useDroidAgent ? "1" : "0"); } catch (_) { /* no-op */ }
  }, [useDroidAgent]);
  const [cicd, setCicd] = useState({ github: true, azure: false, jenkins: false });
  // iter-18.13 — Factory/Droid model selector. Persisted so a re-open of
  // the DCTE page keeps the user's last choice; "auto" lets droid pick.
  const [model, setModel] = useState(
    () => (typeof window !== "undefined" && window.localStorage.getItem("lama:dcte:model")) || "auto",
  );
  useEffect(() => {
    try { window.localStorage.setItem("lama:dcte:model", model); } catch (_) { /* no-op */ }
  }, [model]);
  const [detection, setDetection] = useState({});
  const [busy, setBusy] = useState(false);
  const [picker, setPicker] = useState(null); // { serviceIdx, field }
  // Without the plugin list there is no stack pair to pick and the job
  // cannot be created, so a failed load has to be visible rather than
  // rendering an empty <select> the user can't act on.
  const [pluginsError, setPluginsError] = useState(false);
  // iter-21 — the stack catalogue behind the two dropdowns, plus the list of
  // pairs that have a deterministic transformer (everything else runs on the
  // AI pass, which the row under the selects states before the job starts).
  const [stacks, setStacks] = useState({ sources: [], targets: [] });
  const [stacksLoading, setStacksLoading] = useState(true);
  const [stacksError, setStacksError] = useState(false);

  useEffect(() => {
    dcteListPlugins()
      .then((d) => { setPlugins(d.plugins || []); setPluginsError(false); })
      .catch(() => {
        setPluginsError(true);
        toast.error("Could not load transformation plugins");
      });
    dcteListStacks()
      .then((d) => {
        setStacks({ sources: d.sources || [], targets: d.targets || [] });
        setStacksError(false);
      })
      .catch(() => {
        setStacksError(true);
        toast.error("Could not load the stack catalogue");
      })
      .finally(() => setStacksLoading(false));
    refreshJobs();
  }, []);

  // `plugins` is the list of pairs with a hand-written transformer. Anything
  // else runs through the generic AI plugin — stated under the selects before
  // the job starts, rather than inferred from the event log afterwards.
  const isDeterministic = useCallback(
    (src, tgt) => plugins.some((p) => p.source_stack === src && p.target_stack === tgt),
    [plugins],
  );

  useEffect(() => {
    if (!activeJobId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        // iter-18.18 — Poll the jobs list on every tick too. Previously
        // the left list only refreshed on mount/create/delete, so the
        // row status could freeze at "transforming 60%" while the
        // right panel (which polls the single job) advanced to
        // "completed 100%". That desync is the main "log vs screen
        // vs progress — nothing in sync" bug the user reported.
        const [j, e, r, t, listRes] = await Promise.all([
          dcteGetJob(activeJobId),
          dcteGetEvents(activeJobId, 200),
          dcteGetReports(activeJobId),
          dcteGetTransforms(activeJobId),
          dcteListJobs().catch(() => ({ jobs: null })),
        ]);
        if (cancelled) return;
        setActiveJob(j);
        setEvents(e.events || []);
        setReports(r.reports || []);
        setTransforms(t.transforms || []);
        if (listRes && Array.isArray(listRes.jobs)) {
          setJobs(listRes.jobs);
        }
      } catch (_) { /* transient */ }
    };
    tick();
    const iv = setInterval(tick, 1500);  // iter-18.10 — faster tick for live feel
    return () => { cancelled = true; clearInterval(iv); };
  }, [activeJobId]);

  const refreshJobs = () =>
    dcteListJobs()
      .then((d) => setJobs(d.jobs || []))
      .catch(() => toast.error("Could not load Direct Transform jobs"));

  const onDetect = async (idx) => {
    const svc = services[idx];
    if (!svc?.source_path) {
      toast.error("Enter a source_path first");
      return;
    }
    setBusy(true);
    try {
      const d = await dcteDetectProject(svc.source_path);
      setDetection((prev) => ({ ...prev, [idx]: d }));
      if (d.is_valid && d.detected_stack && d.suggested_target) {
        setServices((prev) => prev.map((s, i) => i === idx ? {
          ...s,
          source_stack: d.detected_stack,
          target_stack: d.suggested_target,
        } : s));
      }
      toast.success(`Detected: ${d.detected_stack} (${Math.round(d.confidence * 100)}%)`);
    } catch (e) {
      toast.error("Detection failed");
    } finally {
      setBusy(false);
    }
  };

  const providers = useMemo(
    () => Object.entries(cicd).filter(([, v]) => v).map(([k]) => k),
    [cicd],
  );

  const onCreateAndStart = async () => {
    if (services.some(s => !s.source_path || !s.destination_path)) {
      toast.error("Every service needs source_path and destination_path");
      return;
    }
    setBusy(true);
    try {
      // iter-18.2 — send derived roots explicitly so old backends that
      // still require them don't 422. New backends ignore/re-derive.
      const first = services[0];
      const job = await dcteCreateJob({
        name,
        source_root: first.source_path,
        output_root: first.destination_path,
        services,
        ai_refactor: aiRefactor,
        generate_cicd: providers,
        model: model || "auto",
        use_droid_agent: useDroidAgent,
      });
      await dcteStartJob(job.id);
      toast.success("Direct Transform job started");
      setActiveJobId(job.id);
      refreshJobs();
    } catch (e) {
      toast.error("Job creation failed");
    } finally {
      setBusy(false);
    }
  };

  const setService = (idx, patch) =>
    setServices((prev) => prev.map((s, i) => i === idx ? { ...s, ...patch } : s));

  const addService = () =>
    setServices((prev) => [...prev, { ...DEFAULT_SERVICE(), name: `service-${prev.length + 1}` }]);

  const removeService = (idx) =>
    setServices((prev) => prev.filter((_, i) => i !== idx));

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0" data-testid="dcte-page">
      {/* Same header shell as the three sibling Tools pages (Console,
          Integrations, Prompt Library): kicker / display h1 with a brand
          icon / HelpIcon, on a surface bar over the bg ground. */}
      <header className="bg-surface border-b border-border px-6 py-3">
        <div className="text-micro uppercase tracking-widest text-fg-subtle">Tools · Standalone</div>
        <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center gap-2"
            data-testid="dcte-title">
          <ArrowRightLeft className="w-4 h-4 text-brand" />
          Direct Transform
          <HelpIcon
            text="Point it at a folder on the server and it migrates the code in place — Helidon MicroProfile → Spring Boot 3, Oracle → PostgreSQL. Plugin-driven and deterministic first, with an optional AI pass on top. Needs no project, no KB and no frozen stage."
            testId="help-direct-transform"
          />
        </h1>
      </header>

      <div className="flex-1 grid grid-cols-12 min-h-0 overflow-hidden">
        {/* LEFT — configuration */}
        <section className="col-span-5 border-r border-border overflow-y-auto mos-scroll p-4 space-y-4"
                 data-testid="dcte-config">
          <fieldset className="bg-surface border border-border rounded-sm p-3 space-y-2">
            <legend className="text-micro font-semibold text-fg-muted uppercase">Job</legend>
            <label className="block text-micro">Name
              <input data-testid="dcte-input-name" value={name}
                     onChange={(e) => setName(e.target.value)}
                     className="mt-1 w-full border border-border rounded-sm px-2 py-1 text-xs" />
            </label>
            <div className="text-micro text-fg-muted">
              Paths are read from each service below. Reports + CI/CD land under
              service&nbsp;1's <code>destination_path</code>. Absolute paths (or&nbsp;<code>~/…</code>)
              recommended when running locally via Droid.
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <label className="text-micro flex items-center gap-1">
                <input type="checkbox" checked={aiRefactor}
                       data-testid="dcte-chk-ai"
                       onChange={(e) => setAiRefactor(e.target.checked)} />
                AI-assisted refactor pass
              </label>
              {/* iter-19 — Autonomous droid mode. Skips the per-file LLM
                  sweep + Python mvn loop; hands the whole service tree
                  to `droid exec --auto medium` and lets droid finish. */}
              <label className="text-micro flex items-center gap-1"
                     title="Hand each service to `droid exec --auto medium` for an autonomous migration + build-fix loop. Falls back to the AI-assisted pass if droid is unavailable or fails.">
                <input type="checkbox" checked={useDroidAgent}
                       data-testid="dcte-droid-agent-toggle"
                       onChange={(e) => setUseDroidAgent(e.target.checked)} />
                Autonomous droid mode <span className="text-micro uppercase tracking-wide text-amber-700 border border-amber-300 bg-amber-50 rounded-sm px-1 ml-0.5">beta</span>
              </label>
              {["github", "azure", "jenkins"].map((p) => (
                <label key={p} className="text-micro flex items-center gap-1">
                  <input type="checkbox" checked={cicd[p]}
                         data-testid={`dcte-chk-cicd-${p}`}
                         onChange={(e) => setCicd({ ...cicd, [p]: e.target.checked })} />
                  {p}
                </label>
              ))}
            </div>
            {/* iter-18.13 — Factory/Droid model selector. Values mirror
                the curated set in frontend/src/lib/factoryModels.js which
                is shared with the Console → Factory Orchestrator tab, so
                the two dropdowns can never drift. */}
            <label className="block text-micro">Model
              <select value={model}
                      onChange={(e) => setModel(e.target.value)}
                      data-testid="dcte-select-model"
                      disabled={!aiRefactor}
                      className="mt-1 w-full border border-border rounded-sm px-2 py-1 text-xs disabled:opacity-50">
                {FACTORY_MODEL_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
              <div className="mt-1 text-micro text-fg-muted">
                Used by the AI-assisted refactor pass. Forwarded to Factory Droid via <code>-m</code>.
                {!aiRefactor && <> Enable "AI-assisted refactor pass" to use.</>}
              </div>
            </label>
          </fieldset>

          <fieldset className="bg-surface border border-border rounded-sm p-3 space-y-3"
                    data-testid="dcte-services">
            <legend className="text-micro font-semibold text-fg-muted uppercase">
              Services ({services.length})
            </legend>
            {services.map((s, i) => (
              <div key={i} className="border border-border rounded-sm p-2 space-y-1"
                   data-testid={`dcte-service-${i}`}>
                <div className="flex items-center gap-2">
                  <input value={s.name} onChange={(e) => setService(i, { name: e.target.value })}
                         className="flex-1 border border-border rounded-sm px-2 py-1 text-xs font-semibold" />
                  {services.length > 1 && (
                    <button onClick={() => removeService(i)}
                            data-testid={`dcte-btn-remove-service-${i}`}
                            className="text-micro text-red-600 hover:underline">Remove</button>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-1">
                  <div className="flex gap-1 min-w-0">
                    <input value={s.source_path} placeholder="source_path (e.g. ~/projects/legacy)"
                           onChange={(e) => setService(i, { source_path: e.target.value })}
                           data-testid={`dcte-input-src-${i}`}
                           className="flex-1 min-w-0 border border-border rounded-sm px-2 py-1 text-micro" />
                    <button onClick={() => setPicker({ serviceIdx: i, field: "source_path" })}
                            data-testid={`dcte-btn-browse-src-${i}`}
                            title="Browse server folders"
                            className="flex-shrink-0 px-2 text-micro border border-border rounded-sm hover:bg-bg">
                      📁
                    </button>
                    {/* iter-18.14 — Detect button was getting cropped in the
                        50% grid column. Give it a solid yellow (matches the
                        primary Create & Start CTA), fixed padding, and
                        whitespace-nowrap so it never truncates. */}
                    <button disabled={busy || !s.source_path} onClick={() => onDetect(i)}
                            data-testid={`dcte-btn-detect-${i}`}
                            title="Auto-detect stack from source_path"
                            className="flex-shrink-0 whitespace-nowrap px-3 py-1 text-micro font-bold bg-brand text-fg border border-fg rounded-sm hover:bg-fg hover:text-brand disabled:opacity-40 disabled:cursor-not-allowed">
                      Detect
                    </button>
                  </div>
                  <input value={s.module_path} placeholder="module_path (optional)"
                         onChange={(e) => setService(i, { module_path: e.target.value })}
                         className="border border-border rounded-sm px-2 py-1 text-micro" />
                  <div className="flex gap-1">
                    <input value={s.destination_path} placeholder="destination_path (e.g. ~/projects/out)"
                           onChange={(e) => setService(i, { destination_path: e.target.value })}
                           data-testid={`dcte-input-dst-${i}`}
                           className="flex-1 border border-border rounded-sm px-2 py-1 text-micro" />
                    <button onClick={() => setPicker({ serviceIdx: i, field: "destination_path" })}
                            data-testid={`dcte-btn-browse-dst-${i}`}
                            title="Browse server folders"
                            className="px-2 text-micro border border-border rounded-sm hover:bg-bg">
                      📁
                    </button>
                  </div>
                  {/* iter-21 — Source and target are picked independently.
                      They used to be one <select> of the registered plugin
                      pairs, which capped the choice at the two pairs that
                      have a deterministic transformer. Any pair drawn from
                      these two lists runs: the deterministic plugin when one
                      claims it, the generic AI plugin otherwise. */}
                  <label className="text-micro text-fg-muted">
                    Source stack
                    <StackSelect
                      value={s.source_stack}
                      options={stacks.sources}
                      testId={`dcte-select-source-${i}`}
                      loading={stacksLoading}
                      error={stacksError}
                      onChange={(v) => setService(i, { source_stack: v })}
                    />
                  </label>
                  <label className="text-micro text-fg-muted">
                    Target stack
                    <StackSelect
                      value={s.target_stack}
                      options={stacks.targets}
                      testId={`dcte-select-target-${i}`}
                      loading={stacksLoading}
                      error={stacksError}
                      onChange={(v) => setService(i, { target_stack: v })}
                    />
                  </label>
                </div>
                <div className="text-micro text-fg-muted" data-testid={`dcte-pair-mode-${i}`}>
                  {pluginsError ? (
                    <>Could not load the transformer list, so this pair&rsquo;s mode is unknown.</>
                  ) : isDeterministic(s.source_stack, s.target_stack) ? (
                    <>Deterministic transformer available for this pair.</>
                  ) : (
                    <>
                      No deterministic transformer for this pair — it runs via the{" "}
                      <b>AI pass</b>, so keep &ldquo;AI-assisted refactor&rdquo; on.
                    </>
                  )}
                </div>
                {detection[i] && (
                  <div className="text-micro text-fg-muted" data-testid={`dcte-detection-${i}`}>
                    Detected <b>{detection[i].detected_stack}</b> ({Math.round(detection[i].confidence * 100)}%)
                    {detection[i].suggested_target && <> → <b>{detection[i].suggested_target}</b></>}
                    {!detection[i].is_valid && <span className="text-red-600"> · path invalid</span>}
                  </div>
                )}
              </div>
            ))}
            <button onClick={addService} data-testid="dcte-btn-add-service"
                    className="text-micro text-fg hover:underline">+ Add service</button>
          </fieldset>

          <button disabled={busy} onClick={onCreateAndStart}
                  data-testid="dcte-btn-start"
                  className="w-full py-2 bg-brand text-fg font-semibold rounded-sm disabled:opacity-50">
            Create &amp; Start Job
          </button>
        </section>

        {/* CENTER — job progress + events */}
        <section className="col-span-4 border-r border-border overflow-y-auto mos-scroll p-4"
                 data-testid="dcte-progress">
          <div className="mb-3">
            <div className="text-micro font-semibold text-fg-muted uppercase mb-1">Jobs</div>
            <div className="space-y-1">
              {jobs.length === 0 && <div className="text-micro text-fg-muted">No Direct Transform jobs yet.</div>}
              {jobs.map((j) => (
                <div key={j.id}
                     className={`flex items-stretch rounded-sm border ${activeJobId === j.id ? "bg-brand border-fg" : "bg-surface border-border hover:bg-bg"}`}>
                  <button
                          onClick={() => setActiveJobId(j.id)}
                          data-testid={`dcte-job-row-${j.id}`}
                          className="flex-1 text-left px-2 py-1 text-micro">
                    <div className="flex justify-between">
                      <span className="font-semibold truncate">
                        {j.name}
                        <span className="ml-1 text-micro font-mono text-fg-muted">
                          {String(j.id).slice(-8)}
                        </span>
                      </span>
                      <span data-testid={`dcte-job-status-${j.id}`}>{j.status}</span>
                    </div>
                    <div className="text-micro text-fg-muted">
                      {j.services.length} svc · progress {Math.round((j.progress || 0) * 100)}%
                    </div>
                  </button>
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      if (!window.confirm(`Delete job "${j.name}"? This removes job records and events. Output files on disk are not touched.`)) return;
                      try {
                        await dcteDeleteJob(j.id);
                        if (activeJobId === j.id) {
                          setActiveJobId(null);
                          setActiveJob(null);
                        }
                        toast.success("Job deleted");
                        refreshJobs();
                      } catch {
                        toast.error("Delete failed");
                      }
                    }}
                    data-testid={`dcte-job-delete-${j.id}`}
                    title="Delete job"
                    className="px-2 flex items-center justify-center text-fg-muted hover:text-red-600 hover:bg-red-50 border-l border-border">
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          </div>

          {activeJob && (
            <div className="bg-surface border border-border rounded-sm p-2 space-y-2">
              <div className="text-micro font-semibold flex items-center gap-2">
                <span>Active: {activeJob.name}</span>
                {/* iter-18.18 — short id disambiguator; multiple jobs can
                    share a name (e.g. re-runs) and the list looks
                    identical without this. */}
                <span className="text-micro text-fg-muted font-mono">
                  {String(activeJob.id).slice(-8)}
                </span>
              </div>
              {/* iter-18.9 — live narration banner. The backend narrator emits
                  phase="narration" events every ~6s while the job is running.
                  iter-18.10 — fall back to the latest engine event so the
                  user always sees SOMETHING moving, even if the narrator
                  hasn't spoken yet (or the low-tier model is slow).
                  iter-18.18 — Prefer the latest engine event if the last
                  narration is older than _NARRATION_STALE_MS. Previously
                  the code always preferred the last narration event —
                  when the low-tier narrator model stalled, the banner
                  would freeze at e.g. "batch 30 of 106" while the log
                  advanced to batch 100. */}
              {(() => {
                const _NARRATION_STALE_MS = 30_000;
                const isRunning = !["completed", "failed", "cancelled", "rolled_back"].includes(activeJob.status);
                const reversed = [...events].reverse();
                const lastNarration = reversed.find((e) => e.phase === "narration");
                const lastEngine = reversed.find((e) => e.phase !== "narration" && (e.message || "").trim());
                const lastAny = reversed.find((e) => (e.message || "").trim());
                const _ts = (x) => (x && x.at ? Date.parse(x.at) : 0);
                const narrationAge = lastNarration ? Date.now() - _ts(lastNarration) : Infinity;
                const engineAge = lastEngine ? Date.now() - _ts(lastEngine) : Infinity;
                // Prefer engine event when the narration is stale OR
                // when the engine event is significantly newer.
                let shown = lastNarration;
                if (!shown) shown = lastAny;
                else if (
                  narrationAge > _NARRATION_STALE_MS ||
                  (lastEngine && _ts(lastEngine) > _ts(lastNarration) + 5_000)
                ) {
                  shown = lastEngine || lastNarration;
                }
                const referenceAge = Math.min(narrationAge, engineAge);
                const staleForMs = Number.isFinite(referenceAge) ? referenceAge : 0;
                if (!shown && !isRunning) return null;
                const label = shown
                  ? (shown.phase === "narration" ? shown.message : `[${shown.phase}] ${shown.message}`)
                  : "Starting job…";
                // "Stuck" = running but no new event in > 60s.  Renders
                // an amber pill so the operator can see the pipeline
                // has gone quiet without staring at the raw log.
                const stuck = isRunning && staleForMs > 60_000;
                const ageLabel = staleForMs < 1500
                  ? "just now"
                  : staleForMs < 60_000
                    ? `${Math.round(staleForMs / 1000)}s ago`
                    : `${Math.round(staleForMs / 60_000)}m ago`;
                return (
                  <div className={`flex items-start gap-2 rounded-sm px-2 py-1.5 ${
                        stuck
                          ? "bg-amber-50 border border-amber-400"
                          : "bg-brand-tint border border-brand-edge"
                       }`}
                       data-testid="dcte-narration-banner">
                    <span className={`inline-block w-2 h-2 rounded-full mt-1 flex-shrink-0 ${
                      stuck
                        ? "bg-amber-600"
                        : isRunning ? "bg-fg animate-pulse" : "bg-fg-muted"
                    }`} />
                    <div className="flex-1 min-w-0">
                      <div className="text-micro leading-snug text-fg break-words">
                        {label}
                      </div>
                      <div className="text-micro text-fg-muted mt-0.5"
                           data-testid="dcte-narration-age">
                        last event {ageLabel}{stuck ? " · pipeline appears stuck" : ""}
                      </div>
                    </div>
                  </div>
                );
              })()}
              <div className="text-micro text-fg-muted">
                {/* iter-18.11 — user-friendly labels for each phase so
                    "building" doesn't look like a raw enum.
                    iter-18.18 — surface completed-with-warnings as an
                    explicit amber badge so the operator can tell a
                    green run from one that finished with residual
                    Helidon markers / needs_manual / unresolved
                    DevOps gaps / Tester FAIL. */}
                {(() => {
                  const label = {
                    created: "queued",
                    detecting: "detecting project type",
                    analyzing: "analyzing source",
                    transforming: "AI refactor pass",
                    building: "compile-and-fix agent",
                    devops: "DevOps agent (structural gaps)",
                    testing: "Tester agent (parity + boot smoke)",
                    validating: "validating artefacts",
                    reporting: "writing reports",
                    completed: "completed",
                    failed: "failed",
                    paused: "paused",
                    rolled_back: "rolled back",
                    cancelled: "cancelled",
                  }[activeJob.status] || activeJob.status;
                  // Detect completed-with-warnings.
                  const needsManualCt = transforms.filter(
                    (t) => t.status === "needs_manual" || t.status === "failed",
                  ).length;
                  const testerFailEvt = [...events].reverse().find(
                    (e) => e.phase === "test" &&
                      typeof e.message === "string" &&
                      /Tester verdict:\s*FAIL/.test(e.message),
                  );
                  const devopsUnresolved = [...events].reverse().find(
                    (e) => e.phase === "devops" &&
                      typeof e.message === "string" &&
                      /unresolved/i.test(e.message) &&
                      !/0 unresolved/.test(e.message),
                  );
                  const isTerminal = ["completed", "failed", "cancelled", "rolled_back"].includes(activeJob.status);
                  const hasWarnings = isTerminal && activeJob.status === "completed" &&
                    (needsManualCt > 0 || !!testerFailEvt || !!devopsUnresolved);
                  return (
                    <>
                      status: <b data-testid="dcte-active-status">{label}</b>
                      {hasWarnings && (
                        <span className="ml-1 inline-block px-1.5 py-0.5 text-micro rounded-sm bg-amber-100 border border-amber-400 text-amber-800 font-semibold"
                              data-testid="dcte-completed-with-warnings">
                          with warnings
                        </span>
                      )}
                      {activeJob.status === "failed" && (
                        <span className="ml-1 inline-block px-1.5 py-0.5 text-micro rounded-sm bg-red-100 border border-red-400 text-red-800 font-semibold">
                          error
                        </span>
                      )}
                      {" · "}progress {Math.round((activeJob.progress || 0) * 100)}%
                      {hasWarnings && (
                        <div className="text-micro text-amber-800 mt-1">
                          {needsManualCt > 0 && <>· {needsManualCt} file(s) need manual review </>}
                          {testerFailEvt && <>· Tester FAILED </>}
                          {devopsUnresolved && <>· DevOps has unresolved gaps </>}
                        </div>
                      )}
                    </>
                  );
                })()}
                {activeJob.error && <div className="text-red-600 mt-1">Error: {activeJob.error}</div>}
              </div>
              <div className="flex gap-1">
                <button onClick={() => dctePauseJob(activeJob.id)}
                        className="text-micro px-2 py-0.5 border border-border rounded-sm">Pause</button>
                <button onClick={() => dcteResumeJob(activeJob.id)}
                        className="text-micro px-2 py-0.5 border border-border rounded-sm">Resume</button>
                <button onClick={() => dcteRollbackJob(activeJob.id)}
                        data-testid="dcte-btn-rollback"
                        className="text-micro px-2 py-0.5 border border-red-300 text-red-600 rounded-sm">
                  Rollback
                </button>
              </div>
              <div className="border-t border-border pt-2 max-h-[50vh] overflow-y-auto mos-scroll"
                   data-testid="dcte-events"
                   ref={(el) => {
                     // Events arrive OLDEST-first: JobManager.events_for
                     // sorts `at` descending to take the newest `limit`,
                     // then reverses before returning. So the freshest
                     // line is at the BOTTOM and we pin the scroll there.
                     // (The comment here used to claim newest-first and
                     // scroll to 0, which parked the pane on the oldest
                     // event and made a running job look frozen. The
                     // narration banner right above already reads the
                     // array as oldest-first, so the two disagreed.)
                     // No smooth scroll — we re-render every 1.5s.
                     if (el) el.scrollTop = el.scrollHeight;
                   }}>
                {events.length === 0 && <div className="text-micro text-fg-muted">No events yet.</div>}
                {events.map((e) => (
                  <div key={e.id || e.at + e.message}
                       className={`text-micro leading-tight ${
                         e.phase === "narration"
                           ? "text-fg italic"
                           : e.level === "error" ? "text-red-600"
                           : e.level === "warn" ? "text-amber-700"
                           : "text-fg"
                       }`}>
                    <span className="text-fg-muted">
                      [{e.phase === "narration" ? "▸" : e.phase}]
                    </span> {e.message}
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* RIGHT — reports + traceability */}
        <section className="col-span-3 overflow-y-auto mos-scroll p-4" data-testid="dcte-reports">
          <div className="text-micro font-semibold text-fg-muted uppercase mb-1">Reports</div>
          {reports.length === 0 && <div className="text-micro text-fg-muted">No reports yet.</div>}
          {reports.map((r) => (
            <div key={r.id || r.path} className="text-micro mb-1"
                 data-testid={`dcte-report-${r.kind}`}>
              <b>{r.kind}</b>
              <div className="text-micro text-fg-muted break-all">{r.path}</div>
            </div>
          ))}
          <div className="text-micro font-semibold text-fg-muted uppercase mt-4 mb-1">
            Transforms ({transforms.length})
          </div>
          <div className="max-h-[40vh] overflow-y-auto space-y-0.5">
            {transforms.slice(0, 300).map((t) => (
              <div key={t.id || `${t.source_file}->${t.target_file}`}
                   className="text-micro break-all"
                   data-testid={`dcte-transform-${t.kind}`}>
                <span className={t.status === "success" ? "text-emerald-700" : t.status === "needs_manual" ? "text-amber-700" : "text-red-600"}>
                  {t.status}
                </span> · {t.kind} · <code>{t.source_file}</code> → <code>{t.target_file}</code>
              </div>
            ))}
          </div>
        </section>
      </div>
      {picker && (
        <FolderPicker
          initialPath={services[picker.serviceIdx]?.[picker.field] || ""}
          onSelect={(p) => setService(picker.serviceIdx, { [picker.field]: p })}
          onClose={() => setPicker(null)}
        />
      )}
    </div>
  );
}
