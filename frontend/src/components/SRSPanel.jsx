import { useEffect, useState, useCallback, useRef, lazy, Suspense } from "react";
import { FileDown, Lock, Unlock, RefreshCw, Sparkles, Loader2, ChevronRight, Pencil, Check, X, Code, Pause, Play, Square, MoreVertical, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getSRS, updateSRSSection, freezeSRS, unfreezeSRS, resetSRS, srsPdfUrl, API, getSRSGenerateStatus,
  pauseSRSGeneration, resumeSRSGeneration, cancelSRSGeneration, regenerateSRSSection,
  regenerateSRSSectionStream,
} from "@/lib/api";
import HelpIcon from "@/components/HelpIcon";
// d3 is ~75 KB gzipped and ERDiagram renders inside a single section that
// is usually collapsed. SRSPanel is reachable from the eager Discovery
// route, so a static import would keep d3 on the critical path for
// every user regardless of whether they ever open the ER view.
const ERDiagram = lazy(() => import("@/components/ERDiagram"));
import ConfidenceBadge from "@/components/ConfidenceBadge";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";   // iter-13.21 — refresh global project after freeze/unfreeze

// Mirrors backend SECTION_CONFIGS in routes/srs.py — IEEE 830 / IEEE 29148
// 11-section structure plus the deterministic ER model as section 12. Keep
// the order in sync with the backend; the PDF exporter derives its own
// section_order from the same list there.
const SECTIONS = [
  { key: "introduction", label: "1. Introduction" },
  { key: "overall_description", label: "2. Overall Description" },
  { key: "actors_use_case_inventory", label: "3. Actors and Use Case Inventory" },
  { key: "specific_requirements", label: "4. Specific Requirements" },
  { key: "detailed_use_cases", label: "5. Detailed Use Cases" },
  { key: "external_interfaces", label: "6. External Interfaces" },
  { key: "non_functional_requirements", label: "7. Non-Functional Requirements (NFRs)" },
  { key: "integration_requirements", label: "8. Integration Requirements" },
  { key: "validation_verification", label: "9. Validation and Verification" },
  { key: "traceability_matrix", label: "10. Traceability Matrix" },
  { key: "appendices", label: "11. Appendices" },
  { key: "entity_model", label: "12. Entity Relationship Model" },
];

// iter — Gap-marker highlighting.
// Any SRS content that carries `⚠ EVIDENCE GAP`, `NOT_EVIDENCED`, or the
// legacy `OUT_OF_SCOPE` sentinel is a KNOWN missing piece of evidence that
// the migration team must close. We give those blocks a distinct amber/red
// pastel background so reviewers see them at a glance rather than skimming
// past a plain blockquote.
const GAP_RE = /⚠\s*EVIDENCE\s*GAP|NOT[_\s-]?EVIDENCED|OUT[_\s-]?OF[_\s-]?SCOPE/i;
function _mdText(children) {
  if (children == null || children === false) return "";
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(_mdText).join("");
  if (children.props && children.props.children) return _mdText(children.props.children);
  return "";
}
const _isGap = (children) => GAP_RE.test(_mdText(children));

const MD_COMPONENTS = {
  table: (props) => (
    <div className="overflow-x-auto my-3">
      <table className="w-full text-xs border-collapse" {...props} />
    </div>
  ),
  thead: (props) => <thead className="bg-ink text-ink-fg" {...props} />,
  th: (props) => <th className="px-3 py-2 text-left font-semibold text-xs" {...props} />,
  td: ({ children, ...props }) => (
    <td
      className={
        _isGap(children)
          ? "px-3 py-2 border-b border-border text-xs align-top bg-crit-bg text-crit font-semibold"
          : "px-3 py-2 border-b border-border text-xs align-top"
      }
      {...props}
    >
      {children}
    </td>
  ),
  tr: ({ children, ...props }) => (
    <tr
      className={_isGap(children) ? "bg-crit-bg" : "even:bg-bg"}
      {...props}
    >
      {children}
    </tr>
  ),
  h1: (props) => <h1 className="text-base font-bold text-fg mt-5 mb-2" {...props} />,
  h2: (props) => <h2 className="text-sm font-bold text-fg mt-4 mb-2" {...props} />,
  h3: (props) => <h3 className="text-xs font-semibold text-fg mt-3 mb-1" {...props} />,
  h4: (props) => <h4 className="text-xs font-semibold text-fg mt-2 mb-1 uppercase tracking-wider" {...props} />,
  p:  ({ children, ...props }) => (
    <p
      className={
        _isGap(children)
          ? "text-[13px] leading-relaxed text-crit bg-crit-bg border-l-4 border-crit px-2 py-1 rounded-sm mb-3 font-medium"
          : "text-[13px] leading-relaxed text-fg mb-3"
      }
      {...props}
    >
      {children}
    </p>
  ),
  ul: (props) => <ul className="list-disc pl-5 my-2 text-[13px] leading-relaxed space-y-1" {...props} />,
  ol: (props) => <ol className="list-decimal pl-5 my-2 text-[13px] leading-relaxed space-y-1" {...props} />,
  li: ({ children, ...props }) => (
    <li
      className={
        _isGap(children)
          ? "text-crit bg-crit-bg px-2 py-0.5 rounded-sm font-medium"
          : "text-fg"
      }
      {...props}
    >
      {children}
    </li>
  ),
  hr: () => <hr className="my-3 border-t border-border" />,
  blockquote: ({ children, ...props }) => (
    <blockquote
      className={
        _isGap(children)
          ? "border-l-4 border-crit bg-crit-bg px-3 py-2 my-2 text-[13px] font-semibold text-crit rounded-sm"
          : "border-l-2 border-brand bg-brand-tint px-3 py-1 my-2 text-[13px] italic text-fg"
      }
      {...props}
    >
      {children}
    </blockquote>
  ),
  // Block code: only show as <pre> when it has a real language/multiline.
  // Inline code stays a small mono pill.
  pre: (props) => <pre className="bg-bg border border-border rounded-sm p-2 text-micro font-mono overflow-x-auto my-2" {...props} />,
  code: ({ inline, ...props }) => inline
    ? <code className="bg-bg px-1 rounded text-micro font-mono" {...props} />
    : <code className="text-micro font-mono" {...props} />,
  strong: (props) => <strong className="font-semibold text-fg" {...props} />,
  em: (props) => <em className="italic" {...props} />,
  a: (props) => <a className="text-ink-hover underline hover:no-underline" target="_blank" rel="noreferrer" {...props} />,
};

/**
 * Normalise LLM-generated section content before handing it to ReactMarkdown.
 * Some models wrap the entire response in ```markdown … ``` fences (or just
 * ``` … ```). When that happens ReactMarkdown treats the whole block as a
 * single <pre><code>, which looks identical to the in-line edit textarea —
 * the user's report: "format is not generating properly. it is showing as
 * like as edit mode."
 *
 * We also trim a leading "```\n" / trailing "```" with surrounding whitespace,
 * and strip a one-line "Section N:" / "## Section N:" preface the LLM
 * sometimes parrots from the prompt (the section heading already renders
 * above the body).
 */
