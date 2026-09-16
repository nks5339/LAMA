import { useEffect, useRef, useState, useCallback } from "react";
import { Terminal, ChevronDown, Copy, Check, X } from "lucide-react";
import { getTransformationLogs } from "../lib/api";

/**
 * iter-15.16 — TransformerTelemetry
 * ─────────────────────────────────
 * Live worker-log drawer for the Transformer page.
 *
 *   • Polls GET /api/tools/transformer/{id}/logs?since=<iso> every 1.5s.
 *   • Renders as a slim bottom drawer (fixed) so the code viewer keeps
 *     its full-height layout from iter-15.14 / 15.15.
 *   • Auto-scrolls to newest line unless the user manually scrolled up;
 *     shows a "Jump to latest" pill in that case.
 *   • Persists open/closed in localStorage("lama:transformer:telemetry:open").
 *   • Client-side ring cap = 500 lines.
 *
 * Contract testids:
 *   transformer-telemetry-drawer, transformer-telemetry-toggle,
 *   transformer-telemetry-copy, transformer-telemetry-line-{i}
 *
 * NOTE: This component is deliberately NOT MiniConsole — MiniConsole
 * is the global console tile; this one is per-transformation.
 */

const CLIENT_MAX = 500;
const POLL_MS = 1500;
const LS_KEY = "lama:transformer:telemetry:open";

const LEVEL_COLORS = {
  info: "text-fg-subtle",
  warn: "text-amber-400",
  error: "text-red-400",
  llm: "text-violet-300",
};

const LEVEL_BADGE = {
  info: "bg-ink text-fg-onDark",
  warn: "bg-amber-900/60 text-amber-300",
  error: "bg-red-900/60 text-red-300",
  llm: "bg-violet-900/60 text-violet-300",
};

function formatTs(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  } catch {
    return "";
  }
}

