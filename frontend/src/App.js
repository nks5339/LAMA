import { useState, useEffect, lazy, Suspense } from "react";
import "@/App.css";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation,
} from "react-router-dom";
import { Toaster } from "sonner";
import { Menu } from "lucide-react";
import { QueryClientProvider } from "@tanstack/react-query";
import { LazyMotion, domAnimation, m, AnimatePresence } from "framer-motion";

import { queryClient } from "@/lib/queryClient";
import { ProjectProvider, useProjects } from "@/state/ProjectContext";
import { AuthProvider, useAuth } from "@/state/AuthContext";
import Sidebar from "@/components/Sidebar";
import StageProgress from "@/components/StageProgress";
import MiniConsole from "@/components/MiniConsole";
import TopToolbar from "@/components/TopToolbar";
import StatusBar from "@/components/StatusBar";
import CommandPalette from "@/components/CommandPalette";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useIsMobile, usePrefersReducedMotion } from "@/hooks/useBreakpoint";

// Discovery is the landing route, so it stays eager — lazy-loading it would
// trade a smaller bundle for a skeleton on the very first paint.
import DiscoveryPage from "@/pages/DiscoveryV2";

// Every other route is its own chunk. Before this, all 15 pages were static
// imports, which is why main.js carried mermaid (Architecture), monaco
// (CodeGen), d3 (ERDiagram), jszip (ZipFileRow) and recharts, and measured
// 769 KB gzipped for a user who only ever opened Discovery.
const DataModelPage = lazy(() => import("@/pages/DataModel"));
const ArchitecturePage = lazy(() => import("@/pages/Architecture"));
const CodeGenPage = lazy(() => import("@/pages/CodeGen"));
const IntegrationsPage = lazy(() => import("@/pages/Integrations"));
const ConsolePage = lazy(() => import("@/pages/Console"));
const OntologyStudioPage = lazy(() => import("@/pages/OntologyStudio"));
const LivingPage = lazy(() => import("@/pages/Living"));
const PromptLibraryPage = lazy(() => import("@/pages/PromptLibrary"));
const AuditLogPage = lazy(() => import("@/pages/AuditLog"));
const GitHubSettingsPage = lazy(() => import("@/pages/GitHubSettings"));
const AboutUsPage = lazy(() => import("@/pages/AboutUs"));
const LoginPage = lazy(() => import("@/pages/Login"));
const AdminDashboard = lazy(() => import("@/pages/AdminDashboard"));
const UIShowcase = lazy(() => import("@/pages/UIShowcase"));
const GapAnalyzerPage = lazy(() => import("@/pages/GapAnalyzer"));
const TransformerPage = lazy(() => import("@/pages/Transformer"));

/**
 * Route content, with a 180ms cross-fade between pages.
 *
 * Motion is loaded through LazyMotion + `m`, which is ~5 KB rather than the
 * ~34 KB full framer-motion bundle — worth being careful about on a page
 * weight this app is trying to reduce.
 */
function RouteTransition({ children }) {
  const location = useLocation();
  const reduce = usePrefersReducedMotion();

  if (reduce) {
    return <div className="flex-1 flex flex-col min-w-0 min-h-0">{children}</div>;
  }

  return (
    <AnimatePresence mode="wait" initial={false}>
      <m.div
        key={location.pathname}
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -4 }}
        transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
        className="flex-1 flex flex-col min-w-0 min-h-0"
      >
        {children}
      </m.div>
    </AnimatePresence>
  );
}

// iter-13.89 — Responsive Shell.
//   • Desktop (≥ lg): inline sidebar at left, page content fills the rest.
//   • Mobile / tablet (< lg): sidebar is an off-canvas drawer behind a slim
//     top app-bar.
// iter-14.0 — top toolbar, status bar, command palette (Cmd/Ctrl+K).
//
// `pipeline` gates StageProgress. It used to render on every route,
// including Console, Audit, Settings and About, where a migration pipeline
// has no meaning — and it was part of roughly 400px of chrome stacked above
// the content on a 768px-tall laptop.
function Shell({ children, pipeline = false }) {
  const isMobile = useIsMobile();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const location = useLocation();

  // Global keyboard shortcuts.
  useEffect(() => {
    const handler = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCommandPaletteOpen((o) => !o);
      }
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Close the drawer when we cross to desktop, otherwise the fixed overlay's
  // transform stays applied off-screen and clicks intermittently misroute.
  useEffect(() => {
    if (!isMobile && drawerOpen) setDrawerOpen(false);
  }, [isMobile, drawerOpen]);

  // And after every route change, so it doesn't linger on a stage tap.
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden bg-bg">
      {/* First tab stop on every page. */}
      <a href="#main-content" className="skip-link">
        Skip to main content
      </a>

      <CommandPalette
        isOpen={commandPaletteOpen}
        onClose={() => setCommandPaletteOpen(false)}
      />

      {/* Mobile top app-bar — below lg only. */}
      {isMobile && (
        <header
          data-testid="mobile-topbar"
          className="lg:hidden flex items-center gap-3 px-3 h-12 shrink-0 bg-surface border-b border-border z-30"
        >
          <button
            type="button"
            data-testid="mobile-menu-toggle"
            aria-label="Open navigation menu"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
            className="size-9 -ml-1 grid place-items-center rounded hover:bg-surface-2"
          >
            <Menu className="size-5 text-fg" aria-hidden />
          </button>
          <div className="flex items-center gap-2 min-w-0">
            <div className="size-7 bg-brand text-brand-fg grid place-items-center rounded-sm font-display font-bold text-sm shrink-0">
              L
            </div>
            <div className="font-display font-bold text-base leading-none tracking-tight text-fg truncate">
              LAMA
            </div>
          </div>
        </header>
      )}

      <div className="flex-1 flex flex-col lg:flex-row min-h-0 overflow-hidden">
        <Sidebar
          mobileOpen={isMobile ? drawerOpen : false}
          onMobileClose={() => setDrawerOpen(false)}
        />

        <div className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden">
          {!isMobile && (
            <TopToolbar onSearchClick={() => setCommandPaletteOpen(true)} />
          )}

          {pipeline && <StageProgress />}

          <main
            id="main-content"
            tabIndex={-1}
            className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden focus:outline-none"
          >
            <Suspense fallback={<PageSkeleton />}>
              <RouteTransition>{children}</RouteTransition>
            </Suspense>
          </main>

          {/* iter-13.31 — always-on mini console (models / tokens) */}
          <MiniConsole />
        </div>
      </div>

      <StatusBar />
    </div>
  );
}