function normaliseSectionContent(raw) {
  if (typeof raw !== "string") return "";
  let s = raw.replace(/\r\n/g, "\n").trim();
  // Strip a single outer fence: ```lang … ``` or ``` … ```
  const fence = /^```(?:[a-zA-Z0-9_-]+)?\s*\n([\s\S]*?)\n```\s*$/;
  const m = fence.exec(s);
  if (m) s = m[1].trim();
  // Strip a leading "## 4. Specific Requirements" / "Section 4: …" parrot line
  s = s.replace(/^#{1,6}\s+\d+\.\s+[^\n]+\n+/, "");
  s = s.replace(/^Section\s+\d+\s*[:\-—]\s*[^\n]+\n+/i, "");
  // iter-14.10 — Strip the machine-readable CONFIDENCE_SELF_SCORE footer
  // if the LLM emitted it malformed (single-line, missing whitespace,
  // missing closer). The properly-formed `<!-- ... -->` is invisible in
  // markdown, but the screenshot showed a squashed variant leaking as
  // visible body text. Nuke ANY comment or bare text containing the
  // literal `CONFIDENCE_SELF_SCORE` marker so it never renders.
  s = s.replace(/<!--[\s\S]*?CONFIDENCE_SELF_SCORE[\s\S]*?-->/gi, "");
  s = s.replace(/<!--[\s\S]*?CONFIDENCE_SELF_SCORE[\s\S]*$/gi, "");
  s = s.replace(/(^|\n)\s*<!--?\s*CONFIDENCE_SELF_SCORE[^\n]*$/gim, "");
  s = s.replace(/(^|\n)\s*CONFIDENCE_SELF_SCORE\s*:[^\n]*(?:\n\s*(?:COVERAGE|OPEN_GAPS|business_workflows|role_workflows|access_gates|business_rules|preconditions|postconditions)[^\n]*)*/gi, "");
  return s.trim();
}

export default function SRSPanel({ projectId, conversationId, kbReady, model, onFrozen, onCollapse }) {
  const [srs, setSrs] = useState(null);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState({ index: 0, total: 9, label: "" });
  const [doneSections, setDoneSections] = useState(new Set());
  const [editingSection, setEditingSection] = useState(null);
  const [editContent, setEditContent] = useState("");
  const [rawEditOpen, setRawEditOpen] = useState(false);
  const [rawBuf, setRawBuf] = useState("");
  const [savingRaw, setSavingRaw] = useState(false);
  // iter-13.71 — kebab (⋮) menu for secondary header actions so the
  // primary Generate / Confidence / Freeze controls have room to breathe.
  const [menuOpen, setMenuOpen] = useState(false);
  // iter-13.35 — per-section regen + live-job controls.
  const [regeneratingSection, setRegeneratingSection] = useState(""); // section key currently being regenerated
  // iter-13.35 — per-section regen outcome map.
  //   sectionState[key] = { status: "success" | "error", at: <epoch ms>, label?: string }
  // Used to tint each section with a light pastel so the user can see at a
  // glance which section just refreshed and how it went. Auto-clears after
  // 12s so the colour doesn't linger forever.
  const [sectionState, setSectionState] = useState({});
  // iter-14.11 — live per-section score-gated retry telemetry from SSE.
  //   sectionScore[key] = {
  //     attempts, maxAttempts, initialScore, finalScore,
  //     band, plateaued, minConfidence, at
  //   }
  // Merged with `srs?.sections_meta` on render so the tint survives page
  // refreshes (meta is persisted to Mongo).
  const [sectionScore, setSectionScore] = useState({});
  const [jobPaused, setJobPaused] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const { refresh: refreshProjects } = useProjects();   // iter-13.21

  const onPauseJob = async () => {
    if (!projectId || controlBusy) return;
    setControlBusy(true);
    try {
      await pauseSRSGeneration(projectId);
      setJobPaused(true);
      toast.info("Pause requested — will take effect after current section finishes.");
    } catch (e) {
      toast.error("Pause failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setControlBusy(false);
    }
  };

  const onResumeJob = async () => {
    if (!projectId || controlBusy) return;
    setControlBusy(true);
    try {
      await resumeSRSGeneration(projectId);
      setJobPaused(false);
      toast.success("Resuming…");
    } catch (e) {
      toast.error("Resume failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setControlBusy(false);
    }
  };

  const onCancelJob = async () => {
    if (!projectId || controlBusy) return;
    if (!window.confirm("Cancel SRS generation? Sections already written will be kept.")) return;
    setControlBusy(true);
    try {
      await cancelSRSGeneration(projectId);
      toast.warning("SRS generation cancelled. Sections already written were kept.");
    } catch (e) {
      toast.error("Cancel failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setControlBusy(false);
    }
  };

  const onRegenerateOne = async (sectionKey) => {
    if (!projectId || regeneratingSection) return;
    setRegeneratingSection(sectionKey);
    // Clear any prior outcome tint for this section as we start.
    setSectionState((s) => {
      const next = { ...s };
      delete next[sectionKey];
      return next;
    });
    try {
      // iter-13.37 — Use the SSE streaming endpoint. The plain POST blew
      // through the 60 s ingress timeout on long regenerations and the
      // user got `Request failed with status code 504` even though the
      // backend would have produced a perfectly good section. The stream
      // keeps the HTTP connection alive with a `ping` event every 4 s.
      let r;
      try {
        r = await regenerateSRSSectionStream(
          projectId,
          sectionKey,
          { model: model || "", conversationId: conversationId || "" },
          (evt) => {
            if (evt.type === "section_start") {
              toast.info(`Regenerating "${evt.label}"…`, { duration: 3500 });
            }
            // `ping` events are just keep-alive — no UI needed.
          },
        );
      } catch (streamErr) {
        // Network blip / proxy killed the SSE / backend doesn't have the
        // streaming route yet. Fall back to the legacy POST so the user
        // still gets a result (it might 504 again — but at that point
        // the backend has already persisted the section, so a refresh
        // will pick it up). Console-only — don't toast twice.
         
        console.warn("[SRS] stream regen failed, falling back to POST:", streamErr);
        r = await regenerateSRSSection(projectId, sectionKey, {
          model: model || "",
          conversationId: conversationId || "",
        });
      }
      setSrs((s) => ({
        ...(s || { project_id: projectId, version: 0, sections: {} }),
        version: r.version,
        sections: { ...((s && s.sections) || {}), [sectionKey]: r.content },
      }));
      const outcome = r.failed ? "error" : "success";
      setSectionState((s) => ({
        ...s,
        [sectionKey]: { status: outcome, at: Date.now(), label: r.label || sectionKey },
      }));
      // Auto-clear the tint after 12s so the panel doesn't stay coloured forever.
      window.setTimeout(() => {
        setSectionState((s) => {
          const cur = s[sectionKey];
          if (!cur || Date.now() - cur.at < 11000) return s;
          const next = { ...s };
          delete next[sectionKey];
          return next;
        });
      }, 12000);
      if (r.failed) {
        toast.warning(`Section "${r.label}" regenerated but the output still looks failed`, {
          description: "Check provider credits / Factory.ai status and try again.",
          duration: 8000,
        });
      } else {
        toast.success(`Section "${r.label}" regenerated`, {
          description: `${(r.tokens || 0).toLocaleString()} tokens · v${r.version}${r.model_used ? ` · ${r.model_used}` : ""}`,
        });
      }
      try { await refresh(); } catch (_) { /* ignore — local merge above is the floor */ }
    } catch (e) {
      setSectionState((s) => ({
        ...s,
        [sectionKey]: { status: "error", at: Date.now(), label: sectionKey },
      }));
      window.setTimeout(() => {
        setSectionState((s) => {
          const cur = s[sectionKey];
          if (!cur || Date.now() - cur.at < 11000) return s;
          const next = { ...s };
          delete next[sectionKey];
          return next;
        });
      }, 12000);
      toast.error("Section regenerate failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setRegeneratingSection("");
    }
  };

  const refresh = useCallback(async () => {
    if (!projectId) return;
    const data = await getSRS(projectId);
    setSrs(data);
  }, [projectId]);

  useEffect(() => { refresh(); }, [refresh]);

  // ── Auto-resume on mount (iter-13.15) ──────────────────────────────────
  // If there's a background SRS job running for this project (e.g. the user
  // accidentally reloaded the tab mid-generation, or my recovery polling
  // exited via deadline last time), we need to KNOW about it and re-attach
  // — otherwise the user sees only sections 1–2 (whatever was persisted
  // before they reloaded) and assumes generation died at section 2 again.
  // The status endpoint tells us if a job is live; if yes we kick off a
  // fresh SSE attach via handleGenerate which will re-attach (NOT restart,
  // because the backend reuses the in-flight _SRS_JOBS entry).
  // ────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!projectId || generating) return;
    let cancelled = false;
    (async () => {
      try {
        const st = await getSRSGenerateStatus(projectId);
        if (cancelled) return;
        if (st && st.running) {
          toast.info("Resuming in-progress SRS generation…", {
            description: `Server-side · section ${st.last_section_index || "?"}/${st.total_sections || 12}`,
            duration: 6000,
          });
          // Re-attach by re-opening the SSE stream — backend reattaches to
          // the existing _SRS_JOBS entry and replays history into our queue.
          handleGenerateRef.current?.();
        }
      } catch (_) { /* status endpoint unreachable — ignore */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);
  // We use a ref to break the circular dep between handleGenerate and this
  // effect (handleGenerate is declared below). The ref is assigned just below.
  const handleGenerateRef = useRef(null);
  // iter-14.6 — tracks sections currently generating in parallel (wave mode).
  // Populated on section_start, drained on section_complete / section_error /
  // run_aborted / complete. Used only by the progress-label formatter.
  const inFlightRef = useRef(null);

  const handleGenerate = async ({ resume = false } = {}) => {
    if (!projectId) return;
    // iter-13.16 — immediate visible confirmation that the click registered.
    // Without this, if the network request stalled silently the user thought
    // "nothing happened, regeneration is broken". Now they always see a
    // toast within ~50ms of clicking, regardless of what comes next.
     
    console.info("[SRS] Generate clicked", { projectId, conversationId, model, resume });
    toast.info(resume ? "Resuming SRS generation — keeping completed sections…" : "Starting SRS generation…", { duration: 2500 });
    setGenerating(true);
    setDoneSections(new Set());
    inFlightRef.current = new Map();
    setProgress({ index: 0, total: 12, label: "Starting…" });
    let completedCleanly = false;
    let streamStarted = false;   // becomes true once HTTP 2xx + body received

    // ── Recovery polling helper ─────────────────────────────────────────
    // Survives network drops by polling the backend job status until it
    // finishes, then refreshes the SRS document. Called both when the
    // stream ends cleanly without a `complete` event AND when reader.read()
    // throws a network/abort error mid-stream (the user previously saw a
    // red "network error" toast in that case — even though the backend
    // background job kept generating to completion).
    // ────────────────────────────────────────────────────────────────────
    const waitForBackgroundJob = async (reason) => {
      toast.info(
        reason === "network"
          ? "Network interrupted — generation still running on the server, waiting…"
          : "Stream ended early — waiting for backend to finish in background…",
        { duration: 6000 },
      );
      // Poll every 4s for up to 20 minutes. Backend job has its own
      // internal timeouts so we don't need to outlast it dramatically.
      const deadline = Date.now() + 20 * 60 * 1000;
       
      while (Date.now() < deadline) {
         
        await new Promise((r) => setTimeout(r, 4000));
        let status = null;
        try {
           
          status = await getSRSGenerateStatus(projectId);
        } catch (_) {
          // status endpoint itself unreachable — just keep polling, the
          // user's network might be flaky but the backend is still working
          continue;
        }
        if (status && status.running) {
          if (status.last_section_index) {
            setProgress({
              index: status.last_section_index,
              total: status.total_sections || 9,
              label: `Server-side · section ${status.last_section_index}/${status.total_sections || 9}…`,
            });
          }
          continue;
        }
        // Job no longer running → fetch the final document.
        try {
           
          const finalDoc = await getSRS(projectId);
          const sectionKeys = Object.keys(finalDoc?.sections || {});
          const nonEmpty = sectionKeys.filter((k) => (finalDoc.sections[k] || "").trim());
          setSrs(finalDoc);
          setDoneSections(new Set(nonEmpty));
          setProgress({ index: sectionKeys.length, total: sectionKeys.length, label: "Complete" });
          if (nonEmpty.length === sectionKeys.length) {
            toast.success(`SRS v${finalDoc?.version || ""} ready`, {
              description: `All ${sectionKeys.length} sections populated`,
            });
          } else {
            toast.warning(`SRS v${finalDoc?.version || ""} ready (partial)`, {
              description: `${nonEmpty.length}/${sectionKeys.length} sections populated — click Regenerate to retry the rest`,
              duration: 8000,
            });
          }
        } catch (_) { /* outer refresh() will pick up whatever's persisted */ }
        return;
      }
      // Deadline reached without the job finishing.
      toast.warning("Background job still running after 20 min", {
        description: "Refresh the page to check the latest state.",
      });
    };

    try {
      const res = await fetch(`${API}/srs/generate/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        // `model` is picked from the Discovery chat dropdown — same selection
        // drives both conversational chat and SRS generation. Falls back to
        // the backend's own default when the parent hasn't supplied one.
        //
        // iter-13.97 — REVERTED to PER-SECTION mode (user-visible UX fix).
        // iter-13.42 had defaulted to batched (ONE LLM call streams every
        // section delimited by `<<<SECTION:key>>>`) for token savings, but
        // when the active provider buffers (Anthropic native, Factory.ai)
        // the user sits at "0/12 sections" for 60-120s and then every
        // section pops in at once — exactly the "happening in one go"
        // complaint. Per-section mode runs 12 short LLM calls and emits
        // `section_start { index, total }` + `section_complete` events
        // per section, so the progress label flips through 0/12 → 1/12 →
        // 2/12 → … → 12/12 progressively, regardless of provider buffering
        // behaviour. The token-savings regression is acceptable; UX wins.
        body: JSON.stringify({
          project_id: projectId,
          conversation_id: conversationId,
          mode: "per-section",
          batch: false,
          resume,
          ...(model ? { model } : {}),
        }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.json().catch(() => ({ detail: res.statusText }));
        const msg = detail.detail || `HTTP ${res.status}`;
        // iter-13.16 — distinguish the recoverable "frozen" 400 from real
        // failures, with explicit action guidance. The user previously saw
        // "SRS generation failed: SRS is frozen" and didn't understand
        // they needed to click the Unlock button.
        if (res.status === 400 && /frozen/i.test(msg)) {
           
          console.warn("[SRS] backend rejected generate: SRS is frozen");
          toast.warning("SRS is frozen — click 'Unlock' first, then Regenerate", {
            duration: 8000,
          });
          return;
        }
         
        console.error("[SRS] generate POST failed", { status: res.status, body: detail });
        throw new Error(msg);
      }
      streamStarted = true;
       
      console.info("[SRS] stream opened, reading events");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      // Initialise sections object so progressive UI works
      setSrs((s) => ({ ...(s || { project_id: projectId, version: 0 }), sections: { ...(s?.sections || {}) }, frozen: false }));

      try {
         
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const events = buf.split("\n\n");
          buf = events.pop() || "";
          for (const evt of events) {
            const line = evt.split("\n").find((l) => l.startsWith("data:"));
            if (!line) continue;
            let data;
            try { data = JSON.parse(line.slice(5).trim()); } catch { continue; }
            if (data.type === "start") {
              // iter-13.34 — surface Graphify status so the user knows
              // whether SRS will be informed by the property graph or
              // running on TOON+RAG only. Three meaningful states:
              const g = data.graph_meta || {};
              if (g.enabled && g.built) {
                // iter-13.34 — be honest about LLM enrichment status.
                // The deterministic graph (OWL→nodes) always succeeds;
                // the Graphify LLM pass is the part that fails silently
                // on credit errors. Distinguish them visibly.
                if (g.enriched) {
                  toast.success(`Graphify KB ON — ${g.nodes_count} nodes / ${g.edges_count} edges · enriched (+${g.graphify_nodes_added} nodes via LLM)`, {
                    description: "Each section prompt will include a YAML subgraph extracted from this graph.",
                    duration: 6000,
                  });
                } else {
                  toast.warning(`Graph KB has ${g.nodes_count} deterministic nodes — but Graphify LLM enrichment SKIPPED`, {
                    description: "The graph the SRS sees is OWL-extraction only (no LLM-inferred edges / business entities). Same credit issue that blocks SRS — top up Anthropic / OpenRouter and re-run Build KB to get the enriched graph.",
                    duration: 10000,
                  });
                }
              } else if (g.enabled && !g.built) {
                toast.warning("Graphify KB ON but no graph built yet", {
                  description: "Run Build KB so SRS can consult the property graph. Falling back to TOON+RAG for this run.",
                  duration: 8000,
                });
              } else if (!g.enabled) {
                toast.info("Graphify KB OFF — running on TOON+RAG only", {
                  description: "Toggle Graph KB in the sidebar to enable graph-grounded SRS sections.",
                });
              }
            } else if (data.type === "section_start") {
              // iter-14.6 — wave-parallel: multiple section_start events may
              // arrive within the same wave. Track the set of in-flight
              // sections so the progress label shows "Writing 3 in parallel:
              // Introduction, Overall Description, System Features" instead
              // of flickering between labels last-write-wins style.
              inFlightRef.current = inFlightRef.current || new Map();
              inFlightRef.current.set(data.section, data.label || data.section);
              const _lbls = Array.from(inFlightRef.current.values()).slice(0, 3);
              const _n = inFlightRef.current.size;
              setProgress({
                index: data.index,
                total: data.total,
                label: _n > 1
                  ? `Writing ${_n} in parallel: ${_lbls.join(", ")}${_n > 3 ? "…" : ""}`
                  : data.label,
              });
            } else if (data.type === "section_progress") {
              // Heartbeat from backend while a heavy section is still being
              // generated — refresh the progress label so the user knows we
              // are alive (and so does any proxy on the path). No toast.
              const _map = inFlightRef.current;
              const _n = _map ? _map.size : 0;
              setProgress({
                index: data.index,
                total: data.total,
                label: _n > 1
                  ? `Writing ${_n} in parallel: ${Array.from(_map.values()).slice(0, 3).join(", ")}${_n > 3 ? "…" : ""}`
                  : `Writing ${data.label}…`,
              });
            } else if (data.type === "batch_start") {
              // iter-13.86 — surface batched-mode progress. In batch mode the
              // backend issues ONE LLM call that streams every section
              // inline (delimited by <<<SECTION:key>>>). Until the first
              // delimiter is detected, the user otherwise sees no movement
              // for up to several minutes. Show "Batched generation · 0/N"
              // immediately, then refresh on each `batch_progress` (4s) and
              // each individual `section_complete` (delimiter detected).
              setProgress({
                index: 0,
                total: data.total || 12,
                label: `Batched generation · 0/${data.total || 12} sections`,
              });
            } else if (data.type === "batch_progress") {
              setProgress({
                index: data.sections_done || 0,
                total: data.total || 12,
                label: `Batched generation · ${data.sections_done || 0}/${data.total || 12} sections · ${data.elapsed_sec || 0}s elapsed`,
              });
            } else if (data.type === "batch_complete") {
              const repair = (data.sections_repair || []).length;
              setProgress((p) => ({
                ...p,
                index: data.sections_done || p.index,
                label: repair
                  ? `Batch done · ${data.sections_done} sections · repairing ${repair}`
                  : `Batch done · ${data.sections_done} sections · validating`,
              }));
            } else if (data.type === "batch_repair_start") {
              setProgress((p) => ({
                ...p,
                label: `Repairing ${(data.subset_keys || []).length} section(s) in one batched call…`,
              }));
            } else if (data.type === "batch_error") {
              toast.warning("Batched generation hit an error — falling back to per-section repair", {
                description: data.error,
                duration: 8000,
              });
            } else if (data.type === "section_repair") {
              setProgress((p) => ({ ...p, label: `Repairing "${data.label}"…` }));
            } else if (data.type === "ping") {
              // Pure SSE-relay heartbeat. Ignore.
              continue;
            } else if (data.type === "paused") {
              setJobPaused(true);
              setProgress((p) => ({ ...p, label: `Paused at ${data.label}` }));
            } else if (data.type === "paused_heartbeat") {
              // Keep the SSE alive while the user holds the run paused. No UI.
              continue;
            } else if (data.type === "resumed") {
              setJobPaused(false);
              setProgress({ index: data.index, total: data.total, label: `Resuming ${data.label}…` });
            } else if (data.type === "section_complete") {
              if (inFlightRef.current) inFlightRef.current.delete(data.section);
              setSrs((s) => ({ ...(s || {}), sections: { ...(s?.sections || {}), [data.section]: data.content } }));
              setDoneSections((d) => { const n = new Set(d); n.add(data.section); return n; });
              // iter-14.11 — score/attempt telemetry from the retry loop.
              if (typeof data.final_score === "number") {
                setSectionScore((m) => ({
                  ...m,
                  [data.section]: {
                    attempts: data.attempts || 1,
                    maxAttempts: data.max_attempts || 1,
                    initialScore: data.initial_score ?? data.final_score,
                    finalScore: data.final_score,
                    band: data.band || "poor",
                    plateaued: !!data.plateaued,
                    minConfidence: data.min_confidence || 95,
                    at: Date.now(),
                  },
                }));
                // Also merge into srs.sections_meta so the tint survives
                // a refetch that lags the SSE stream.
                setSrs((s) => ({
                  ...(s || {}),
                  sections_meta: {
                    ...(s?.sections_meta || {}),
                    [data.section]: {
                      attempts: data.attempts || 1,
                      max_attempts: data.max_attempts || 1,
                      initial_score: data.initial_score ?? data.final_score,
                      final_score: data.final_score,
                      band: data.band || "poor",
                      plateaued: !!data.plateaued,
                      min_confidence: data.min_confidence || 95,
                    },
                  },
                }));
              }
              // iter-13.86 follow-up — when in batched mode, the periodic
              // `batch_progress` event only fires every ~4s. Bump the
              // "Batched generation · N/total" counter immediately whenever
              // a delimiter is detected so the user sees stage-wise
              // progression (1/11, 2/11, …) instead of being stuck on 0/11.
              setProgress((p) => {
                if (!p || !p.label || !p.label.startsWith("Batched generation")) return p;
                const total = p.total || 12;
                const nextIndex = Math.min(total, (p.index || 0) + 1);
                return {
                  ...p,
                  index: nextIndex,
                  label: `Batched generation · ${nextIndex}/${total} sections`,
                };
              });
              // iter-13.34 — surface silent failover to the user. When the
              // model the user asked for (e.g. Claude Sonnet) is not the
              // model that actually ran (e.g. qwen via OpenRouter), tell
              // them WHY this happened — otherwise they see Claude in the
              // dropdown but qwen in MiniConsole and assume the system is
              // broken. The mismatch is the failover walker rescuing them
              // from a 402 on the primary provider.
              const requested = (data.model_requested || "").trim();
              const used = (data.model_used || "").trim();
              if (requested && used && requested.toLowerCase() !== used.toLowerCase()) {
                toast.info(`Section "${data.label}" ran on ${used.split("/").pop()}`, {
                  description: `Failover from ${requested.split("/").pop()} — primary provider refused (likely out of credits). Top up the primary, or change the Console default to ${used.split("/").pop()}.`,
                  duration: 8000,
                });
              }
            } else if (data.type === "section_error") {
              if (inFlightRef.current) inFlightRef.current.delete(data.section);
              // Backend chose to continue past a failing section — surface but don't abort.
              toast.error(`Section "${data.label}" failed`, { description: data.error });
            } else if (data.type === "section_retry") {
              // iter-14.11 — surface auto-retry so the user sees WHY a
              // section is being regenerated in the same run. Kept as a
              // toast + progress-label bump (no state mutation — the
              // section_complete of the retry will land the final score).
              const _s = Number(data.score || 0).toFixed(1);
              const _min = Number(data.min_confidence || 95).toFixed(0);
              toast.info(
                `Retrying "${data.label}" (attempt ${data.next_attempt}/${data.max_attempts}) — score ${_s}% < ${_min}%`,
                {
                  description: (data.gaps || []).slice(0, 2).join(" · ") || "Feeding gaps back to the LLM.",
                  duration: 5000,
                },
              );
              setProgress((p) => ({
                ...p,
                label: `Retrying ${data.label} · attempt ${data.next_attempt}/${data.max_attempts} (score ${_s}%)`,
              }));
            } else if (data.type === "repair_start") {
              setProgress({
                index: data.count,
                total: data.count,
                label: `Repairing ${data.count} failed section(s)…`,
              });
              toast.info(`Retrying ${data.count} section(s)…`);
            } else if (data.type === "repair_complete") {
              if (data.still_failed && data.still_failed.length) {
                toast.warning(`${data.still_failed.length} section(s) still failed after repair`, {
                  description: data.still_failed.join(", "),
                });
              } else if (data.repaired_count) {
                toast.success(`Repaired ${data.repaired_count} section(s)`);
              }
            } else if (data.type === "run_aborted") {
              // iter-13.35 — backend gave up after first-section failure.
              // Show a clear red toast so the user knows nothing else was
              // generated and exactly what to fix.
              toast.error("SRS run aborted after first section failed", {
                description: data.message || `Section "${data.label || data.section}" failed — rest of the run was skipped.`,
                duration: 12000,
              });
              setProgress({ index: 1, total: 12, label: "Aborted · first section failed" });
            } else if (data.type === "complete") {
              completedCleanly = true;
              const okCount = data.sections_ok ?? data.total ?? 0;
              const totalCount = data.sections_total ?? data.total ?? 0;
              const aborted = !!data.aborted;
              setProgress({ index: totalCount || 9, total: totalCount || 9, label: aborted ? "Aborted" : (data.partial ? "Complete (partial)" : "Complete") });
              if (aborted) {
                // run_aborted toast already fired — keep this one quiet
                // so we don't double-notify.
              } else if (data.partial) {
                toast.warning(`SRS v${data.version} generated with ${okCount}/${totalCount} sections`, {
                  description: `Failed: ${(data.sections_failed || []).join(", ")} — click Regenerate to retry`,
                  duration: 8000,
                });
              } else {
                toast.success(`SRS v${data.version} generated`, { description: `${(data.total_tokens || 0).toLocaleString()} tokens used` });
              }
            } else if (data.type === "error") {
              // Backend-reported terminal error — surface and stop reading.
              toast.error("SRS generation failed", { description: data.message || "Backend reported error" });
              completedCleanly = true; // don't trigger recovery polling; backend already aborted
              break;
            }
          }
          if (completedCleanly) break;
        }
      } catch (streamErr) {
        // reader.read() threw — almost always a network blip / proxy idle
        // timeout / mobile-tab-throttle. The backend job is detached and
        // KEEPS GENERATING, so don't show a red error toast: switch into
        // polling-recovery mode and tell the user we're waiting.
         
        console.warn("[SRS] stream reader error — falling back to status polling:", streamErr);
      }

      if (!completedCleanly) {
         
        console.info("[SRS] stream ended without complete event — entering recovery polling");
        await waitForBackgroundJob("stream-end");
      }
      await refresh();
       
      console.info("[SRS] handleGenerate finished cleanly", { completedCleanly });
    } catch (e) {
      // Only true HTTP-level / setup failures land here. If the stream
      // had already started, treat it as a recoverable interruption.
      if (streamStarted) {
         
        console.warn("[SRS] stream setup-after error — recovering via polling:", e);
        await waitForBackgroundJob("network");
        try { await refresh(); } catch (_) { /* noop */ }
      } else {
         
        console.error("[SRS] fetch failed before stream started:", e);
        toast.error("SRS generation failed", { description: e.message });
      }
    } finally {
      setGenerating(false);
      setJobPaused(false);
    }
  };
  // Keep the ref pointing at the latest closure so the auto-resume effect
  // can invoke handleGenerate without a circular dep.
  handleGenerateRef.current = handleGenerate;

  const handleSection = async (key, content) => {
    if (!srs || srs.frozen) return;
    setSrs((s) => ({ ...s, sections: { ...s.sections, [key]: content } }));
    try {
      await updateSRSSection(projectId, key, content);
    } catch (e) {
      toast.error("Save failed", { description: e.message });
    }
  };

  const startEdit = (key) => {
    setEditingSection(key);
    setEditContent(srs?.sections?.[key] || "");
  };
  const cancelEdit = () => {
    setEditingSection(null);
    setEditContent("");
  };
  const saveEdit = async () => {
    if (!editingSection) return;
    await handleSection(editingSection, editContent);
    toast.success("Section saved");
    setEditingSection(null);
    setEditContent("");
  };

  const handleFreeze = async () => {
    setBusy(true);
    try {
      await freezeSRS(projectId, "current-user");
      toast.success("SRS frozen");
      await refresh();
      await refreshProjects();     // iter-13.21 — propagate stage_status flip so DataModel unlocks immediately
      if (onFrozen) onFrozen();
    } catch (e) {
      // iter-14.11 — surface SRS_CONFIDENCE_BELOW_THRESHOLD (422) with a
      // typed-confirmation OVERRIDE prompt. Mirrors the RESET / FREEZE
      // typed-confirmation pattern already in place elsewhere.
      const detail = e?.response?.data?.detail;
      if (
        e?.response?.status === 422
        && detail
        && detail.error === "SRS_CONFIDENCE_BELOW_THRESHOLD"
      ) {
        const _score = Number(detail.effective_score || 0).toFixed(1);
        const _thr = Number(detail.threshold || 95).toFixed(0);
        const _plateau = (detail.plateaued_sections || []).length;
        const msg =
          `Mean SRS confidence is ${_score}% — below the freeze threshold ${_thr}%.\n\n` +
          (_plateau
            ? `${_plateau} section(s) plateaued below the threshold. `
            : "") +
          "Type OVERRIDE (uppercase) to freeze anyway, or Cancel to fix the sections.";
        const typed = window.prompt(msg, "");
        if (typed !== "OVERRIDE") {
          toast.warning("Freeze cancelled — SRS confidence below threshold.", {
            description: "Regenerate the amber-tinted (Plateau) sections and try again.",
            duration: 7000,
          });
          return;
        }
        try {
          await freezeSRS(projectId, "current-user", "OVERRIDE");
          toast.warning("SRS frozen with OVERRIDE — audit-logged.", { duration: 8000 });
          await refresh();
          await refreshProjects();
          if (onFrozen) onFrozen();
        } catch (e2) {
          toast.error("Freeze failed even with OVERRIDE", { description: e2?.response?.data?.detail?.message || e2.message });
        }
        return;
      }
      toast.error("Freeze failed", { description: detail?.message || detail || e.message });
    } finally {
      setBusy(false);
    }
  };

  const handleUnfreeze = async () => {
    setBusy(true);
    try {
      await unfreezeSRS(projectId);
      toast.success("SRS unlocked");
      await refresh();
      await refreshProjects();     // iter-13.21
    } catch (e) {
      toast.error("Unfreeze failed", { description: e.message });
    } finally {
      setBusy(false);
    }
  };

  // iter-14.25.9 — Remove SRS. Wipes the SRS document + downstream stages
  // and returns Discovery to a clean slate (KB / chat / journeys / vectors
  // preserved). Typed confirmation guarded — same pattern as the Freeze
  // override / factory-reset dialogs.
  const handleResetSRS = async () => {
    if (!projectId) return;
    const typed = window.prompt(
      "This will DELETE the entire SRS document and every downstream artefact\n" +
      "(DataModel, Architecture, CodeGen, Living). KB, chat, analysis digest,\n" +
      "and journeys are preserved.\n\n" +
      'Type "RESET" (uppercase) to confirm:',
      ""
    );
    if (typed == null) return;
    if (String(typed).trim().toUpperCase() !== "RESET") {
      toast.error('Reset cancelled — you must type "RESET" exactly.');
      return;
    }
    setBusy(true);
    setMenuOpen(false);
    try {
      const res = await resetSRS(projectId, "RESET");
      toast.success("SRS removed — Discovery is a clean slate.", {
        description: `Deleted: ${Object.entries(res.deleted || {})
          .filter(([, v]) => typeof v === "number" && v > 0)
          .map(([k, v]) => `${k}(${v})`).join(", ") || "no rows"}`,
      });
      await refresh();
      await refreshProjects();
    } catch (e) {
      toast.error("Reset failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setBusy(false);
    }
  };

  const handleExport = () => {
    if (!projectId) return;
    window.open(srsPdfUrl(projectId), "_blank");
  };

  const frozen = !!srs?.frozen;
  const hasContent = srs?.sections && Object.values(srs.sections).some((v) => v && v.trim());
  // iter-14.5 — RESUME. Count how many sections are already populated (non-empty
  // and don't start with a "generation failed" marker). Enables the Resume
  // button whenever the SRS is partially populated (1..11 of 12 sections good).
  const _FAIL_MARKERS = ["> ⚠️", "⚠️", "Section generation failed"];
  const goodSectionCount = srs?.sections
    ? Object.values(srs.sections).filter((v) => {
        if (!v || !v.trim()) return false;
        const head = v.trim().slice(0, 40);
        return !_FAIL_MARKERS.some((m) => head.includes(m));
      }).length
    : 0;
  const totalSectionsExpected = 12;
  const hasPartial = goodSectionCount > 0 && goodSectionCount < totalSectionsExpected;

  return (
    <div className="h-full flex flex-col mos-panel min-h-0">
      {/* Header */}
      <div className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <h3 className="font-display text-micro font-bold tracking-tight uppercase text-fg whitespace-nowrap">IEEE 830 SRS</h3>
          <HelpIcon text="Live preview of the Software Requirements Specification. Edit any section inline. Freeze to lock and advance the pipeline." testId="help-srs" />
          {frozen && <span className="text-micro uppercase tracking-wider bg-brand text-fg px-1.5 py-0.5 rounded-sm font-bold whitespace-nowrap">Frozen v{srs?.version}</span>}
          {!frozen && hasContent && <span className="text-micro uppercase tracking-wider bg-bg border border-border text-fg px-1.5 py-0.5 rounded-sm whitespace-nowrap">Draft v{srs?.version}</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {/* iter-14.5 — RESUME button. Only visible when the SRS is partially
              populated. Preserves already-good sections and only regenerates
              missing/failed ones. Saves 60–90% of tokens + time on rescue runs. */}
          {!frozen && hasPartial && !generating && (
            <Button
              data-testid="resume-srs-btn"
              variant="secondary"
              size="sm"
              onClick={() => handleGenerate({ resume: true })}
              disabled={!kbReady}
              className="bg-brand hover:bg-brand-hover text-fg border border-fg/10 rounded-sm text-xs h-7 font-bold"
              title={`Resume — keep the ${goodSectionCount} good sections, regenerate the missing ${totalSectionsExpected - goodSectionCount}`}
            >
              <RefreshCw className="w-3 h-3 mr-1" />
              Resume ({goodSectionCount}/{totalSectionsExpected})
            </Button>
          )}
          {/* PRIMARY actions — always visible */}
          <Button
            data-testid="generate-srs-btn"
            variant="secondary"
            size="sm"
            onClick={() => handleGenerate()}
            disabled={generating || frozen || !kbReady}
            className="bg-surface border border-border hover:bg-bg text-fg rounded-sm text-xs h-7"
            title={hasContent ? "Regenerate all 12 sections from scratch (overwrites completed sections)" : "Generate the full SRS"}
          >
            {generating ? <RefreshCw className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
            {hasContent ? "Regenerate all" : "Generate"}
          </Button>

          {/* iter-13.71 — Confidence pill (self-labelled "CONFIDENCE NN.N%") */}
          <ConfidenceBadge projectId={projectId} stage="Discovery" compact />

          {frozen ? (
            <Button
              data-testid="unfreeze-btn"
              size="sm"
              onClick={handleUnfreeze}
              disabled={busy}
              className="bg-surface border border-border text-fg hover:bg-bg rounded-sm text-xs h-7"
            >
              <Unlock className="w-3 h-3 mr-1" /> Unfreeze
            </Button>
          ) : (
            <Button
              data-testid="freeze-btn"
              size="sm"
              onClick={handleFreeze}
              disabled={busy || !hasContent}
              className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-xs h-7"
            >
              <Lock className="w-3 h-3 mr-1" /> Freeze SRS
            </Button>
          )}

          {/* SECONDARY actions — kebab (⋮) menu */}
          <div className="relative">
            <button
              type="button"
              data-testid="srs-more-menu"
              onClick={() => setMenuOpen((o) => !o)}
              title="More options"
              aria-label="More options"
              className="p-1 rounded-sm border border-border hover:bg-bg text-fg h-7 w-7 flex items-center justify-center"
            >
              <MoreVertical className="w-3.5 h-3.5" />
            </button>
            {menuOpen && (
              <>
                <button
                  type="button"
                  aria-label="Close menu"
                  onClick={() => setMenuOpen(false)}
                  className="fixed inset-0 z-40 cursor-default"
                />
                <div
                  data-testid="srs-more-menu-panel"
                  className="absolute right-0 top-full mt-1 z-50 w-56 rounded-sm border border-border bg-surface shadow-lg py-1 text-[12px]"
                >
                  <button
                    type="button"
                    data-testid="edit-raw-srs-btn"
                    disabled={!hasContent || frozen}
                    onClick={() => {
                      const blob = SECTIONS.map((s) => {
                        const c = (srs?.sections || {})[s.key] || "";
                        return `<!-- SECTION:${s.key} -->\n## ${s.label}\n\n${c.trim()}`;
                      }).join("\n\n");
                      setRawBuf(blob);
                      setRawEditOpen(true);
                      setMenuOpen(false);
                    }}
                    className="w-full text-left px-3 py-1.5 flex items-center gap-2 hover:bg-bg disabled:opacity-40 disabled:cursor-not-allowed text-fg"
                  >
                    <Code className="w-3.5 h-3.5" /> Edit raw markdown
                  </button>
                  <button
                    type="button"
                    data-testid="export-pdf-btn"
                    disabled={!hasContent}
                    onClick={() => { handleExport(); setMenuOpen(false); }}
                    className="w-full text-left px-3 py-1.5 flex items-center gap-2 hover:bg-bg disabled:opacity-40 disabled:cursor-not-allowed text-fg"
                  >
                    <FileDown className="w-3.5 h-3.5" /> Export PDF
                  </button>
                  {/* iter-14.25.9 — Remove SRS (destructive). Typed
                      confirmation via window.prompt. Kept in the kebab
                      to avoid mis-click on the toolbar; disabled while
                      generating or when there's nothing to remove. */}
                  <div className="my-1 border-t border-border" />
                  <button
                    type="button"
                    data-testid="remove-srs-btn"
                    disabled={busy || generating || !hasContent}
                    onClick={handleResetSRS}
                    title='Remove the SRS and start Discovery over (KB and chat are preserved). Typed "RESET" confirmation required.'
                    className="w-full text-left px-3 py-1.5 flex items-center gap-2 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed text-red-600"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Remove SRS…
                  </button>
                </div>
              </>
            )}
          </div>

          {onCollapse && (
            <button
              type="button"
              onClick={onCollapse}
              data-testid="collapse-srs"
              className="text-fg-muted hover:text-fg p-1 ml-1"
              aria-label="Collapse panel"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Progress bar (during streaming generation) */}
      {generating && (
        <div className="px-4 py-3 border-b border-border bg-brand-tint" data-testid="srs-progress">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 text-fg font-semibold">
              {jobPaused
                ? <Pause className="w-3.5 h-3.5 text-amber-600" />
                : <Loader2 className="w-3.5 h-3.5 animate-spin" />
              }
              {progress.label || "Starting…"}
            </div>
            <div className="flex items-center gap-2">
              {/* iter-13.35 — live-job controls. Sit next to the progress
                  ticker so they're impossible to miss. */}
              {!jobPaused ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onPauseJob}
                  disabled={controlBusy}
                  className="h-7 text-micro rounded-sm border-border hover:bg-amber-50 hover:border-amber-400"
                  data-testid="srs-pause-btn"
                  title="Pause after the current section finishes"
                >
                  {controlBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Pause className="w-3 h-3" />}
                  <span className="ml-1">Pause</span>
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onResumeJob}
                  disabled={controlBusy}
                  className="h-7 text-micro rounded-sm border-amber-400 bg-amber-50 hover:bg-amber-100 text-amber-700"
                  data-testid="srs-resume-btn"
                  title="Resume generation"
                >
                  {controlBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
                  <span className="ml-1">Resume</span>
                </Button>
              )}
              <Button
                size="sm"
                variant="outline"
                onClick={onCancelJob}
                disabled={controlBusy}
                className="h-7 text-micro rounded-sm border-red-300 hover:bg-red-50 text-red-600 hover:text-red-700"
                data-testid="srs-cancel-btn"
                title="Cancel the run; sections already written are kept"
              >
                <Square className="w-3 h-3 fill-current" />
                <span className="ml-1">Cancel</span>
              </Button>
              <div className="text-fg font-mono pl-1">{progress.index}/{progress.total}</div>
            </div>
          </div>
          <div className="mt-2 h-1.5 bg-border rounded-sm overflow-hidden">
            <div
              className={`h-full ${jobPaused ? "bg-amber-400" : "bg-brand"}`}
              style={{ width: `${(progress.index / progress.total) * 100}%` }}
              data-testid="srs-progress-bar"
            />
          </div>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-y-auto mos-scroll bg-bg">
        <div className="w-full px-6 py-6">
          <div className="bg-surface border border-border shadow-sm rounded-sm p-8">
            {/* EY yellow accent bar */}
            <div className="h-1.5 bg-brand -mx-8 -mt-8 mb-6 rounded-t-sm" />
            <div className="border-b border-border pb-3 mb-4">
              <div className="text-micro uppercase tracking-widest text-fg-muted">Software Requirements Specification</div>
              <h1 className="font-display text-2xl font-bold tracking-tight text-fg mt-1">
                {srs?.project_id ? `Migration SRS · v${srs?.version || 0}` : "—"}
              </h1>
              {frozen && (
                <div className="text-micro text-fg-muted mt-1">
                  Frozen on {new Date(srs.frozen_at).toLocaleString()} by {srs.frozen_by}
                </div>
              )}
            </div>

            {!hasContent && !generating && (
              <div className="text-center py-16 text-fg-muted">
                <Sparkles className="w-6 h-6 mx-auto mb-2 text-fg-muted" />
                <div className="text-sm">No SRS yet. Have a discovery conversation, then click <b>Generate</b>.</div>
              </div>
            )}

            {/* iter-14.11 — iteration-wise color-code legend. Only shown once
                any section has scored telemetry so the panel doesn't look
                busy on an empty SRS. */}
            {hasContent && !generating && srs?.sections_meta
              && Object.keys(srs.sections_meta).length > 0 && (
              <div
                data-testid="srs-iteration-legend"
                className="mb-5 flex flex-wrap items-center gap-2 text-micro text-fg-muted"
              >
                <span className="uppercase tracking-widest text-fg-muted">Iteration key:</span>
                <span className="inline-flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm bg-emerald-50 border border-emerald-300" />
                  <span className="uppercase">1-shot ≥ 95%</span>
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm bg-sky-50 border border-sky-300" />
                  <span className="uppercase">Retry-1 fixed</span>
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm bg-violet-50 border border-violet-300" />
                  <span className="uppercase">Retry-2 fixed</span>
                </span>
                <span className="inline-flex items-center gap-1">
                  <span className="inline-block w-3 h-3 rounded-sm bg-amber-50 border border-amber-300" />
                  <span className="uppercase">Plateau &lt; 95% — KB gap</span>
                </span>
                <span className="inline-flex items-center gap-1" data-testid="srs-legend-evidence-gap">
                  <span className="inline-block w-3 h-3 rounded-sm bg-crit-bg border border-crit" />
                  <span className="uppercase">⚠ Evidence gap in body</span>
                </span>
              </div>
            )}

            {(hasContent || generating) && SECTIONS.map((s) => {
              const isDone = doneSections.has(s.key);
              const isCurrent = generating && progress.label.startsWith(s.label);
              const content = srs?.sections?.[s.key];
              const isEditing = editingSection === s.key;
              // iter-13.35 — per-section visual state for the refresh flow.
              const isRegenerating = regeneratingSection === s.key;
              const outcome = sectionState[s.key]; // {status, at, label} | undefined
              // iter-14.11 — score-gated retry telemetry (live SSE + persisted meta).
              // Live state takes precedence during a generation run so the tint
              // updates the moment section_complete lands; persisted meta powers
              // subsequent page loads.
              const scoreLive = sectionScore[s.key];
              const scoreMeta = srs?.sections_meta?.[s.key];
              const scoreInfo = scoreLive || (scoreMeta ? {
                attempts: scoreMeta.attempts,
                maxAttempts: scoreMeta.max_attempts,
                initialScore: scoreMeta.initial_score,
                finalScore: scoreMeta.final_score,
                band: scoreMeta.band,
                plateaued: !!scoreMeta.plateaued,
                minConfidence: scoreMeta.min_confidence || 95,
              } : null);
              //   running  → soft amber/yellow with subtle pulse
              //   success  → soft emerald
              //   error    → soft rose
              //   idle     → no tint
              // iter-14.11 additions (only applied when NOT regenerating and
              // NOT in the middle of the run — so the color-code visualises
              // "iteration-wise perfection" without fighting the live badges).
              //   perfect first shot   → emerald  (attempts=1, score>=min)
              //   improved on retry-1  → sky blue (attempts=2, score>=min)
              //   improved on retry-2  → violet   (attempts=3, score>=min)
              //   plateaued below min  → amber    (attention needed)
              let sectionTint = "";
              let sectionRing = "";
              let iterationTag = "";  // 3-letter label shown in a corner chip
              if (isRegenerating) {
                sectionTint = "bg-amber-50/70";
                sectionRing = "ring-1 ring-amber-300";
              } else if (outcome?.status === "success") {
                sectionTint = "bg-emerald-50/70";
                sectionRing = "ring-1 ring-emerald-300";
              } else if (outcome?.status === "error") {
                sectionTint = "bg-rose-50/70";
                sectionRing = "ring-1 ring-rose-300";
              } else if (scoreInfo && !generating) {
                const _min = scoreInfo.minConfidence || 95;
                const _clean = scoreInfo.finalScore >= _min;
                if (scoreInfo.plateaued || !_clean) {
                  sectionTint = "bg-amber-50/80";
                  sectionRing = "ring-1 ring-amber-300";
                  iterationTag = "PLATEAU";
                } else if (scoreInfo.attempts <= 1) {
                  sectionTint = "bg-emerald-50/60";
                  sectionRing = "ring-1 ring-emerald-200";
                  iterationTag = "1-SHOT";
                } else if (scoreInfo.attempts === 2) {
                  sectionTint = "bg-sky-50/80";
                  sectionRing = "ring-1 ring-sky-300";
                  iterationTag = "RETRY-1";
                } else {
                  sectionTint = "bg-violet-50/80";
                  sectionRing = "ring-1 ring-violet-300";
                  iterationTag = "RETRY-2";
                }
              }
              const tintClass = sectionTint
                ? `mb-8 rounded-md px-4 py-3 transition-colors duration-500 ${sectionTint} ${sectionRing}`
                : "mb-8 transition-colors duration-500";

              // Section 9: Entity Relationship Diagram (computed, not editable)
              if (s.key === "entity_model") {
                let erData = null;
                try { erData = content ? JSON.parse(content) : null; } catch { erData = null; }
                return (
                  <section
                    key={s.key}
                    className={tintClass}
                    data-testid="srs-section-entity_model"
                    data-section-state={isRegenerating ? "running" : (outcome?.status || "idle")}
                  >
                    <div className="flex items-center justify-between mb-2">
                      <h2 className="font-display text-base font-bold text-fg tracking-tight">{s.label}</h2>
                      {generating && (
                        isDone ? <span className="text-micro uppercase tracking-wider bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded-sm font-bold">Done</span>
                        : isCurrent ? <span className="text-micro uppercase tracking-wider bg-brand-tint border border-brand text-fg px-1.5 py-0.5 rounded-sm flex items-center"><Loader2 className="w-2.5 h-2.5 mr-1 animate-spin" />Computing</span>
                        : <span className="text-micro uppercase tracking-wider bg-bg text-fg-muted px-1.5 py-0.5 rounded-sm">Queued</span>
                      )}
                    </div>
                    <div className="border border-border rounded-sm overflow-hidden" style={{ height: 520 }}>
                      <Suspense fallback={<div className="skeleton h-[520px] w-full rounded" aria-hidden />}>
                        <ERDiagram data={erData} height={520} />
                      </Suspense>
                    </div>
                  </section>
                );
              }

              return (
                <section
                  key={s.key}
                  className={tintClass}
                  data-testid={`srs-section-${s.key}`}
                  data-section-state={isRegenerating ? "running" : (outcome?.status || "idle")}
                >
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <h2 className="font-display text-base font-bold text-fg tracking-tight">{s.label}</h2>
                      {/* iter-14.11 — iteration-wise perfection chip. Shows the
                          score AND the iteration tag so the operator can tell
                          at a glance whether this section landed on the first
                          shot (emerald 1-SHOT), improved on retry (sky/violet
                          RETRY-1 / RETRY-2), or plateaued below the min-confidence
                          threshold (amber PLATEAU · needs KB evidence). */}
                      {iterationTag && scoreInfo && !generating && (
                        <span
                          data-testid={`srs-iter-chip-${s.key}`}
                          className={
                            iterationTag === "1-SHOT"
                              ? "text-micro uppercase tracking-widest bg-emerald-100 text-emerald-800 border border-emerald-300 px-1.5 py-0.5 rounded-sm font-bold"
                              : iterationTag === "RETRY-1"
                              ? "text-micro uppercase tracking-widest bg-sky-100 text-sky-800 border border-sky-300 px-1.5 py-0.5 rounded-sm font-bold"
                              : iterationTag === "RETRY-2"
                              ? "text-micro uppercase tracking-widest bg-violet-100 text-violet-800 border border-violet-300 px-1.5 py-0.5 rounded-sm font-bold"
                              : "text-micro uppercase tracking-widest bg-amber-100 text-amber-900 border border-amber-300 px-1.5 py-0.5 rounded-sm font-bold"
                          }
                          title={
                            `Attempt ${scoreInfo.attempts}/${scoreInfo.maxAttempts} · ` +
                            `initial ${Number(scoreInfo.initialScore).toFixed(1)}% → ` +
                            `final ${Number(scoreInfo.finalScore).toFixed(1)}% ` +
                            `(threshold ${scoreInfo.minConfidence}%)` +
                            (scoreInfo.plateaued
                              ? " — plateaued below threshold, likely a KB evidence gap"
                              : "")
                          }
                        >
                          {iterationTag} · {Number(scoreInfo.finalScore).toFixed(1)}%
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5">
                      {/* iter-13.35 — per-section regen state badge (visible
                          only outside the full-run generation flow). */}
                      {!generating && isRegenerating && (
                        <span className="text-micro uppercase tracking-wider bg-amber-100 text-amber-800 px-1.5 py-0.5 rounded-sm flex items-center font-bold animate-pulse">
                          <Loader2 className="w-2.5 h-2.5 mr-1 animate-spin" />Regenerating
                        </span>
                      )}
                      {!generating && !isRegenerating && outcome?.status === "success" && (
                        <span className="text-micro uppercase tracking-wider bg-emerald-100 text-emerald-800 px-1.5 py-0.5 rounded-sm font-bold flex items-center">
                          <Check className="w-2.5 h-2.5 mr-1" />Refreshed
                        </span>
                      )}
                      {!generating && !isRegenerating && outcome?.status === "error" && (
                        <span className="text-micro uppercase tracking-wider bg-rose-100 text-rose-700 px-1.5 py-0.5 rounded-sm font-bold flex items-center">
                          <X className="w-2.5 h-2.5 mr-1" />Refresh failed
                        </span>
                      )}
                      {generating && (
                        isDone ? <span className="text-micro uppercase tracking-wider bg-brand text-fg px-1.5 py-0.5 rounded-sm font-bold">Done</span>
                        : isCurrent ? <span className="text-micro uppercase tracking-wider bg-brand-tint border border-brand text-fg px-1.5 py-0.5 rounded-sm flex items-center"><Loader2 className="w-2.5 h-2.5 mr-1 animate-spin" />Writing</span>
                        : <span className="text-micro uppercase tracking-wider bg-bg text-fg-muted px-1.5 py-0.5 rounded-sm">Queued</span>
                      )}
                      {!frozen && !generating && !isEditing && content && (
                        <button
                          type="button"
                          onClick={() => startEdit(s.key)}
                          className="text-fg-muted hover:text-fg p-1"
                          data-testid={`srs-edit-btn-${s.key}`}
                          aria-label="Edit section"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {/* iter-13.35 — per-section refresh button. Re-runs ONLY
                          this section through the backend's _gen_one_section
                          + persists it with a bumped version. Disabled while
                          the full-run generator or another single-section
                          regen is in flight. */}
                      {!frozen && !generating && !isEditing && (
                        <button
                          type="button"
                          onClick={() => onRegenerateOne(s.key)}
                          disabled={!!regeneratingSection}
                          className="flex items-center gap-1 text-micro font-bold uppercase tracking-wider bg-brand text-fg hover:bg-brand-hover px-2 py-1 rounded-sm border border-fg/10 disabled:opacity-40 disabled:cursor-not-allowed"
                          data-testid={`srs-refresh-btn-${s.key}`}
                          aria-label="Regenerate just this section"
                          title="Re-validate and regenerate just this section (leaves other sections untouched)"
                        >
                          {regeneratingSection === s.key
                            ? <><Loader2 className="w-3 h-3 animate-spin" /> Regenerating…</>
                            : <><RefreshCw className="w-3 h-3" /> Regenerate</>
                          }
                        </button>
                      )}
                    </div>
                  </div>
                  {isEditing ? (
                    <div data-testid={`srs-editor-${s.key}`}>
                      <textarea
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        className="w-full min-h-[14rem] font-mono text-xs bg-surface border border-border rounded-sm p-2 focus:border-fg focus:ring-1 focus:ring-fg outline-none"
                      />
                      <div className="mt-2 flex gap-2 justify-end">
                        <Button size="sm" variant="outline" onClick={cancelEdit} className="h-8 text-xs rounded-sm" data-testid={`srs-cancel-${s.key}`}>
                          <X className="w-3.5 h-3.5 mr-1" /> Cancel
                        </Button>
                        <Button size="sm" onClick={saveEdit} className="bg-ink text-ink-fg hover:bg-ink-hover h-8 text-xs rounded-sm" data-testid={`srs-save-${s.key}`}>
                          <Check className="w-3.5 h-3.5 mr-1" /> Save
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div className="lama-srs-content" data-testid={`srs-edit-${s.key}`}>
                      {content ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                          {normaliseSectionContent(content)}
                        </ReactMarkdown>
                      ) : (
                        <span className="text-fg-muted italic text-xs">
                          {isCurrent ? "Writing…" : generating ? "Pending — waiting for previous sections" : "(empty)"}
                        </span>
                      )}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        </div>
      </div>

      {/* Raw markdown edit modal (escape hatch when chat-edit isn't enough) */}
      {rawEditOpen && (
        <div className="fixed inset-0 bg-ink/50 z-50 flex items-center justify-center p-4" data-testid="raw-edit-modal">
          <div className="bg-surface rounded-sm w-full max-w-5xl h-[85vh] flex flex-col border-2 border-brand">
            <div className="px-4 py-3 border-b border-border flex items-center justify-between">
              <div>
                <div className="text-micro uppercase tracking-widest text-fg-muted">Edit raw SRS markdown</div>
                <div className="text-sm font-display font-bold">
                  Keep the <code className="text-micro bg-bg px-1">{`<!-- SECTION:key -->`}</code> markers — they split the document back into the 9 sections on save.
                </div>
              </div>
              <button onClick={() => setRawEditOpen(false)} className="text-fg-muted hover:text-fg" data-testid="raw-edit-close"><X className="w-4 h-4" /></button>
            </div>
            <textarea
              value={rawBuf}
              onChange={(e) => setRawBuf(e.target.value)}
              data-testid="raw-edit-textarea"
              className="flex-1 min-h-0 p-3 font-mono text-[12px] outline-none resize-none w-full"
              spellCheck={false}
            />
            <div className="px-4 py-3 border-t border-border flex items-center justify-between bg-surface-2">
              <div className="text-micro text-fg-muted">{rawBuf.length.toLocaleString()} chars · ~{Math.round(rawBuf.length / 4).toLocaleString()} tokens</div>
              <div className="flex items-center gap-1.5">
                <Button onClick={() => setRawEditOpen(false)} variant="outline" className="h-7 text-micro" data-testid="raw-edit-cancel">Cancel</Button>
                <Button
                  data-testid="raw-edit-save"
                  disabled={savingRaw}
                  onClick={async () => {
                    setSavingRaw(true);
                    try {
                      // Split rawBuf back into sections using the markers
                      const re = /<!--\s*SECTION:([a-z_]+)\s*-->/g;
                      const matches = [];
                      let m;
                      while ((m = re.exec(rawBuf)) !== null) matches.push({ key: m[1], start: m.index, end: m.index + m[0].length });
                      if (matches.length === 0) {
                        toast.error("No <!-- SECTION:key --> markers found. Refusing to save — please keep the markers so we can split the document back into the 9 IEEE-830 sections.");
                        setSavingRaw(false);
                        return;
                      }
                      const validKeys = new Set(SECTIONS.map((s) => s.key));
                      const blocks = {};
                      for (let i = 0; i < matches.length; i++) {
                        const next = matches[i + 1];
                        const body = rawBuf.slice(matches[i].end, next ? next.start : rawBuf.length);
                        // Strip the leading "## Heading" line that follows the marker
                        blocks[matches[i].key] = body.replace(/^\s*##[^\n]*\n+/, "").trim();
                      }
                      const unknown = Object.keys(blocks).filter((k) => !validKeys.has(k));
                      if (unknown.length > 0) {
                        toast.error(`Unknown section key(s): ${unknown.join(", ")}. Valid keys are: ${[...validKeys].join(", ")}.`);
                        setSavingRaw(false);
                        return;
                      }
                      let saved = 0;
                      for (const s of SECTIONS) {
                        if (!(s.key in blocks)) continue; // don't wipe sections the user didn't include
                        await updateSRSSection(projectId, s.key, blocks[s.key]);
                        saved += 1;
                      }
                      toast.success(`Saved ${saved} section(s)`);
                      setRawEditOpen(false);
                      await refresh();
                    } catch (e) {
                      toast.error("Save failed: " + (e?.response?.data?.detail || e.message));
                    } finally { setSavingRaw(false); }
                  }}
                  className="h-7 text-micro bg-brand text-fg hover:bg-brand-hover font-bold"
                >
                  {savingRaw ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : <Check className="w-3 h-3 mr-1" />} Save all sections
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
