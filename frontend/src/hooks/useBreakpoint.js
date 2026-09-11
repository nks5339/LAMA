/**
 * iter-13.89 — Responsive breakpoint hooks.
 *
 * Returns booleans for the current viewport width vs. Tailwind's default
 * breakpoints (sm=640, md=768, lg=1024). Centralised here so every
 * component agrees on what "mobile" / "tablet" means, and so we don't
 * scatter window.matchMedia listeners.
 *
 * Usage:
 *   const isMobile = useIsMobile();          // < 1024
 *   const isSmall  = useIsSmall();           // < 640
 *   const bp       = useBreakpoint();        // {sm, md, lg, xl, '2xl'}
 */
import { useEffect, useState } from "react";

const BREAKPOINTS = { sm: 640, md: 768, lg: 1024, xl: 1280, "2xl": 1536 };

function readWidth() {
  if (typeof window === "undefined") return 1280;
  return window.innerWidth;
}

export function useViewportWidth() {
  const [w, setW] = useState(readWidth);
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    let raf = 0;
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => setW(window.innerWidth));
    };
    window.addEventListener("resize", onResize, { passive: true });
    window.addEventListener("orientationchange", onResize, { passive: true });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, []);
  return w;
}

export function useBreakpoint() {
  const w = useViewportWidth();
  return {
    sm: w >= BREAKPOINTS.sm,
    md: w >= BREAKPOINTS.md,
    lg: w >= BREAKPOINTS.lg,
    xl: w >= BREAKPOINTS.xl,
    "2xl": w >= BREAKPOINTS["2xl"],
    width: w,
  };
}

/** True for phones AND small tablets (anything narrower than 1024px).
 *  Use this to decide between the inline sidebar and the off-canvas drawer. */
export function useIsMobile() {
  return useViewportWidth() < BREAKPOINTS.lg;
}

/** True for phones only (< 640px). Use to drop non-essential chrome. */
export function useIsSmall() {
  return useViewportWidth() < BREAKPOINTS.sm;
}

export default useIsMobile;

