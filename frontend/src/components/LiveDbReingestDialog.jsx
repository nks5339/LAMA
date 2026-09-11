/*
 * iter-14.56 — Inline "Re-ingest Live DB" recovery flow.
 *
 * Root cause it addresses: LAMA only stores the sha256 hash of the DB
 * password used at ingest time, so it cannot silently re-connect after
 * a `Build KB` accidentally wiped the live-DB TABLE / FK_HINT entities
 * from `kb_entities`. This dialog prompts the operator for the password
 * once (per registered DB), re-hits POST /api/kb/{pid}/db-connect with
 * the saved descriptor, and shows the live table / FK count that came
 * back. Once complete, OLTP / OLAP / Migration deterministic paths see
 * the tables again.
 *
 * Triggered from DataModel.jsx (a) automatically when an OLTP job
 * fails with the "No TABLE entities found in KB" diagnostic, and (b)
 * on demand from a toolbar button so operators can pre-emptively
 * refresh the schema.
 */
import React, { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Database,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  Info,
} from "lucide-react";
import { toast } from "sonner";
import { listDataSources, dbConnect } from "@/lib/api";

const DEFAULT_PORTS = {
  oracle: 1521,
  postgres: 5432,
  mysql: 3306,
  mssql: 1433,
  sqlite: 0,
};

