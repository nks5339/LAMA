/**
 * Design-system contract.
 *
 * These assert on the SOURCE, not on a render, because the invariants they
 * protect are codebase-wide and that is the only way to catch a regression
 * the moment it is written rather than when someone opens the page.
 *
 * Baselines the codemods moved:
 *   hardcoded hex        2,449 → 0
 *   fixed-palette greys  ~1,900 → 0
 *   sub-12px type        1,179 → 0
 *   dark-mode support    none → three theme states
 *
 * Each rule below is one of those, restated as a gate.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..");

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__tests__" || entry.name === "node_modules") continue;
      walk(p, out);
    } else if (/\.(jsx?|tsx?)$/.test(entry.name) && !entry.name.endsWith(".d.ts")) {
      out.push(p);
    }
  }
  return out;
}

const FILES = walk(SRC);
const rel = (p) => path.relative(SRC, p);

/** Collect [file, match] pairs for a pattern across the tree. */
function findAll(re) {
  const hits = [];
  for (const f of FILES) {
    const body = fs.readFileSync(f, "utf8");
    for (const m of body.matchAll(re)) hits.push(`${rel(f)}: ${m[0]}`);
  }
  return hits;
}

/**
 * Extract an element's opening-tag attributes.
 *
 * A naive /<div[^>]*>/ stops at the first ">" — which in JSX is usually the
 * one inside `onKeyDown={(e) => …}`, so the handler you are checking for is
 * cut off and every guarded element reads as unguarded. Scan to the ">"
 * that closes the tag at brace depth zero instead.
 */
function openingTag(body, start) {
  let depth = 0;
  for (let j = start; j < body.length; j += 1) {
    const ch = body[j];
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    else if (ch === ">" && depth === 0 && body[j - 1] !== "=") {
      return body.slice(start, j);
    }
  }
  return body.slice(start, start + 400);
}

/** Every <div|span|li> opening tag in the tree, as [file, attrs]. */
function elements() {
  const out = [];
  for (const f of FILES) {
    const body = fs.readFileSync(f, "utf8");
    for (const m of body.matchAll(/<(div|span|li)\b/g)) {
      out.push([rel(f), m[1], openingTag(body, m.index)]);
    }
  }
  return out;
}

