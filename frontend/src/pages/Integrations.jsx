import React, { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Link } from "react-router-dom";
import {
  IdCard,
  Fingerprint,
  ReceiptText,
  FolderLock,
  PenSquare,
  IndianRupee,
  ScrollText,
  KeyRound,
  Wallet,
  UserCheck,
  BadgeCheck,
  Database,
  Banknote,
  Landmark,
  Coins,
  Network,
  Languages,
  HeartPulse,
  FileBadge,
  Plug,
  ChevronDown,
  ChevronRight,
  Loader2,
  CheckCircle2,
} from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
import {
  getProjectIntegrations,
  setProjectIntegration,
  injectIntegrations,
} from "@/lib/api";

// Map catalog "icon" string → lucide component. Add more as the catalog grows.
const ICONS = {
  "id-card": IdCard,
  fingerprint: Fingerprint,
  "receipt-text": ReceiptText,
  "folder-lock": FolderLock,
  "pen-square": PenSquare,
  "indian-rupee": IndianRupee,
  "scroll-text": ScrollText,
  "key-round": KeyRound,
  wallet: Wallet,
  "user-check": UserCheck,
  "badge-check": BadgeCheck,
  database: Database,
  banknote: Banknote,
  landmark: Landmark,
  coins: Coins,
  network: Network,
  languages: Languages,
  "heart-pulse": HeartPulse,
  "file-badge": FileBadge,
};

const CATEGORY_COLOR = {
  KYC: "bg-blue-100 text-blue-700 border-blue-200",
  Tax: "bg-emerald-100 text-emerald-700 border-emerald-200",
  Identity: "bg-violet-100 text-violet-700 border-violet-200",
  Payments: "bg-amber-100 text-amber-700 border-amber-200",
  Language: "bg-pink-100 text-pink-700 border-pink-200",
  Health: "bg-rose-100 text-rose-700 border-rose-200",
  Observability: "bg-slate-100 text-slate-700 border-slate-200",
};

// Four top-level sections on the Integrations page, driven by `item.family`
// (set by backend/integrations/catalog.py). A `category` (KYC / Identity /
// Payments / Health / Language / ...) can appear in multiple sections — e.g.
// "Identity" lives across govt-india, dpg-india and dpg.
//   - govt:      Indian govt-services catalog (PAN/Aadhaar/GSTIN/...)
//   - dpg-india: India-specific DPG/DPI stack (Bharatkosh, PFMS, API Setu,
//                Bhashini, ABDM, Jeevan Pramaan, ...)
//   - dpg:       Platform-style DPG/DPI projects (MOSIP eSignet, Inji,
//                MOSIP Auth, DIVOC, Sunbird RC, OpenG2P)
//   - utilities: Cross-cutting utilities injected into every service
//                (audit logging today; future: tracing, rate-limit, ...)
const SECTIONS = [
  {
    id: "govt",
    title: "Govt Services (India)",
    subtitle: "PAN / Aadhaar / GSTIN / DigiLocker / e-Sign / UPI",
    families: ["govt-india"],
    defaultOpen: true,
  },
  {
    id: "dpg-india",
    title: "DPG / DPI — Indian Stack",
    subtitle:
      "Bharatkosh (NTRP) / PFMS / API Setu / Bhashini / ABDM / Jeevan Pramaan",
    families: ["dpg-india"],
    defaultOpen: true,
  },
  {
    id: "dpg",
    title: "DPG / DPI — Platforms",
    subtitle:
      "MOSIP eSignet / Inji / MOSIP Auth / DIVOC / Sunbird RC / OpenG2P",
    families: ["dpg"],
    defaultOpen: true,
  },
  {
    id: "utilities",
    title: "Utilities",
    subtitle:
      "Cross-cutting concerns injected into every generated service " +
      "(audit logging, observability, …)",
    families: ["utility"],
    defaultOpen: true,
  },
];

const _familyToSection = (() => {
  const m = {};
  SECTIONS.forEach((s) =>
    (s.families || []).forEach((f) => {
      m[f] = s.id;
    }),
  );
  return m;
})();

// Backward-compat: catalog entries without an explicit `family` are inferred
// from category — Observability → utility, anything else → govt-india.
const sectionIdFor = (item) => {
  const fam =
    item?.family ||
    (item?.category === "Observability" ? "utility" : "govt-india");
  return _familyToSection[fam] || "utilities";
};

