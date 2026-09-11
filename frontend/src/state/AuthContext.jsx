// iter-13.68 — Multi-tenant auth context.
// Holds {user, tenant, token, loading}. Persists token + a snapshot of
// user/tenant in localStorage so reloads don't bounce to /login.
// On mount, validates the persisted token via GET /api/auth/me — if
// 401, the axios interceptor in lib/api.js drops the token and redirects.
import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { login as loginApi, logout as logoutApi, me as meApi } from "@/lib/api";

const AuthContext = createContext(null);

const TOKEN_KEY = "lama:auth:token";
const USER_KEY = "lama:auth:user";
const TENANT_KEY = "lama:auth:tenant";

function loadSnapshot() {
  try {
    const token = window.localStorage.getItem(TOKEN_KEY) || "";
    const u = window.localStorage.getItem(USER_KEY);
    const t = window.localStorage.getItem(TENANT_KEY);
    return {
      token,
      user: u ? JSON.parse(u) : null,
      tenant: t ? JSON.parse(t) : null,
    };
  } catch (_) {
    return { token: "", user: null, tenant: null };
  }
}

export function AuthProvider({ children }) {
  const [{ token, user, tenant }, setState] = useState(loadSnapshot);
  const [loading, setLoading] = useState(!!loadSnapshot().token);

  // On mount: if we have a persisted token, validate it.
  useEffect(() => {
    if (!token) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await meApi();
        if (cancelled) return;
        const nextUser = r?.user || null;
        const nextTenant = r?.tenant || null;
        try {
          if (nextUser) window.localStorage.setItem(USER_KEY, JSON.stringify(nextUser));
          if (nextTenant) window.localStorage.setItem(TENANT_KEY, JSON.stringify(nextTenant));
          else window.localStorage.removeItem(TENANT_KEY);
        } catch (_) { /* */ }
        setState((s) => ({ ...s, user: nextUser, tenant: nextTenant }));
      } catch (_) {
        // The axios interceptor will handle 401 → /login redirect.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = useCallback(async (username, password) => {
    const r = await loginApi(username, password);
    try {
      window.localStorage.setItem(TOKEN_KEY, r.token);
      window.localStorage.setItem(USER_KEY, JSON.stringify(r.user));
      if (r.tenant) window.localStorage.setItem(TENANT_KEY, JSON.stringify(r.tenant));
      else window.localStorage.removeItem(TENANT_KEY);
    } catch (_) { /* */ }
    setState({ token: r.token, user: r.user, tenant: r.tenant || null });
    return r;
  }, []);

  const logout = useCallback(async () => {
    try { await logoutApi(); } catch (_) { /* server may be down; don't block UX */ }
    try {
      window.localStorage.removeItem(TOKEN_KEY);
      window.localStorage.removeItem(USER_KEY);
      window.localStorage.removeItem(TENANT_KEY);
    } catch (_) { /* */ }
    setState({ token: "", user: null, tenant: null });
    if (typeof window !== "undefined") window.location.replace("/login");
  }, []);

  const value = {
    token,
    user,
    tenant,
    loading,
    isAuthenticated: !!token && !!user,
    isSuperAdmin: !!user && user.role === "super_admin",
    login,
    logout,
    setSnapshot: setState,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}

