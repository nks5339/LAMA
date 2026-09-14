import { useState, useEffect } from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Toaster } from "sonner";
import { Menu } from "lucide-react";
import { ProjectProvider } from "@/state/ProjectContext";
import { useProjects } from "@/state/ProjectContext";
import { AuthProvider, useAuth } from "@/state/AuthContext";
import Sidebar from "@/components/Sidebar";
import StageProgress from "@/components/StageProgress";
import DiscoveryPage from "@/pages/DiscoveryV2";
import DataModelPage from "@/pages/DataModel";
import ArchitecturePage from "@/pages/Architecture";
import CodeGenPage from "@/pages/CodeGen";
import IntegrationsPage from "@/pages/Integrations";
import ConsolePage from "@/pages/Console";
import OntologyStudioPage from "@/pages/OntologyStudio";
import LivingPage from "@/pages/Living";
import PromptLibraryPage from "@/pages/PromptLibrary";
import AuditLogPage from "@/pages/AuditLog";
import GitHubSettingsPage from "@/pages/GitHubSettings";
import AboutUsPage from "@/pages/AboutUs";
import LoginPage from "@/pages/Login";
import AdminDashboard from "@/pages/AdminDashboard";
import UIShowcase from "@/pages/UIShowcase";
import GapAnalyzerPage from "@/pages/GapAnalyzer";
import TransformerPage from "@/pages/Transformer";
import MiniConsole from "@/components/MiniConsole";
import TopToolbar from "@/components/TopToolbar";
import StatusBar from "@/components/StatusBar";
import CommandPalette from "@/components/CommandPalette";
import { useIsMobile } from "@/hooks/useBreakpoint";

