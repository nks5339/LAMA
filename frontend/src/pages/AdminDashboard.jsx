// iter-13.68 — Super-admin dashboard.
// Tabs: Tenants · Users · Analytics. Recharts bar/pie for per-tenant
// project count + token usage + cost.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Building2,
  ChevronRight as ChevronRightIcon,
  Coins,
  Loader2,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserCog,
  Users,
} from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/state/AuthContext";
import {
  adminCreateTenant,
  adminCreateUser,
  adminDashboard,
  adminDeleteTenant,
  adminDeleteUser,
  adminListTenants,
  adminListUsers,
  adminUpdateTenant,
  adminUpdateUser,
} from "@/lib/api";

const PIE_COLORS = ["#FFE600", "#2E2E38", "#9DBF1A", "#33ABA6", "#A26FF7", "#F8B4B4", "#7A4FFE", "#FFA500"];

function fmtNumber(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString();
}
function fmtUsd(n) {
  if (n == null) return "$0";
  return `$${Number(n).toFixed(4)}`;
}

export default function AdminDashboard() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [tab, setTab] = useState("tenants");

  if (!user) return null;
  if (user.role !== "super_admin") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="bg-surface border border-rose-200 p-6 rounded-sm max-w-md text-center">
          <div className="text-rose-600 font-bold mb-2">403 — Super-admin only</div>
          <button onClick={() => navigate("/")} className="text-sm underline text-fg">
            Go to your project workspace →
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-screen min-h-0 bg-bg flex flex-col overflow-hidden" data-testid="admin-page">
      <header className="bg-surface border-b-2 border-brand px-6 py-3 flex items-center justify-between gap-4">
        <div>
          <div className="text-micro uppercase tracking-widest text-fg-muted">LAMA Admin</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-brand" /> Tenants · Users · Analytics
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => navigate("/")}
            className="text-[12px] px-3 py-1.5 border border-border rounded-sm hover:bg-bg"
          >
            Workspace →
          </button>
          <button
            type="button"
            data-testid="admin-logout"
            onClick={logout}
            className="text-[12px] px-3 py-1.5 border border-border rounded-sm hover:bg-rose-50 text-rose-600 flex items-center gap-1"
          >
            <LogOut className="w-3 h-3" /> Sign out
          </button>
        </div>
      </header>

      <div className="bg-surface border-b border-border px-6 flex items-center gap-1">
        {[
          { id: "tenants", label: "Tenants", icon: Building2 },
          { id: "users", label: "Users", icon: Users },
          { id: "analytics", label: "Analytics", icon: Coins },
        ].map((t) => {
          const Icon = t.icon;
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              data-testid={`admin-tab-${t.id}`}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1 text-[12px] px-3 py-2 border-b-2 transition-colors ${
                active ? "border-brand text-fg font-bold" : "border-transparent text-fg-muted hover:text-fg"
              }`}
            >
              <Icon className="w-3 h-3" /> {t.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-6xl mx-auto">
          {tab === "tenants" && <TenantsTab />}
          {tab === "users" && <UsersTab />}
          {tab === "analytics" && <AnalyticsTab />}
        </div>
      </div>
    </div>
  );
}

// ---------- Tenants ----------
function TenantsTab() {
  const [tenants, setTenants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const r = await adminListTenants();
      setTenants(r.tenants || []);
    } catch (e) {
      toast.error("Could not load tenants", { description: e?.response?.data?.detail || e.message });
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const create = async (e) => {
    e.preventDefault();
    if (!name.trim()) { toast.error("Tenant name is required"); return; }
    setBusy(true);
    try {
      await adminCreateTenant({ name: name.trim(), slug: slug.trim(), description: description.trim() });
      toast.success("Tenant created");
      setName(""); setSlug(""); setDescription(""); setShowNew(false);
      await load();
    } catch (e) {
      toast.error("Create failed", { description: e?.response?.data?.detail || e.message });
    } finally { setBusy(false); }
  };

  const toggleActive = async (t) => {
    try {
      await adminUpdateTenant(t.id, { is_active: !t.is_active });
      await load();
    } catch (e) { toast.error("Update failed", { description: e?.response?.data?.detail || e.message }); }
  };

  const remove = async (t) => {
    if (!window.confirm(`Delete tenant "${t.name}"? Tenants with projects or users cannot be deleted.`)) return;
    try {
      await adminDeleteTenant(t.id);
      toast.success("Tenant deleted");
      await load();
    } catch (e) { toast.error("Delete failed", { description: e?.response?.data?.detail || e.message }); }
  };

  return (
    <div className="space-y-4" data-testid="admin-tenants-tab">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="font-display font-bold text-fg">Tenants</h2>
          <p className="text-[12px] text-fg-muted">Each tenant has its own users + projects. Default tenant cannot be deleted.</p>
        </div>
        <div className="flex gap-2">
          <button onClick={load} className="text-[12px] px-2 py-1 border border-border rounded-sm hover:bg-surface flex items-center gap-1">
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
          <button
            data-testid="admin-tenant-new"
            onClick={() => setShowNew((s) => !s)}
            className="text-[12px] px-3 py-1.5 rounded-sm bg-brand text-fg font-bold border border-fg hover:bg-brand-hover flex items-center gap-1"
          >
            <Plus className="w-3 h-3" /> New tenant
          </button>
        </div>
      </div>

      {showNew && (
        <form onSubmit={create} className="bg-surface border border-border rounded-sm p-3 space-y-2">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <input
              data-testid="admin-tenant-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Tenant name"
              className="text-[12px] border border-border rounded-sm px-2 py-1.5"
            />
            <input
              data-testid="admin-tenant-slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="Slug (optional, auto-derived)"
              className="text-[12px] border border-border rounded-sm px-2 py-1.5 font-mono"
            />
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Description (optional)"
              className="text-[12px] border border-border rounded-sm px-2 py-1.5"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setShowNew(false)} className="text-[12px] px-3 py-1.5 border border-border rounded-sm">
              Cancel
            </button>
            <button type="submit" disabled={busy} className="text-[12px] px-3 py-1.5 rounded-sm bg-brand text-fg font-bold border border-fg flex items-center gap-1 disabled:opacity-50">
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />} Create
            </button>
          </div>
        </form>
      )}

      <div className="bg-surface border border-border rounded-sm overflow-hidden">
        {loading ? (
          <div className="p-6 text-[12px] text-fg-muted flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> Loading…</div>
        ) : tenants.length === 0 ? (
          <div className="p-6 text-[12px] text-fg-muted">No tenants yet.</div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-bg">
              <tr className="text-left">
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Name</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Slug</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Projects</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Users</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Status</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Created</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((t) => (
                <tr key={t.id} data-testid={`admin-tenant-row-${t.id}`} className="border-t border-surface-2">
                  <td className="px-3 py-2 font-bold">{t.name}</td>
                  <td className="px-3 py-2 font-mono text-fg-muted">{t.slug || t.id}</td>
                  <td className="px-3 py-2">{t.project_count}</td>
                  <td className="px-3 py-2">{t.user_count}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => toggleActive(t)} className={`text-micro uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold ${t.is_active ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}>
                      {t.is_active ? "Active" : "Disabled"}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-fg-muted">{(t.created_at || "").slice(0, 10)}</td>
                  <td className="px-3 py-2 text-right">
                    {t.id !== "tenant_default" && (
                      <button
                        onClick={() => remove(t)}
                        title="Delete"
                        className="p-1 text-rose-500 hover:bg-rose-50 rounded-sm"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------- Users ----------
function UsersTab() {
  const [tenants, setTenants] = useState([]);
  const [users, setUsers] = useState([]);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState({ username: "", password: "", full_name: "", email: "", role: "tenant_user", tenant_id: "" });
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [t, u] = await Promise.all([adminListTenants(), adminListUsers(filter || undefined)]);
      setTenants(t.tenants || []);
      setUsers(u.users || []);
    } catch (e) {
      toast.error("Load failed", { description: e?.response?.data?.detail || e.message });
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [filter]); // eslint-disable-line

  const create = async (e) => {
    e.preventDefault();
    if (!form.username.trim()) { toast.error("Username required"); return; }
    if (form.password.length < 8) { toast.error("Password must be at least 8 characters"); return; }
    if (form.role !== "super_admin" && !form.tenant_id) { toast.error("Tenant is required for non-super-admin users"); return; }
    setBusy(true);
    try {
      await adminCreateUser(form);
      toast.success("User created");
      setForm({ username: "", password: "", full_name: "", email: "", role: "tenant_user", tenant_id: form.tenant_id });
      setShowNew(false);
      await load();
    } catch (e) {
      toast.error("Create failed", { description: e?.response?.data?.detail || e.message });
    } finally { setBusy(false); }
  };

  const toggleActive = async (u) => {
    try { await adminUpdateUser(u.id, { is_active: !u.is_active }); await load(); }
    catch (e) { toast.error("Update failed", { description: e?.response?.data?.detail || e.message }); }
  };

  const resetPwd = async (u) => {
    const np = window.prompt(`Reset password for ${u.username}? Enter the new password (min 8 chars):`);
    if (!np) return;
    try { await adminUpdateUser(u.id, { password: np }); toast.success("Password reset"); }
    catch (e) { toast.error("Reset failed", { description: e?.response?.data?.detail || e.message }); }
  };

  const remove = async (u) => {
    if (!window.confirm(`Delete user "${u.username}"?`)) return;
    try { await adminDeleteUser(u.id); toast.success("Deleted"); await load(); }
    catch (e) { toast.error("Delete failed", { description: e?.response?.data?.detail || e.message }); }
  };

  return (
    <div className="space-y-4" data-testid="admin-users-tab">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h2 className="font-display font-bold text-fg">Users</h2>
          <p className="text-[12px] text-fg-muted">Create tenant_user / tenant_admin accounts. Super-admins manage everyone.</p>
        </div>
        <div className="flex gap-2">
          <select value={filter} onChange={(e) => setFilter(e.target.value)} className="text-[12px] border border-border rounded-sm px-2 py-1.5">
            <option value="">All tenants</option>
            {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <button onClick={() => setShowNew((s) => !s)} className="text-[12px] px-3 py-1.5 rounded-sm bg-brand text-fg font-bold border border-fg hover:bg-brand-hover flex items-center gap-1">
            <Plus className="w-3 h-3" /> New user
          </button>
        </div>
      </div>

      {showNew && (
        <form onSubmit={create} className="bg-surface border border-border rounded-sm p-3 grid grid-cols-1 md:grid-cols-3 gap-x-3 gap-y-2">
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Username <span className="text-red-600">*</span></span>
            <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="e.g. jsmith" autoComplete="off" className="text-[12px] border border-border rounded-sm px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Password <span className="text-red-600">*</span></span>
            <input value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} type="password" placeholder="min 8 characters" autoComplete="new-password" className="text-[12px] border border-border rounded-sm px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Full name</span>
            <input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} placeholder="optional" className="text-[12px] border border-border rounded-sm px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Email</span>
            <input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="optional" className="text-[12px] border border-border rounded-sm px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Role <span className="text-red-600">*</span></span>
            <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="text-[12px] border border-border rounded-sm px-2 py-1.5">
              <option value="tenant_user">tenant_user</option>
              <option value="tenant_admin">tenant_admin</option>
              <option value="super_admin">super_admin</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-micro uppercase tracking-wider text-fg-muted font-bold">Tenant {form.role !== "super_admin" && <span className="text-red-600">*</span>}</span>
            <select value={form.tenant_id} onChange={(e) => setForm({ ...form, tenant_id: e.target.value })} className="text-[12px] border border-border rounded-sm px-2 py-1.5 disabled:bg-surface-2" disabled={form.role === "super_admin"}>
              <option value="">— pick tenant —</option>
              {tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </label>
          <div className="md:col-span-3 flex justify-end gap-2 pt-1">
            <button type="button" onClick={() => setShowNew(false)} className="text-[12px] px-3 py-1.5 border border-border rounded-sm">Cancel</button>
            <button type="submit" disabled={busy} className="text-[12px] px-3 py-1.5 rounded-sm bg-brand text-fg font-bold border border-fg flex items-center gap-1 disabled:opacity-50">
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />} Create
            </button>
          </div>
        </form>
      )}

      <div className="bg-surface border border-border rounded-sm overflow-hidden">
        {loading ? (
          <div className="p-6 text-[12px] text-fg-muted flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> Loading…</div>
        ) : users.length === 0 ? (
          <div className="p-6 text-[12px] text-fg-muted">No users.</div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-bg">
              <tr className="text-left">
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Username</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Name</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Role</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Tenant</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Status</th>
                <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Last login</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const tenant = tenants.find((t) => t.id === u.tenant_id);
                return (
                  <tr key={u.id} data-testid={`admin-user-row-${u.id}`} className="border-t border-surface-2">
                    <td className="px-3 py-2 font-mono">{u.username}</td>
                    <td className="px-3 py-2">{u.full_name || <span className="text-fg-muted">—</span>}</td>
                    <td className="px-3 py-2">
                      <span className={`text-micro uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold ${u.role === "super_admin" ? "bg-brand text-fg" : u.role === "tenant_admin" ? "bg-blue-100 text-blue-700" : "bg-surface-2 text-fg-muted"}`}>
                        {u.role}
                      </span>
                    </td>
                    <td className="px-3 py-2">{u.tenant_id === "*" ? <em className="text-fg-muted">all</em> : (tenant?.name || u.tenant_id)}</td>
                    <td className="px-3 py-2">
                      <button onClick={() => toggleActive(u)} className={`text-micro uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold ${u.is_active ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}>
                        {u.is_active ? "Active" : "Disabled"}
                      </button>
                    </td>
                    <td className="px-3 py-2 text-fg-muted">{u.last_login_at ? u.last_login_at.slice(0, 16).replace("T", " ") : "—"}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      <button onClick={() => resetPwd(u)} title="Reset password" className="p-1 hover:bg-bg rounded-sm mr-1">
                        <UserCog className="w-3 h-3" />
                      </button>
                      <button onClick={() => remove(u)} title="Delete" className="p-1 text-rose-500 hover:bg-rose-50 rounded-sm">
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------- Analytics ----------
function AnalyticsTab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try { setData(await adminDashboard()); }
    catch (e) { toast.error("Dashboard load failed", { description: e?.response?.data?.detail || e.message }); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const chartData = useMemo(() => {
    if (!data) return [];
    return (data.tenants || []).map((t) => ({
      name: t.name,
      projects: t.project_count,
      users: t.user_count,
      tokens: t.total_tokens,
      cost: Number((t.total_cost_usd || 0).toFixed(4)),
      calls: t.llm_calls,
    }));
  }, [data]);

  if (loading) {
    return <div className="text-[12px] text-fg-muted flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" /> Loading dashboard…</div>;
  }
  if (!data) return <div className="text-[12px] text-fg-muted">No data.</div>;

  const totals = data.totals || {};
  return (
    <div className="space-y-5" data-testid="admin-analytics-tab">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="font-display font-bold text-fg">Tenant analytics</h2>
          <p className="text-[12px] text-fg-muted">
            Generated {(data.generated_at || "").slice(0, 19).replace("T", " ")} UTC.{" "}
            <button onClick={load} className="underline">refresh</button>
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[
          { label: "Tenants", value: totals.tenant_count },
          { label: "Projects", value: totals.project_count },
          { label: "Users", value: totals.user_count },
          { label: "Total tokens", value: fmtNumber(totals.total_tokens) },
          { label: "Total cost (USD)", value: fmtUsd(totals.total_cost_usd) },
        ].map((s, i) => (
          <div key={i} className="bg-surface border border-border rounded-sm p-3">
            <div className="text-micro uppercase tracking-wider text-fg-muted font-bold">{s.label}</div>
            <div className="text-2xl font-display font-bold text-fg mt-1">{s.value ?? "—"}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <div className="bg-surface border border-border rounded-sm p-3">
          <div className="text-[12px] font-bold text-fg mb-2 flex items-center gap-1"><ChevronRightIcon className="w-3 h-3" /> Projects per tenant</div>
          <div style={{ height: 280 }}>
            <ResponsiveContainer>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Legend />
                <Bar dataKey="projects" fill="#FFE600" name="Projects" />
                <Bar dataKey="users" fill="#2E2E38" name="Users" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="bg-surface border border-border rounded-sm p-3">
          <div className="text-[12px] font-bold text-fg mb-2 flex items-center gap-1"><ChevronRightIcon className="w-3 h-3" /> Token usage per tenant</div>
          <div style={{ height: 280 }}>
            <ResponsiveContainer>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip formatter={(v) => fmtNumber(v)} />
                <Legend />
                <Bar dataKey="tokens" fill="#33ABA6" name="Tokens" />
                <Bar dataKey="calls" fill="#A26FF7" name="LLM calls" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="bg-surface border border-border rounded-sm p-3 lg:col-span-2">
          <div className="text-[12px] font-bold text-fg mb-2 flex items-center gap-1"><ChevronRightIcon className="w-3 h-3" /> Cost share (USD)</div>
          <div style={{ height: 320 }}>
            <ResponsiveContainer>
              <PieChart>
                <Pie data={chartData.filter((d) => d.cost > 0)} dataKey="cost" nameKey="name" innerRadius={60} outerRadius={110} label={(e) => `${e.name}: $${e.value}`}>
                  {chartData.filter((d) => d.cost > 0).map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                </Pie>
                <Tooltip formatter={(v) => `$${Number(v).toFixed(4)}`} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="bg-surface border border-border rounded-sm overflow-hidden">
        <table className="w-full text-[12px]">
          <thead className="bg-bg">
            <tr className="text-left">
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Tenant</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Projects</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Users</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">LLM calls</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Tokens</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Cost (USD)</th>
              <th className="px-3 py-2 font-bold uppercase text-micro tracking-wider">Status</th>
            </tr>
          </thead>
          <tbody>
            {(data.tenants || []).map((t) => (
              <tr key={t.tenant_id} className="border-t border-surface-2">
                <td className="px-3 py-2 font-bold">{t.name}</td>
                <td className="px-3 py-2">{t.project_count}</td>
                <td className="px-3 py-2">{t.user_count}</td>
                <td className="px-3 py-2">{fmtNumber(t.llm_calls)}</td>
                <td className="px-3 py-2">{fmtNumber(t.total_tokens)}</td>
                <td className="px-3 py-2">{fmtUsd(t.total_cost_usd)}</td>
                <td className="px-3 py-2">
                  <span className={`text-micro uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold ${t.is_active ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"}`}>
                    {t.is_active ? "Active" : "Disabled"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

