// iter-13.68 — Login screen. Username/password → POST /api/auth/login.
import { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { Loader2, Lock, User as UserIcon, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/state/AuthContext";

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login, isAuthenticated, isSuperAdmin } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  // If already logged in, bounce to landing (admins → /admin, users → /).
  useEffect(() => {
    if (isAuthenticated) {
      const next = (location.state && location.state.from) || (isSuperAdmin ? "/admin" : "/");
      navigate(next, { replace: true });
    }
  }, [isAuthenticated, isSuperAdmin, location.state, navigate]);

  const onSubmit = async (e) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      toast.error("Username and password are required.");
      return;
    }
    setBusy(true);
    try {
      const r = await login(username.trim(), password);
      toast.success(`Welcome, ${r.user.full_name || r.user.username}`);
      const next = (location.state && location.state.from) || (r.user.role === "super_admin" ? "/admin" : "/");
      navigate(next, { replace: true });
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || "Login failed";
      toast.error(detail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen w-screen flex items-center justify-center bg-bg" data-testid="login-page">
      <div className="bg-surface border border-border rounded-sm shadow-sm w-full max-w-sm p-7">
        <div className="flex items-center gap-3 mb-5">
          <div className="w-10 h-10 bg-brand text-fg flex items-center justify-center rounded-sm font-display font-bold text-base">
            L
          </div>
          <div>
            <div className="font-display font-bold text-xl leading-none tracking-tight text-fg">LAMA</div>
            <div className="text-micro uppercase tracking-widest text-fg-muted mt-1 leading-tight">
              Legacy Application Modernisation AI Studio
            </div>
          </div>
        </div>

        <h1 className="font-display font-bold text-base text-fg mb-1 flex items-center gap-1.5">
          <ShieldCheck className="w-4 h-4 text-brand" /> Sign in
        </h1>
        <p className="text-[12px] text-fg-muted mb-4">
          Enter your tenant credentials. Super-admins manage tenants &amp; users from the admin dashboard.
        </p>

        <form onSubmit={onSubmit} className="space-y-3">
          <label className="block">
            <span className="text-micro uppercase font-bold tracking-wider text-fg-muted">Username</span>
            <div className="mt-0.5 flex items-center border border-border rounded-sm px-2 py-1.5 focus-within:border-fg">
              <UserIcon className="w-3.5 h-3.5 text-fg-muted mr-1.5" />
              <input
                data-testid="login-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                className="flex-1 text-[13px] outline-none bg-transparent"
                placeholder="username"
                autoFocus
              />
            </div>
          </label>

          <label className="block">
            <span className="text-micro uppercase font-bold tracking-wider text-fg-muted">Password</span>
            <div className="mt-0.5 flex items-center border border-border rounded-sm px-2 py-1.5 focus-within:border-fg">
              <Lock className="w-3.5 h-3.5 text-fg-muted mr-1.5" />
              <input
                data-testid="login-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                className="flex-1 text-[13px] outline-none bg-transparent"
                placeholder="••••••••"
              />
            </div>
          </label>

          <button
            type="submit"
            data-testid="login-submit"
            disabled={busy}
            className="w-full mt-2 px-3 py-2 rounded-sm bg-brand text-fg font-bold text-sm border border-fg hover:bg-brand-hover disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
            Sign in
          </button>
        </form>

        <div className="mt-5 text-micro text-fg-muted leading-relaxed">
          Default super-admin (first-boot only): <code className="bg-bg px-1 rounded-sm">superadmin / lama-admin-2026</code>.
          Override via <code>LAMA_SUPERADMIN_USER</code> / <code>LAMA_SUPERADMIN_PASS</code> env vars and rotate as soon as you log in.
        </div>
      </div>
    </div>
  );
}

