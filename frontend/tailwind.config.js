/** @type {import('tailwindcss').Config} */
//
// Tokens live in src/index.css as space-separated RGB CHANNELS, bound here
// as `rgb(var(--x) / <alpha-value>)` so opacity modifiers (bg-ink/40,
// text-fg-onDark/70) actually compile. With a plain hex behind the var they
// compile to NOTHING, silently — which is how 86 such classes across the app
// ended up producing no CSS at all.
//
// This file only binds tokens to utility names. The previous four-palette setup (shadcn HSL + `ey.*` + the
// --mos-* / --pro-* CSS blocks) collapsed into the single set below.
//
module.exports = {
  content: ["./src/**/*.{js,jsx,ts,tsx}", "./public/index.html"],
  theme: {
    // ---- 8px spacing system --------------------------------------
    // Replaces (not extends) the default scale so odd values cannot be
    // reached by accident. The sub-8px steps that survive are the ones
    // real UI needs: hairlines, icon gaps, badge padding.
    spacing: {
      px: "1px",
      0: "0px",
      0.5: "2px",
      1: "4px",
      1.5: "6px",
      2: "8px",
      3: "12px",
      4: "16px",
      5: "20px",
      6: "24px",
      7: "28px",
      8: "32px",
      9: "36px",
      10: "40px",
      11: "44px",
      12: "48px",
      14: "56px",
      16: "64px",
      20: "80px",
      24: "96px",
      28: "112px",
      32: "128px",
      40: "160px",
      48: "192px",
      56: "224px",
      64: "256px",
      72: "288px",
      80: "320px",
      96: "384px",
    },

    // ---- Type scale, floored at 12px -----------------------------
    // 9/10/11px are deliberately absent. The audit found 1,179
    // declarations below 12px; removing those steps turns each one
    // into a build-visible arbitrary value you can grep for.
    fontSize: {
      micro: ["12px", { lineHeight: "16px", letterSpacing: "0.03em" }],
      xs: ["13px", { lineHeight: "18px" }],
      sm: ["14px", { lineHeight: "20px" }],
      base: ["15px", { lineHeight: "24px" }],
      lg: ["17px", { lineHeight: "26px", letterSpacing: "-0.005em" }],
      xl: ["20px", { lineHeight: "28px", letterSpacing: "-0.01em" }],
      "2xl": ["26px", { lineHeight: "32px", letterSpacing: "-0.02em" }],
      "3xl": ["33px", { lineHeight: "40px", letterSpacing: "-0.022em" }],
      "4xl": ["42px", { lineHeight: "48px", letterSpacing: "-0.025em" }],
    },

    extend: {
      colors: {
        bg: "rgb(var(--bg) / <alpha-value>)",
        surface: {
          DEFAULT: "rgb(var(--surface) / <alpha-value>)",
          2: "rgb(var(--surface-2) / <alpha-value>)",
          3: "rgb(var(--surface-3) / <alpha-value>)",
        },
        fg: {
          DEFAULT: "rgb(var(--fg) / <alpha-value>)",
          muted: "rgb(var(--fg-muted) / <alpha-value>)",
          subtle: "rgb(var(--fg-subtle) / <alpha-value>)",
          // Text ON the dark console panels. Three steps, same as the
          // light side, so those surfaces are not stuck with one colour.
          onDark: "rgb(var(--fg-onDark) / <alpha-value>)",
          onDarkMuted: "rgb(var(--fg-onDark-muted) / <alpha-value>)",
          onDarkSubtle: "rgb(var(--fg-onDark-subtle) / <alpha-value>)",
        },
        brand: {
          DEFAULT: "rgb(var(--brand) / <alpha-value>)",
          hover: "rgb(var(--brand-hover) / <alpha-value>)",
          fg: "rgb(var(--brand-fg) / <alpha-value>)",
          tint: "rgb(var(--brand-tint) / <alpha-value>)",
          edge: "rgb(var(--brand-edge) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--ink) / <alpha-value>)",
          hover: "rgb(var(--ink-hover) / <alpha-value>)",
          fg: "rgb(var(--ink-fg) / <alpha-value>)",
        },
        ok: {
          DEFAULT: "rgb(var(--ok) / <alpha-value>)",
          bg: "rgb(var(--ok-bg) / <alpha-value>)",
          edge: "rgb(var(--ok-edge) / <alpha-value>)",
          fg: "rgb(var(--ok-fg) / <alpha-value>)",
        },
        warn: {
          DEFAULT: "rgb(var(--warn) / <alpha-value>)",
          bg: "rgb(var(--warn-bg) / <alpha-value>)",
          edge: "rgb(var(--warn-edge) / <alpha-value>)",
          fg: "rgb(var(--warn-fg) / <alpha-value>)",
        },
        crit: {
          DEFAULT: "rgb(var(--crit) / <alpha-value>)",
          bg: "rgb(var(--crit-bg) / <alpha-value>)",
          edge: "rgb(var(--crit-edge) / <alpha-value>)",
          fg: "rgb(var(--crit-fg) / <alpha-value>)",
        },
        info: {
          DEFAULT: "rgb(var(--info) / <alpha-value>)",
          bg: "rgb(var(--info-bg) / <alpha-value>)",
          edge: "rgb(var(--info-edge) / <alpha-value>)",
          fg: "rgb(var(--info-fg) / <alpha-value>)",
        },
        border: {
          DEFAULT: "rgb(var(--border) / <alpha-value>)",
          strong: "rgb(var(--border-strong) / <alpha-value>)",
        },
        ring: "rgb(var(--ring) / <alpha-value>)",

        // Kept so the remaining shadcn primitives keep compiling while
        // call sites migrate. All point at the tokens above.
        background: "rgb(var(--bg) / <alpha-value>)",
        foreground: "rgb(var(--fg) / <alpha-value>)",
        card: { DEFAULT: "rgb(var(--surface) / <alpha-value>)", foreground: "rgb(var(--fg) / <alpha-value>)" },
        popover: { DEFAULT: "rgb(var(--surface) / <alpha-value>)", foreground: "rgb(var(--fg) / <alpha-value>)" },
        primary: { DEFAULT: "rgb(var(--ink) / <alpha-value>)", foreground: "rgb(var(--ink-fg) / <alpha-value>)" },
        secondary: { DEFAULT: "rgb(var(--surface-2) / <alpha-value>)", foreground: "rgb(var(--fg) / <alpha-value>)" },
        muted: { DEFAULT: "rgb(var(--surface-2) / <alpha-value>)", foreground: "rgb(var(--fg-muted) / <alpha-value>)" },
        accent: { DEFAULT: "rgb(var(--surface-2) / <alpha-value>)", foreground: "rgb(var(--fg) / <alpha-value>)" },
        destructive: { DEFAULT: "rgb(var(--crit) / <alpha-value>)", foreground: "rgb(var(--crit-fg) / <alpha-value>)" },
        input: "rgb(var(--border-strong) / <alpha-value>)",
      },

      borderRadius: {
        sm: "var(--radius-sm)",
        DEFAULT: "var(--radius)",
        md: "var(--radius)",
        lg: "var(--radius-lg)",
      },

      boxShadow: {
        raised: "var(--shadow-raised)",
        overlay: "var(--shadow-overlay)",
        modal: "var(--shadow-modal)",
      },

      transitionTimingFunction: { DEFAULT: "var(--ease)", ease: "var(--ease)" },
      transitionDuration: { DEFAULT: "200ms", fast: "150ms", slow: "300ms" },

      fontFamily: {
        sans: ['"IBM Plex Sans"', "system-ui", "-apple-system", "sans-serif"],
        display: ['"Chivo"', '"IBM Plex Sans"', "sans-serif"],
        mono: ['"IBM Plex Mono"', "Menlo", "monospace"],
      },

      // Minimum interactive target — WCAG 2.5.8 is 24px. Buttons set
      // --tap per size variant and read it back through min-h-tap.
      minHeight: { tap: "var(--tap, 32px)" },
      minWidth: { tap: "var(--tap, 32px)" },

      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "rgb(var(--radix-accordion-content-height) / <alpha-value>)" },
        },
        "accordion-up": {
          from: { height: "rgb(var(--radix-accordion-content-height) / <alpha-value>)" },
          to: { height: "0" },
        },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
      },
      animation: {
        "accordion-down": "accordion-down 200ms var(--ease)",
        "accordion-up": "accordion-up 200ms var(--ease)",
        "fade-in": "fade-in 150ms var(--ease)",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