export default function TransformerTelemetry({ transformId, isRunning }) {
  const [open, setOpen] = useState(() => {
    try {
      const saved = window.localStorage.getItem(LS_KEY);
      if (saved === "1") return true;
      if (saved === "0") return false;
      return !!isRunning;
    } catch {
      return !!isRunning;
    }
  });
  const [lines, setLines] = useState([]);
  const [copied, setCopied] = useState(false);
  const [autoscroll, setAutoscroll] = useState(true);
  const lastTsRef = useRef(null);
  const scrollRef = useRef(null);
  const linesRef = useRef([]);

  // keep ref in sync so the poll callback isn't stale
  useEffect(() => { linesRef.current = lines; }, [lines]);

  // Persist open/closed
  useEffect(() => {
    try {
      window.localStorage.setItem(LS_KEY, open ? "1" : "0");
    } catch {
    // localStorage/CustomEvent may be unavailable (private mode,
    // blocked site data). The feature degrades; it never fails.
    }
  }, [open]);

  // Reset buffer when the transformation changes (fresh session)
  useEffect(() => {
    setLines([]);
    lastTsRef.current = null;
  }, [transformId]);

  useEffect(() => {
    try {
      if (!window.localStorage.getItem(LS_KEY) && isRunning) {
        setOpen(true);
      }
    } catch {
    // localStorage/CustomEvent may be unavailable (private mode,
    // blocked site data). The feature degrades; it never fails.
    }
  }, [isRunning]);

  // Poll loop
  useEffect(() => {
    if (!transformId) return undefined;
    let cancelled = false;
    let timer = null;

    const tick = async () => {
      if (cancelled) return;
      try {
        const data = await getTransformationLogs(transformId, {
          since: lastTsRef.current,
          limit: 200,
        });
        const rows = Array.isArray(data?.logs) ? data.logs : [];
        if (rows.length) {
          setLines((prev) => {
            const merged = prev.concat(rows);
            return merged.length > CLIENT_MAX
              ? merged.slice(merged.length - CLIENT_MAX)
              : merged;
          });
          const newest = rows[rows.length - 1]?.ts;
          if (newest) lastTsRef.current = newest;
        }
      } catch (_e) {
        // transient — keep polling
      }
      if (!cancelled) timer = setTimeout(tick, POLL_MS);
    };

    // fire immediately, then loop
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [transformId]);

  // Auto-scroll to bottom on new lines (unless user scrolled up)
  useEffect(() => {
    if (!open || !autoscroll) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, open, autoscroll]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setAutoscroll(nearBottom);
  }, []);

  const handleCopy = useCallback(async () => {
    try {
      const text = lines
        .map((l) => `${formatTs(l.ts)} [${(l.level || "info").toUpperCase()}] ${l.msg || ""}`)
        .join("\n");
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (_e) {
    // One failed poll tick is not an error — the next tick retries,
    // and a toast every 2s during a network blip would be worse.
    }
  }, [lines]);

  const jumpToLatest = useCallback(() => {
    setAutoscroll(true);
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  if (!transformId) return null;

  // Collapsed pill (bottom-left toggle) — always mounted so the user
  // can open the drawer even when idle to inspect the previous run.
  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-3 left-3 z-40 h-8 px-3 rounded-full bg-ink text-fg-onDark shadow-lg hover:bg-ink flex items-center gap-1.5 text-micro font-medium border border-border-strong"
        data-testid="transformer-telemetry-toggle"
        title="Show live worker logs"
      >
        <Terminal size={12} />
        Live Telemetry
        {lines.length > 0 && (
          <span className="ml-1 px-1.5 py-0.5 rounded bg-ink text-micro tabular-nums">
            {lines.length}
          </span>
        )}
        {isRunning && (
          <span className="ml-1 w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
        )}
      </button>
    );
  }

  return (
    <div
      className="fixed bottom-0 left-0 right-0 z-40 bg-ink border-t border-border-strong shadow-2xl flex flex-col"
      style={{ height: "240px" }}
      data-testid="transformer-telemetry-drawer"
    >
      {/* Header */}
      <div className="h-8 flex-shrink-0 px-3 flex items-center justify-between bg-ink border-b border-border-strong">
        <div className="flex items-center gap-2">
          <Terminal size={12} className="text-fg-subtle" />
          <span className="text-micro font-semibold text-fg-onDark">Live Telemetry</span>
          <span className="text-micro text-fg-subtle tabular-nums">
            {lines.length} log{lines.length === 1 ? "" : "s"}
          </span>
          <span className="flex items-center gap-1 text-micro">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                isRunning ? "bg-emerald-400 animate-pulse" : "bg-fg-muted"
              }`}
            />
            <span className={isRunning ? "text-emerald-400" : "text-fg-subtle"}>
              {isRunning ? "Live" : "Idle"}
            </span>
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            className="h-6 px-2 rounded hover:bg-ink hover:text-fg-onDark flex items-center gap-1 text-micro text-fg-muted"
            data-testid="transformer-telemetry-copy"
            title="Copy all logs"
          >
            {copied ? <Check size={11} className="text-emerald-400" /> : <Copy size={11} />}
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            onClick={() => setOpen(false)}
            className="h-6 w-6 rounded hover:bg-ink hover:text-fg-onDark flex items-center justify-center text-fg-muted"
            title="Hide telemetry"
          >
            <X size={12} />
          </button>
        </div>
      </div>

      {/* Log body */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="flex-1 overflow-y-auto font-mono text-micro leading-[1.45] px-3 py-2 bg-ink"
      >
        {lines.length === 0 ? (
          <div className="text-fg-muted italic">
            Waiting for worker output…
          </div>
        ) : (
          lines.map((l, i) => {
            const lvl = l.level || "info";
            return (
              <div
                key={`${l.ts}-${i}`}
                data-testid={`transformer-telemetry-line-${i}`}
                className="flex items-start gap-2 whitespace-pre-wrap break-words"
              >
                <span className="text-fg-muted tabular-nums shrink-0">
                  {formatTs(l.ts)}
                </span>
                <span
                  className={`shrink-0 px-1 rounded text-micro font-bold uppercase tracking-wider ${LEVEL_BADGE[lvl] || LEVEL_BADGE.info}`}
                >
                  {lvl}
                </span>
                <span className={LEVEL_COLORS[lvl] || LEVEL_COLORS.info}>
                  {l.msg}
                </span>
              </div>
            );
          })
        )}
      </div>

      {!autoscroll && (
        <button
          onClick={jumpToLatest}
          className="absolute bottom-3 right-3 h-6 px-2 rounded-full bg-violet-600 text-white shadow-lg hover:bg-violet-500 flex items-center gap-1 text-micro font-semibold"
        >
          <ChevronDown size={11} />
          Jump to latest
        </button>
      )}
    </div>
  );
}