function SourceRow({ src, onReingested }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState(null);

  const descriptor = src.descriptor || {};
  const dbType = src.db_type || descriptor.db_type || "";
  const host = src.host || descriptor.host || "";
  const database = src.database || descriptor.database || "";
  const username = descriptor.username || "";
  const port = descriptor.port || DEFAULT_PORTS[dbType] || 0;
  const summary = src.schema_summary || {};
  const lastError = src.last_extract_error || "";
  const prevTables = Number(summary.tables || 0);
  const prevFks = Number(summary.fk_count || 0);

  const run = async () => {
    if (!password.trim() && dbType !== "sqlite") {
      toast.error("Password required");
      return;
    }
    setBusy(true);
    setLastResult(null);
    try {
      const projectId = src.project_id;
      const payload = {
        db_type: dbType,
        host,
        port,
        database_name: database,
        username,
        password,
        ssl: !!descriptor.ssl,
        application_url: descriptor.application_url || "",
        descriptor_only: false,
      };
      const res = await dbConnect(projectId, payload);
      // iter-14.56.2 — Response keys are FLAT on the endpoint body
      // (`extract_error`, `tables_seen`, `fk_count`, `entities_ingested`,
      // `tcp_probe`). The nested `schema_summary` shape only exists on
      // the persisted `data_sources` Mongo doc returned by
      // `listDataSources`, not on this response — reading it here silently
      // erased the real ORA-xxxxx / auth / TNS error and made every
      // failure look like a bland "0 tables / 0 entities" result.
      const tables = Number(res?.tables_seen || 0);
      const fks = Number(res?.fk_count || 0);
      const ingested = Number(res?.entities_ingested || 0);
      const err = res?.extract_error || "";
      const tcp = res?.tcp_probe || {};
      const tcpReachable = tcp?.reachable !== false;
      const ok = ingested > 0 && tables > 0 && !err;
      if (!ok) {
        let msg = err;
        if (!msg) {
          if (!tcpReachable) {
            msg =
              `Cannot reach ${host}:${port} over TCP — ${tcp?.error || "no route / firewall / VPN"}. ` +
              `Check network / VPN / security-group before retrying.`;
          } else {
            msg =
              `Re-ingest returned 0 tables and 0 KB entities but the server did not raise an error. ` +
              `The credentials may authenticate against a schema with no visible tables ` +
              `(for Oracle: the connecting user needs SELECT on ALL_TAB_COLUMNS / ALL_CONSTRAINTS, ` +
              `or set LAMA_ORACLE_OWNER to the target schema).`;
          }
        }
        if (/psycopg2/i.test(msg)) {
          msg += "\n→ Run: docker compose exec lama pip install psycopg2-binary==2.9.9";
        } else if (/oracledb/i.test(msg)) {
          msg += "\n→ Run: docker compose exec lama pip install oracledb==2.4.1";
        } else if (/pymysql/i.test(msg)) {
          msg += "\n→ Run: docker compose exec lama pip install pymysql==1.1.1";
        } else if (/pymssql/i.test(msg)) {
          msg += "\n→ Run: docker compose exec lama pip install pymssql==2.3.0";
        }
        setLastResult({ ok: false, message: msg });
        toast.error("Re-ingest failed", { description: msg.slice(0, 240) });
      } else {
        setLastResult({
          ok: true,
          message: `Re-ingested ${tables} tables · ${fks} FKs (${ingested} KB entities written).`,
        });
        setPassword("");
        toast.success("Live DB re-ingested", {
          description: `${tables} tables · ${fks} FKs`,
        });
        onReingested?.({ tables, fks });
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || "Unknown error";
      setLastResult({ ok: false, message: detail });
      toast.error("Re-ingest failed", { description: String(detail).slice(0, 200) });
    } finally {
      setBusy(false);
    }
  };

  // Compact DSN string — the raw `db_type://host:port/database` breaks the
  // dialog layout on medium screens (long AWS RDS hostnames push the
  // action button off-screen). Show host + database only; the full DSN
  // lives in the tooltip.
  const shortHost = host
    .replace(/^https?:\/\//, "")
    .replace(/\.rds\.amazonaws\.com/i, ".rds.aws")
    .replace(/\.ap-south-1/i, ".aps1")
    .replace(/\.us-east-1/i, ".use1")
    .replace(/\.eu-west-1/i, ".euw1");
  const fullDsn = `${dbType}://${host}${port ? `:${port}` : ""}/${database}`;

  return (
    <div
      className="border rounded-lg p-3 space-y-2 min-w-0"
      data-testid={`live-db-reingest-source-${dbType}`}
    >
      <div className="flex items-start justify-between gap-3 min-w-0">
        <div className="min-w-0 flex-1">
          <div
            className="flex items-center gap-2 text-sm font-medium min-w-0"
            title={fullDsn}
          >
            <Database className="w-4 h-4 text-slate-500 shrink-0" />
            <span className="truncate">
              <span className="uppercase text-xs text-slate-500 mr-1">
                {dbType}
              </span>
              {shortHost}
              {port ? `:${port}` : ""} / <b>{database}</b>
            </span>
          </div>
          <div className="text-xs text-slate-500 mt-0.5 truncate">
            Username: <code className="text-slate-700">{username || "—"}</code>
            {prevTables > 0 && (
              <>
                {" · Last ingest: "}
                <span className="text-slate-700">
                  {prevTables} tables · {prevFks} FKs
                </span>
              </>
            )}
          </div>
          {lastError && !lastResult && (
            <div className="text-xs text-amber-700 mt-1 flex items-start gap-1">
              <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
              <span className="break-words">
                Last attempt: {lastError.slice(0, 160)}
              </span>
            </div>
          )}
        </div>
      </div>

      {dbType !== "sqlite" && (
        <div className="flex items-center gap-2 min-w-0">
          <Input
            type="password"
            placeholder="Enter DB password to re-ingest"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            className="min-w-0 flex-1"
            data-testid={`live-db-reingest-password-${dbType}`}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) run();
            }}
          />
          <Button
            onClick={run}
            disabled={busy}
            size="sm"
            className="shrink-0 whitespace-nowrap"
            data-testid={`live-db-reingest-btn-${dbType}`}
          >
            {busy ? (
              <>
                <RefreshCw className="w-3.5 h-3.5 mr-1 animate-spin" />
                Re-ingesting…
              </>
            ) : (
              <>
                <RefreshCw className="w-3.5 h-3.5 mr-1" />
                Re-ingest
              </>
            )}
          </Button>
        </div>
      )}
      {dbType === "sqlite" && (
        <Button onClick={run} disabled={busy} size="sm">
          <RefreshCw className="w-3.5 h-3.5 mr-1" />
          {busy ? "Re-ingesting…" : "Re-ingest"}
        </Button>
      )}

      {lastResult && (
        <Alert
          variant={lastResult.ok ? "default" : "destructive"}
          className="py-2"
        >
          <AlertDescription className="text-xs flex items-start gap-2 min-w-0">
            {lastResult.ok ? (
              <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-emerald-600" />
            ) : (
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            )}
            <span className="whitespace-pre-wrap break-words min-w-0">
              {lastResult.message}
            </span>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}

export default function LiveDbReingestDialog({
  open,
  onOpenChange,
  projectId,
  onReingested,
}) {
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !projectId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const res = await listDataSources(projectId);
        const items = (res?.items || []).filter((s) => s.kind === "database");
        if (!cancelled) setSources(items);
      } catch (e) {
        toast.error("Could not load registered data sources", {
          description: e?.message || String(e),
        });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, projectId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="w-[min(96vw,52rem)] sm:max-w-[52rem] max-h-[92vh] overflow-y-auto overflow-x-hidden"
        data-testid="live-db-reingest-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Database className="w-4 h-4 text-slate-500" />
            Re-ingest Live Database
          </DialogTitle>
          <DialogDescription>
            LAMA stores only a SHA-256 hash of your DB password, so a live
            re-connect requires you to re-enter it once. This writes
            <code className="mx-1 text-xs">TABLE</code> and
            <code className="mx-1 text-xs">FK_HINT</code> entities into the KB
            so OLTP / OLAP / Migration generation can proceed.
          </DialogDescription>
        </DialogHeader>

        <Alert className="my-2">
          <Info className="w-4 h-4" />
          <AlertDescription className="text-xs">
            A prior <b>Build KB</b> can wipe live-DB entities. iter-14.56
            patched Build KB to preserve <code>from_live_db</code> rows going
            forward — this repair only needs to happen once per project.
          </AlertDescription>
        </Alert>

        {loading && (
          <div className="text-sm text-slate-500 py-6 text-center">
            Loading registered data sources…
          </div>
        )}

        {!loading && sources.length === 0 && (
          <div className="text-sm text-slate-500 py-6 text-center">
            No live database is registered for this project. Open{" "}
            <b>Discovery → Data Sources</b> to register one, or upload a
            <code className="mx-1">.sql</code> DDL dump.
          </div>
        )}

        {!loading && sources.length > 0 && (
          <div className="space-y-3 mt-1">
            {sources.map((s, i) => (
              <SourceRow
                key={`${s.db_type}-${s.host}-${s.database}-${i}`}
                src={s}
                onReingested={onReingested}
              />
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