export default function IntegrationsPage() {
  const { active } = useProjects();
  const projectId = active?.id;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [expanded, setExpanded] = useState({});
  const [overrides, setOverrides] = useState({});
  const [injecting, setInjecting] = useState(false);
  const [lastInject, setLastInject] = useState(null);
  const [sectionsOpen, setSectionsOpen] = useState(() =>
    SECTIONS.reduce((acc, s) => {
      acc[s.id] = !!s.defaultOpen;
      return acc;
    }, {}),
  );

  const load = async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const d = await getProjectIntegrations(projectId);
      setData(d);
      const ov = {};
      (d.items || []).forEach((it) => {
        ov[it.id] = { ...(it.config_overrides || {}) };
      });
      setOverrides(ov);
    } catch (e) {
      toast.error(`Failed to load integrations: ${e?.message || e}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const enabledCount = useMemo(
    () => (data?.items || []).filter((i) => i.enabled).length,
    [data],
  );

  // Group items into the two top-level sections (govt + utilities).
  // Preserves the catalog ordering inside each section.
  const itemsBySection = useMemo(() => {
    const groups = {};
    SECTIONS.forEach((s) => {
      groups[s.id] = [];
    });
    (data?.items || []).forEach((it) => {
      const sid = sectionIdFor(it);
      (groups[sid] = groups[sid] || []).push(it);
    });
    return groups;
  }, [data]);

  const toggle = async (item) => {
    if (!projectId) return;
    setBusyId(item.id);
    try {
      await setProjectIntegration(
        projectId,
        item.id,
        !item.enabled,
        overrides[item.id] || {},
      );
      toast.success(
        `${item.label} ${!item.enabled ? "enabled" : "disabled"} for this project`,
      );
      await load();
    } catch (e) {
      toast.error(`Failed: ${e?.message || e}`);
    } finally {
      setBusyId("");
    }
  };

  const saveOverrides = async (item) => {
    if (!projectId) return;
    setBusyId(item.id);
    try {
      await setProjectIntegration(
        projectId,
        item.id,
        item.enabled,
        overrides[item.id] || {},
      );
      toast.success("Defaults saved");
      await load();
    } catch (e) {
      toast.error(`Failed: ${e?.message || e}`);
    } finally {
      setBusyId("");
    }
  };

  const inject = async () => {
    if (!projectId) return;
    if (enabledCount === 0) {
      toast.message("Nothing to inject", {
        description: "Enable at least one integration first.",
      });
      return;
    }
    setInjecting(true);
    try {
      const r = await injectIntegrations(projectId);
      setLastInject(r);
      toast.success(
        `Injected ${r.injected} integration(s) (${r.files.length} files written) — ${r.language} target`,
      );
      await load();
    } catch (e) {
      toast.error(`Inject failed: ${e?.message || e}`);
    } finally {
      setInjecting(false);
    }
  };

  const renderCard = (item) => {
    const Icon = ICONS[item.icon] || Plug;
    const isExpanded = !!expanded[item.id];
    const cat =
      CATEGORY_COLOR[item.category] ||
      "bg-slate-100 text-slate-700 border-slate-200";
    // iter-13.67 — Config-completeness signal.
    // An integration is "fully configured" when every declared env var has
    // a non-empty effective value (either a user-supplied override or a
    // catalogue default). Items with zero env vars are trivially configured.
    // The toggle, icon swatch and card border switch to emerald-green when
    // (enabled && fullyConfigured) so the user can scan the grid for ready-
    // to-inject services at a glance.
    const fullyConfigured = (item.env_vars || []).every((ev) => {
      const v = overrides[item.id]?.[ev.name] ?? ev.default ?? "";
      return String(v).trim() !== "";
    });
    const enabledReady = item.enabled && fullyConfigured;
    return (
      <div
        key={item.id}
        data-testid={`integration-card-${item.id}`}
        className={`border rounded-sm bg-white p-4 ${
          enabledReady
            ? "border-emerald-500 ring-1 ring-emerald-500"
            : item.enabled
            ? "border-[#FFE600] ring-1 ring-[#FFE600]"
            : "border-slate-200"
        }`}
      >
        <div className="flex items-start gap-3">
          <div
            className={`w-10 h-10 rounded-sm flex items-center justify-center shrink-0 ${
              enabledReady
                ? "bg-emerald-500 text-white"
                : item.enabled
                ? "bg-[#FFE600] text-[#2E2E38]"
                : "bg-slate-100 text-slate-500"
            }`}
          >
            <Icon className="w-5 h-5" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-display font-bold text-[15px] text-[#2E2E38] truncate">
                {item.label}
              </h3>
              <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-sm border ${cat}`}>
                {item.category}
              </span>
              {item.last_injected_at && (
                <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-sm border border-emerald-200 bg-emerald-50 text-emerald-700">
                  Injected
                </span>
              )}
            </div>
            <p className="text-xs text-slate-600 mt-1 leading-snug">{item.description}</p>
            <div className="text-[11px] text-slate-500 mt-2">
              <strong>Providers:</strong> {(item.providers || []).join(", ") || "—"}
            </div>
            {(item.endpoints || []).length > 0 && (
              <div className="text-[11px] text-slate-500 mt-0.5">
                <strong>Endpoints:</strong>{" "}
                {item.endpoints.map((e, i) => (
                  <span key={i} className="font-mono">
                    {i > 0 ? " · " : ""}
                    {e.method} {e.path}
                  </span>
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            data-testid={`integration-toggle-${item.id}`}
            onClick={() => toggle(item)}
            disabled={busyId === item.id}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition shrink-0 ${
              enabledReady ? "bg-emerald-500" : item.enabled ? "bg-[#FFE600]" : "bg-slate-300"
            } ${busyId === item.id ? "opacity-50" : ""}`}
            title={
              enabledReady
                ? "Enabled · fully configured"
                : item.enabled
                ? "Enabled · configuration incomplete"
                : "Disabled"
            }
          >
            <span
              className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
                item.enabled ? "translate-x-5" : "translate-x-0.5"
              }`}
            />
          </button>
        </div>

        <button
          type="button"
          onClick={() => setExpanded((s) => ({ ...s, [item.id]: !s[item.id] }))}
          className="mt-3 text-xs text-slate-600 hover:text-[#2E2E38] flex items-center gap-1"
          data-testid={`integration-expand-${item.id}`}
        >
          {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          {isExpanded ? "Hide" : "Show"} environment variables ({item.env_vars?.length || 0})
        </button>

        {isExpanded && (
          <div className="mt-3 p-3 rounded-sm bg-slate-50 border border-slate-200">
            <p className="text-[11px] text-slate-500 mb-2">
              These are written into <code>.env.integrations.example</code> on inject.
              Override defaults below (the user can also edit them at deploy time).
            </p>
            <div className="space-y-2">
              {(item.env_vars || []).map((ev) => {
                const cur = overrides[item.id]?.[ev.name] ?? ev.default ?? "";
                return (
                  <div key={ev.name} className="grid grid-cols-12 gap-2 items-start">
                    <label className="col-span-4 text-[11px] font-mono text-slate-700 break-all pt-1.5">
                      {ev.name}
                    </label>
                    <input
                      type="text"
                      className="col-span-5 text-xs border border-slate-300 rounded-sm px-2 py-1 font-mono bg-white"
                      placeholder={ev.default || "(unset)"}
                      value={cur}
                      onChange={(e) =>
                        setOverrides((s) => ({
                          ...s,
                          [item.id]: {
                            ...(s[item.id] || {}),
                            [ev.name]: e.target.value,
                          },
                        }))
                      }
                    />
                    <div className="col-span-3 text-[11px] text-slate-500 pt-1.5">{ev.desc}</div>
                  </div>
                );
              })}
            </div>
            <div className="mt-3 flex justify-end">
              <button
                type="button"
                onClick={() => saveOverrides(item)}
                disabled={busyId === item.id}
                className="text-xs px-3 py-1.5 rounded-sm bg-[#2E2E38] text-white hover:bg-black disabled:opacity-50"
                data-testid={`integration-save-${item.id}`}
              >
                Save defaults
              </button>
            </div>
          </div>
        )}
      </div>
    );
  };

  if (!projectId) {
    return (
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <header className="bg-white border-b border-[#E6E6E6] px-6 py-3">
          <div className="text-[10px] uppercase tracking-widest text-slate-500">Stage 4 · Add-on</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38] flex items-center gap-2">
            <Plug className="w-4 h-4 text-[#FFE600]" />
            Integration & Utility Services
          </h1>
        </header>
        <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-[#F6F6FA]">
          <div className="text-slate-500 text-sm">Select an active project to manage integrations.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0" data-testid="integrations-page">
      <header className="bg-white border-b border-[#E6E6E6] px-6 py-3 flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-widest text-slate-500">Stage 4 · Add-on</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38] flex items-center gap-2">
            <Plug className="w-4 h-4 text-[#FFE600]" />
            Integration & Utility Services
          </h1>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="text-xs text-slate-500 text-right hidden md:block">
            <div>
              {enabledCount} of {data?.items?.length ?? 0} enabled
            </div>
            <div>
              Target lang:{" "}
              <span className="font-mono text-slate-700">{data?.resolved_language || "—"}</span>
            </div>
          </div>
          <button
            type="button"
            data-testid="inject-integrations-btn"
            onClick={inject}
            disabled={injecting || enabledCount === 0}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-sm bg-[#FFE600] text-[#2E2E38] font-bold text-sm border border-[#2E2E38] hover:bg-[#FFF38A] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {injecting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plug className="w-4 h-4" />}
            Inject into codebase
          </button>
          <Link
            to="/code-gen"
            className="text-xs text-slate-600 hover:text-[#2E2E38] underline underline-offset-2"
          >
            View in Code Gen →
          </Link>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-[#F6F6FA]">
        <div className="max-w-6xl mx-auto">
          <p className="text-sm text-slate-600 mb-4 max-w-3xl">
            Enable any of the Indian govt services below and click{" "}
            <strong>Inject into codebase</strong>. A mock-first, environment-configurable
            client + FastAPI router lands directly in your generated service tree — flip{" "}
            <code className="text-xs bg-slate-200 px-1 py-0.5 rounded">*_MODE=live</code>{" "}
            and add credentials to switch from mocks to real provider calls. No code changes.
          </p>

          {lastInject && !lastInject.skipped && (
            <div
              data-testid="inject-result"
              className="mb-6 p-3 rounded-sm border border-emerald-200 bg-emerald-50 text-emerald-800 text-sm flex items-start gap-2"
            >
              <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
              <div>
                Wrote {lastInject.files.length} file(s) into{" "}
                <code className="text-xs">codegen_files</code> as language{" "}
                <span className="font-mono">{lastInject.language}</span>:
                <ul className="mt-1 list-disc list-inside text-xs text-emerald-700">
                  {lastInject.files.map((f) => (
                    <li key={f.file_path}>
                      {f.file_path} <span className="text-emerald-500">(v{f.version})</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {loading && !data ? (
            <div className="text-slate-500 text-sm flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading catalog…
            </div>
          ) : (data?.items || []).length === 0 ? (
            <div className="p-6 text-sm text-slate-500 text-center bg-white border border-slate-200 rounded-sm">
              No integrations available. Backend may not have registered the /api/integrations router yet — restart the backend.
            </div>
          ) : (
            <div className="space-y-4">
              {SECTIONS.map((section) => {
                const items = itemsBySection[section.id] || [];
                if (items.length === 0) return null;
                const open = !!sectionsOpen[section.id];
                const sectionEnabled = items.filter((i) => i.enabled).length;
                return (
                  <section
                    key={section.id}
                    data-testid={`integrations-section-${section.id}`}
                    className="border border-slate-200 rounded-sm bg-white overflow-hidden"
                  >
                    <button
                      type="button"
                      data-testid={`integrations-section-toggle-${section.id}`}
                      onClick={() =>
                        setSectionsOpen((s) => ({ ...s, [section.id]: !s[section.id] }))
                      }
                      aria-expanded={open}
                      className="w-full flex items-center gap-3 px-4 py-3 border-b border-slate-200 bg-slate-50 hover:bg-slate-100 transition"
                    >
                      {open ? (
                        <ChevronDown className="w-4 h-4 text-slate-500 shrink-0" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-slate-500 shrink-0" />
                      )}
                      <div className="text-left min-w-0 flex-1">
                        <h2 className="font-display font-bold text-sm text-[#2E2E38] tracking-tight">
                          {section.title}
                        </h2>
                        <p className="text-[11px] text-slate-500 truncate">
                          {section.subtitle}
                        </p>
                      </div>
                      <span className="text-[11px] uppercase tracking-wider px-2 py-0.5 rounded-sm border border-slate-200 bg-white text-slate-600 shrink-0">
                        {sectionEnabled} / {items.length} enabled
                      </span>
                    </button>
                    {open && (
                      <div className="p-4">
                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                          {items.map((item) => renderCard(item))}
                        </div>
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
