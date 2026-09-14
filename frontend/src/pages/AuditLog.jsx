import { useEffect, useState } from "react";
import { listAudit, getAuditTrace } from "@/lib/api";
import { useProjects } from "@/state/ProjectContext";
import { Activity, FileSearch, Copy, CheckCircle2, AlertTriangle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";

// iter-13.101 — Detail Log Trace dialog.
// Maps the raw llm_traces doc into the contract shape the user asked for:
//   { request_prompt, response_prompt, request_time, response_time,
//     elapsed_ms, stage, agent_key, status, error_reason, ... }
function shapeTraceForDisplay(t) {
  if (!t) return null;
  return {
    trace_id:        t.trace_id,
    project_id:      t.project_id || "",
    stage:           t.stage || "",
    agent_key:       t.agent_key || "",
    status:          t.status || "",
    error_reason:    t.error_reason || "",
    request_time:    t.request_time || "",
    response_time:   t.response_time || "",
    elapsed_ms:      typeof t.elapsed_ms === "number" ? t.elapsed_ms : null,
    request_prompt:  t.request_prompt || [],
    response_prompt: t.response_prompt || "",
    response_model:  t.response_model || "",
    response_usage:  t.response_usage || null,
    kwargs:          t.kwargs || {},
  };
}

function TraceDialog({ open, onClose, traceId, fallbackRow }) {
  const [loading, setLoading] = useState(false);
  const [trace, setTrace]     = useState(null);
  const [error, setError]     = useState("");
  const [copied, setCopied]   = useState(false);

  useEffect(() => {
    if (!open) return;
    setError(""); setTrace(null); setCopied(false);
    if (!traceId) {
      // iter-13.101 — Legacy audit row with no llm_traces pointer.
      // Surface the row's own details JSON so the button is still useful
      // and the user can see WHY there's no rich trace (the row pre-dates
      // the wrapper or came from a manual audit_log.insert_one elsewhere).
      setTrace({
        trace_id:        "",
        project_id:      fallbackRow?.project_id || "",
        stage:           fallbackRow?.details?.stage || "",
        agent_key:       fallbackRow?.details?.agent_key || fallbackRow?.action || "",
        status:          fallbackRow?.details?.status || "N/A",
        error_reason:    fallbackRow?.details?.error || "",
        request_time:    "",
        response_time:   fallbackRow?.at || "",
        elapsed_ms:      fallbackRow?.details?.elapsed_ms ?? null,
        request_prompt:  [],
        response_prompt: "",
        response_model:  fallbackRow?.details?.model || "",
        response_usage:  null,
        kwargs:          {},
        legacy_audit:    fallbackRow || null,
        _note: "No llm_traces entry for this row — showing the original audit details. " +
               "Rich traces (full request/response prompts) appear on audit rows produced " +
               "AFTER the backend was restarted with iter-13.101.",
      });
      return;
    }
    setLoading(true);
    getAuditTrace(traceId)
      .then((d) => setTrace(shapeTraceForDisplay(d)))
      .catch((e) => setError(e?.response?.data?.detail || e?.message || "Failed to load trace"))
      .finally(() => setLoading(false));
  }, [open, traceId, fallbackRow]);

  const json = trace ? JSON.stringify(trace, null, 2) : "";
  const copyJson = async () => {
    try {
      await navigator.clipboard.writeText(json);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (_) {/* ignore */}
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent
        className="w-[min(95vw,56rem)] max-w-[95vw] max-h-[90vh] overflow-hidden p-4 sm:p-6"
        data-testid="audit-trace-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FileSearch className="w-4 h-4 text-slate-500" />
            Detail Log Trace
          </DialogTitle>
          <DialogDescription>
            Full request / response envelope captured by the LLM fabric.
          </DialogDescription>
        </DialogHeader>

        {loading && (
          <div className="text-sm text-slate-500 py-8 text-center">Loading…</div>
        )}

        {error && (
          <div className="text-sm text-rose-600 py-4 flex items-center gap-2" data-testid="audit-trace-error">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}

        {trace && !loading && !error && (
          <div className="space-y-3 min-w-0 overflow-hidden">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] min-w-0">
              <div className="bg-slate-50 rounded px-2 py-1 min-w-0">
                <div className="text-slate-500">Stage</div>
                <div className="font-mono text-[#2E2E38] truncate" data-testid="audit-trace-stage">{trace.stage || "—"}</div>
              </div>
              <div className="bg-slate-50 rounded px-2 py-1 min-w-0">
                <div className="text-slate-500">Agent</div>
                <div className="font-mono text-[#2E2E38] truncate" data-testid="audit-trace-agent">{trace.agent_key || "—"}</div>
              </div>
              <div className="bg-slate-50 rounded px-2 py-1 min-w-0">
                <div className="text-slate-500">Status</div>
                <div
                  className={`font-mono inline-flex items-center gap-1 truncate ${
                    trace.status === "SUCCESS" ? "text-emerald-700" : "text-rose-700"
                  }`}
                  data-testid="audit-trace-status"
                >
                  {trace.status === "SUCCESS"
                    ? <CheckCircle2 className="w-3 h-3" />
                    : <AlertTriangle className="w-3 h-3" />}
                  {trace.status || "—"}
                </div>
              </div>
              <div className="bg-slate-50 rounded px-2 py-1 min-w-0">
                <div className="text-slate-500">Elapsed</div>
                <div className="font-mono text-[#2E2E38] truncate" data-testid="audit-trace-elapsed">
                  {trace.elapsed_ms == null ? "—" : `${trace.elapsed_ms} ms`}
                </div>
              </div>
              <div className="bg-slate-50 rounded px-2 py-1 col-span-2 min-w-0">
                <div className="text-slate-500">Request time (UTC)</div>
                <div className="font-mono text-[11px] text-[#2E2E38] truncate">{trace.request_time || "—"}</div>
              </div>
              <div className="bg-slate-50 rounded px-2 py-1 col-span-2 min-w-0">
                <div className="text-slate-500">Response time (UTC)</div>
                <div className="font-mono text-[11px] text-[#2E2E38] truncate">{trace.response_time || "—"}</div>
              </div>
              {trace.response_model && (
                <div className="bg-slate-50 rounded px-2 py-1 col-span-2 md:col-span-4 min-w-0">
                  <div className="text-slate-500">Model</div>
                  <div className="font-mono text-[11px] text-[#2E2E38] truncate">{trace.response_model}</div>
                </div>
              )}
              {trace.error_reason && (
                <div className="bg-rose-50 rounded px-2 py-1 col-span-2 md:col-span-4 min-w-0">
                  <div className="text-rose-600">Error reason</div>
                  <div className="font-mono text-[11px] text-rose-700 whitespace-pre-wrap break-words">{trace.error_reason}</div>
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-2">
              <div className="text-[11px] text-slate-500">Raw JSON</div>
              <button
                type="button"
                onClick={copyJson}
                className="text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 shrink-0"
                data-testid="audit-trace-copy"
              >
                <Copy className="w-3 h-3" /> {copied ? "Copied" : "Copy JSON"}
              </button>
            </div>
            <pre
              className="text-[11px] font-mono bg-slate-900 text-slate-100 rounded p-3 max-h-[55vh] overflow-auto whitespace-pre-wrap break-words"
              data-testid="audit-trace-json"
            >
              {json}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function AuditLogPage() {
  const { active } = useProjects();
  const [items, setItems] = useState([]);
  const [openRow, setOpenRow] = useState(null);  // { traceId, row } | null

  useEffect(() => {
    if (active?.id) listAudit(active.id).then(setItems);
  }, [active?.id]);

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      <header className="bg-white border-b border-[#E6E6E6] px-6 py-3">
        <div className="text-[10px] uppercase tracking-widest text-slate-500">Audit</div>
        <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38]">Audit Log</h1>
      </header>
      <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-[#F6F6FA]">
        <div className="max-w-3xl mx-auto mos-panel">
          {items.length === 0 ? (
            <div className="p-6 text-sm text-slate-500 text-center">
              <Activity className="w-5 h-5 mx-auto mb-2 text-slate-400" />
              No events yet.
            </div>
          ) : (
            <ul className="divide-y divide-slate-200">
              {items.map((it, i) => {
                const traceId = it?.details?.trace_id || "";
                return (
                  <li
                    key={i}
                    className="px-4 py-3 flex items-center justify-between gap-3"
                    data-testid={`audit-${i}`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-mono text-[#2E2E38] truncate">{it.action}</div>
                      <div className="text-[11px] text-slate-500 truncate">
                        {JSON.stringify(it.details || {})}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        type="button"
                        onClick={() => setOpenRow({ traceId, row: it })}
                        className={
                          "text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded border " +
                          (traceId
                            ? "border-slate-200 hover:bg-slate-50 text-[#2E2E38]"
                            : "border-slate-200 hover:bg-slate-50 text-slate-600")
                        }
                        data-testid={`audit-trace-btn-${i}`}
                        title={traceId
                          ? "View full request / response envelope"
                          : "No LLM trace — showing original audit details"}
                      >
                        <FileSearch className="w-3 h-3" /> Detail Log Trace
                      </button>
                      <div className="text-[11px] text-slate-500 whitespace-nowrap">
                        {new Date(it.at).toLocaleString()}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      <TraceDialog
        open={!!openRow}
        traceId={openRow?.traceId || ""}
        fallbackRow={openRow?.row || null}
        onClose={() => setOpenRow(null)}
      />
    </div>
  );
}
