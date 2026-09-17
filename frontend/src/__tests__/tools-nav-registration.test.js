/**
 * Nav registration — Tools sections vs project types.
 *
 * Two registries, two shapes, and putting something in the wrong one is the
 * mistake this suite exists to catch:
 *
 *   A **Tool** is a global utility you visit (Console, Integrations, Prompt
 *   Library). It lives in the sidebar's Tools accordion and owns no data.
 *
 *   A **project type** is chosen in New Project → Choose project type. It
 *   owns a source tree, settings and a run history, and everything it
 *   creates is scoped to that project.
 *
 * Direct Transform shipped as a Tool in iter-18 and moved to a project type
 * in iter-22. It owns a source tree, a stack pair and a job history, and two
 * of them have to keep their runs apart — which a global Tools page cannot
 * do. The half of this file that asserts its ABSENCE from Tools matters as
 * much as the half asserting its presence as a project type: landing it in
 * both would give it two front doors with different behaviour.
 *
 * Read as source text rather than rendered, because these registries live in
 * six different modules (several of which pull in the whole app shell) and
 * the property under test is "the entry exists, in this order", not runtime
 * behaviour.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(SRC, p), "utf8");

// The three Tools sections, in the order the sidebar lists them.
const TOOLS = [
  { label: "Console", route: "/console", testId: "nav-console" },
  { label: "Integrations", route: "/integrations", testId: "nav-integrations" },
  { label: "Prompt Library", route: "/prompts", testId: "nav-prompts" },
];

// Every project type offered in New Project → Choose project type.
const PROJECT_TYPES = ["legacy_migration", "gap_analysis", "tech_transformer",
                       "direct_transform"];

describe("Tools nav registration", () => {
  it("lists the three tools in the sidebar Tools accordion, in order", () => {
    const sidebar = read("components/Sidebar.jsx");
    const positions = TOOLS.map((t) => {
      const i = sidebar.indexOf(`data-testid="${t.testId}"`);
      expect(i).toBeGreaterThan(-1);
      return i;
    });
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("routes every tool in App.js", () => {
    const app = read("App.js");
    for (const t of TOOLS) expect(app).toContain(`<Route path="${t.route}"`);
  });

  it("gives every tool a breadcrumb label", () => {
    const toolbar = read("components/TopToolbar.jsx");
    for (const t of TOOLS) {
      expect(toolbar).toContain(`"${t.route}": { label: "${t.label}"`);
    }
  });

  it("makes every tool reachable from the command palette", () => {
    const palette = read("components/CommandPalette.jsx");
    for (const t of TOOLS) expect(palette).toContain(`path: "${t.route}"`);
  });
});

describe("Direct Transform is a project type, not a Tool", () => {
  it("has no entry in the sidebar Tools accordion", () => {
    const sidebar = read("components/Sidebar.jsx");
    expect(sidebar).not.toContain('data-testid="nav-direct-transform"');
  });

  it("is absent from the command palette and the breadcrumb map", () => {
    // Both are registries of global destinations. A project page's identity
    // comes from the selected project, which the project switcher shows —
    // which is why /transformer and /gap-analyzer are absent from them too.
    for (const mod of ["components/CommandPalette.jsx", "components/TopToolbar.jsx"]) {
      expect(read(mod)).not.toContain("/direct-transform");
    }
    const toolbar = read("components/TopToolbar.jsx");
    expect(toolbar).not.toContain("/transformer");
    expect(toolbar).not.toContain("/gap-analyzer");
  });

  it("is offered in New Project → Choose project type", () => {
    const switcher = read("components/ProjectSwitcher.jsx");
    expect(switcher).toContain('key: "direct_transform"');
    expect(switcher).toContain('landing: "/direct-transform"');
    for (const t of PROJECT_TYPES) expect(switcher).toContain(t);
  });

  it("declares its pipeline in both stage registries", () => {
    // Sidebar renders the rail; StageProgress renders the header strip. A
    // type in one and not the other shows a pipeline in one place only.
    for (const mod of ["components/Sidebar.jsx", "components/StageProgress.jsx"]) {
      const src = read(mod);
      expect(src).toContain("direct_transform:");
      expect(src).toContain("/direct-transform#input");
      expect(src).toContain("/direct-transform#transform");
      expect(src).toContain("/direct-transform#output");
    }
  });

  it("has no KnowledgeBase stage — it builds no KB", () => {
    const switcher = read("components/ProjectSwitcher.jsx");
    const line = switcher
      .split("\n")
      .find((l) => l.includes("direct_transform: [") && l.includes("Input"));
    expect(line).toBeTruthy();
    expect(line).not.toContain("KnowledgeBase");
  });

  it("is treated as a tool-style project wherever the other two are", () => {
    // isToolProject drives hash-derived stage progression. Missing it here
    // would leave the rail stuck on stage 1 forever.
    for (const mod of ["components/Sidebar.jsx", "components/StageProgress.jsx"]) {
      const src = read(mod);
      const idx = src.indexOf("isToolProject");
      expect(idx).toBeGreaterThan(-1);
      expect(src.slice(idx, idx + 220)).toContain("direct_transform");
    }
  });

  it("lands on its own page when such a project is selected", () => {
    const app = read("App.js");
    expect(app).toContain('if (ptype === "direct_transform")');
    expect(app).toContain('/direct-transform#input');
  });

  it("still has a route and a prefetchable chunk", () => {
    expect(read("App.js")).toContain('<Route path="/direct-transform"');
    expect(read("lib/routes.ts")).toContain('"/direct-transform":');
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

  it("scopes the job list by project", () => {
    expect(read("lib/api.js")).toContain("project_id: projectId");
    expect(read("pages/DirectTransform.jsx")).toContain("dcteListJobs(null, projectId)");
  });
});
