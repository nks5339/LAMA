import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import {
  Loader2,
  Sparkles,
  Download,
  Lock,
  Pencil,
  Check,
  X,
  Database,
  GitBranch,
  FileCode,
  AlertTriangle,
  RotateCcw,
  Trash2,
  ChevronDown,
  ChevronRight as ChevronRightIcon,
  TableProperties,
  Network,
  Layers,
  MoreVertical,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import { useIsMobile } from "@/hooks/useBreakpoint";
import { EmptyState } from "@/components/ux";
import { FolderOpen } from "lucide-react";
import {
  startOLTPJob,
  startOLAPJob,
  startScriptsJob,
  getDataModelJob,
  generateBusMatrix,
  generateEntityGraph,
  getDataModelArtifacts,
  getArtifact,
  updateArtifact,
  freezeArtifact,
  downloadArtifactUrl,
  factoryReset,
  resetStage2,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import ERDiagramV3 from "@/components/ERDiagramV3";
import ERDiagramOld from "@/components/ERDiagram";
import FloatingChat from "@/components/FloatingChat";
import ConfidenceBadge from "@/components/ConfidenceBadge";
import LiveDbReingestDialog from "@/components/LiveDbReingestDialog";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";

// SQL syntax highlight (lightweight, no extra deps)
const SQL_KEYWORDS = /\b(CREATE|TABLE|INDEX|TYPE|ENUM|PRIMARY|FOREIGN|KEY|REFERENCES|NOT|NULL|DEFAULT|UNIQUE|ON|DELETE|UPDATE|RESTRICT|CASCADE|GENERATED|ALWAYS|AS|IDENTITY|PARTITION|BY|RANGE|COMMENT|VIEW|MATERIALIZED|MATERIALISED|ALTER|ADD|DROP|SELECT|FROM|WHERE|GROUP|ORDER|JOIN|INNER|LEFT|RIGHT|FULL|OUTER)\b/gi;
const SQL_TYPES = /\b(UUID|TEXT|VARCHAR|CHAR|INTEGER|INT|BIGINT|SMALLINT|TINYINT|BOOLEAN|BOOL|DECIMAL|NUMERIC|FLOAT|DOUBLE|TIMESTAMPTZ|TIMESTAMP|DATE|TIME|JSONB|JSON|BYTEA|SERIAL|BIGSERIAL)\b/g;
function highlightSql(code) {
  if (!code) return "";
  // Escape HTML
  let esc = code.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  esc = esc.replace(/('([^']|'')*')/g, '<span style="color:#168736">$1</span>');
  esc = esc.replace(SQL_KEYWORDS, '<span style="color:#2E2E38;font-weight:600">$1</span>');
  esc = esc.replace(SQL_TYPES, '<span style="color:#0066CC">$1</span>');
  esc = esc.replace(/(--[^\n]*)/g, '<span style="color:#747480;font-style:italic">$1</span>');
  return esc;
}

const TYPE_META = {
  oltp_ddl: { label: "OLTP Schema", icon: Database, ext: "sql" },
  olap_ddl: { label: "OLAP Star Schema", icon: Layers, ext: "sql" },
  bus_matrix: { label: "Bus Matrix", icon: TableProperties, ext: "json" },
  migrate_old_to_oltp: { label: "Legacy → OLTP", icon: FileCode, ext: "py" },
  migrate_oltp_to_olap: { label: "OLTP → OLAP", icon: FileCode, ext: "py" },
  test_migration: { label: "Migration Tests", icon: FileCode, ext: "py" },
};

// ============================================================
// Reset Modal — typed "RESET" confirmation
// ============================================================
function ResetModal({ open, onClose, onConfirm, title, warning, accent = "red" }) {
  const [typed, setTyped] = useState("");
  useEffect(() => {
    if (!open) setTyped("");
  }, [open]);
  if (!open) return null;
  const enabled = typed === "RESET";
  const accentBg = accent === "orange" ? "bg-orange-600 hover:bg-orange-700" : "bg-red-600 hover:bg-red-700";
  return (
    <div aria-hidden="true" className="fixed inset-0 bg-ink/40 z-50 flex items-center justify-center" data-testid="reset-modal" onClick={onClose}>
      <div role="presentation" className="bg-surface rounded-sm border-2 border-border shadow-xl max-w-md w-full p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 mb-3">
          <AlertTriangle className={`w-5 h-5 ${accent === "orange" ? "text-orange-600" : "text-red-600"}`} />
          <h3 className="font-display font-bold text-base text-fg">{title}</h3>
        </div>
        <p className="text-xs text-fg-muted mb-4 leading-relaxed whitespace-pre-line">{warning}</p>
        <label className="block text-micro uppercase tracking-wider text-fg-muted font-semibold mb-1">
          Type <span className="font-mono text-fg">RESET</span> to confirm
        </label>
        <input
          autoFocus
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          data-testid="reset-confirm-input"
          className="w-full border border-border rounded-sm px-2 py-1.5 text-sm font-mono focus:border-fg outline-none"
        />
        <div className="flex justify-end gap-2 mt-4">
          <Button size="sm" variant="outline" onClick={onClose} className="text-xs h-8 rounded-sm" data-testid="reset-cancel">
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={!enabled}
            onClick={onConfirm}
            data-testid="reset-confirm"
            className={`text-xs h-8 rounded-sm text-white ${accentBg} disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            Confirm Reset
          </Button>
        </div>
      </div>
    </div>
  );
}

// ============================================================
// DDL Viewer / Editor (used for OLTP, OLAP)
// ============================================================
function DDLViewer({ projectId, type, artifact, onArtifactChange, onRegenerate, generating, generationLog }) {
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!editing) setEditText(artifact?.content || "");
  }, [artifact?.id, artifact?.content, editing]);

  const startEdit = () => {
    setEditText(artifact?.content || "");
    setEditing(true);
  };
  const cancelEdit = () => setEditing(false);
  const saveEdit = async () => {
    setBusy(true);
    try {
      await updateArtifact(projectId, artifact.id, editText);
      toast.success("Saved");
      setEditing(false);
      onArtifactChange?.();
    } catch (e) {
      toast.error("Save failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setBusy(false);
    }
  };
  const handleFreeze = async () => {
    setBusy(true);
    try {
      await freezeArtifact(projectId, artifact.id);
      toast.success(`${TYPE_META[type]?.label || type} frozen`);
      onArtifactChange?.();
    } catch (e) {
      toast.error("Freeze failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setBusy(false);
    }
  };

  const empty = !artifact || !artifact.content;

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="px-3 py-2 border-b border-border flex items-center gap-2 bg-surface">
        <Button
          size="sm"
          onClick={onRegenerate}
          disabled={generating}
          data-testid={`generate-${type}-btn`}
          className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-xs h-7"
        >
          {generating ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
          {empty ? "Generate" : "Regenerate"}
        </Button>
        {!empty && !editing && (
          <Button size="sm" variant="outline" onClick={startEdit} disabled={artifact.frozen} className="text-xs h-7 rounded-sm border-border" data-testid={`edit-${type}-btn`}>
            <Pencil className="w-3 h-3 mr-1" />
            Edit
          </Button>
        )}
        {editing && (
          <>
            <Button size="sm" onClick={saveEdit} disabled={busy} className="text-xs h-7 rounded-sm bg-ink text-ink-fg hover:bg-ink-hover" data-testid={`save-${type}-btn`}>
              <Check className="w-3 h-3 mr-1" /> Save
            </Button>
            <Button size="sm" variant="outline" onClick={cancelEdit} className="text-xs h-7 rounded-sm border-border">
              <X className="w-3 h-3 mr-1" /> Cancel
            </Button>
          </>
        )}
        {!empty && (
          <>
            {artifact.frozen ? (
              <>
                {/* iter-13.71 — confidence badge visible on frozen
                    artifacts too so reviewers / super-admins can
                    audit an already-frozen old project. */}
                <ConfidenceBadge projectId={projectId} stage="DataModel" compact />
                <span className="text-micro uppercase tracking-wider bg-brand text-fg px-1.5 py-0.5 rounded-sm font-bold">
                  Frozen v{artifact.version}
                </span>
              </>
            ) : (
              <>
                {/* iter-13.71 — per-stage Accuracy / Confidence badge */}
                <ConfidenceBadge projectId={projectId} stage="DataModel" compact />
                <Button size="sm" variant="outline" onClick={handleFreeze} disabled={busy} className="text-xs h-7 rounded-sm border-border" data-testid={`freeze-${type}-btn`}>
                  <Lock className="w-3 h-3 mr-1" /> Freeze
                </Button>
              </>
            )}
            <a
              href={downloadArtifactUrl(projectId, artifact.id)}
              target="_blank"
              rel="noreferrer"
              data-testid={`download-${type}-btn`}
              className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-border rounded-sm h-7 hover:border-fg"
            >
              <Download className="w-3 h-3" /> .{TYPE_META[type]?.ext}
            </a>
            <span className="text-micro text-fg-muted ml-auto" data-testid={`version-${type}`}>v{artifact.version}</span>
          </>
        )}
      </div>

      {/* SSE log */}
      {generating && generationLog && (
        <div className="px-3 py-2 bg-brand-tint border-b border-brand/40 text-xs text-fg flex items-center gap-2" data-testid={`gen-log-${type}`}>
          <Loader2 className="w-3 h-3 animate-spin" />
          <span className="flex-1">{generationLog.step || generationLog.message}</span>
          {generationLog.pct != null && (
            <div className="w-24 h-1.5 bg-border rounded-sm overflow-hidden">
              <div className="h-full bg-brand" style={{ width: `${generationLog.pct}%` }} />
            </div>
          )}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-auto mos-scroll bg-bg" data-testid={`ddl-body-${type}`}>
        {empty && !generating && (
          <div className="text-center py-16 text-sm text-fg-muted">
            <Database className="w-6 h-6 mx-auto mb-2" />
            No {TYPE_META[type]?.label} yet. Click <b>Generate</b>.
          </div>
        )}
        {editing ? (
          <textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            data-testid={`editor-${type}`}
            className="w-full h-full font-mono text-xs bg-surface p-3 border-0 outline-none resize-none"
            style={{ minHeight: "100%" }}
          />
        ) : (
          !empty && (
            <pre
              data-testid={`viewer-${type}`}
              className="text-xs font-mono whitespace-pre p-3 leading-relaxed"
              dangerouslySetInnerHTML={{ __html: highlightSql(artifact.content) }}
            />
          )
        )}
      </div>
    </div>
  );
}

// ============================================================
// Bus Matrix Viewer
// ============================================================
function BusMatrixViewer({ projectId, artifact, onGenerate, generating }) {
  const matrix = useMemo(() => {
    if (!artifact?.content) return null;
    try {
      return JSON.parse(artifact.content);
    } catch {
      return null;
    }
  }, [artifact?.content]);

  const [openFact, setOpenFact] = useState(null);

  return (
    <div className="h-full flex flex-col">
      <div className="px-3 py-2 border-b border-border flex items-center gap-2 bg-surface">
        <Button
          size="sm"
          onClick={onGenerate}
          disabled={generating}
          data-testid="generate-bus-matrix-btn"
          className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-xs h-7"
        >
          {generating ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
          {matrix ? "Regenerate" : "Generate"}
        </Button>
        {artifact && (
          <a
            href={downloadArtifactUrl(projectId, artifact.id)}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-border rounded-sm h-7 hover:border-fg"
            data-testid="download-bus-matrix-btn"
          >
            <Download className="w-3 h-3" /> .json
          </a>
        )}
        {matrix && (
          <span className="text-micro text-fg-muted ml-auto">
            {(matrix.facts || []).length} facts · {(matrix.dimensions || []).length} dims
          </span>
        )}
      </div>
      <div className="flex-1 overflow-auto mos-scroll bg-bg p-3" data-testid="bus-matrix-body">
        {!matrix && !generating && (
          <div className="text-center py-16 text-sm text-fg-muted">
            <TableProperties className="w-6 h-6 mx-auto mb-2" />
            No bus matrix yet. Generate OLTP first, then click <b>Generate</b>.
          </div>
        )}
        {matrix && (
          <>
            <div className="overflow-x-auto bg-surface border border-border rounded-sm">
              <table className="text-xs border-collapse" data-testid="bus-matrix-table">
                <thead>
                  <tr>
                    <th className="bg-ink text-ink-fg px-2 py-1.5 text-left sticky left-0 z-10">Fact \\ Dim</th>
                    {(matrix.dimensions || []).map((d) => (
                      <th key={d.name} className="bg-ink text-ink-fg px-2 py-1.5 text-left whitespace-nowrap font-mono">
                        {d.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(matrix.facts || []).map((f) => (
                    <tr key={f.name} className="border-b border-border">
                      <td className="px-2 py-1.5 font-mono font-semibold text-fg sticky left-0 bg-bg">{f.name}</td>
                      {(matrix.dimensions || []).map((d) => {
                        const checked = (matrix.matrix?.[f.name] || {})[d.name];
                        return (
                          <td key={d.name} className="px-2 py-1.5 text-center" style={{ background: checked ? "#FFE600" : "transparent" }}>
                            {checked ? "✓" : ""}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-4 space-y-2">
              <div className="text-micro uppercase tracking-wider text-fg-muted font-semibold">Fact details</div>
              {(matrix.facts || []).map((f) => (
                <div key={f.name} className="bg-surface border border-border rounded-sm">
                  <button
                    type="button"
                    onClick={() => setOpenFact(openFact === f.name ? null : f.name)}
                    className="w-full px-3 py-2 flex items-center justify-between text-left text-xs hover:bg-bg"
                    data-testid={`fact-toggle-${f.name}`}
                  >
                    <span className="font-mono font-semibold text-fg">{f.name}</span>
                    <span className="text-micro text-fg-muted">grain: {f.grain || "—"}</span>
                    {openFact === f.name ? <ChevronDown className="w-3 h-3" /> : <ChevronRightIcon className="w-3 h-3" />}
                  </button>
                  {openFact === f.name && (
                    <div className="px-3 pb-3 border-t border-border">
                      <div className="text-micro uppercase tracking-wider text-fg-muted mt-2">Source tables</div>
                      <div className="text-xs font-mono">{(f.source_tables || []).join(", ") || "—"}</div>
                      <div className="text-micro uppercase tracking-wider text-fg-muted mt-2">Measures</div>
                      <table className="w-full text-xs mt-1">
                        <thead className="bg-bg">
                          <tr>
                            <th className="px-2 py-1 text-left">name</th>
                            <th className="px-2 py-1 text-left">type</th>
                            <th className="px-2 py-1 text-left">agg</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(f.measures || []).map((m, i) => (
                            <tr key={i} className="border-t border-border">
                              <td className="px-2 py-1 font-mono">{m.name}</td>
                              <td className="px-2 py-1 text-fg-muted">{m.type}</td>
                              <td className="px-2 py-1 text-info">{m.agg}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================
// RAG Chat panel (OLTP/OLAP toggle, [DDL_CHANGE] detection)
// ============================================================

// ============================================================
// Artifacts panel — cards + traceability + migration scripts
// ============================================================
function ArtifactsPanel({ projectId, artifacts, onRefresh, scriptsGenerating, scriptsLog, onGenerateScripts }) {
  const [openTrace, setOpenTrace] = useState(null);
  const byType = useMemo(() => {
    const m = {};
    (artifacts || []).forEach((a) => (m[a.type] = a));
    return m;
  }, [artifacts]);

  const renderCard = (type) => {
    const a = byType[type];
    const meta = TYPE_META[type];
    const Icon = meta?.icon || FileCode;
    return (
      <div key={type} className="bg-surface border border-border rounded-sm p-3" data-testid={`artifact-card-${type}`}>
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Icon className="w-4 h-4 text-fg shrink-0" />
            <div className="min-w-0">
              <div className="text-xs font-semibold text-fg truncate">{meta?.label || type}</div>
              <div className="text-micro text-fg-muted">
                {a ? (
                  <>
                    v{a.version} · {a.generated_by?.slice(0, 24) || "—"} · {new Date(a.updated_at).toLocaleString()}
                  </>
                ) : (
                  "Not yet generated"
                )}
              </div>
            </div>
          </div>
          {a && (
            <span
              className={`text-micro uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold shrink-0 ${
                a.frozen ? "bg-brand text-fg" : "bg-bg text-fg-muted"
              }`}
            >
              {a.frozen ? "Frozen" : "Draft"}
            </span>
          )}
        </div>
        {a && (
          <div className="flex flex-wrap items-center gap-1 mt-2">
            <a
              href={downloadArtifactUrl(projectId, a.id)}
              target="_blank"
              rel="noreferrer"
              data-testid={`artifact-download-${type}`}
              className="inline-flex items-center gap-1 text-micro px-1.5 py-0.5 border border-border rounded-sm hover:border-fg"
            >
              <Download className="w-2.5 h-2.5" /> .{meta?.ext}
            </a>
            <button
              type="button"
              onClick={() => setOpenTrace(openTrace === a.id ? null : a.id)}
              data-testid={`artifact-trace-${type}`}
              className="inline-flex items-center gap-1 text-micro px-1.5 py-0.5 border border-border rounded-sm hover:border-fg"
            >
              <GitBranch className="w-2.5 h-2.5" /> Traceability
            </button>
          </div>
        )}
        {a && openTrace === a.id && (
          <div className="mt-2 bg-bg border border-border rounded-sm p-2 text-micro font-mono leading-relaxed text-fg" data-testid={`trace-${type}`}>
            <div className="font-semibold mb-1">📄 Created from</div>
            {Object.entries(a.tracability || {}).map(([k, v]) => (
              <div key={k} className="ml-3">
                ├── <span className="text-fg-muted">{k}:</span>{" "}
                <span className="text-fg">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
              </div>
            ))}
            {Object.keys(a.tracability || {}).length === 0 && <div className="ml-3 text-fg-muted">No traceability metadata.</div>}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="h-full overflow-y-auto mos-scroll p-3 space-y-3 bg-bg">
      <div>
        <div className="text-micro uppercase tracking-wider text-fg-muted font-semibold mb-2">DDL & Bus Matrix</div>
        <div className="space-y-2">{["oltp_ddl", "olap_ddl", "bus_matrix"].map(renderCard)}</div>
      </div>
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-micro uppercase tracking-wider text-fg-muted font-semibold">Migration Scripts</div>
          <Button
            size="sm"
            onClick={onGenerateScripts}
            disabled={scriptsGenerating || !byType.oltp_ddl || !byType.olap_ddl}
            data-testid="generate-scripts-btn"
            className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-micro h-6 px-2"
          >
            {scriptsGenerating ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
            Generate All
          </Button>
        </div>
        {scriptsLog && (
          <div className="text-micro text-fg-muted bg-brand-tint border border-brand/40 rounded-sm px-2 py-1 mb-2" data-testid="scripts-log">
            {scriptsLog}
          </div>
        )}
        <div className="space-y-2">{["migrate_old_to_oltp", "migrate_oltp_to_olap", "test_migration"].map(renderCard)}</div>
      </div>
      <button
        type="button"
        onClick={onRefresh}
        className="text-micro text-fg-muted hover:text-fg underline"
        data-testid="refresh-artifacts"
      >
        Refresh
      </button>
    </div>
  );
}

// ============================================================
// MAIN PAGE
// ============================================================
export default function DataModelPage() {
  const { active, refresh: refreshProject } = useProjects();
  const isMobile = useIsMobile();
  const navigate = useNavigate();
  const [erData, setErData] = useState(null);
  const [erLoading, setErLoading] = useState(false);
  const [erError, setErError] = useState(null);
  const [erTab, setErTab] = useState("new"); // new | old
  const [artifacts, setArtifacts] = useState([]);
  const [tab, setTab] = useState("oltp_ddl"); // oltp_ddl | olap_ddl | bus_matrix
  const [oltpGenerating, setOltpGenerating] = useState(false);
  const [olapGenerating, setOlapGenerating] = useState(false);
  const [bmGenerating, setBmGenerating] = useState(false);
  const [scriptsGenerating, setScriptsGenerating] = useState(false);
  const [oltpLog, setOltpLog] = useState(null);
  const [olapLog, setOlapLog] = useState(null);
  const [scriptsLog, setScriptsLog] = useState(null);
  const [resetMode, setResetMode] = useState(null); // null | "stage2" | "factory"
  const [menuOpen, setMenuOpen] = useState(false);
  // iter-14.56 — Inline live-DB re-ingest dialog. Auto-opens when OLTP /
  // OLAP / Migration fails with the "No TABLE entities" diagnostic emitted
  // by routes/datamodel.py::_no_tables_error. Also reachable manually from
  // the toolbar so operators can pre-emptively refresh the schema.
  const [reingestOpen, setReingestOpen] = useState(false);

  const byType = useMemo(() => {
    const m = {};
    (artifacts || []).forEach((a) => (m[a.type] = a));
    return m;
  }, [artifacts]);

  const loadArtifacts = async () => {
    if (!active?.id) return;
    try {
      const r = await getDataModelArtifacts(active.id);
      // The list endpoint omits content — load each full artifact for the active tab + scripts via individual GETs
      const full = await Promise.all(
        (r.artifacts || []).map((a) => getArtifact(active.id, a.id).catch(() => a))
      );
      setArtifacts(full);
    } catch (e) {
      // 400 if Discovery not frozen — handled separately
    }
  };

  const loadEr = async () => {
    if (!active?.id) return;
    setErLoading(true);
    setErError(null);
    try {
      const data = await generateEntityGraph(active.id);
      setErData(data);
    } catch (e) {
      setErError(e.response?.data?.detail || e.message);
    } finally {
      setErLoading(false);
    }
  };

  useEffect(() => {
    loadEr();
    loadArtifacts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id]);

  // ---------------- SSE helpers ----------------

  // iter-14.56 — DataModel deterministic paths (OLTP / OLAP / Migration) fail
  // with a "No TABLE entities found in KB" message when a previous Build KB
  // accidentally wiped live-DB entities. Detect the marker in the job error
  // and offer inline recovery (auto-open the dialog + add a toast action).
  const _isNoTableErr = (msg) =>
    typeof msg === "string" && /No TABLE entities found in KB/i.test(msg);
  const handleGenFailure = (label, msg) => {
    const description = (msg || "Unknown error").toString();
    if (_isNoTableErr(description)) {
      toast.error(`${label} — live DB schema missing from KB`, {
        description: description.slice(0, 400),
        duration: 10000,
        action: {
          label: "Re-ingest Live DB",
          onClick: () => setReingestOpen(true),
        },
      });
      setReingestOpen(true);
    } else {
      toast.error(`${label} failed`, { description: description.slice(0, 400) });
    }
  };

  const generateOLTP = async () => {
    setOltpGenerating(true);
    setOltpLog({ step: "Starting…", pct: 0 });
    try {
      const { job_id } = await startOLTPJob(active.id);
      const final = await pollJob(job_id, (j) => {
        setOltpLog({ step: j.step || "Running…", pct: j.pct || 0 });
      });
      if (final.status === "complete") {
        toast.success(`OLTP DDL generated (${final.result.tables} tables, ${final.result.fks} FKs)`);
      } else {
        handleGenFailure("OLTP generation", final.error);
      }
      await loadArtifacts();
    } catch (e) {
      handleGenFailure("OLTP generation", e.response?.data?.detail || e.message);
    } finally {
      setOltpGenerating(false);
      setOltpLog(null);
    }
  };

  // Poll a backend job every 2s until terminal status. Returns the final job object.
  const pollJob = async (jobId, onProgress) => {
    while (true) {
      const job = await getDataModelJob(jobId);
      onProgress?.(job);
      if (job.status === "complete" || job.status === "error") return job;
      await new Promise((r) => setTimeout(r, 2000));
    }
  };

  const generateOLAP = async () => {
    setOlapGenerating(true);
    setOlapLog({ step: "Starting…", pct: 0 });
    try {
      const { job_id } = await startOLAPJob(active.id);
      const final = await pollJob(job_id, (j) => {
        setOlapLog({ step: j.step || "Running…", pct: j.pct || 0 });
      });
      if (final.status === "complete") {
        toast.success(`OLAP DDL generated (${final.result.dims} dims, ${final.result.facts} facts)`);
      } else {
        handleGenFailure("OLAP generation", final.error);
      }
      await loadArtifacts();
    } catch (e) {
      handleGenFailure("OLAP generation", e.response?.data?.detail || e.message);
    } finally {
      setOlapGenerating(false);
      setOlapLog(null);
    }
  };

  const generateBM = async () => {
    setBmGenerating(true);
    try {
      const r = await generateBusMatrix(active.id);
      toast.success(`Bus Matrix v${r.version} generated`);
      await loadArtifacts();
    } catch (e) {
      toast.error("Bus matrix failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setBmGenerating(false);
    }
  };

  const generateScripts = async () => {
    setScriptsGenerating(true);
    setScriptsLog("Starting…");
    try {
      const { job_id } = await startScriptsJob(active.id);
      const final = await pollJob(job_id, (j) => {
        setScriptsLog(`${j.step} (${j.pct}%)`);
      });
      if (final.status === "complete") {
        toast.success("All migration scripts generated");
      } else {
        handleGenFailure("Scripts", final.error);
      }
      await loadArtifacts();
    } catch (e) {
      handleGenFailure("Scripts", e.response?.data?.detail || e.message);
    } finally {
      setScriptsGenerating(false);
      setTimeout(() => setScriptsLog(null), 2500);
    }
  };

  const handleReset = async (mode) => {
    try {
      if (mode === "stage2") {
        await resetStage2(active.id);
        toast.success("Stage 2 reset");
      } else {
        await factoryReset(active.id);
        toast.success("Project factory reset");
        await refreshProject?.();
        navigate("/");
        return;
      }
      setResetMode(null);
      await loadArtifacts();
      await loadEr();
    } catch (e) {
      toast.error("Reset failed", { description: e.response?.data?.detail || e.message });
    }
  };

  if (!active) {
    return (
      <EmptyState
        icon={FolderOpen}
        title="No project selected"
        description="Create or open a project from the sidebar to start modelling data."
        action={
          <button
            type="button"
            onClick={() => navigate("/")}
            className="text-xs px-3 py-1.5 bg-brand text-fg rounded-sm hover:bg-yellow-300 font-semibold focus:outline-none focus:ring-2 focus:ring-fg"
            data-testid="empty-goto-discovery"
          >
            Go to Discovery →
          </button>
        }
      />
    );
  }

  const dataModelLocked = (active.stage_status?.DataModel || "locked") === "locked";

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      {/* Header */}
      <header className="bg-surface border-b-2 border-brand px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3" data-testid="datamodel-header">
        <div>
          <div className="text-micro uppercase tracking-widest text-fg-muted">Stage 2 of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-fg">Data Model</h1>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right">
            <div className="text-micro uppercase tracking-widest text-fg-muted">Project</div>
            <div className="text-sm font-semibold text-fg">{active.name}</div>
          </div>
          <div className="relative">
            <button
              type="button"
              onClick={() => setMenuOpen((m) => !m)}
              data-testid="datamodel-menu-btn"
              className="p-1.5 rounded-sm hover:bg-bg border border-border"
              aria-label="Open menu"
            >
              <MoreVertical className="w-4 h-4 text-fg" />
            </button>
            {menuOpen && (
              <div className="absolute right-0 top-9 bg-surface border border-border rounded-sm shadow-md z-30 min-w-[200px]" data-testid="datamodel-menu">
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    setReingestOpen(true);
                  }}
                  data-testid="open-live-db-reingest"
                  className="w-full text-left px-3 py-2 text-xs hover:bg-bg flex items-center gap-2 text-fg border-b border-surface-2"
                >
                  <Database className="w-3 h-3" /> Re-ingest Live DB
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    setResetMode("stage2");
                  }}
                  data-testid="open-stage2-reset"
                  className="w-full text-left px-3 py-2 text-xs hover:bg-bg flex items-center gap-2 text-orange-600"
                >
                  <RotateCcw className="w-3 h-3" /> Stage 2 Reset
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    setResetMode("factory");
                  }}
                  data-testid="open-factory-reset"
                  className="w-full text-left px-3 py-2 text-xs hover:bg-bg flex items-center gap-2 text-red-600"
                >
                  <Trash2 className="w-3 h-3" /> Full Factory Reset
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      {dataModelLocked && (
        <div className="bg-brand-tint border-b border-brand px-6 py-3 flex items-center gap-2 text-sm text-fg" data-testid="datamodel-locked">
          <Lock className="w-4 h-4" />
          Stage 2 is locked. Freeze the Discovery SRS first.
          <button
            type="button"
            onClick={() => navigate("/")}
            className="ml-2 text-xs underline text-fg font-semibold"
          >
            Go to Discovery →
          </button>
        </div>
      )}

      {!dataModelLocked && (
        <div className="flex-1 min-h-0 bg-bg flex flex-col">
          {/* ER diagram - Accordion */}
          <div className="px-2 pt-2">
            <Accordion type="single" collapsible defaultValue="er-diagram" className="bg-surface border border-border rounded-sm">
              <AccordionItem value="er-diagram" className="border-0">
                <AccordionTrigger className="px-3 py-2 hover:no-underline hover:bg-surface-2">
                  <div className="flex items-center gap-2">
                    <Network className="w-4 h-4 text-fg" />
                    <h3 className="font-display text-sm font-bold text-fg tracking-tight">Database Schema</h3>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        loadEr();
                      }}
                      disabled={erLoading}
                      data-testid="reload-er"
                      className="ml-2 text-micro underline text-fg-muted hover:text-fg"
                    >
                      {erLoading ? "Loading…" : "Reload"}
                    </button>
                  </div>
                </AccordionTrigger>
                <AccordionContent className="px-0 pb-0">
                  <div className="border-t border-border">
                    {/* Tabs for diagram views */}
                    <div className="border-b border-border flex">
                      {[
                        { k: "new", label: "Table View" },
                        { k: "old", label: "Graph View" },
                      ].map((t) => (
                        <button
                          key={t.k}
                          type="button"
                          onClick={() => setErTab(t.k)}
                          className={`text-xs px-3 py-2 font-semibold tracking-tight border-r border-border ${
                            erTab === t.k
                              ? "bg-ink text-ink-fg"
                              : "bg-surface text-fg-muted hover:text-fg"
                          }`}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                    
                    {erError && (
                      <div className="p-4 text-xs text-red-600" data-testid="er-error">
                        {erError}
                      </div>
                    )}
                    {!erError && erTab === "new" && <ERDiagramV3 data={erData} height={500} />}
                    {!erError && erTab === "old" && <ERDiagramOld data={erData} height={500} />}
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>

          {/* Bottom row: DDL | Artifacts */}
          <div className="flex-1 min-h-0 p-2">
            <PanelGroup
              key={isMobile ? "v" : "h"}
              direction={isMobile ? "vertical" : "horizontal"}
              className="h-full"
              autoSaveId={isMobile ? "lama-datamodel-h-v" : "lama-datamodel-h"}
            >

              <Panel defaultSize={50} minSize={25} id="ddl" order={1}>
                <div className="h-full flex flex-col">
                  <div className="bg-surface border border-border rounded-sm flex-1 flex flex-col overflow-hidden">
                    <div className="border-b border-border flex">
                      {[
                        { k: "oltp_ddl", label: "OLTP" },
                        { k: "olap_ddl", label: "OLAP" },
                        { k: "bus_matrix", label: "Bus Matrix" },
                      ].map((t) => (
                        <button
                          key={t.k}
                          type="button"
                          onClick={() => setTab(t.k)}
                          data-testid={`tab-${t.k}`}
                          className={`text-xs px-3 py-2 font-semibold tracking-tight border-r border-border ${
                            tab === t.k
                              ? "bg-ink text-ink-fg"
                              : "bg-surface text-fg-muted hover:text-fg"
                          }`}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                    <div className="flex-1 min-h-0">
                      {tab === "oltp_ddl" && (
                        <DDLViewer
                          projectId={active.id}
                          type="oltp_ddl"
                          artifact={byType.oltp_ddl}
                          onArtifactChange={loadArtifacts}
                          onRegenerate={generateOLTP}
                          generating={oltpGenerating}
                          generationLog={oltpLog}
                        />
                      )}
                      {tab === "olap_ddl" && (
                        <DDLViewer
                          projectId={active.id}
                          type="olap_ddl"
                          artifact={byType.olap_ddl}
                          onArtifactChange={loadArtifacts}
                          onRegenerate={generateOLAP}
                          generating={olapGenerating}
                          generationLog={olapLog}
                        />
                      )}
                      {tab === "bus_matrix" && (
                        <BusMatrixViewer
                          projectId={active.id}
                          artifact={byType.bus_matrix}
                          onGenerate={generateBM}
                          generating={bmGenerating}
                        />
                      )}
                    </div>
                  </div>
                </div>
              </Panel>
              <PanelResizeHandle className="lama-resize-handle" />

              <Panel defaultSize={50} minSize={18} id="artifacts" order={2}>
                <div className="h-full">
                  <div className="h-full bg-surface border border-border rounded-sm overflow-hidden flex flex-col">
                    <div className="px-3 py-2 border-b border-border flex items-center gap-2">
                      <h3 className="font-display text-sm font-bold tracking-tight text-fg">Artifacts & Traceability</h3>
                    </div>
                    <div className="flex-1 min-h-0">
                      <ArtifactsPanel
                        projectId={active.id}
                        artifacts={artifacts}
                        onRefresh={loadArtifacts}
                        scriptsGenerating={scriptsGenerating}
                        scriptsLog={scriptsLog}
                        onGenerateScripts={generateScripts}
                      />
                    </div>
                  </div>
                </div>
              </Panel>
            </PanelGroup>
          </div>
        </div>
      )}

      {/* Floating Chat - DataModel specific */}
      {!dataModelLocked && (
        <FloatingChat
          projectId={active.id}
          kbReady={true}
          model=""
          onConversationUpdated={() => {}}
          stage="DataModel"
          agentKey="datamodel.chat"
          enableSrsEdit={false}
          chatTitle="Data Model Chat"
          categories={[
            { key: "oltp", label: "OLTP" },
            { key: "olap", label: "OLAP" },
            { key: "busmatrix", label: "Bus Matrix" },
          ]}
        />
      )}

      {/* Reset modals */}
      <ResetModal
        open={resetMode === "stage2"}
        onClose={() => setResetMode(null)}
        onConfirm={() => handleReset("stage2")}
        title="Reset Stage 2: Data Model"
        warning={
          "This deletes all data model artifacts:\n• OLTP DDL\n• OLAP DDL\n• Bus Matrix\n• All 3 migration scripts\n\nDiscovery, SRS, and KB are preserved."
        }
        accent="orange"
      />
      <ResetModal
        open={resetMode === "factory"}
        onClose={() => setResetMode(null)}
        onConfirm={() => handleReset("factory")}
        title="Full Factory Reset"
        warning={
          "PERMANENTLY deletes ALL project data:\n• KB files, chunks, entities, TOON\n• SRS document & frozen state\n• All chat conversations\n• All data model artifacts\n• Stage context & freeze gates\n• Qdrant vectors\n• Audit log entries\n\nThe project itself is kept but reset to Discovery (locked stages 2-5)."
        }
        accent="red"
      />

      {/* iter-14.56 — Live DB re-ingest dialog. Auto-opens on
          "No TABLE entities" failures; also reachable from the toolbar
          "Re-ingest Live DB" button rendered next to Generate OLTP. */}
      <LiveDbReingestDialog
        open={reingestOpen}
        onOpenChange={setReingestOpen}
        projectId={active?.id}
        onReingested={async () => {
          // iter-14.57 — Also refresh the Database Schema (ER diagram)
          // after re-ingesting live-DB entities, not just artifacts.
          await Promise.all([loadArtifacts(), loadEr()]);
        }}
      />
    </div>
  );
}