// iter-13.89 — Responsive Shell.
//   • Desktop  (≥ lg, 1024+): inline sidebar at left, page content fills
//     the rest. Same as before.
//   • Mobile / tablet (< lg): sidebar is an off-canvas drawer. A
//     slim top app-bar with brand + hamburger sits above the page so
//     the user can summon the drawer at any time.
// iter-14.0 — Professional UI/UX enhancements:
//   • Top toolbar with breadcrumbs, search, user profile
//   • Status bar at bottom with connection status, token stats
//   • Command palette (Cmd/Ctrl+K) for quick navigation
function Shell({ children }) {
  const isMobile = useIsMobile();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const location = useLocation();

  // Global keyboard shortcut: Cmd/Ctrl+K for command palette
  useEffect(() => {
    const handler = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCommandPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Close the drawer whenever we move from mobile to desktop (otherwise
  // the fixed overlay's transform stays applied off-screen and clicks
  // intermittently misroute on resize).
  useEffect(() => {
    if (!isMobile && drawerOpen) setDrawerOpen(false);
  }, [isMobile, drawerOpen]);

  // Also close after every route change so the drawer doesn't linger
  // when the user taps a stage button.
  useEffect(() => {
    if (drawerOpen) setDrawerOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden bg-[#F6F6FA]">
      {/* Command Palette (Cmd/Ctrl+K) */}
      <CommandPalette 
        isOpen={commandPaletteOpen} 
        onClose={() => setCommandPaletteOpen(false)} 
      />

      {/* Mobile top app-bar — visible only below lg. */}
      {isMobile && (
        <header
          data-testid="mobile-topbar"
          className="lg:hidden flex items-center gap-3 px-3 h-12 shrink-0 bg-white border-b border-[#E6E6E6] z-30"
        >
          <button
            type="button"
            data-testid="mobile-menu-toggle"
            aria-label="Open menu"
            onClick={() => setDrawerOpen(true)}
            className="w-9 h-9 -ml-1 flex items-center justify-center rounded-sm hover:bg-[#F6F6FA]"
          >
            <Menu className="w-5 h-5 text-[#2E2E38]" />
          </button>
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-7 h-7 bg-[#FFE600] text-[#2E2E38] flex items-center justify-center rounded-sm font-display font-bold text-sm shrink-0">
              L
            </div>
            <div className="font-display font-bold text-base leading-none tracking-tight text-[#2E2E38] truncate">
              LAMA
            </div>
          </div>
        </header>
      )}

      {/* Professional IDE Layout: Top Toolbar + Sidebar + Main + Status Bar */}
      <div className="flex-1 flex flex-col lg:flex-row min-h-0 overflow-hidden">
        <Sidebar
          mobileOpen={isMobile ? drawerOpen : false}
          onMobileClose={() => setDrawerOpen(false)}
        />

        {/* Main content area with top toolbar */}
        <div className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden">
          {/* Top Toolbar - breadcrumbs, search, user */}
          {!isMobile && (
            <TopToolbar onSearchClick={() => setCommandPaletteOpen(true)} />
          )}

          {/* iter-14.1 — Prominent full-width pipeline stepper */}
          <StageProgress />

          {/* Page content */}
          <div className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden">
            {children}
          </div>

          {/* iter-13.31 — Always-on mini console (models/tokens) */}
          <MiniConsole />
        </div>
      </div>

      {/* Status Bar - connection status, system info */}
      <StatusBar />
    </div>
  );
}

// iter-13.68 — Route guards.
// `RequireAuth` redirects unauthenticated users to /login (preserving
// the requested path so they bounce back after sign-in). While the
// AuthContext is still validating the persisted token, render a tiny
// blank state instead of flashing the login screen.
function RequireAuth({ children }) {
  const { isAuthenticated, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center text-[12px] text-[#747480]">Checking session…</div>;
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return children;
}

function RequireSuperAdmin({ children }) {
  const { isSuperAdmin, loading } = useAuth();
  if (loading) return null;
  if (!isSuperAdmin) return <Navigate to="/" replace />;
  return children;
}

// iter-14.97 — Root route dispatcher. The default landing page is Discovery
// (legacy migration), but if the active project is a Gap Analyzer or
// Transformer project, we redirect the user to that project's own page so
// the header + main content match what the left sidebar shows selected.
// While projects are still loading, we render Discovery to avoid a flash
// blank state — the redirect happens the moment `active` resolves.
function HomeDispatcher() {
  const { active, loading } = useProjects();
  const ptype = active?.project_type || "legacy_migration";
  if (loading || !active) {
    return <DiscoveryPage />;
  }
  if (ptype === "gap_analysis") {
    return <Navigate to="/gap-analyzer#input" replace />;
  }
  if (ptype === "tech_transformer") {
    return <Navigate to="/transformer#input" replace />;
  }
  return <DiscoveryPage />;
}

// iter-14.97 — Guard legacy-only stage routes. If the active project is a
// tool project (gap_analysis / tech_transformer), these routes redirect
// back to "/" (which the HomeDispatcher will route to the tool page).
function LegacyOnly({ children }) {
  const { active, loading } = useProjects();
  if (loading || !active) return children;
  const ptype = active?.project_type || "legacy_migration";
  if (ptype !== "legacy_migration") {
    return <Navigate to="/" replace />;
  }
  return children;
}

function App() {
  return (
    // iter-13.21 — ProjectProvider must be INSIDE BrowserRouter so its
    // internal useLocation() (re-fetch on every route change) resolves.
    // iter-13.68 — AuthProvider wraps everything so api.js requests carry
    // the bearer token and the RequireAuth guard can short-circuit.
    <BrowserRouter>
      <AuthProvider>
        <ProjectProvider>
          <Toaster position="top-right" />
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/admin"
              element={
                <RequireAuth>
                  <RequireSuperAdmin>
                    <AdminDashboard />
                  </RequireSuperAdmin>
                </RequireAuth>
              }
            />
            <Route path="/" element={<RequireAuth><Shell><HomeDispatcher /></Shell></RequireAuth>} />
            <Route path="/data-model" element={<RequireAuth><Shell><LegacyOnly><DataModelPage /></LegacyOnly></Shell></RequireAuth>} />
            <Route path="/architecture" element={<RequireAuth><Shell><LegacyOnly><ArchitecturePage /></LegacyOnly></Shell></RequireAuth>} />
            <Route path="/code-gen" element={<RequireAuth><Shell><LegacyOnly><CodeGenPage /></LegacyOnly></Shell></RequireAuth>} />
            <Route path="/integrations" element={<RequireAuth><Shell><IntegrationsPage /></Shell></RequireAuth>} />
            <Route path="/living" element={<RequireAuth><Shell><LegacyOnly><LivingPage /></LegacyOnly></Shell></RequireAuth>} />
            <Route path="/prompts" element={<RequireAuth><Shell><PromptLibraryPage /></Shell></RequireAuth>} />
            <Route path="/console" element={<RequireAuth><Shell><ConsolePage /></Shell></RequireAuth>} />
            <Route path="/ontology-studio" element={<RequireAuth><Shell><OntologyStudioPage /></Shell></RequireAuth>} />
            <Route path="/settings" element={<RequireAuth><Shell><GitHubSettingsPage /></Shell></RequireAuth>} />
            <Route path="/about" element={<RequireAuth><Shell><AboutUsPage /></Shell></RequireAuth>} />
            <Route path="/audit" element={<RequireAuth><Shell><AuditLogPage /></Shell></RequireAuth>} />
            <Route path="/ui-showcase" element={<RequireAuth><Shell><UIShowcase /></Shell></RequireAuth>} />
            <Route path="/gap-analyzer" element={<RequireAuth><Shell><GapAnalyzerPage /></Shell></RequireAuth>} />
            <Route path="/transformer" element={<RequireAuth><Shell><TransformerPage /></Shell></RequireAuth>} />
          </Routes>
        </ProjectProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
