/**
 * Route chunk registry.
 *
 * Lives in its own module rather than App.js so the Sidebar can warm a
 * chunk without importing App (App imports Sidebar, so that would be a
 * cycle).
 *
 * Every entry mirrors a React.lazy call in App.js. Calling one starts the
 * same dynamic import the router will make, so the chunk is usually parsed
 * by the time the click lands — which is what keeps route-splitting from
 * trading bundle size for a visible skeleton on every navigation.
 */
type ChunkLoader = () => Promise<unknown>;

export const ROUTE_CHUNKS: Record<string, ChunkLoader> = {
  "/data-model": () => import("@/pages/DataModel"),
  "/architecture": () => import("@/pages/Architecture"),
  "/code-gen": () => import("@/pages/CodeGen"),
  "/integrations": () => import("@/pages/Integrations"),
  "/console": () => import("@/pages/Console"),
  "/ontology-studio": () => import("@/pages/OntologyStudio"),
  "/living": () => import("@/pages/Living"),
  "/prompts": () => import("@/pages/PromptLibrary"),
  "/direct-transform": () => import("@/pages/DirectTransform"),
  "/audit": () => import("@/pages/AuditLog"),
  "/settings": () => import("@/pages/GitHubSettings"),
  "/about": () => import("@/pages/AboutUs"),
  "/admin": () => import("@/pages/AdminDashboard"),
  "/ui-showcase": () => import("@/pages/UIShowcase"),
  "/gap-analyzer": () => import("@/pages/GapAnalyzer"),
  "/transformer": () => import("@/pages/Transformer"),
};

const warmed = new Set<string>();

/** Idempotent: a path is only ever fetched once per session. */
export function prefetchRoute(path: string): void {
  const base = (path || "").split("#")[0];
  if (!base || warmed.has(base) || !ROUTE_CHUNKS[base]) return;
  warmed.add(base);
  ROUTE_CHUNKS[base]().catch(() => {
    // A failed prefetch is not an error — the real navigation will retry.
    warmed.delete(base);
  });
}