// iter-13.68 — Route guards. RequireAuth redirects unauthenticated users to
// /login preserving the requested path. While AuthContext validates the
// persisted token, render a skeleton rather than flashing the login screen.
function RequireAuth({ children }) {
  const { isAuthenticated, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div
        className="min-h-screen grid place-items-center text-xs text-fg-muted"
        role="status"
        aria-live="polite"
      >
        Checking session…
      </div>
    );
  }
  if (!isAuthenticated) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: location.pathname + location.search }}
      />
    );
  }
  return children;
}

function RequireSuperAdmin({ children }) {
  const { isSuperAdmin, loading } = useAuth();
  if (loading) return null;
  if (!isSuperAdmin) return <Navigate to="/" replace />;
  return children;
}

// iter-14.97 — Root dispatcher. Default landing is Discovery, but a Gap
// Analyzer / Transformer project routes to its own page so the header and
// content match what the sidebar shows selected.
function HomeDispatcher() {
  const { active, loading } = useProjects();
  const ptype = active?.project_type || "legacy_migration";
  if (loading || !active) return <DiscoveryPage />;
  if (ptype === "gap_analysis") return <Navigate to="/gap-analyzer#input" replace />;
  if (ptype === "tech_transformer") return <Navigate to="/transformer#input" replace />;
  return <DiscoveryPage />;
}

// iter-14.97 — Legacy-only stage routes bounce tool projects back to "/".
function LegacyOnly({ children }) {
  const { active, loading } = useProjects();
  if (loading || !active) return children;
  const ptype = active.project_type || "legacy_migration";
  if (ptype !== "legacy_migration") return <Navigate to="/" replace />;
  return children;
}

/** Toasts, styled from the same tokens as the rest of the UI. */
function ThemedToaster() {
  return (
    <Toaster
      position="top-right"
      theme="light"
      closeButton
      richColors={false}
      toastOptions={{
        classNames: {
          toast:
            "!bg-surface !text-fg !border-border !shadow-overlay !rounded",
          description: "!text-fg-muted",
          actionButton: "!bg-ink !text-ink-fg",
          cancelButton: "!bg-surface-2 !text-fg-muted",
        },
      }}
    />
  );
}

/** Stage routes: the pipeline stepper belongs here and nowhere else. */
const stage = (el) => (
  <RequireAuth>
    <Shell pipeline>
      <LegacyOnly>{el}</LegacyOnly>
    </Shell>
  </RequireAuth>
);

/** Tool routes: their own 3-step pipeline, so the stepper still applies. */
const tool = (el) => (
  <RequireAuth>
    <Shell pipeline>{el}</Shell>
  </RequireAuth>
);

/** Everything else: no stepper. */
const plain = (el) => (
  <RequireAuth>
    <Shell>{el}</Shell>
  </RequireAuth>
);

function App() {
  return (
    // iter-13.21 — ProjectProvider must be INSIDE BrowserRouter so its
    // useLocation() (re-fetch on route change) resolves.
    // iter-13.68 — AuthProvider wraps everything so api.js requests carry
    // the bearer token and RequireAuth can short-circuit.
    <BrowserRouter>
        <QueryClientProvider client={queryClient}>
          <LazyMotion features={domAnimation} strict>
            <AuthProvider>
              <ProjectProvider>
                <ThemedToaster />
                <Suspense fallback={<PageSkeleton />}>
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

                    <Route
                      path="/"
                      element={
                        <RequireAuth>
                          <Shell pipeline>
                            <HomeDispatcher />
                          </Shell>
                        </RequireAuth>
                      }
                    />

                    <Route path="/data-model" element={stage(<DataModelPage />)} />
                    <Route path="/architecture" element={stage(<ArchitecturePage />)} />
                    <Route path="/code-gen" element={stage(<CodeGenPage />)} />
                    <Route path="/living" element={stage(<LivingPage />)} />

                    <Route path="/gap-analyzer" element={tool(<GapAnalyzerPage />)} />
                    <Route path="/transformer" element={tool(<TransformerPage />)} />

                    <Route path="/integrations" element={plain(<IntegrationsPage />)} />
                    <Route path="/prompts" element={plain(<PromptLibraryPage />)} />
                    <Route path="/console" element={plain(<ConsolePage />)} />
                    <Route path="/ontology-studio" element={plain(<OntologyStudioPage />)} />
                    <Route path="/settings" element={plain(<GitHubSettingsPage />)} />
                    <Route path="/about" element={plain(<AboutUsPage />)} />
                    <Route path="/audit" element={plain(<AuditLogPage />)} />
                    <Route path="/ui-showcase" element={plain(<UIShowcase />)} />
                  </Routes>
                </Suspense>
              </ProjectProvider>
            </AuthProvider>
          </LazyMotion>
        </QueryClientProvider>
    </BrowserRouter>
  );
}

export default App;