describe("colour tokens", () => {
  it("has no hardcoded hex in a Tailwind arbitrary value", () => {
    // 2,449 of these existed. Every one is now a token, so dark mode and
    // any future rebrand are a token edit rather than a find-and-replace.
    expect(findAll(/\b[a-z-]+-\[#[0-9A-Fa-f]{3,8}\]/g)).toEqual([]);
  });

  it("has no fixed Tailwind palette greys, which cannot respond to a theme", () => {
    // `text-white` on a BRAND/ink fill is correct (--ink-fg is white in
    // light and dark ink is the yellow). `text-white` on a SEMANTIC fill is
    // not — that is the separate rule below.
    const hits = findAll(
      /\b(?:bg|border|divide|ring|fill|stroke)-(?:white|black|slate|gray|zinc|neutral|stone)(?:-\d{2,3})?\b/g
    );
    expect(hits).toEqual([]);
  });

  it("never puts plain white text on a semantic fill", () => {
    // In dark, --ok/--warn/--crit/--info become light tints, so white on
    // them is unreadable. The *-fg tokens flip with the theme.
    expect(
      findAll(
        /\bbg-(?:ok|warn|crit|info)\b[^"'`]{0,60}\btext-white\b|\btext-white\b[^"'`]{0,60}\bbg-(?:ok|warn|crit|info)\b/g
      )
    ).toEqual([]);
  });
});

describe("type scale", () => {
  it("has no type below the 12px floor", () => {
    // 1,179 declarations sat at 8–11px. Shrinking text was doing the job
    // hierarchy should do, and the result was uniform illegibility.
    expect(findAll(/text-\[(?:[0-9]|10|11)px\]/g)).toEqual([]);
  });

  it("removed 9/10/11px from the Tailwind scale so they cannot come back", () => {
    const cfg = require(path.join(SRC, "..", "tailwind.config.js"));
    const sizes = Object.values(cfg.theme.fontSize).map((v) =>
      parseInt(Array.isArray(v) ? v[0] : v, 10)
    );
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(12);
  });
});

describe("spacing", () => {
  it("is an 8px system with only named sub-8 steps", () => {
    const cfg = require(path.join(SRC, "..", "tailwind.config.js"));
    const px = Object.entries(cfg.theme.spacing)
      .filter(([k]) => k !== "px" && k !== "0")
      .map(([, v]) => parseInt(v, 10));
    // Everything is either a multiple of 8, or one of the deliberate
    // sub-8 steps real UI needs (hairlines, icon gaps, badge padding).
    const allowed = new Set([2, 4, 6, 12, 20, 28, 36, 44]);
    for (const v of px) {
      expect(v % 8 === 0 || allowed.has(v)).toBe(true);
    }
  });
});

describe("theme", () => {
  const css = fs.readFileSync(path.join(SRC, "index.css"), "utf8");

  it("is light-only — no dark theme", () => {
    // A dark theme was added and removed. It shipped with a toolbar toggle
    // that cycled on every click and persisted to localStorage, so two
    // clicks put the whole studio in a scheme nobody had asked for.
    expect(css).not.toContain("prefers-color-scheme: dark");
    expect(css).not.toContain('data-theme="dark"');
    expect(css).toMatch(/^:root\s*\{/m);
    expect(css).toContain("color-scheme: light");
  });

  it("keeps the original surfaces — white panels on the #F6F6FA ground", () => {
    // Stored as RGB channels, not hex — see the alpha rule below.
    expect(css).toMatch(/--bg:\s*246 246 250/);        // #F6F6FA
    expect(css).toMatch(/--surface:\s*255 255 255/);   // #FFFFFF
  });

  it("stores colour tokens as RGB channels so opacity modifiers compile", () => {
    // With a hex behind the var, `bg-ink/40` compiles to NOTHING — silently.
    // 86 such classes (modal scrims, panel tints, on-dark text) were
    // producing no CSS at all until the tokens became channel triplets.
    const root = css.slice(css.indexOf(":root {"), css.indexOf("\n}"));
    const hexTokens = [...root.matchAll(/(--[a-z0-9-]+):\s*#[0-9a-f]{3,8};/gi)]
      .map((m) => m[1])
      // Shadows are not colours and never take an alpha modifier.
      .filter((t) => !t.startsWith("--shadow"));
    expect(hexTokens).toEqual([]);

    const cfg = require(path.join(SRC, "..", "tailwind.config.js"));
    const flat = JSON.stringify(cfg.theme.extend.colors);
    // Every colour binding must carry the alpha placeholder.
    expect(flat).not.toMatch(/"var\(--[a-z0-9-]+\)"/);
    expect(flat).toContain("<alpha-value>");
  });

  it("declares every token in one :root block", () => {
    const start = css.indexOf(":root {");
    const root = css.slice(start, css.indexOf("\n}", start));
    const declared = new Set([...root.matchAll(/(--[a-z0-9-]+):/gi)].map((m) => m[1]));
    const used = new Set([...css.matchAll(/(--[a-z0-9-]+):/gi)].map((m) => m[1]));
    expect([...used].filter((t) => !declared.has(t))).toEqual([]);
  });

  it("paints the body background from a token", () => {
    expect(css).toMatch(/body\s*\{[^}]*background:\s*rgb\(var\(--bg\)\)/s);
  });

  it("uses a focus ring that can actually be seen", () => {
    // The previous global ring was #FFE600 on white — 1.27:1 against the
    // 3:1 WCAG 1.4.11 floor. --ring is 4.9:1 on white.
    expect(css).toMatch(/:focus-visible\s*\{[^}]*outline:[^}]*rgb\(var\(--ring\)\)/s);
    expect(css).not.toMatch(/:focus-visible\s*\{[^}]*#FFE600/is);
  });

  it("respects prefers-reduced-motion", () => {
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });
});

describe("accessibility invariants", () => {
  it("has no onClick on a bare div/span/li without a role or aria-hidden", () => {
    // A clickable non-button must declare what it is. Three valid answers:
    //   role="button" + tabIndex + key handler  — a real control
    //   role="presentation"                     — event plumbing only
    //   aria-hidden="true"                      — a modal scrim, where a
    //     tab stop would be noise and Escape already closes the dialog
    // Anything else is a control with no keyboard path.
    const hits = elements()
      .filter(([, , a]) => /onClick=/.test(a) && !/role=|aria-hidden/.test(a))
      .map(([f, tag]) => `${f}: <${tag}>`);
    expect(hits).toEqual([]);
  });

  it("gives every role=button div a tab stop and a key handler", () => {
    // role without tabIndex is worse than no role: it announces a control
    // a keyboard user cannot reach.
    const hits = elements()
      .filter(
        ([, , a]) =>
          /role="button"/.test(a) &&
          (!/tabIndex/.test(a) || !/onKey(Down|Up|Press)/.test(a))
      )
      .map(([f, tag]) => `${f}: <${tag} role="button">`);
    expect(hits).toEqual([]);
  });

  it("keeps console.log out of shipped code", () => {
    expect(findAll(/console\.log\(/g)).toEqual([]);
  });

  it("has no empty catch blocks that swallow a failure silently", () => {
    // This is the class of bug that hid the kbStatus TypeError for months.
    // A catch may be empty only with a comment saying why.
    const hits = [];
    for (const f of FILES) {
      const body = fs.readFileSync(f, "utf8");
      for (const m of body.matchAll(/catch\s*(?:\([^)]*\))?\s*\{\s*\}/g)) {
        hits.push(`${rel(f)}: ${m[0]}`);
      }
    }
    expect(hits).toEqual([]);
  });
});

describe("bundle discipline", () => {
  const appSrc = fs.readFileSync(path.join(SRC, "App.js"), "utf8");

  it("lazy-loads every route except the eager landing page", () => {
    const lazies = [...appSrc.matchAll(/lazy\(\(\) => import\("@\/pages\//g)];
    expect(lazies.length).toBeGreaterThanOrEqual(15);
  });

  it("keeps Discovery eager so the first paint is not a skeleton", () => {
    expect(appSrc).toMatch(/import DiscoveryPage from "@\/pages\/DiscoveryV2"/);
  });

  it("does not statically import the heavy libraries anywhere eager", () => {
    // mermaid / monaco / d3 / jszip / recharts are 450 KB gzip between
    // them. Each must be reached only through a lazy chunk.
    const eager = ["App.js", "index.js"];
    for (const name of eager) {
      const body = fs.readFileSync(path.join(SRC, name), "utf8");
      expect(body).not.toMatch(/from "(mermaid|d3|jszip|recharts|@monaco-editor)/);
    }
  });

  it("loads motion through LazyMotion, not the full framer-motion bundle", () => {
    // `strict` bans <motion.div>, forcing the ~5 KB `m` import instead of
    // the ~34 KB default.
    expect(appSrc).toContain("LazyMotion");
    expect(appSrc).toContain("strict");
    expect(appSrc).not.toMatch(/\bmotion\.\w+/);
  });
});
