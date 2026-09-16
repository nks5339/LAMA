/**
 * Tools nav registration — symmetry across every registry.
 *
 * A Tools section is not "wired up" because its page renders. It is wired
 * up when it appears in the same registries its siblings appear in, and in
 * the same order. The failure this catches is the common one: the sidebar
 * button lands, the route lands, and then the command palette, the
 * breadcrumb map and the prefetch chunk list quietly don't know about it —
 * so the tab works but ⌘K can't find it and the breadcrumb goes blank.
 *
 * Read as source text rather than rendered, because these five registries
 * live in five different modules (two of which pull in the whole app
 * shell) and the property under test is "the entry exists, in this
 * order", not runtime behaviour.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(SRC, p), "utf8");

// The four Tools sections, in the order the sidebar is meant to list them.
const TOOLS = [
  { label: "Console", route: "/console", testId: "nav-console" },
  { label: "Integrations", route: "/integrations", testId: "nav-integrations" },
  { label: "Prompt Library", route: "/prompts", testId: "nav-prompts" },
  { label: "Direct Transform", route: "/direct-transform", testId: "nav-direct-transform" },
];

describe("Tools nav registration", () => {
  it("lists all four in the sidebar Tools accordion, Direct Transform last", () => {
    const sidebar = read("components/Sidebar.jsx");
    const positions = TOOLS.map((t) => {
      const i = sidebar.indexOf(`data-testid="${t.testId}"`);
      expect(i).toBeGreaterThan(-1);
      return i;
    });
    // Strictly increasing == rendered in this order.
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    expect(positions[3]).toBe(Math.max(...positions));
  });

  it("routes every one of them in App.js", () => {
    const app = read("App.js");
    for (const t of TOOLS) {
      expect(app).toContain(`<Route path="${t.route}"`);
    }
    expect(app).toContain('import("@/pages/DirectTransform")');
  });

  it("registers every one of them as a prefetchable chunk", () => {
    const routes = read("lib/routes.ts");
    for (const t of TOOLS) {
      expect(routes).toContain(`"${t.route}":`);
    }
  });

  it("gives every one of them a breadcrumb label", () => {
    const toolbar = read("components/TopToolbar.jsx");
    for (const t of TOOLS) {
      expect(toolbar).toContain(`"${t.route}": { label: "${t.label}"`);
    }
  });

  it("makes every one of them reachable from the command palette", () => {
    const palette = read("components/CommandPalette.jsx");
    for (const t of TOOLS) {
      expect(palette).toContain(`path: "${t.route}"`);
    }
  });

  it("keeps Direct Transform out of the collapsed rail, where Integrations is also absent", () => {
    // The rail is a curated four-shortcut strip (Console, Prompt Library,
    // Settings, Audit), not a Tools registry — Integrations never appears
    // there either. Adding a fourth tool would make it asymmetric with a
    // sibling, which is exactly what this suite is here to prevent.
    const sidebar = read("components/Sidebar.jsx");
    const railStart = sidebar.indexOf('aria-label="Console"');
    const railEnd = sidebar.indexOf('data-testid="expand-sidebar-bottom"');
    expect(railStart).toBeGreaterThan(-1);
    expect(railEnd).toBeGreaterThan(railStart);
    const rail = sidebar.slice(railStart, railEnd);
    expect(rail).not.toContain("/direct-transform");
    expect(rail).not.toContain("/integrations");
  });
});

describe("Direct Transform API surface", () => {
  it("declares every api.js helper the page imports, so tsc can gate them", () => {
    const apiJs = read("lib/api.js");
    const apiDts = read("lib/api.d.ts");
    const page = read("pages/DirectTransform.jsx");

    const imported = page
      .slice(page.indexOf("import {"), page.indexOf('} from "@/lib/api";'))
      .match(/dcte[A-Za-z]+/g);
    // A floor, not an exact count: the invariant is "every helper the page
    // imports is declared in both places", which the loop below checks. An
    // exact number only guaranteed this test had to be edited every time a
    // helper was added — which is what it did when dcteListStacks landed.
    expect(imported.length).toBeGreaterThanOrEqual(14);

    for (const name of imported) {
      expect(apiJs).toContain(`export const ${name} =`);
      expect(apiDts).toContain(`export declare const ${name}:`);
    }
  });

  it("points every helper at /dcte, never at /tools", () => {
    const apiJs = read("lib/api.js");
    const block = apiJs.slice(apiJs.indexOf("export const dcteListPlugins"));
    expect(block).not.toMatch(/api\.\w+\(\s*[`"']\/tools/);
    expect((block.match(/\/dcte\//g) || []).length).toBeGreaterThan(10);
  });
});
