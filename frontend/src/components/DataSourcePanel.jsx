import { useEffect, useState, useCallback } from "react";
import { Database, Globe, Plug, RefreshCw, CheckCircle2, AlertCircle, Loader2, Plus } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { dbConnect, registerAppUrl, listDataSources } from "@/lib/api";
import { toast } from "sonner";

/**
 * DataSourcePanel — iter 13.8
 *
 * Lets the user register a live database (postgres/mysql/oracle/mssql/sqlite)
 * and an application URL so LAMA can ingest schema metadata directly into the
 * KB. Posts to:
 *   POST /api/kb/{pid}/db-connect    — fetches information_schema, persists
 *                                       tables + FKs as KB entities.
 *   POST /api/kb/{pid}/app-url       — best-effort HEAD probe, persists URL.
 *   GET  /api/kb/{pid}/data-sources  — lists every registered source.
 *
 * Used by Discovery (right under UploadPanel) and is also routable standalone.
 */

const DB_TYPES = [
  { value: "postgres",  label: "PostgreSQL",         port: 5432 },
  { value: "mysql",     label: "MySQL / MariaDB",    port: 3306 },
  { value: "oracle",    label: "Oracle",             port: 1521 },
  { value: "mssql",     label: "MS SQL Server",      port: 1433 },
  { value: "sqlite",    label: "SQLite (file path)", port: 0 },
];

const EMPTY_FORM = {
  db_type: "postgres",
  host: "localhost",
  port: 5432,
  database_name: "",
  username: "",
  password: "",
  application_url: "",
  ssl: false,
  descriptor_only: false,
};

