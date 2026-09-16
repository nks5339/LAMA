/**
 * iter-20 — the export gate, asserted on the SOURCE.
 *
 * A broken Helidon -> Spring Boot tree reached the operator's disk because
 * `download_transformed_code` checked only that the transformation existed
 * and the scope string was valid: a red build exported exactly like a green
 * one. The backend now refuses with a 409, and the UI must not offer a
 * button that would hit it.
 *
 * Asserted on source rather than on a render, following
 * design-system.test.js: Transformer.jsx is ~5k lines with a large mock
 * surface, and the invariant here is structural — "no export affordance
 * outside the gate" is a property of the file, not of one render path. A
 * render test would prove one case; this proves there is no second case.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..");
const TRANSFORMER = path.join(SRC, "pages", "Transformer.jsx");
const API = path.join(SRC, "lib", "api.js");

const body = fs.readFileSync(TRANSFORMER, "utf8");

describe("export gate — the UI cannot offer a download the backend will refuse", () => {
  test("the page reads the backend's own verdict rather than recomputing it", () => {
    // The gate is computed once, server-side (`_build_readiness_gate`) and
    // enforced by the download endpoint itself. If the UI re-derived it
    // from `status` strings the two could disagree, which is the bug this
    // field exists to prevent.
    expect(body).toMatch(/downloadBlockedReason/);
    expect(body).toMatch(/download_blocked_reason/);
  });

  test("the verdict is assigned on every poll, including when it clears", () => {
    // `?? null` matters: the export must unblock the moment the build goes
    // green. A truthy-guarded assignment would leave a stale reason on
    // screen and keep the buttons hidden after a successful rerun.
    expect(body).toMatch(
      /setDownloadBlockedReason\(\s*s\.download_blocked_reason\s*\?\?\s*null\s*\)/
    );
  });

  test("every download affordance sits behind the gate", () => {
    // Each of the three ZIP links and the GitHub push must be inside the
    // `!downloadBlockedReason` branch. Counting them is what catches a
    // fourth export being added later outside it.
    const gatedRegion = body.slice(
      body.indexOf("{downloadBlockedReason ? ("),
      body.indexOf("transformer-history-btn")
    );
    expect(gatedRegion).toMatch(/transformer-download-btn/);
    expect(gatedRegion).toMatch(/transformer-download-tests-btn/);
    expect(gatedRegion).toMatch(/transformer-download-bundle-btn/);
    expect(gatedRegion).toMatch(/transformer-github-push-btn/);
  });

  test("the second export site is gated too", () => {
    // The Tester tab has its own "Tests ZIP" link. Gating the kebab menu
    // and leaving this one open would be a hole in the same gate.
    const idx = body.indexOf("tester-download-tests-btn");
    expect(idx).toBeGreaterThan(-1);
    const before = body.slice(Math.max(0, idx - 600), idx);
    expect(before).toMatch(/!downloadBlockedReason/);
  });

  test("a blocked state explains itself instead of showing nothing", () => {
    // An export that silently disappears reads as a bug. The reason text
    // is the backend's, so it names the actual failure.
    expect(body).toMatch(/transformer-download-blocked/);
    expect(body).toMatch(/Download not available yet/);
  });

  test("no export helper is called from outside the gated region", () => {
    // Belt and braces: the three helpers exist in lib/api.js, and the only
    // call sites in the page must be the gated ones counted above.
    const helpers = [
      "downloadTransformedCode",
      "downloadTransformedTests",
      "downloadTransformedBundle",
    ];
    const apiSrc = fs.readFileSync(API, "utf8");
    for (const h of helpers) {
      expect(apiSrc).toMatch(new RegExp(`export const ${h}`));
    }
    // Count invocations (not the import line) in the page.
    const calls = [...body.matchAll(/download(?:TransformedCode|TransformedTests|TransformedBundle)\(/g)];
    // 3 in the kebab menu + 1 on the Tester tab = 4. A fifth means a new
    // export affordance was added and must be gated deliberately.
    expect(calls.length).toBe(4);
  });
});
