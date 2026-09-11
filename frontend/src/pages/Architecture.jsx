import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import mermaid from "mermaid";
import {
  Loader2,
  Sparkles,
  Download,
  Lock,
  Pencil,
  Check,
  Send,
  Boxes,
  GitBranch,
  FileText,
  RotateCcw,
  ChevronDown,
  ChevronRight as ChevronRightIcon,
  Workflow,
  Network,
  Wand2,
  Plug,
  ArrowRight,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import { useIsMobile } from "@/hooks/useBreakpoint";
import { EmptyState } from "@/components/ux";
import { FolderOpen } from "lucide-react";
import {
  startArchRecommend,
  startArchHld,
  startArchLld,
  startArchSequence,
  startArchApiContracts,
  getArchJob,
  approveServiceMap,
  listArchUtilities,
  sendArchChat,
  applyArchChanges,
  getArchArtifacts,
  getArchArtifact,
  updateArchArtifact,
  freezeArchArtifact,
  downloadArchArtifactUrl,
  downloadArchArtifactPdfUrl,
  resetArch,
  purgeBrokenArch,
  mergeServices,
  mergeServicesBatch,
  unmergeService,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import ConfidenceBadge from "@/components/ConfidenceBadge";

mermaid.initialize({
  startOnLoad: false,
  theme: "neutral",
  securityLevel: "loose",
  suppressErrorRendering: true,
  flowchart: { htmlLabels: true },
});

const _mmId = (() => {
  let n = 0;
  return () => `mm-${++n}-${Math.random().toString(36).slice(2, 8)}`;
})();

// iter-13.49 — Client-side sanitiser for LLM-emitted Mermaid sequence
// diagrams. Mirrors the backend `_sanitize_mermaid_sequence` so artifacts
// generated before the backend fix shipped still render, and so any
// mermaid output that slips past the backend pipeline (chat-driven edits,
// HLD/LLD embedded blocks) gets a final repair pass.
function sanitizeMermaid(raw) {
  if (!raw) return "sequenceDiagram\n  Note over System: (empty diagram)";
  let text = String(raw).trim();

  // iter-13.52 — Pre-pass: kill non-ASCII chars mermaid 11 chokes on.
  const UC_REPLACE = {
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\u2013": "-", "\u2014": "-",
    "\u00a0": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
  };
  text = text.replace(/[\u201c\u201d\u2018\u2019\u2013\u2014\u00a0\u200b\u200c\u200d\ufeff]/g,
    (ch) => UC_REPLACE[ch] ?? "");
  // strip remaining control chars except \n / \t
  // eslint-disable-next-line no-control-regex
  text = text.replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");

  // 1. Strip outer fences (handles double fences, bare ``` fences).
  for (let i = 0; i < 3; i++) {
    text = text.replace(/^[ \t]*(?:```|~~~)[ \t]*(?:mermaid|mmd)?[^\n]*\n/, "");
    text = text.replace(/\n[ \t]*(?:```|~~~)[ \t]*$/, "");
    text = text.trim();
    if (!text.startsWith("```") && !text.startsWith("~~~")) break;
  }

  // Only sequenceDiagram gets per-line repair. Other diagram kinds pass
  // through unchanged (flowchart / classDiagram / C4Context etc.).
  const lower = text.toLowerCase();
  const isSeq = lower.startsWith("sequencediagram") || /\n\s*sequencediagram\b/.test("\n" + lower);
  if (!isSeq) return text;

  let lines = text.split("\n");
  // Drop prose before the header.
  const headIdx = lines.findIndex(l => l.trim().toLowerCase().startsWith("sequencediagram"));
  if (headIdx > 0) lines = lines.slice(headIdx);
  if (headIdx === -1) lines = ["sequenceDiagram", ...lines];
  lines[0] = "sequenceDiagram";

  // Drop trailing prose after the last mermaid-looking line.
  const keywords = ["participant","actor","activate","deactivate","Note","note",
                    "loop","end","alt","else","opt","par","and","rect",
                    "autonumber","title","box","critical","break"];
  let lastGood = 0;
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (!s || s.startsWith("%%")) continue;
    const head = s.split(/\s/)[0];
    if (s.includes("->>") || s.includes("-->>") || s.includes("-x") || s.includes("--x")
        || keywords.includes(head)) lastGood = i;
  }
  if (lastGood > 0) lines = lines.slice(0, lastGood + 1);

  const safeId = (id) => {
    let s = (id || "").replace(/[^A-Za-z0-9_]/g, "_").replace(/^_+|_+$/g, "") || "P";
    if (!/^[A-Za-z]/.test(s)) s = "P_" + s;
    return s;
  };

  const fixed = lines.map((ln) => {
    let s = ln.replace(/\s+$/, "");
    if (s.includes("```")) s = s.split("```").join("");
    s = s.replace(/<br\s*\/?>/gi, " — ");

    // iter-13.52 — escape sequences inside messages
    s = s.replace(/\\n/g, " ").replace(/\\r/g, " ").replace(/\\t/g, " ");

    // iter-13.52 — `end note` / `end alt` etc. → bare `end`
    s = s.replace(/^(\s*)end\s+(note|alt|loop|opt|par|rect|critical|box)\s*$/i, "$1end");

    // participant / actor declarations (with optional quoted ident)
    const decl = s.match(
      /^(\s*)(participant|actor)\s+(?:"([^"]+)"|(\S+))(?:\s+as\s+(.+?))?\s*$/i,
    );
    if (decl) {
      const indent = decl[1];
      const kw = decl[2].toLowerCase();
      const ident = decl[3] || decl[4] || "";
      const label = decl[5];
      const sid = safeId(ident);
      if (label) {
        let lc = label.trim().replace(/^["']|["']$/g, "");
        if (/[,:#]/.test(lc)) lc = `"${lc}"`;
        s = `${indent}${kw} ${sid} as ${lc}`;
      } else if (sid !== ident) {
        const disp = ident.includes(" ") ? `"${ident}"` : ident;
        s = `${indent}${kw} ${sid} as ${disp}`;
      } else {
        s = `${indent}${kw} ${sid}`;
      }
    }

    // Arrows with hyphenated IDs on either side (incl. activation +/- prefix
    // on dst, e.g. `A->>+B: foo`). Ensures non-empty message body.
    s = s.replace(
      /(^|\s)([A-Za-z][A-Za-z0-9_\-\.\/]*?)\s*(-->>|->>|--x|-x|->|-->)\s*([+\-])?\s*([A-Za-z][A-Za-z0-9_\-\.\/]*?)\s*:(.*)$/,
      (_m, head, a, arrow, act, b, msg) => {
        const m = (msg || "").replace(/\s+$/, "");
        const body = m.trim() ? m : " .";
        return `${head}${safeId(a)} ${arrow} ${act || ""}${safeId(b)}:${body}`;
      },
    );

    // Note over / left of / right of
    s = s.replace(
      /^(\s*Note\s+(?:over|left of|right of)\s+)([^:\n]+?)(\s*:)/i,
      (_m, head, csv, tail) =>
        head + csv.split(",").map((x) => safeId(x.trim().replace(/^["']|["']$/g, ""))).join(", ") + tail,
    );

    // activate / deactivate
    s = s.replace(
      /^(\s*(?:activate|deactivate)\s+)(\S+)(\s*)$/i,
      (_m, head, id, tail) => head + safeId(id) + tail,
    );

    return s;
  });

  // Auto-close unbalanced alt/loop/par/opt/rect/critical/box blocks.
  let opens = 0;
  for (const ln of fixed) {
    const head = (ln.trim().split(/\s/)[0] || "").toLowerCase();
    if (["alt","loop","par","opt","rect","critical","box"].includes(head)) opens++;
    else if (head === "end") opens--;
  }
  while (opens-- > 0) fixed.push("end");

  let body = fixed.join("\n").replace(/\s+$/, "");
  if (!body || body.trim().toLowerCase() === "sequencediagram") {
    body = "sequenceDiagram\n  Note over System: (no diagram body)";
  }
  return body;
}

// iter-13.52 — Aggressive last-resort rebuilder. When even sanitizeMermaid
// produces a diagram mermaid 11 refuses to parse, this throws away EVERY
// line that doesn't match a strict whitelist of known-good patterns and
// returns a guaranteed-parseable skeleton. The result will be lossy but
// will always render — the user gets a usable artifact instead of a red
// error box.
function aggressiveRebuildMermaid(raw) {
  const sanitised = sanitizeMermaid(raw);
  const lines = sanitised.split("\n");
  const out = ["sequenceDiagram"];
  const ARROW = /^([A-Za-z][A-Za-z0-9_]*)\s*(-->>|->>|--x|-x|-->|->)\s*([+\-]?)([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.+)$/;
  const NOTE = /^Note\s+(over|left of|right of)\s+([A-Za-z][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z][A-Za-z0-9_]*)*)\s*:\s*(.+)$/i;
  const PART = /^(participant|actor)\s+([A-Za-z][A-Za-z0-9_]*)(\s+as\s+.+)?$/i;
  const ACT = /^(activate|deactivate)\s+([A-Za-z][A-Za-z0-9_]*)$/i;
  const SIMPLE = /^(loop|alt|else|opt|par|and|rect|critical|break|option)\b.*$/i;
  const COMMENT = /^%%/;
  for (let i = 1; i < lines.length; i++) {
    const t = lines[i].trim();
    if (!t) continue;
    if (t === "end" || t.toLowerCase() === "end") { out.push("end"); continue; }
    if (t.toLowerCase() === "autonumber") { out.push("autonumber"); continue; }
    if (COMMENT.test(t)) continue;
    if (PART.test(t)) { out.push(t); continue; }
    const a = t.match(ARROW);
    if (a) {
      const msg = a[5].replace(/[\r\n]+/g, " ").trim() || ".";
      out.push(`${a[1]} ${a[2]} ${a[3] || ""}${a[4]}: ${msg}`);
      continue;
    }
    if (NOTE.test(t)) { out.push(t); continue; }
    if (ACT.test(t)) { out.push(t); continue; }
    if (SIMPLE.test(t)) { out.push(t); continue; }
    // Drop everything else.
  }
  // Re-balance.
  let opens = 0;
  for (const ln of out) {
    const h = (ln.trim().split(/\s/)[0] || "").toLowerCase();
    if (["loop", "alt", "opt", "par", "rect", "critical", "box"].includes(h)) opens++;
    else if (h === "end") opens--;
  }
  while (opens-- > 0) out.push("end");
  if (out.length === 1) {
    out.push("  Note over System: (diagram could not be parsed — see source)");
  }
  return out.join("\n");
}

const ARTIFACT_META = {
  service_map: { label: "Service Map", icon: Network, ext: "json" },
  hld: { label: "HLD — High-Level Design", icon: Boxes, ext: "md" },
  lld: { label: "LLD — Low-Level Design", icon: FileText, ext: "md" },
  sequence_diagrams: { label: "Sequence Diagrams", icon: Workflow, ext: "md" },
  api_contracts: { label: "API Contracts", icon: GitBranch, ext: "yaml" },
};

// -----------------------------------------------------------
// Mermaid block renderer (one diagram in a div)
// -----------------------------------------------------------
function MermaidBlock({ chart, id }) {
  const ref = useRef(null);
  const [err, setErr] = useState("");
  const [rendered, setRendered] = useState("");
  useEffect(() => {
    if (!ref.current || !chart) return;
    let cancelled = false;
    const render = async () => {
      const raw = (chart || "").trim();
      // iter-13.49 — Try the chart as-is first; on parse failure, fall back
      // to the sanitised version. We don't sanitise unconditionally because
      // valid flowchart / classDiagram / C4 blocks should pass straight
      // through without sequenceDiagram-specific rewrites.
      const tryRender = async (src) => {
        const ok = await mermaid.parse(src, { suppressErrors: true });
        if (ok === false) return null;
        const { svg } = await mermaid.render(_mmId(), src);
        return svg;
      };
      try {
        let svg = null;
        try { svg = await tryRender(raw); } catch (_e) { svg = null; }
        if (!svg) {
          const repaired = sanitizeMermaid(raw);
          try { svg = await tryRender(repaired); } catch (_e2) { svg = null; }
          if (svg && !cancelled) setRendered(repaired);
        } else if (!cancelled) {
          setRendered(raw);
        }
        // iter-13.52 — Third-tier fallback: aggressive whitelist rebuild.
        // If both raw and the sanitised version still fail to parse,
        // throw away every line that doesn't match a strict known-good
        // pattern and render the resulting skeleton. The user gets a
        // partial-but-renderable diagram instead of a red error box.
        if (!svg) {
          const aggressive = aggressiveRebuildMermaid(raw);
          try { svg = await tryRender(aggressive); } catch (_e3) { svg = null; }
          if (svg && !cancelled) setRendered(aggressive);
        }
        if (svg && !cancelled && ref.current) {
          ref.current.innerHTML = svg;
          setErr("");
        } else if (!svg && !cancelled) {
          // iter-13.52 — final stub. Even the aggressive rebuild can fail
          // for completely degenerate input; render a minimal valid stub
          // so the artifact never shows the red error UI.
          const stub = "sequenceDiagram\n  Note over System: (diagram could not be parsed — see source)";
          try {
            const { svg: stubSvg } = await mermaid.render(_mmId(), stub);
            if (!cancelled && ref.current) {
              ref.current.innerHTML = stubSvg;
              setRendered(stub);
              setErr("");
            }
          } catch (_e4) {
            if (!cancelled) setErr("Invalid Mermaid syntax — even after auto-repair. Showing source below.");
          }
        }
      } catch (e) {
        if (!cancelled) setErr(String(e?.message || e));
      }
    };
    render();
    return () => { cancelled = true; };
  }, [chart, id]);
  if (err) {
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-sm p-2 my-1" data-testid="mermaid-error">
        <div className="text-[10px] uppercase font-bold text-amber-700">Mermaid render error</div>
        <pre className="text-[11px] text-amber-800 mt-1 whitespace-pre-wrap">{err}</pre>
        <details className="mt-1">
          <summary className="text-[10px] text-[#747480] cursor-pointer">Show source</summary>
          <pre className="text-[10px] mt-1 bg-white border border-[#E6E6E6] p-2 rounded-sm overflow-x-auto whitespace-pre-wrap">{chart}</pre>
        </details>
      </div>
    );
  }
  return <div ref={ref} data-testid={`mermaid-${id}`} title={rendered && rendered !== chart ? "auto-repaired" : undefined} className="bg-white border border-[#E6E6E6] rounded-sm p-3 overflow-x-auto" />;
}

// Render markdown with embedded mermaid fenced blocks
function MarkdownWithMermaid({ source, idPrefix }) {
  const parts = useMemo(() => {
    if (!source) return [];
    const out = [];
    // iter-13.49 — Support ```mermaid, ~~~mermaid, ```mmd AND untagged
    // ``` fences whose body starts with sequenceDiagram / flowchart /
    // classDiagram / etc. The LLM sometimes forgets the language tag.
    const re = /(?:```|~~~)[ \t]*([a-zA-Z0-9_-]*)\b[^\n]*\n([\s\S]*?)(?:```|~~~)/gi;
    let last = 0; let m; let i = 0;
    const diagramHeads = /^(?:sequenceDiagram|flowchart|graph|classDiagram|stateDiagram|stateDiagram-v2|erDiagram|journey|gantt|pie|gitGraph|C4Context|C4Container|C4Component|mindmap|timeline|requirementDiagram)\b/i;
    while ((m = re.exec(source)) !== null) {
      const tag = (m[1] || "").toLowerCase();
      let chart = m[2] || "";
      const bodyTrim = chart.trim();
      const isMermaid =
        tag === "mermaid" || tag === "mmd" ||
        (!tag && diagramHeads.test(bodyTrim));
      if (!isMermaid) continue; // leave non-mermaid fences for the markdown renderer
      if (m.index > last) out.push({ type: "md", content: source.slice(last, m.index) });
      // Strip any stray inner fences the LLM may have nested
      chart = chart.replace(/^[ \t]*(?:```|~~~)[a-zA-Z]*\s*\n?/, "")
                   .replace(/\n?(?:```|~~~)\s*$/, "")
                   .trim();
      out.push({ type: "mermaid", content: chart, key: `${idPrefix}-mm-${i++}` });
      last = m.index + m[0].length;
    }
    if (last < source.length) out.push({ type: "md", content: source.slice(last) });
    return out;
  }, [source, idPrefix]);
  if (!source) return <div className="text-xs text-[#747480]">No content yet.</div>;
  return (
    <div className="prose prose-sm max-w-none text-[#2E2E38]">
      {parts.map((p, i) =>
        p.type === "mermaid" ? (
          <MermaidBlock key={p.key} chart={p.content} id={p.key} />
        ) : (
          <ReactMarkdown key={i} remarkPlugins={[remarkGfm]}>
            {p.content}
          </ReactMarkdown>
        )
      )}
    </div>
  );
}

// -----------------------------------------------------------
// iter-14.20.8 — API Contracts JSON view
// The artifact is stored as concatenated OpenAPI 3.1 YAML blocks
// (one per service, separated by `---`). Users have asked for the
// details to be shown JSON-formatted, which is also how most
// tooling (Postman / Swagger UI / codegen) consumes OpenAPI now.
// We parse each block via js-yaml and pretty-print as JSON in a
// Monaco read-only editor, with a service selector + copy button.
// Raw YAML remains available via the Edit / Download buttons.
// -----------------------------------------------------------
function ApiContractsView({ source, projectId }) {
  const [format, setFormat] = useState("json");
  const [activeIdx, setActiveIdx] = useState(0);
  const [yamlLib, setYamlLib] = useState(null);

  useEffect(() => {
    let alive = true;
    import("js-yaml").then((m) => {
      if (alive) setYamlLib(() => (m.default || m));
    }).catch(() => { /* fallback below */ });
    return () => { alive = false; };
  }, []);

  const services = useMemo(() => {
    const raw = (source || "").trim();
    if (!raw) return [];
    // Split multi-service artifact by the YAML doc separator our
    // renderer emits (`\n\n---\n\n`). Fall back to single-doc if no
    // separator is present.
    const blocks = raw.split(/\n\s*---\s*\n/).map((b) => b.trim()).filter(Boolean);
    return blocks.map((block, i) => {
      const lines = block.split("\n");
      // Extract leading `# === Service: Foo (foo) — 12 endpoint(s) ===`
      // header comment for the tab label.
      let label = `Service ${i + 1}`;
      let headerLines = 0;
      for (const ln of lines) {
        if (ln.startsWith("#")) {
          headerLines++;
          const m = ln.match(/# === Service:\s*(.+?)\s*(?:\(|—|$)/);
          if (m) label = m[1].trim();
          continue;
        }
        break;
      }
      const yamlBody = lines.slice(headerLines).join("\n").trim();
      const headerText = lines.slice(0, headerLines).join("\n");
      let parsed = null;
      let parseErr = "";
      if (yamlLib) {
        try {
          parsed = yamlLib.load(yamlBody);
        } catch (e) {
          parseErr = e?.message || String(e);
        }
      } else {
        parseErr = "js-yaml loading…";
      }
      const jsonText = parsed
        ? JSON.stringify(parsed, null, 2)
        : `// Failed to parse OpenAPI YAML for this service:\n// ${parseErr}\n\n${yamlBody}`;
      const endpointCount = (() => {
        try {
          return Object.keys(parsed?.paths || {}).length;
        } catch { return 0; }
      })();
      return {
        label,
        headerText,
        yamlText: yamlBody,
        jsonText,
        parsed,
        parseErr,
        endpointCount,
      };
    });
  }, [source, yamlLib]);

  useEffect(() => {
    if (activeIdx >= services.length) setActiveIdx(0);
  }, [services.length, activeIdx]);

  if (!services.length) {
    return <div className="text-xs text-[#747480] p-4">No API Contracts content yet.</div>;
  }

  const active = services[activeIdx] || services[0];
  const displayText = format === "json" ? active.jsonText : active.yamlText;

  const copyToClipboard = async () => {
    try {
      await navigator.clipboard.writeText(displayText);
      toast.success(`${format.toUpperCase()} copied for ${active.label}`);
    } catch (e) {
      toast.error("Copy failed — clipboard unavailable");
    }
  };

  const downloadCurrent = () => {
    const blob = new Blob([displayText], {
      type: format === "json" ? "application/json" : "application/x-yaml",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safe = (active.label || "service").toLowerCase().replace(/[^a-z0-9]+/g, "-");
    a.href = url;
    a.download = `openapi-${safe}.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex flex-col h-full" data-testid="api-contracts-view">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[#E6E6E6] bg-[#FAFAFC] flex-wrap">
        <div className="flex items-center gap-1" data-testid="api-contracts-format-toggle">
          <button
            onClick={() => setFormat("json")}
            className={`text-[11px] px-2 py-1 rounded-sm border ${
              format === "json"
                ? "bg-[#2E2E38] text-white border-[#2E2E38]"
                : "bg-white text-[#2E2E38] border-[#E6E6E6] hover:bg-[#F6F6FA]"
            }`}
            data-testid="api-contracts-format-json"
          >JSON</button>
          <button
            onClick={() => setFormat("yaml")}
            className={`text-[11px] px-2 py-1 rounded-sm border ${
              format === "yaml"
                ? "bg-[#2E2E38] text-white border-[#2E2E38]"
                : "bg-white text-[#2E2E38] border-[#E6E6E6] hover:bg-[#F6F6FA]"
            }`}
            data-testid="api-contracts-format-yaml"
          >YAML</button>
        </div>
        <div className="mx-2 h-4 border-l border-[#E6E6E6]" />
        <label className="text-[11px] text-[#747480]">Service:</label>
        <select
          value={activeIdx}
          onChange={(e) => setActiveIdx(Number(e.target.value))}
          className="text-[11px] border border-[#E6E6E6] rounded-sm px-2 py-1 bg-white max-w-xs"
          data-testid="api-contracts-service-selector"
        >
          {services.map((s, i) => (
            <option key={i} value={i}>
              {s.label} ({s.endpointCount} endpoint{s.endpointCount === 1 ? "" : "s"})
            </option>
          ))}
        </select>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={copyToClipboard}
            className="text-[11px] px-2 py-1 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA]"
            data-testid="api-contracts-copy"
          >Copy</button>
          <button
            onClick={downloadCurrent}
            className="text-[11px] px-2 py-1 border border-[#E6E6E6] rounded-sm hover:bg-[#F6F6FA] flex items-center gap-1"
            data-testid="api-contracts-download-current"
          ><Download className="w-3 h-3" /> {format.toUpperCase()}</button>
        </div>
      </div>

      {/* Header comment (provenance banner from the renderer) */}
      {active.headerText && (
        <div className="px-3 py-1.5 text-[11px] text-[#747480] bg-[#F6F6FA] border-b border-[#E6E6E6] whitespace-pre-wrap font-mono">
          {active.headerText}
        </div>
      )}

      {/* Parse-error banner */}
      {format === "json" && active.parseErr && (
        <div className="px-3 py-1.5 text-[11px] text-amber-800 bg-amber-50 border-b border-amber-200">
          YAML parse failed — showing raw text. {active.parseErr}
        </div>
      )}

      {/* JSON / YAML content via Monaco */}
      <div className="flex-1 min-h-[300px]">
        <ApiContractsEditor value={displayText} language={format} />
      </div>
    </div>
  );
}

// Lazy Monaco wrapper — importing at module top would bloat the
// initial Architecture bundle. Consumers only pay when they open
// the API Contracts artifact.
function ApiContractsEditor({ value, language }) {
  const [Editor, setEditor] = useState(null);
  useEffect(() => {
    let alive = true;
    import("@monaco-editor/react").then((mod) => {
      if (alive) setEditor(() => mod.default);
    });
    return () => { alive = false; };
  }, []);
  if (!Editor) {
    return (
      <pre className="text-[11px] leading-snug font-mono p-3 overflow-auto h-full whitespace-pre bg-white">
        {value}
      </pre>
    );
  }
  return (
    <Editor
      height="100%"
      language={language === "json" ? "json" : "yaml"}
      value={value}
      theme="vs"
      options={{
        readOnly: true,
        minimap: { enabled: false },
        fontSize: 12,
        lineNumbers: "on",
        scrollBeyondLastLine: false,
        wordWrap: "on",
        renderLineHighlight: "none",
        folding: true,
        automaticLayout: true,
      }}
    />
  );
}

// -----------------------------------------------------------
// Reset modal (typed RESET)
// -----------------------------------------------------------
function ResetModal({ open, onClose, onConfirm, title, warning }) {
  const [typed, setTyped] = useState("");
  useEffect(() => { if (!open) setTyped(""); }, [open]);
  if (!open) return null;
  const enabled = typed === "RESET";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" data-testid="arch-reset-modal">
      <div className="bg-white max-w-md w-full rounded-sm border-2 border-orange-500 p-5">
        <h3 className="font-display font-bold text-lg text-orange-700 flex items-center gap-2">
          <RotateCcw className="w-4 h-4" /> {title}
        </h3>
        <p className="text-xs text-[#2E2E38] mt-2 leading-snug">{warning}</p>
        <p className="text-xs text-[#747480] mt-3">Type <code className="bg-[#F6F6FA] px-1">RESET</code> to confirm.</p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          data-testid="arch-reset-input"
          className="mt-1 w-full border border-[#E6E6E6] focus:border-orange-500 outline-none px-2 py-1.5 text-sm rounded-sm"
        />
        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose} className="text-xs px-3 py-1.5 border border-[#E6E6E6] rounded-sm">Cancel</button>
          <button
            disabled={!enabled}
            onClick={onConfirm}
            data-testid="arch-reset-confirm"
            className={`text-xs px-3 py-1.5 rounded-sm font-bold text-white ${enabled ? "bg-orange-600 hover:bg-orange-700" : "bg-orange-300 cursor-not-allowed"}`}
          >
            Reset Stage 3
          </button>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------
// iter-13.59 — Utilities panel (cross-cutting services).
// Lets the user opt-in to features like the Audit Logger (ELK-ready);
// selections are sent through approveServiceMap and persisted as
// arch_services rows with kind="utility". CodeGen then materialises
// deterministic files for the utility itself + injects an aspect /
// middleware into every business service.
// -----------------------------------------------------------
function UtilitiesPanel({ utilities, selected, configs, onToggle, onConfigChange, frozen }) {
  if (!utilities || utilities.length === 0) return null;
  return (
    <div
      className="border border-violet-200 bg-gradient-to-br from-violet-50 to-white rounded-md p-3 mb-3"
      data-testid="utilities-panel"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] uppercase tracking-wider text-violet-700 font-semibold">
          Cross-cutting utilities
        </div>
        <span className="text-[10px] text-slate-500">
          Generated into every service when checked
        </span>
      </div>
      <div className="space-y-1.5">
        {utilities.map((u) => {
          const on = selected.has(u.key);
          const cfg = configs[u.key] || {};
          return (
            <div
              key={u.key}
              data-testid={`utility-${u.key}`}
              className={`border rounded-sm p-2 bg-white transition-opacity ${on ? "border-violet-200" : "border-dashed border-slate-200 opacity-75"}`}
            >
              <label className="flex items-start gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  data-testid={`utility-checkbox-${u.key}`}
                  checked={on}
                  disabled={frozen}
                  onChange={() => onToggle(u.key)}
                  className="accent-violet-600 mt-0.5 shrink-0"
                />
                <div className="flex-1 min-w-0">
                  <div className="font-semibold text-[13px] text-slate-800">{u.display_name}</div>
                  <div className="text-[11px] text-slate-600 leading-snug">{u.description}</div>
                  <div className="text-[10px] text-slate-400 mt-0.5">
                    Supported targets: {(u.supported_langs || []).join(" · ")}
                  </div>
                </div>
              </label>
              {on && !frozen && u.config_schema && (
                <div className="mt-2 ml-6 grid grid-cols-1 md:grid-cols-2 gap-1.5">
                  {u.config_schema.map((field) => (
                    <label
                      key={field.key}
                      className="text-[10px] text-slate-600 flex flex-col"
                      title={field.label}
                    >
                      <span className="truncate">{field.label}</span>
                      {field.type === "bool" ? (
                        <input
                          type="checkbox"
                          data-testid={`utility-cfg-${u.key}-${field.key}`}
                          checked={!!cfg[field.key]}
                          onChange={(e) => onConfigChange(u.key, field.key, e.target.checked)}
                          className="accent-violet-600 mt-0.5 self-start"
                        />
                      ) : (
                        <input
                          type={field.type === "number" ? "number" : "text"}
                          data-testid={`utility-cfg-${u.key}-${field.key}`}
                          value={cfg[field.key] ?? ""}
                          onChange={(e) =>
                            onConfigChange(
                              u.key,
                              field.key,
                              field.type === "number" ? Number(e.target.value) : e.target.value,
                            )
                          }
                          className="block w-full mt-0.5 border border-slate-200 rounded-sm px-1.5 py-0.5 text-[11px] font-mono"
                        />
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// -----------------------------------------------------------
// Service Map JSON pretty viewer
// iter-13.57 — each service card carries a checkbox. Selection state
// lives in the parent (Architecture page) so the Approve button can
// pass it to the backend, which prunes arch_services to exactly the
// selected subset before HLD/LLD/Sequence/API/CodeGen run.
// -----------------------------------------------------------
function ServiceMapView({ artifact, selected, onToggle, onToggleAll, frozen, utilitiesSlot,
                         onMerge, onUnmerge, mergeName, setMergeName, mergeBusy,
                         onMergeGroups, mergeGroupsBusy }) {
  // iter-13.123 — deep-link from Service Map to the Integrations page (the
  // "Govt. Service Integration" catalog: PAN, Aadhaar e-KYC, GSTIN,
  // DigiLocker, e-Sign, UPI). Mirrors the Sidebar > Tools > Integrations
  // entry so users discover it from the architecture context too.
  const navigate = useNavigate();
  let data = {};
  try { data = JSON.parse(artifact?.content || "{}"); } catch { /* */ }
  const services = data.services || [];
  const selectedCount = services.reduce(
    (n, s) => n + (selected?.has(s.name) ? 1 : 0), 0,
  );
  const allChecked = services.length > 0 && selectedCount === services.length;

  // iter-14.33 — Multi-group merge state. Each entry:
  //   { id: unique-int, name: "billing-svc", members: Set<string> }
  // `assignedGroupId[svcName]` maps a service to its assigned group id.
  const [groups, setGroups] = React.useState([]);           // list of groups
  const [nextGroupId, setNextGroupId] = React.useState(1);  // monotonic id
  const assignedGroupId = React.useMemo(() => {
    const m = {};
    for (const g of groups) {
      for (const nm of g.members) m[nm] = g.id;
    }
    return m;
  }, [groups]);
  const inAnyGroup = React.useCallback((svcName) => !!assignedGroupId[svcName], [assignedGroupId]);

  const addGroup = () => {
    setGroups((g) => [...g, { id: nextGroupId, name: "", members: new Set() }]);
    setNextGroupId((n) => n + 1);
  };
  const removeGroup = (gid) => {
    setGroups((gs) => gs.filter((g) => g.id !== gid));
  };
  const renameGroup = (gid, newName) => {
    const clean = (newName || "").toLowerCase().replace(/[^a-z0-9-]/g, "");
    setGroups((gs) => gs.map((g) => g.id === gid ? { ...g, name: clean } : g));
  };
  const assignSvcToGroup = (svcName, gid) => {
    setGroups((gs) => gs.map((g) => {
      const members = new Set(g.members);
      if (g.id === gid) members.add(svcName);
      else members.delete(svcName);
      return { ...g, members };
    }));
  };
  const unassignSvc = (svcName) => {
    setGroups((gs) => gs.map((g) => {
      if (!g.members.has(svcName)) return g;
      const members = new Set(g.members); members.delete(svcName);
      return { ...g, members };
    }));
  };
  const clearAllGroups = () => { setGroups([]); setNextGroupId(1); };

  // Validation summary for the "Apply groups" button.
  const groupErrors = React.useMemo(() => {
    const errs = [];
    const seenNames = new Set();
    for (const g of groups) {
      if (!g.name) errs.push(`Group #${g.id}: name required (kebab-case)`);
      else if (!/^[a-z0-9][a-z0-9-]{1,60}$/.test(g.name))
        errs.push(`Group ${g.name}: must be lower-kebab-case (a-z, 0-9, -)`);
      else if (seenNames.has(g.name))
        errs.push(`Duplicate group name: ${g.name}`);
      else seenNames.add(g.name);
      if (g.members.size < 2) errs.push(`Group ${g.name || `#${g.id}`}: needs ≥ 2 services`);
    }
    return errs;
  }, [groups]);
  const canApplyGroups = groups.length > 0 && groupErrors.length === 0 && !mergeGroupsBusy;

  const applyGroups = async () => {
    if (!canApplyGroups) return;
    const payload = groups.map((g) => ({
      merged_name: g.name,
      service_names: [...g.members],
    }));
    await onMergeGroups?.(payload);
    // Parent will refresh; clear local state.
    clearAllGroups();
  };

  return (
    <div className="space-y-3" data-testid="service-map-view">
      <div className="flex items-center justify-between text-xs">
        <div>
          <span className="text-[#747480]">Recommended pattern:</span>{" "}
          <span className="font-bold text-[#2E2E38]" data-testid="recommended-pattern">{data.recommended_pattern || "—"}</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => navigate("/integrations")}
            data-testid="govt-service-integration-link"
            title="Open the Govt. Service Integration catalog (PAN, Aadhaar e-KYC, GSTIN, DigiLocker, e-Sign, UPI). Inject any of these into the generated services."
            className="flex items-center gap-1 text-[11px] font-semibold text-[#2E2E38] bg-[#FFFCE6] hover:bg-[#FFE600] border border-[#FFE600] px-2 py-1 rounded-sm"
          >
            <Plug className="w-3 h-3" />
            Govt. Service Integration
            <ArrowRight className="w-3 h-3" />
          </button>
          <label className="flex items-center gap-1 text-[11px] text-[#2E2E38] cursor-pointer">
            <input
              type="checkbox"
              data-testid="svc-select-all"
              checked={allChecked}
              disabled={frozen || services.length === 0}
              onChange={(e) => onToggleAll?.(e.target.checked)}
              className="accent-[#FFE600]"
            />
            <span>Select all</span>
          </label>
          <span className="text-[#747480]" data-testid="svc-selected-count">
            {selectedCount}/{services.length} selected
          </span>
        </div>
      </div>
      {!frozen && selectedCount === 0 && services.length > 0 && (
        <div className="text-[11px] bg-amber-50 border border-amber-200 text-amber-800 rounded-sm p-2">
          No services selected — Approve is disabled. Tick at least one service to continue.
        </div>
      )}
      {/* iter-13.59 — Utilities panel slot (rendered from the page so
          state lives next to the Approve handler). */}
      {utilitiesSlot}
      {/* iter-14.33 — MULTI-GROUP MERGE PANEL.
          Replaces the old single "select-N-and-merge-into-one" bar. The
          operator can define multiple named groups (Project A = svc1+svc2,
          Project B = svc3+svc4+svc5, …) in a single screen, then apply
          them all in ONE batch call. Each service card gets a group
          selector; unassigned services are left untouched. */}
      {!frozen && services.length > 0 && (
        <div className="border border-emerald-300 bg-emerald-50 rounded-sm p-2 space-y-2" data-testid="merge-groups-panel">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-[11px] text-emerald-900 font-semibold">
              <Boxes className="w-3 h-3 text-emerald-700" />
              Merge Groups
              <span className="text-emerald-700 font-normal">
                — create N combined applications, one per group
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={addGroup}
                className="text-[11px] px-2 py-0.5 border border-emerald-300 text-emerald-800 hover:bg-emerald-100 rounded-sm font-semibold"
                data-testid="add-merge-group-btn"
                title="Create a new merge group"
              >
                + Add group
              </button>
              {groups.length > 0 && (
                <button
                  onClick={clearAllGroups}
                  className="text-[11px] px-2 py-0.5 border border-emerald-300 text-emerald-800 hover:bg-emerald-100 rounded-sm"
                  data-testid="clear-merge-groups-btn"
                >
                  Clear
                </button>
              )}
              <button
                onClick={applyGroups}
                disabled={!canApplyGroups}
                data-testid="apply-merge-groups-btn"
                title={groupErrors.length ? groupErrors.join("; ") : `Apply ${groups.length} merge group(s)`}
                className="text-[11px] px-2 py-0.5 bg-emerald-600 text-white rounded-sm font-bold disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
              >
                {mergeGroupsBusy ? <Loader2 className="w-3 h-3 animate-spin" /> : <GitBranch className="w-3 h-3" />}
                Apply {groups.length} group{groups.length === 1 ? "" : "s"}
              </button>
            </div>
          </div>
          {groups.length === 0 && (
            <div className="text-[11px] text-emerald-700 italic">
              No groups yet. Click <b>+ Add group</b>, name it (e.g. <code>billing-invoicing-svc</code>),
              then assign 2+ services to it via the <b>Group</b> dropdown on each service card below.
              Unassigned services stay as-is.
            </div>
          )}
          {groups.map((g) => (
            <div
              key={g.id}
              className="border border-emerald-200 bg-white rounded-sm p-2 flex flex-wrap items-center gap-2"
              data-testid={`merge-group-card-${g.id}`}
            >
              <span className="text-[10px] uppercase font-bold text-emerald-700 shrink-0">
                Group #{g.id}
              </span>
              <input
                type="text"
                placeholder="merged-name (e.g. billing-invoicing-svc)"
                value={g.name}
                onChange={(e) => renameGroup(g.id, e.target.value)}
                data-testid={`merge-group-name-${g.id}`}
                className="text-[11px] flex-1 min-w-[220px] border border-emerald-300 focus:border-emerald-600 outline-none rounded-sm px-1.5 py-0.5 font-mono"
              />
              <span className="text-[11px] text-emerald-800">
                {g.members.size} service{g.members.size === 1 ? "" : "s"}
                {g.members.size > 0 && `: ${[...g.members].slice(0, 3).join(", ")}${g.members.size > 3 ? ` +${g.members.size - 3}` : ""}`}
              </span>
              <button
                onClick={() => removeGroup(g.id)}
                className="text-[11px] text-red-700 hover:text-red-900 px-1"
                data-testid={`remove-merge-group-${g.id}`}
                title="Delete this group (services return to unassigned)"
              >
                ✕
              </button>
            </div>
          ))}
          {groupErrors.length > 0 && (
            <div className="text-[10px] bg-red-50 border border-red-200 text-red-800 rounded-sm p-1.5 space-y-0.5">
              {groupErrors.slice(0, 4).map((e, i) => <div key={i}>• {e}</div>)}
              {groupErrors.length > 4 && <div>… {groupErrors.length - 4} more</div>}
            </div>
          )}
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {services.map((s, i) => {
          const isSel = selected?.has(s.name) ?? true;
          const currentGid = assignedGroupId[s.name] || 0;
          return (
            <div
              key={i}
              className={`border rounded-sm p-2 bg-white transition-opacity ${isSel ? "border-[#E6E6E6]" : "border-dashed border-[#E6E6E6] opacity-60"} ${currentGid ? "ring-2 ring-emerald-400" : ""}`}
              data-testid={`service-card-${s.name}`}
            >
              <div className="flex items-center justify-between gap-2">
                <label className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer">
                  <input
                    type="checkbox"
                    data-testid={`svc-checkbox-${s.name}`}
                    checked={isSel}
                    disabled={frozen}
                    onChange={() => onToggle?.(s.name)}
                    className="accent-[#FFE600] shrink-0"
                  />
                  <div className="font-semibold text-[13px] truncate">{s.display_name || s.name || "(unnamed)"}</div>
                </label>
                <div className="flex items-center gap-1 shrink-0">
                  {/* iter-14.33 — Group picker per card. Only visible when the
                      operator has created ≥ 1 merge group. Assigning the
                      service adds it to that group's members set; picking
                      "Unassigned" removes it from any group it was in.
                      Utility / frontend rows and already-merged rows are
                      excluded from grouping. */}
                  {!frozen && groups.length > 0 && s.kind !== "utility" && !s.frontend && !(s.merged_from?.length > 0) && (
                    <select
                      value={currentGid || 0}
                      onChange={(e) => {
                        const gid = Number(e.target.value);
                        if (gid === 0) unassignSvc(s.name);
                        else assignSvcToGroup(s.name, gid);
                      }}
                      data-testid={`svc-group-picker-${s.name}`}
                      className={`text-[10px] border rounded-sm px-1 py-0.5 font-semibold ${currentGid ? "bg-emerald-100 border-emerald-400 text-emerald-900" : "bg-white border-[#E6E6E6] text-[#747480]"}`}
                      title="Assign this service to a merge group"
                    >
                      <option value={0}>Unassigned</option>
                      {groups.map((g) => (
                        <option key={g.id} value={g.id}>
                          → {g.name || `Group #${g.id}`}
                        </option>
                      ))}
                    </select>
                  )}
                  {/* iter-13.81.13 — surface merged-from provenance */}
                  {s.merged_from?.length > 0 && (
                    <span
                      className="text-[9px] uppercase bg-emerald-100 text-emerald-700 px-1 rounded-sm font-bold"
                      title={`Merged from: ${s.merged_from.join(", ")}`}
                      data-testid={`merged-badge-${s.name}`}
                    >
                      Merged ×{s.merged_from.length}
                    </span>
                  )}
                  {!frozen && s.merged_from?.length > 0 && (
                    <button
                      onClick={(e) => { e.preventDefault(); onUnmerge?.(s.name); }}
                      className="text-[9px] uppercase text-emerald-700 hover:text-red-700 px-1"
                      title="Delete this merged service. Re-run Recommend to repopulate the originals from KB."
                      data-testid={`unmerge-btn-${s.name}`}
                    >
                      Unmerge
                    </button>
                  )}
                  {/* iter-13.46 — show api_count alongside the language badge
                      so the user can see per-service surface size at a glance */}
                  {(s.api_count ?? s.route_count) > 0 && (
                    <span className="text-[10px] bg-[#FFE600] text-[#2E2E38] px-1.5 py-0.5 rounded-sm font-semibold">
                      {s.api_count ?? s.route_count} API{(s.api_count ?? s.route_count) === 1 ? '' : 's'}
                    </span>
                  )}
                  {/* iter-14.22 — API PARITY (1:1 legacy → target) badge.
                      Rendered whenever the backend attached a parity block
                      to the service. Green = full coverage; amber = <100%;
                      neutral = no legacy routes to mirror (e.g. utility
                      or auto-synth `legacy-crud` bucket).                */}
                  {s.parity && (
                    <span
                      data-testid={`service-parity-badge-${s.name}`}
                      title={
                        s.parity.legacy_endpoint_count > 0
                          ? `Parity: ${s.parity.coverage_pct}% — ` +
                            `${s.parity.covered}/${s.parity.legacy_endpoint_count} legacy endpoints covered` +
                            (s.parity.missing_count > 0
                              ? ` (missing ${s.parity.missing_count}: ${s.parity.missing.slice(0, 3).join(', ')}${s.parity.missing_count > 3 ? '…' : ''})`
                              : '')
                          : 'No legacy endpoints to mirror (synthetic / utility bucket)'
                      }
                      className={`text-[10px] px-1.5 py-0.5 rounded-sm font-semibold ${
                        s.parity.legacy_endpoint_count === 0
                          ? 'bg-[#F6F6FA] text-[#747480]'
                          : s.parity.pass
                            ? 'bg-emerald-100 text-emerald-800 border border-emerald-300'
                            : 'bg-amber-100 text-amber-800 border border-amber-300'
                      }`}
                    >
                      Parity: {s.parity.coverage_pct}%
                      {s.parity.missing_count > 0 && ` (–${s.parity.missing_count})`}
                    </span>
                  )}
                  <span className="text-[10px] uppercase bg-[#F6F6FA] px-1.5 py-0.5 rounded-sm">{s.backend_lang || "—"}</span>
                </div>
              </div>
              <div className="text-[11px] text-[#747480] mt-1">{s.description || s.responsibility}</div>
              {s.tables?.length > 0 && (
                <div className="text-[11px] mt-1"><span className="text-[#747480]">Tables:</span> {s.tables.slice(0, 6).join(", ")}{s.tables.length > 6 ? ` +${s.tables.length - 6}` : ""}</div>
              )}
              {s.api_endpoints?.length > 0 && (
                <div className="text-[11px] mt-0.5"><span className="text-[#747480]">Endpoints:</span> {s.api_endpoints.length}</div>
              )}
            </div>
          );
        })}
      </div>
      {data.event_bus && (
        <div className="text-[11px] text-[#2E2E38] bg-[#FFFCE6] border border-[#FFE600] rounded-sm p-2">
          Event bus enabled: {data.event_bus_type || "Kafka"}
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------
// Job poll hook
// -----------------------------------------------------------
function useJobPoll(getter) {
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const startId = useRef(null);
  useEffect(() => {
    if (!startId.current) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const j = await getter(startId.current);
        if (cancelled) return;
        setJob(j);
        if (j.status === "complete" || j.status === "error") {
          setRunning(false);
          return;
        }
      } catch (e) { /* ignore */ }
      if (!cancelled) setTimeout(tick, 2000);
    };
    setRunning(true);
    tick();
    return () => { cancelled = true; };
  }, [job?.id, getter]); // eslint-disable-line
  const start = (jid) => { startId.current = jid; setJob({ id: jid, status: "queued", step: "Starting…", pct: 0 }); setRunning(true); };
  return { job, running, start };
}

// -----------------------------------------------------------
// Main page
// -----------------------------------------------------------
export default function ArchitecturePage() {
  const navigate = useNavigate();
  const { active } = useProjects();
  const isMobile = useIsMobile();
  const projectId = active?.id;

  // Clean up any stray mermaid error SVGs that may have been injected into document.body
  // by previous failed renders (mermaid <11 quirk). Runs once on mount.
  useEffect(() => {
    document.querySelectorAll('body > svg[id^="dmermaid"], body > svg[aria-roledescription="error"]')
      .forEach((el) => el.remove());
  }, []);

  const [artifacts, setArtifacts] = useState([]);
  const [services, setServices] = useState([]);
  const [activeType, setActiveType] = useState("service_map");
  const [editing, setEditing] = useState(false);
  const [editBuf, setEditBuf] = useState("");
  const [chatMessages, setChatMessages] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [convId, setConvId] = useState(null);
  const [target, setTarget] = useState("all");
  const [resetOpen, setResetOpen] = useState(false);
  const [model] = useState("");  // iter-13.30: empty → Console resolves via AGENT_COMPLEXITY["arch.*"]

  // iter-13.57 — Per-service selection for the service-map approval step.
  // Default = every service checked. When the service_map artifact is
  // refreshed, we re-initialise to "all" — but preserve any explicit
  // user choices made before the refresh (idempotent re-fetches must
  // not unrelease the user's deselections).
  const [selectedSvcs, setSelectedSvcs] = useState(() => new Set());
  const [selectedTouched, setSelectedTouched] = useState(false);

  // iter-13.59 — Cross-cutting utilities (audit-logger etc.).
  // `utilities` = catalog from backend (with `enabled` + `config` per item).
  // `selectedUtils` = Set of utility keys currently checked.
  // `utilCfgs` = { [key]: { ...config } } edited form state.
  const [utilities, setUtilities] = useState([]);
  const [selectedUtils, setSelectedUtils] = useState(() => new Set());
  const [utilCfgs, setUtilCfgs] = useState({});

  // iter-13.81.13 — Merge UI state. Lets the user club 2+ checked
  // services into ONE combined service that CodeGen materialises as a
  // single source tree.
  const [mergeName, setMergeName] = useState("");
  const [mergeBusy, setMergeBusy] = useState(false);
  // iter-14.33 — Busy flag for the multi-group batch merge.
  const [mergeGroupsBusy, setMergeGroupsBusy] = useState(false);

  const status = active?.stage_status?.["Architecture"] || "locked";
  const dmStatus = active?.stage_status?.["DataModel"] || "locked";
  // iter-13.66 alignment — "skipped" unlocks downstream just like "frozen".
  const isLocked = dmStatus !== "frozen" && dmStatus !== "skipped";
  const isFrozen = status === "frozen";

  const recJob = useJobPoll(getArchJob);
  const hldJob = useJobPoll(getArchJob);
  const lldJob = useJobPoll(getArchJob);
  const seqJob = useJobPoll(getArchJob);
  const apiJob = useJobPoll(getArchJob);

  const refresh = async () => {
    if (!projectId) return;
    try {
      const data = await getArchArtifacts(projectId);
      setArtifacts(data.artifacts || []);
      setServices(data.services || []);
    } catch (e) { /* ignore */ }
  };

  useEffect(() => { refresh(); }, [projectId]);

  // iter-13.59 — load the cross-cutting utility catalog and seed
  // selection / config state from whatever is currently persisted as
  // arch_services rows with kind="utility".
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    listArchUtilities(projectId).then((r) => {
      if (cancelled) return;
      const list = r?.utilities || [];
      setUtilities(list);
      setSelectedUtils(new Set(list.filter((u) => u.enabled).map((u) => u.key)));
      setUtilCfgs(Object.fromEntries(list.map((u) => [u.key, { ...(u.config || {}) }])));
    }).catch(() => { /* ignore — panel just stays empty */ });
    return () => { cancelled = true; };
  }, [projectId, artifacts.length]);

  const toggleUtil = (key) => setSelectedUtils((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const changeUtilCfg = (key, field, value) =>
    setUtilCfgs((prev) => ({ ...prev, [key]: { ...(prev[key] || {}), [field]: value } }));

  // Re-fetch when any job completes
  useEffect(() => {
    if (recJob.job?.status === "complete" || hldJob.job?.status === "complete" || lldJob.job?.status === "complete" || seqJob.job?.status === "complete" || apiJob.job?.status === "complete") {
      refresh();
    }
    if (recJob.job?.status === "error") toast.error("Recommend failed: " + recJob.job.error);
    if (hldJob.job?.status === "error") toast.error("HLD generation failed: " + hldJob.job.error);
    if (lldJob.job?.status === "error") toast.error("LLD generation failed: " + lldJob.job.error);
    if (seqJob.job?.status === "error") toast.error("Sequence diagrams failed: " + seqJob.job.error);
    if (apiJob.job?.status === "error") toast.error("API contracts failed: " + apiJob.job.error);
    // iter-13.50 — surface per-section transport failures even when the
    // overall job "completed" (some sections fell through to model-error
    // text). Lets the user know to fix DNS/provider config and rerun.
    // iter-13.51 — also surface billing (402) / auth (401) failures with a
    // tailored message pointing at Console → Models.
    for (const j of [recJob.job, hldJob.job, lldJob.job, seqJob.job, apiJob.job]) {
      const errs = j?.section_errors || [];
      const grouped = errs.reduce((acc, e) => {
        const k = e.kind || "model";
        acc[k] = acc[k] || [];
        acc[k].push(e);
        return acc;
      }, {});
      const friendly = {
        transport: "network/DNS",
        billing:   "credits/billing (e.g. OpenRouter 402 — top up at https://openrouter.ai/settings/credits)",
        auth:      "auth (401 — invalid API key in Console → Models)",
      };
      for (const [kind, list] of Object.entries(grouped)) {
        if (!friendly[kind]) continue; // only toast fatal kinds
        const head = (list[0]?.error || "").slice(0, 220);
        toast.error(
          `${j.kind}: ${list.length} section(s) failed with ${friendly[kind]} — ${head}`,
          { duration: 12000 },
        );
      }
    }
  }, [recJob.job?.status, hldJob.job?.status, lldJob.job?.status, seqJob.job?.status, apiJob.job?.status]); // eslint-disable-line

  const activeArtifact = useMemo(
    () => artifacts.find((a) => a.type === activeType) || null,
    [artifacts, activeType]
  );

  // iter-13.57 — derive the canonical service-name list from the
  // service_map artifact and keep `selectedSvcs` in sync. On first
  // load (or after Reset / fresh Recommend) we default to "all
  // checked". Once the user has touched the checkboxes we only add
  // newly-discovered service names; we never re-enable a service the
  // user explicitly unchecked.
  const serviceNamesFromMap = useMemo(() => {
    const sm = artifacts.find((a) => a.type === "service_map");
    if (!sm) return [];
    try {
      const j = JSON.parse(sm.content || "{}");
      return (j.services || []).map((s) => s.name).filter(Boolean);
    } catch { return []; }
  }, [artifacts]);

  useEffect(() => {
    if (serviceNamesFromMap.length === 0) return;
    setSelectedSvcs((prev) => {
      if (!selectedTouched) {
        // Fresh state — select everything.
        return new Set(serviceNamesFromMap);
      }
      // User has touched; preserve their explicit deselections but
      // auto-select any new services that appeared (e.g. via chat
      // [SERVICE_ADD]).
      const next = new Set(prev);
      for (const n of serviceNamesFromMap) {
        if (!prev.has(n) && !prev.has(`__excluded:${n}`)) {
          next.add(n);
        }
      }
      return next;
    });
  }, [serviceNamesFromMap, selectedTouched]);

  const toggleSvc = (name) => {
    setSelectedTouched(true);
    setSelectedSvcs((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
        // remember the explicit deselection so a refresh doesn't auto-re-add
        next.add(`__excluded:${name}`);
      } else {
        next.add(name);
        next.delete(`__excluded:${name}`);
      }
      return next;
    });
  };

  const toggleAllSvcs = (checked) => {
    setSelectedTouched(true);
    if (checked) {
      setSelectedSvcs(new Set(serviceNamesFromMap));
    } else {
      // Mark every service as explicitly excluded.
      const next = new Set();
      for (const n of serviceNamesFromMap) next.add(`__excluded:${n}`);
      setSelectedSvcs(next);
    }
  };

  const selectedSvcNames = useMemo(
    () => serviceNamesFromMap.filter((n) => selectedSvcs.has(n)),
    [serviceNamesFromMap, selectedSvcs],
  );

  const startEdit = () => {
    if (!activeArtifact || activeArtifact.frozen) return;
    setEditBuf(activeArtifact.content || "");
    setEditing(true);
  };
  const saveEdit = async () => {
    if (!activeArtifact) return;
    try {
      await updateArchArtifact(projectId, activeArtifact.id, editBuf);
      toast.success("Saved");
      setEditing(false);
      await refresh();
    } catch (e) { toast.error("Save failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onFreeze = async (a) => {
    if (!a) return;
    try {
      await freezeArchArtifact(projectId, a.id);
      toast.success(`Froze ${ARTIFACT_META[a.type]?.label || a.type}`);
      await refresh();
    } catch (e) { toast.error("Freeze failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onApproveSm = async () => {
    if (selectedSvcNames.length === 0) {
      toast.error("Select at least one service to approve.");
      return;
    }
    try {
      // iter-13.59 — forward utility selection + per-utility config.
      const utilPayload = utilities
        .filter((u) => selectedUtils.has(u.key))
        .map((u) => ({ key: u.key, config: utilCfgs[u.key] || {} }));
      const r = await approveServiceMap(
        projectId, true, [], selectedSvcNames, utilPayload,
      );
      const pruned = r?.pruned_services || 0;
      const utilsCount = r?.utilities_persisted || 0;
      const stageFrozen = r?.stage_frozen;
      // iter-13.81.13 — Approving the Service Map IS the Architecture
      // stage freeze. HLD / LLD / Sequence / API Contracts are optional
      // deliverables; CodeGen unlocks as soon as the Service Map freezes.
      const msg = stageFrozen
        ? `Architecture frozen — CodeGen unlocked. ${selectedSvcNames.length} service(s) kept`
          + (pruned > 0 ? `, ${pruned} pruned` : "")
          + (utilsCount > 0 ? `, ${utilsCount} utility/utilities enabled.` : ".")
        : `Service map approved — ${selectedSvcNames.length} service(s) kept` +
          (pruned > 0 ? `, ${pruned} pruned` : "") +
          (utilsCount > 0 ? `, ${utilsCount} utility/utilities enabled.` : ".");
      toast.success(msg);
      await refresh();
    } catch (e) { toast.error("Approve failed: " + (e?.response?.data?.detail || e.message)); }
  };

  // iter-13.81.13 — Merge selected services into one combined application.
  const onMergeSelected = async () => {
    const targets = selectedSvcNames;  // already de-duped + excludes __excluded:* sentinels
    if (targets.length < 2) {
      toast.error("Select at least 2 services to merge.");
      return;
    }
    if (!/^[a-z0-9][a-z0-9-]{1,60}$/.test(mergeName)) {
      toast.error("Merged name must be lower-kebab-case (a-z, 0-9, -).");
      return;
    }
    setMergeBusy(true);
    try {
      const r = await mergeServices(projectId, targets, mergeName);
      toast.success(
        `Merged ${targets.length} services → ${r.merged?.name} ` +
        `(${r.merged?.api_count} APIs, ${r.merged?.table_count} tables). ` +
        `CodeGen will produce ONE source tree.`,
      );
      setMergeName("");
      setSelectedSvcs(new Set());
      setSelectedTouched(false);
      await refresh();
    } catch (e) {
      toast.error("Merge failed: " + (e?.response?.data?.detail || e.message));
    } finally { setMergeBusy(false); }
  };

  // iter-14.33 — Apply N named merge-groups in one call. `groups` is an
  // array of { merged_name, service_names } produced by ServiceMapView's
  // multi-group panel. Backend validates disjointness + kebab-case + row
  // existence upfront, then applies each group atomically.
  const onMergeGroups = async (groupsPayload) => {
    if (!Array.isArray(groupsPayload) || groupsPayload.length === 0) {
      toast.error("No groups to apply.");
      return;
    }
    setMergeGroupsBusy(true);
    try {
      const r = await mergeServicesBatch(projectId, groupsPayload);
      const names = (r.merged || []).map((m) => m.name).join(", ");
      toast.success(
        `Applied ${r.groups} merge group${r.groups === 1 ? "" : "s"}: ${names}. ` +
        `CodeGen will produce one source tree per group.`,
      );
      setSelectedSvcs(new Set());
      setSelectedTouched(false);
      await refresh();
    } catch (e) {
      toast.error("Batch merge failed: " + (e?.response?.data?.detail || e.message));
    } finally { setMergeGroupsBusy(false); }
  };

  const onUnmergeService = async (name) => {
    if (!window.confirm(
      `Delete merged service "${name}"?\n\nRe-run Recommend afterwards to repopulate the original split from KB.`,
    )) return;
    try {
      const r = await unmergeService(projectId, name);
      toast.success(`Unmerged ${r.deleted} — ${r.note}`);
      setSelectedSvcs(new Set());
      setSelectedTouched(false);
      await refresh();
    } catch (e) {
      toast.error("Unmerge failed: " + (e?.response?.data?.detail || e.message));
    }
  };

  const onRecommend = async () => {
    try {
      const r = await startArchRecommend(projectId, model, "");
      recJob.start(r.job_id);
      toast.message("Architecture recommendation started");
    } catch (e) { toast.error("Could not start: " + (e?.response?.data?.detail || e.message)); }
  };
  const onHld = async () => {
    const sm = artifacts.find((a) => a.type === "service_map");
    if (!sm || !sm.frozen) { toast.message("Approve the service map first."); return; }
    try { const r = await startArchHld(projectId, model); hldJob.start(r.job_id); }
    catch (e) { toast.error("Could not start HLD: " + (e?.response?.data?.detail || e.message)); }
  };
  const onLld = async () => {
    try { const r = await startArchLld(projectId, model); lldJob.start(r.job_id); }
    catch (e) { toast.error("Could not start LLD: " + (e?.response?.data?.detail || e.message)); }
  };
  const onSeq = async () => {
    try { const r = await startArchSequence(projectId, model); seqJob.start(r.job_id); }
    catch (e) { toast.error("Could not start sequence: " + (e?.response?.data?.detail || e.message)); }
  };
  const onApiContracts = async () => {
    try { const r = await startArchApiContracts(projectId, model); apiJob.start(r.job_id); }
    catch (e) { toast.error("Could not start API contracts: " + (e?.response?.data?.detail || e.message)); }
  };

  const onSendChat = async () => {
    const m = chatInput.trim();
    if (!m) return;
    setChatBusy(true);
    setChatMessages((p) => [...p, { role: "user", content: m }]);
    setChatInput("");
    try {
      const r = await sendArchChat({ project_id: projectId, message: m, conversation_id: convId, target_artifact: target });
      setConvId(r.conversation_id);
      setChatMessages((p) => [...p, { role: "assistant", content: r.content, changes: r.changes || [], message_id: r.message_id }]);
    } catch (e) {
      toast.error("Chat failed: " + (e?.response?.data?.detail || e.message));
    } finally { setChatBusy(false); }
  };

  const onApplyChanges = async (changes, msgId) => {
    try {
      const r = await applyArchChanges(projectId, changes, msgId);
      toast.success(`Applied ${r.updated?.length || 0} change(s)`);
      await refresh();
    } catch (e) { toast.error("Apply failed: " + (e?.response?.data?.detail || e.message)); }
  };

  const onReset = async () => {
    try {
      await resetArch(projectId);
      toast.success("Stage 3 reset");
      setResetOpen(false);
      setArtifacts([]); setServices([]); setChatMessages([]); setConvId(null);
      setSelectedSvcs(new Set()); setSelectedTouched(false);
      setSelectedUtils(new Set()); setUtilCfgs({});
    } catch (e) { toast.error("Reset failed"); }
  };

  // iter-13.50 — Selective cleanup: delete artifacts whose body is just
  // baked-in transport / DNS error text (from before the iter-13.50 fix).
  // Preserves an approved service_map and any frozen artifacts.
  const onPurgeBroken = async () => {
    try {
      const r = await purgeBrokenArch(projectId);
      if (r.count > 0) {
        toast.success(
          `Purged ${r.count} broken artifact(s): ${r.deleted.map((d) => d.type).join(", ")}. ` +
          `Re-run the corresponding Generate button.`,
        );
      } else {
        toast.message("No broken artifacts found.");
      }
      await refresh();
    } catch (e) {
      toast.error("Purge failed: " + (e?.response?.data?.detail || e.message));
    }
  };

  if (!projectId) return (
    <EmptyState
      icon={FolderOpen}
      title="No project selected"
      description="Create or open a project from the sidebar to design your architecture."
      action={
        <button
          type="button"
          onClick={() => navigate("/")}
          className="text-xs px-3 py-1.5 bg-[#FFE600] text-[#2E2E38] rounded-sm hover:bg-yellow-300 font-semibold focus:outline-none focus:ring-2 focus:ring-[#2E2E38]"
          data-testid="empty-goto-discovery"
        >
          Go to Discovery →
        </button>
      }
    />
  );

  // Locked view
  if (isLocked) {
    return (
      <div className="flex-1 flex flex-col bg-[#F6F6FA]" data-testid="arch-locked">
        <header className="bg-white border-b-2 border-[#FFE600] px-6 py-3">
          <div className="text-[10px] uppercase tracking-widest text-[#747480]">Stage 3 of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38]">Architecture</h1>
        </header>
        <div className="flex-1 flex items-center justify-center p-8">
          <div className="max-w-md bg-white border border-[#E6E6E6] rounded-sm p-6 text-center">
            <Lock className="w-8 h-8 mx-auto text-[#747480] mb-3" />
            <h2 className="font-display font-bold text-[#2E2E38]">Locked — DataModel not frozen</h2>
            <p className="text-xs text-[#747480] mt-2">Freeze both OLTP and OLAP DDLs in Stage 2 to unlock Architecture.</p>
            <button onClick={() => navigate("/data-model")} className="mt-4 text-xs px-3 py-1.5 bg-[#2E2E38] text-white rounded-sm">Open DataModel →</button>
          </div>
        </div>
      </div>
    );
  }

  const tabs = [
    { type: "service_map", icon: Network, label: "Service Map", required: true },
    // iter-13.81.13 — HLD / LLD / Sequence / API Contracts are OPTIONAL.
    // Service Map approval freezes the Architecture stage and unlocks CodeGen.
    { type: "hld", icon: Boxes, label: "HLD", optional: true },
    { type: "lld", icon: FileText, label: "LLD", optional: true },
    { type: "sequence_diagrams", icon: Workflow, label: "Sequence", optional: true },
    { type: "api_contracts", icon: GitBranch, label: "API Contracts", optional: true },
  ];

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-[#F6F6FA]" data-testid="arch-page">
      <header className="bg-white border-b-2 border-[#FFE600] px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-[#747480]">Stage 3 of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38]">Architecture</h1>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-[#747480]">{services.length} services · {artifacts.length} artifacts</span>
          {isFrozen && <span className="text-[10px] uppercase font-bold bg-[#FFE600] text-[#2E2E38] px-2 py-0.5 rounded-sm">Frozen</span>}
          <button onClick={onPurgeBroken} data-testid="arch-purge-broken-btn" title="Delete artifacts whose body is just a baked-in DNS / network error from a previous failed run." className="text-xs px-2 py-1 border border-amber-300 text-amber-700 rounded-sm hover:bg-amber-50">
            Purge broken
          </button>
          <button onClick={() => setResetOpen(true)} data-testid="arch-reset-btn" className="text-xs px-2 py-1 border border-orange-300 text-orange-700 rounded-sm flex items-center gap-1 hover:bg-orange-50">
            <RotateCcw className="w-3 h-3" /> Reset
          </button>
        </div>
      </header>

      <div className="flex-1 min-h-0">
        <PanelGroup
          key={isMobile ? "v" : "h"}
          direction={isMobile ? "vertical" : "horizontal"}
        >
          {/* LEFT: actions + chat */}
          <Panel defaultSize={30} minSize={22}>
            <div className="h-full bg-white border-r border-[#E6E6E6] flex flex-col">
              <div className="px-3 py-2 border-b border-[#E6E6E6]">
                <div className="mos-label mb-1">Generate</div>
                <div className="grid grid-cols-2 gap-1">
                  <Button data-testid="btn-recommend" onClick={onRecommend} disabled={recJob.running} className="h-8 text-[11px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]">
                    {recJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />} Recommend
                  </Button>
                  <Button data-testid="btn-hld" onClick={onHld} disabled={hldJob.running} className="h-8 text-[11px]" variant="outline">
                    {hldJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Boxes className="w-3 h-3" />} HLD
                  </Button>
                  <Button data-testid="btn-lld" onClick={onLld} disabled={lldJob.running} className="h-8 text-[11px]" variant="outline">
                    {lldJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <FileText className="w-3 h-3" />} LLD
                  </Button>
                  <Button data-testid="btn-seq" onClick={onSeq} disabled={seqJob.running} className="h-8 text-[11px]" variant="outline">
                    {seqJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <Workflow className="w-3 h-3" />} Sequence
                  </Button>
                  <Button data-testid="btn-api-contracts" onClick={onApiContracts} disabled={apiJob.running} className="h-8 text-[11px] col-span-2" variant="outline">
                    {apiJob.running ? <Loader2 className="w-3 h-3 animate-spin" /> : <GitBranch className="w-3 h-3" />} API Contracts (OpenAPI 3.1)
                  </Button>
                </div>
                <div className="mt-2 space-y-1">
                  {[{ j: recJob, k: "rec" }, { j: hldJob, k: "hld" }, { j: lldJob, k: "lld" }, { j: seqJob, k: "seq" }, { j: apiJob, k: "api" }].filter(x => x.j.job).map(({ j, k }) => (
                    <div key={k} data-testid={`job-${k}`} className="text-[10px]">
                      <div className="flex items-center justify-between">
                        <span className="text-[#747480] truncate">{j.job.kind}: {j.job.step}</span>
                        <span className="text-[#2E2E38] font-semibold">{j.job.pct || 0}%</span>
                      </div>
                      <div className="h-1 bg-[#F6F6FA] rounded-sm overflow-hidden">
                        <div className="h-full bg-[#FFE600]" style={{ width: `${j.job.pct || 0}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2">
                <div className="mos-label">Chat target</div>
                <select data-testid="chat-target" value={target} onChange={(e) => setTarget(e.target.value)} className="text-[11px] border border-[#E6E6E6] rounded-sm px-1 py-0.5">
                  <option value="all">All</option>
                  <option value="hld">HLD</option>
                  <option value="lld">LLD</option>
                  <option value="service_map">Service Map</option>
                  <option value="api_contracts">API Contracts</option>
                </select>
              </div>

              <div className="flex-1 overflow-y-auto mos-scroll p-3 space-y-2" data-testid="arch-chat-log">
                {chatMessages.length === 0 && (
                  <div className="text-[11px] text-[#747480]">Ask the architect-LLM to refine services, modify HLD sections, or update LLDs. The LLM may emit `[HLD_CHANGE:section]…[/HLD_CHANGE]`, `[ARCH_CHANGE:service]…[/ARCH_CHANGE]`, or `[SERVICE_ADD]…[/SERVICE_ADD]` — review and click Apply.</div>
                )}
                {chatMessages.map((m, i) => (
                  <div key={i} className={`text-[12px] p-2 rounded-sm ${m.role === "user" ? "bg-[#FFFCE6] border border-[#FFE600]" : "bg-[#F6F6FA] border border-[#E6E6E6]"}`}>
                    <div className="text-[9px] uppercase font-bold text-[#747480] mb-1">{m.role}</div>
                    <pre className="whitespace-pre-wrap text-[12px] leading-snug text-[#2E2E38]">{m.content}</pre>
                    {m.changes?.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        <button onClick={() => onApplyChanges(m.changes, m.message_id)} data-testid={`apply-changes-${i}`} className="text-[10px] px-2 py-0.5 bg-[#2E2E38] text-white rounded-sm">
                          Apply {m.changes.length} change(s)
                        </button>
                        {m.changes.map((c, j) => (
                          <span key={j} className="text-[9px] uppercase bg-white border border-[#E6E6E6] px-1 py-0.5 rounded-sm">{c.type}{c.target ? `:${c.target}` : ""}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {chatBusy && <div className="text-[11px] text-[#747480] flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> Thinking…</div>}
              </div>

              <div className="border-t border-[#E6E6E6] p-2 flex gap-1">
                <textarea
                  rows={2}
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSendChat(); } }}
                  placeholder="Refine architecture…"
                  data-testid="arch-chat-input"
                  className="flex-1 text-[12px] border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm px-2 py-1.5 resize-none"
                />
                <Button data-testid="arch-chat-send" onClick={onSendChat} disabled={chatBusy} className="h-auto bg-[#2E2E38] text-white px-3"><Send className="w-3 h-3" /></Button>
              </div>
            </div>
          </Panel>

          <PanelResizeHandle className="w-1 bg-[#E6E6E6] hover:bg-[#FFE600]" />

          {/* RIGHT: artifact viewer */}
          <Panel defaultSize={70}>
            <div className="h-full flex flex-col bg-white">
              {/* Tabs */}
              <div className="flex items-center border-b border-[#E6E6E6] px-2">
                {tabs.map((t) => {
                  const a = artifacts.find((x) => x.type === t.type);
                  const Icon = t.icon;
                  const isActive = activeType === t.type;
                  return (
                    <button
                      key={t.type}
                      data-testid={`tab-${t.type}`}
                      onClick={() => { setActiveType(t.type); setEditing(false); }}
                      className={`flex items-center gap-1 text-[12px] px-2.5 py-2 border-b-2 ${isActive ? "border-[#FFE600] text-[#2E2E38] font-semibold" : "border-transparent text-[#747480] hover:text-[#2E2E38]"}`}
                      title={t.optional ? "Optional — not required to unlock CodeGen" : undefined}
                    >
                      <Icon className="w-3 h-3" /> {t.label}
                      {t.optional && (
                        <span
                          className="text-[8px] uppercase font-bold text-[#B0B0B8] bg-[#F6F6FA] px-1 rounded-sm"
                          data-testid={`tab-${t.type}-optional`}
                        >
                          opt
                        </span>
                      )}
                      {a?.frozen && <Lock className="w-3 h-3 text-[#FFE600]" />}
                      {a && !a.frozen && <span className="text-[9px] bg-[#F6F6FA] px-1 rounded-sm">v{a.version}</span>}
                    </button>
                  );
                })}
                <div className="ml-auto flex items-center gap-1 py-1">
                  {activeArtifact && !activeArtifact.frozen && !editing && (
                    <>
                      <button onClick={startEdit} data-testid="edit-artifact" className="text-[11px] px-2 py-1 border border-[#E6E6E6] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1"><Pencil className="w-3 h-3" /> Edit</button>
                      {activeType === "service_map" ? (
                        <button
                          onClick={onApproveSm}
                          disabled={selectedSvcNames.length === 0}
                          data-testid="approve-service-map"
                          title="Approve the service map AND freeze the Architecture stage. HLD / LLD / Sequence / API Contracts are optional and can be generated later."
                          className={`text-[11px] px-2 py-1 rounded-sm font-bold flex items-center gap-1 ${selectedSvcNames.length === 0 ? "bg-[#FFF3A1] text-[#9A9A9A] cursor-not-allowed" : "bg-[#FFE600] text-[#2E2E38]"}`}
                        >
                          <Lock className="w-3 h-3" />
                          Approve &amp; Freeze{selectedSvcNames.length > 0 ? ` (${selectedSvcNames.length})` : ""}
                        </button>
                      ) : (
                        <>
                          {/* iter-13.71 — per-stage Accuracy / Confidence badge */}
                          <ConfidenceBadge projectId={projectId} stage="Architecture" compact />
                          <button onClick={() => onFreeze(activeArtifact)} data-testid="freeze-artifact" className="text-[11px] px-2 py-1 bg-[#2E2E38] text-white rounded-sm flex items-center gap-1"><Lock className="w-3 h-3" /> Freeze</button>
                        </>
                      )}
                    </>
                  )}
                  {activeArtifact && (
                    <>
                      {/* iter-13.71 — confidence badge visible on
                          frozen artifacts so reviewers / super-admins
                          can audit already-frozen old projects. */}
                      {activeArtifact.frozen && activeType !== "service_map" && (
                        <ConfidenceBadge projectId={projectId} stage="Architecture" compact />
                      )}
                      <a href={downloadArchArtifactUrl(projectId, activeArtifact.id)} data-testid="download-artifact" title="Download raw source (markdown / YAML / JSON)" className="text-[11px] px-2 py-1 border border-[#E6E6E6] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1"><Download className="w-3 h-3" /></a>
                      {/* iter-14.25.12 — PDF export for HLD / LLD /
                          Sequence / API Contracts / Service Map. */}
                      <a href={downloadArchArtifactPdfUrl(projectId, activeArtifact.id)} data-testid="download-artifact-pdf" title="Download as PDF (business review format)" className="text-[11px] px-2 py-1 border border-[#E6E6E6] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1"><Download className="w-3 h-3" /> PDF</a>
                    </>
                  )}
                </div>
              </div>

              {/* Body */}
              <div className="flex-1 overflow-y-auto mos-scroll p-4" data-testid={`artifact-body-${activeType}`}>
                {!activeArtifact && (
                  <div className="text-center text-[#747480] mt-12">
                    <Sparkles className="w-8 h-8 mx-auto mb-2 text-[#FFE600]" />
                    <div className="text-sm font-semibold">No {ARTIFACT_META[activeType]?.label || activeType} yet</div>
                    <div className="text-xs mt-1">Use Generate buttons on the left.</div>
                  </div>
                )}
                {editing && activeArtifact && (
                  <div className="flex flex-col gap-2 h-full">
                    <textarea
                      value={editBuf}
                      onChange={(e) => setEditBuf(e.target.value)}
                      data-testid="edit-textarea"
                      className="flex-1 min-h-[300px] text-[12px] font-mono border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm p-2"
                    />
                    <div className="flex justify-end gap-2">
                      <button onClick={() => setEditing(false)} className="text-[11px] px-3 py-1 border border-[#E6E6E6] rounded-sm">Cancel</button>
                      <button onClick={saveEdit} data-testid="save-edit" className="text-[11px] px-3 py-1 bg-[#2E2E38] text-white rounded-sm">Save</button>
                    </div>
                  </div>
                )}
                {!editing && activeArtifact && activeType === "service_map" && (
                  <ServiceMapView
                    artifact={activeArtifact}
                    selected={selectedSvcs}
                    onToggle={toggleSvc}
                    onToggleAll={toggleAllSvcs}
                    frozen={!!activeArtifact.frozen}
                    onMerge={onMergeSelected}
                    onUnmerge={onUnmergeService}
                    mergeName={mergeName}
                    setMergeName={setMergeName}
                    mergeBusy={mergeBusy}
                    onMergeGroups={onMergeGroups}
                    mergeGroupsBusy={mergeGroupsBusy}
                    utilitiesSlot={
                      <UtilitiesPanel
                        utilities={utilities}
                        selected={selectedUtils}
                        configs={utilCfgs}
                        onToggle={toggleUtil}
                        onConfigChange={changeUtilCfg}
                        frozen={!!activeArtifact.frozen}
                      />
                    }
                  />
                )}
                {!editing && activeArtifact && activeType !== "service_map" && activeType !== "api_contracts" && (
                  <MarkdownWithMermaid source={activeArtifact.content} idPrefix={activeType} />
                )}
                {!editing && activeArtifact && activeType === "api_contracts" && (
                  <ApiContractsView source={activeArtifact.content} projectId={projectId} />
                )}
              </div>
            </div>
          </Panel>
        </PanelGroup>
      </div>

      <ResetModal
        open={resetOpen}
        onClose={() => setResetOpen(false)}
        onConfirm={onReset}
        title="Reset Stage 3 — Architecture"
        warning="Removes all architecture artifacts (service map, HLD, LLD, sequence, contracts) and unlocks Architecture for re-generation. Stage 4 / Stage 5 contexts will also be cleared."
      />
    </div>
  );
}