export default function DataSourcePanel({ projectId, onSchemaIngested }) {
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [submitting, setSubmitting] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [sources, setSources] = useState([]);
  const [loadingSources, setLoadingSources] = useState(false);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setLoadingSources(true);
    try {
      const r = await listDataSources(projectId);
      setSources(r.items || []);
    } catch {
      // non-blocking
    } finally {
      setLoadingSources(false);
    }
  }, [projectId]);

  useEffect(() => { refresh(); }, [refresh]);

  const resetForm = useCallback(() => {
    setForm({ ...EMPTY_FORM });
    setLastResult(null);
  }, []);

  const onChange = (k, v) => {
    setForm((f) => {
      const next = { ...f, [k]: v };
      if (k === "db_type") {
        const preset = DB_TYPES.find((x) => x.value === v);
        if (preset) next.port = preset.port;
      }
      return next;
    });
  };

  const handleConnect = async () => {
    if (!projectId) { toast.error("Pick a project first"); return; }
    if (!form.database_name) { toast.error("Database name (or SQLite file path) is required"); return; }
    setSubmitting(true);
    setLastResult(null);
    try {
      const r = await dbConnect(projectId, {
        db_type: form.db_type,
        host: form.host,
        port: Number(form.port) || 0,
        database_name: form.database_name,
        username: form.username,
        password: form.password,
        application_url: form.application_url,
        ssl: !!form.ssl,
        descriptor_only: !!form.descriptor_only,
      });
      setLastResult(r);
      if (r.ok && r.entities_ingested > 0) {
        toast.success("Schema ingested", {
          description: `${r.tables_seen} tables · ${r.fk_count} FKs · ${r.entities_ingested} KB entities added`,
        });
        if (onSchemaIngested) onSchemaIngested(r);
        setForm({ ...EMPTY_FORM });
      } else if (r.descriptor_only || r.ok) {
        toast.success("Descriptor saved", {
          description: r.extract_error || "Connection registered without live schema extraction.",
        });
        setForm({ ...EMPTY_FORM });
      } else {
        toast.error("Connection failed", { description: r.extract_error || "See result below" });
      }
      await refresh();
    } catch (e) {
      toast.error("Request failed", { description: e?.response?.data?.detail || e.message });
    } finally {
      setSubmitting(false);
    }
  };

  const handleSaveAppUrl = async () => {
    if (!projectId || !form.application_url) { toast.error("Application URL required"); return; }
    try {
      const r = await registerAppUrl(projectId, form.application_url);
      toast.success("App URL registered", {
        description: r.probe?.reachable
          ? `Reachable · server=${r.probe.server || "?"} · powered-by=${r.probe.x_powered_by || "?"}`
          : (r.probe?.error || "Stored (probe failed)"),
      });
      setForm({ ...EMPTY_FORM });
      await refresh();
    } catch (e) {
      toast.error("Failed", { description: e?.response?.data?.detail || e.message });
    }
  };

  return (
    <div className="mos-panel p-4 flex flex-col gap-3" data-testid="data-source-panel">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Plug className="w-4 h-4 text-fg" />
          <h3 className="font-display text-sm font-bold uppercase tracking-wider text-fg">
            Live Data Source
          </h3>
        </div>
        <button
          type="button"
          onClick={refresh}
          className="text-micro uppercase tracking-wider text-fg-muted hover:text-fg flex items-center gap-1"
          data-testid="refresh-data-sources"
        >
          <RefreshCw className={`w-3 h-3 ${loadingSources ? "animate-spin" : ""}`} /> Refresh
        </button>
      </div>

      <p className="text-micro text-fg-muted leading-relaxed">
        Provide database credentials and (optionally) the application URL.
        LAMA connects via <code>information_schema</code> and adds the live
        tables + foreign keys to the Knowledge Base so SRS prompts get the
        authoritative schema. Falls back to descriptor-only when the driver
        is missing. <b>You can register multiple data sources</b> — each
        Connect / Save appends a new row to the list below.
      </p>

      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-micro uppercase tracking-wider text-fg-muted">DB Type</span>
          <select
            data-testid="ds-db-type"
            className="border border-border rounded-sm px-2 py-1.5 text-xs bg-surface"
            value={form.db_type}
            onChange={(e) => onChange("db_type", e.target.value)}
          >
            {DB_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-micro uppercase tracking-wider text-fg-muted">Port</span>
          <Input
            data-testid="ds-port"
            type="number"
            value={form.port}
            onChange={(e) => onChange("port", e.target.value)}
            className="text-xs h-8"
          />
        </label>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-micro uppercase tracking-wider text-fg-muted">
          {form.db_type === "sqlite" ? "SQLite file path" : "Host"}
        </span>
        <Input
          data-testid="ds-host"
          value={form.host}
          placeholder={form.db_type === "sqlite" ? "/path/to/app.db" : "db.example.com"}
          onChange={(e) => onChange("host", e.target.value)}
          className="text-xs h-8"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-micro uppercase tracking-wider text-fg-muted">
          {form.db_type === "oracle"
            ? "Service / EZ-connect (e.g. ORCLPDB1 or host:1521/ORCLPDB1)"
            : "Database name"}
        </span>
        <Input
          data-testid="ds-database"
          value={form.database_name}
          onChange={(e) => onChange("database_name", e.target.value)}
          className="text-xs h-8"
        />
      </label>

      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1">
          <span className="text-micro uppercase tracking-wider text-fg-muted">Username</span>
          <Input
            data-testid="ds-user"
            value={form.username}
            onChange={(e) => onChange("username", e.target.value)}
            autoComplete="off"
            className="text-xs h-8"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-micro uppercase tracking-wider text-fg-muted">Password</span>
          <Input
            data-testid="ds-password"
            type="password"
            value={form.password}
            onChange={(e) => onChange("password", e.target.value)}
            autoComplete="new-password"
            className="text-xs h-8"
          />
        </label>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-micro uppercase tracking-wider text-fg-muted">
          <Globe className="w-3 h-3 inline mr-1" /> Application URL (optional)
        </span>
        <Input
          data-testid="ds-app-url"
          value={form.application_url}
          placeholder="http://localhost:8080"
          onChange={(e) => onChange("application_url", e.target.value)}
          className="text-xs h-8"
        />
      </label>

      <div className="flex items-center gap-3 text-micro text-fg">
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={form.ssl}
            onChange={(e) => onChange("ssl", e.target.checked)}
            data-testid="ds-ssl"
          />
          SSL
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={form.descriptor_only}
            onChange={(e) => onChange("descriptor_only", e.target.checked)}
            data-testid="ds-descriptor-only"
          />
          Descriptor only (skip live extraction)
        </label>
      </div>

      <div className="flex items-center gap-2">
        <Button
          onClick={handleConnect}
          disabled={submitting}
          data-testid="ds-connect-btn"
          className="bg-ink text-ink-fg hover:bg-brand hover:text-fg text-xs h-8 px-3"
        >
          {submitting
            ? (<><Loader2 className="w-3 h-3 mr-1 animate-spin" /> Connecting…</>)
            : (<><Database className="w-3 h-3 mr-1" /> Connect &amp; Ingest Schema</>)}
        </Button>
        <Button
          onClick={handleSaveAppUrl}
          disabled={!form.application_url || submitting}
          data-testid="ds-save-url-btn"
          variant="outline"
          className="text-xs h-8 px-3"
        >
          <Globe className="w-3 h-3 mr-1" /> Save URL only
        </Button>
        <Button
          onClick={resetForm}
          disabled={submitting}
          data-testid="ds-add-another-btn"
          variant="outline"
          title="Clear the form to register another data source"
          className="text-xs h-8 px-3"
        >
          <Plus className="w-3 h-3 mr-1" /> Add another
        </Button>
      </div>

      {lastResult && (
        <div
          className="border border-border bg-bg rounded-sm p-2 text-micro"
          data-testid="ds-last-result"
        >
          <div className="flex items-center gap-1.5 mb-1 font-semibold text-fg">
            {lastResult.ok
              ? <CheckCircle2 className="w-3.5 h-3.5 text-green-600" />
              : <AlertCircle className="w-3.5 h-3.5 text-amber-600" />}
            Last result
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-fg-muted">
            <div>TCP reachable:</div><div className="font-mono">{String(lastResult.tcp_probe?.reachable)}</div>
            <div>Tables seen:</div><div className="font-mono">{lastResult.tables_seen}</div>
            <div>FKs:</div><div className="font-mono">{lastResult.fk_count}</div>
            <div>Entities ingested:</div><div className="font-mono">{lastResult.entities_ingested}</div>
            <div>App URL reachable:</div><div className="font-mono">{String(lastResult.app_probe?.reachable)}</div>
            <div>App server header:</div><div className="font-mono truncate">{lastResult.app_probe?.server || "—"}</div>
          </div>
          {lastResult.extract_error && (
            <div className="mt-2 text-amber-700 break-words">
              <b>Extract note:</b> {lastResult.extract_error}
            </div>
          )}
        </div>
      )}

      {sources.length > 0 && (
        <div className="border-t border-border pt-2">
          <div className="flex items-center justify-between mb-1">
            <div className="text-micro uppercase tracking-wider text-fg-muted">
              Registered ({sources.length})
            </div>
            <button
              type="button"
              onClick={resetForm}
              data-testid="ds-add-another-inline"
              className="text-micro uppercase tracking-wider text-fg hover:text-brand flex items-center gap-1"
              title="Clear the form to register another data source"
            >
              <Plus className="w-3 h-3" /> Add data source
            </button>
          </div>
          <ul className="space-y-1 text-micro" data-testid="ds-list">
            {sources.map((s, i) => (
              <li key={i} className="flex items-center gap-2">
                {s.kind === "database" ? <Database className="w-3 h-3" /> : <Globe className="w-3 h-3" />}
                <span className="font-mono truncate">
                  {s.kind === "database"
                    ? `${s.db_type}://${s.host || "?"}/${s.database || "?"}`
                    : s.application_url}
                </span>
                {s.schema_summary && (
                  <span className="text-fg-muted">
                    · {s.schema_summary.tables} tables · {s.schema_summary.fk_count} FKs
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

