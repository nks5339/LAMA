// ESLint flat config — for the standalone `yarn lint` step ONLY.
//
// Why this file did not exist until 2026-09, and why it is separate from
// the build:
//
//   * There was no .eslintrc, no eslint.config.*, no `eslintConfig` key in
//     package.json and no `lint` script anywhere in the repo. The only lint
//     configuration was the inline block in craco.config.js, which extends
//     `eslint-config-react-app/base` -- and `base.js` enables exactly TWO
//     rules (react/jsx-uses-vars, react/jsx-uses-react). With the react-hooks
//     pair that is four rules total. `no-unused-vars` is NOT among them; it
//     lives in eslint-config-react-app/index.js, which is not extended.
//     So flipping DISABLE_ESLINT_PLUGIN off would still have reported none
//     of the 44 unused imports this config finds.
//
//   * package.json already carried @eslint/js, globals, eslint-plugin-import,
//     eslint-plugin-jsx-a11y, eslint-plugin-react and eslint-plugin-react-hooks
//     as devDependencies with nothing referencing them -- the exact shopping
//     list for a flat config that was never written.
//
//   * This is deliberately NOT wired into the webpack build. yarn installed
//     eslint 9.23.0 at the top level, but react-scripts depends on eslint
//     ^8.3.0 and yarn nested a second copy at
//     react-scripts/node_modules/eslint @ 8.57.1. CRA resolves the linter via
//     `require.resolve('eslint')` from inside react-scripts/config/, so the
//     BUILD would load 8.57.1 (eslintrc format) while the CLI loads 9.23.0
//     (flat format). The two cannot share a config. Keeping lint as its own
//     `yarn lint` step sidesteps that split entirely and leaves the build
//     path -- which is known to work -- untouched.
//
// Run:  yarn lint        (add --fix to autofix what is safely fixable)

const js = require("@eslint/js");
const globals = require("globals");
const react = require("eslint-plugin-react");
const reactHooks = require("eslint-plugin-react-hooks");
// ESLint's default parser (espree) cannot read type annotations, so the
// strangler .ts modules need this. Syntax-only — no type-aware rules, so
// lint stays fast and `tsc --noEmit` remains the type gate.
const tseslint = require("typescript-eslint");

module.exports = [
  {
    ignores: [
      "build/**",
      "node_modules/**",
      "plugins/**",          // build-time node scripts, not app source
      "**/*.bak",
      "**/*.bak2",
    ],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2021,
        process: "readonly",   // CRA inlines process.env.REACT_APP_*
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: {
      react: { version: "detect" },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
    },
    rules: {
      // The rules that actually catch waste. These are the point of the file.
      "no-unused-vars": ["error", {
        args: "none",                    // handler signatures are contractual
        ignoreRestSiblings: true,        // `const {a, ...rest} = obj` idiom
        varsIgnorePattern: "^_",
        // ESLint 9 changed `caughtErrors` to default "all". This codebase
        // uses `catch (_) {}` throughout as its deliberate ignore-the-error
        // idiom; flagging it would be a rename campaign, not a waste finding.
        caughtErrors: "none",
      }],
      "no-undef": "error",
      "no-debugger": "error",

      // React correctness. rules-of-hooks is a genuine bug class; the
      // exhaustive-deps warning stays a warning because the repo already
      // carries 44 deliberate eslint-disable comments for it and turning it
      // to error would be a refactor, not a lint pass.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "off",     // React 19 / new JSX transform
      "react/react-in-jsx-scope": "off", // ditto

      // Noise for this codebase: console.* is the deliberate logging channel
      // in MiniConsole, and empty catch blocks are a used idiom.
      "no-empty": ["error", { allowEmptyCatch: true }],

      // Style, not waste -- demoted to warnings for the same reason
      // backend/ruff.toml deliberately does not select E4/E7: this repo
      // enforces no project-wide formatter and new code matches the
      // surrounding file. Both fire only inside the Mermaid-parsing regexes
      // in Architecture.jsx, where "unnecessary" escapes are not safely
      // removable by hand: in `[A-Za-z0-9_\-\.\/]` the `\-` sits between `_`
      // and `.`, so unescaping it turns those three characters into a
      // reversed character range rather than a literal hyphen.
      "no-useless-escape": "warn",
      "no-misleading-character-class": "warn",
    },
  },
  {
    // Test files: Jest globals, plus Node's require/__dirname for the
    // source-level contract suite in src/__tests__/design-system.test.js,
    // which reads the tree off disk rather than rendering it.
    files: ["src/**/*.test.{js,jsx,ts,tsx}", "src/setupTests.js"],
    languageOptions: {
      globals: {
        ...globals.jest,
        ...globals.node,
      },
    },
    rules: {
      // A test may deliberately assert on an empty catch or an unused
      // binding while setting up a fixture.
      "no-unused-vars": ["error", { args: "none", varsIgnorePattern: "^_" }],
    },
  },
  {
    // TypeScript sources — the strangler modules, plus the generated
    // api.d.ts. Parsed by typescript-eslint; `no-undef` is switched off
    // because TS itself resolves identifiers and the rule double-reports
    // every type name.
    files: ["src/**/*.ts", "src/**/*.tsx"],
    languageOptions: {
      parser: tseslint.parser,
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2021 },
    },
    rules: {
      "no-undef": "off",
      "no-unused-vars": "off",   // tsc reports these with better precision
    },
  },
  {
    // Config files run in Node.
    files: ["*.config.js", "craco.config.js", "tailwind.config.js", "postcss.config.js"],
    languageOptions: {
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
  },
];
