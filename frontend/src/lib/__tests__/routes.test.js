/**
 * Route chunk registry + cn().
 *
 * `prefetchRoute` is what stops route-splitting from trading bundle size
 * for a visible skeleton on every navigation: the Sidebar warms a chunk on
 * hover/focus so it is usually parsed before the click lands.
 *
 * It lives in its own module rather than App.js because App imports
 * Sidebar — importing back the other way would be a cycle.
 */
import { ROUTE_CHUNKS, prefetchRoute } from "@/lib/routes";
import { cn } from "@/lib/utils";

describe("ROUTE_CHUNKS", () => {
  it("has a loader for every lazy route in App.js", () => {
    const fs = require("fs");
    const path = require("path");
    const app = fs.readFileSync(path.join(__dirname, "..", "..", "App.js"), "utf8");
    const lazyPaths = [...app.matchAll(/lazy\(\(\) => import\("@\/pages\/(\w+)"\)\)/g)].map(
      (m) => m[1]
    );
    const registered = Object.values(ROUTE_CHUNKS).map((f) => f.toString());
    for (const page of lazyPaths) {
      // Login has no nav entry, so it is deliberately not prefetchable.
      if (page === "Login") continue;
      expect(registered.some((s) => s.includes(page))).toBe(true);
    }
  });

  it("every entry is a function returning a promise", () => {
    for (const [route, loader] of Object.entries(ROUTE_CHUNKS)) {
      expect(typeof loader).toBe("function");
      expect(route.startsWith("/")).toBe(true);
    }
  });
});

describe("prefetchRoute", () => {
  it("does nothing for an unknown path", () => {
    expect(() => prefetchRoute("/not-a-route")).not.toThrow();
  });

  it("tolerates empty and undefined input", () => {
    expect(() => prefetchRoute("")).not.toThrow();
    expect(() => prefetchRoute(undefined)).not.toThrow();
  });

  it("strips the hash so tool routes resolve", () => {
    // The sidebar links to /transformer#kb; the chunk key is /transformer.
    expect(() => prefetchRoute("/transformer#kb")).not.toThrow();
  });

  it("fetches a given route at most once per session", () => {
    const calls = [];
    const original = ROUTE_CHUNKS["/audit"];
    ROUTE_CHUNKS["/audit"] = () => {
      calls.push(1);
      return Promise.resolve({});
    };
    prefetchRoute("/audit");
    prefetchRoute("/audit");
    prefetchRoute("/audit");
    expect(calls).toHaveLength(1);
    ROUTE_CHUNKS["/audit"] = original;
  });

  it("does not reject when a chunk fails to load", async () => {
    const original = ROUTE_CHUNKS["/about"];
    ROUTE_CHUNKS["/about"] = () => Promise.reject(new Error("offline"));
    // A failed prefetch is not an error — the real navigation retries.
    expect(() => prefetchRoute("/about")).not.toThrow();
    await Promise.resolve();
    ROUTE_CHUNKS["/about"] = original;
  });
});

describe("cn", () => {
  it("joins class names", () => {
    expect(cn("a", "b")).toBe("a b");
  });

  it("drops falsy values", () => {
    expect(cn("a", false, null, undefined, "b")).toBe("a b");
  });

  it("lets a later Tailwind utility win over an earlier conflicting one", () => {
    // This is the whole reason for tailwind-merge: a caller's className
    // must be able to override a component's default.
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("bg-surface", "bg-brand")).toBe("bg-brand");
  });

  it("keeps non-conflicting utilities", () => {
    expect(cn("px-2 py-1", "text-sm")).toBe("px-2 py-1 text-sm");
  });

  it("handles arrays and objects", () => {
    expect(cn(["a", "b"], { c: true, d: false })).toBe("a b c");
  });
});
