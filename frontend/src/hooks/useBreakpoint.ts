/**
 * Responsive breakpoint hooks.
 *
 * Rewritten to use matchMedia + useSyncExternalStore. The previous
 * implementation stored window.innerWidth in state and updated it on every
 * resize animation frame, so dragging a window re-rendered the entire tree
 * continuously — useIsMobile() is called in the App shell, so every mounted
 * page re-rendered with it. These hooks now change state only when the
 * query result actually flips, which for a window drag is at most twice.
 *
 * Usage:
 *   const isMobile = useIsMobile();   // < 1024
 *   const bp       = useBreakpoint(); // { sm, md, lg, xl, '2xl' }
 */
import { useSyncExternalStore } from "react";

const BREAKPOINTS = { sm: 640, md: 768, lg: 1024, xl: 1280, "2xl": 1536 };

// One MediaQueryList per query string, created lazily and shared by every
// caller — so N components watching "(min-width: 1024px)" share one listener.
type Store = { subscribe: (cb: () => void) => () => void; get: () => boolean };

const stores = new Map<string, Store>();

function getStore(query: string): Store {
  let store = stores.get(query);
  if (store) return store;

  if (typeof window === "undefined" || !window.matchMedia) {
    store = { subscribe: () => () => {}, get: () => false };
  } else {
    const mql = window.matchMedia(query);
    store = {
      subscribe: (cb) => {
        mql.addEventListener("change", cb);
        return () => mql.removeEventListener("change", cb);
      },
      get: () => mql.matches,
    };
  }
  stores.set(query, store);
  return store;
}

/** Subscribe to a raw media query. Re-renders only when the result flips. */
export function useMediaQuery(query: string): boolean {
  const store = getStore(query);
  return useSyncExternalStore(store.subscribe, store.get, () => false);
}

/** True for phones AND small tablets (< 1024px). Picks drawer vs inline sidebar. */
export function useIsMobile(): boolean {
  return useMediaQuery(`(max-width: ${BREAKPOINTS.lg - 1}px)`);
}

/** True when the viewer has asked for reduced motion. */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/** All breakpoints at once. Each is an independent subscription. */
export function useBreakpoint() {
  return {
    sm: useMediaQuery(`(min-width: ${BREAKPOINTS.sm}px)`),
    md: useMediaQuery(`(min-width: ${BREAKPOINTS.md}px)`),
    lg: useMediaQuery(`(min-width: ${BREAKPOINTS.lg}px)`),
    xl: useMediaQuery(`(min-width: ${BREAKPOINTS.xl}px)`),
    "2xl": useMediaQuery(`(min-width: ${BREAKPOINTS["2xl"]}px)`),
  };
}

export default useIsMobile;
