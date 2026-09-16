// craco.config.js
const path = require("path");
require("dotenv").config();

// The ENABLE_HEALTH_CHECK block and plugins/health-check/ (333 LOC) were
// removed in 2026-09: a repo-wide grep for ENABLE_HEALTH_CHECK returned
// exactly one hit, the line here that read it. It was never set in
// frontend/.env, the Dockerfile or docker-compose.yml, so both modules were
// dead in every environment.

let webpackConfig = {
  eslint: {
    configure: {
      extends: ["plugin:react-hooks/recommended"],
      rules: {
        "react-hooks/rules-of-hooks": "error",
        "react-hooks/exhaustive-deps": "warn",
      },
    },
  },
  webpack: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    configure: (webpackConfig) => {

      // Add ignored patterns to reduce watched directories
        webpackConfig.watchOptions = {
          ...webpackConfig.watchOptions,
          ignored: [
            '**/node_modules/**',
            '**/.git/**',
            '**/build/**',
            '**/dist/**',
            '**/coverage/**',
            '**/public/**',
        ],
      };

      return webpackConfig;
    },
  },
};

// Jest needs the same `@/` alias webpack has, or every test that imports a
// component fails to resolve. react-scripts owns the rest of the Jest
// config (jsdom env, babel transform, setupTests discovery); this only
// adds the alias and keeps coverage pointed at real source.
webpackConfig.jest = {
  configure: {
    moduleNameMapper: {
      '^@/(.*)$': '<rootDir>/src/$1',
      // react-router-dom 7's package.json `main` points at dist/main.js,
      // which does not exist — Node is saved by the `exports` map, but
      // Jest 27's resolver falls back to `main` and fails outright. Point
      // both packages at the CJS build they actually ship.
      '^react-router-dom$': '<rootDir>/node_modules/react-router-dom/dist/index.js',
      '^react-router$': '<rootDir>/node_modules/react-router/dist/development/index.js',
      '^react-router/dom$':
        '<rootDir>/node_modules/react-router/dist/development/dom-export.js',
    },
    collectCoverageFrom: [
      'src/**/*.{js,jsx,ts,tsx}',
      '!src/**/*.d.ts',
      '!src/index.js',
      '!src/reportWebVitals.js',
      '!src/**/__tests__/**',
    ],
  },
};

webpackConfig.devServer = (devServerConfig) => {
  // Proxy /api to the backend during split local dev.
  //
  // The fallback used to be :8382 — the single-image container port. That
  // is never right here: in the container nginx serves the built bundle and
  // this dev server does not run at all. Split dev runs uvicorn on :8000
  // (see CLAUDE.md), so an empty REACT_APP_BACKEND_URL — which is what
  // frontend/.env ships — proxied to a port with nothing on it and every
  // /api call 504'd.
  //
  // Override with REACT_APP_API_PROXY when the backend is elsewhere; it is
  // separate from REACT_APP_BACKEND_URL because setting that one makes the
  // browser call the backend directly and bypass this proxy entirely.
  const proxyTarget =
    process.env.REACT_APP_API_PROXY ||
    process.env.REACT_APP_BACKEND_URL ||
    'http://127.0.0.1:8000';

  devServerConfig.proxy = {
    '/api': {
      target: proxyTarget,
      changeOrigin: true,
      secure: false,
    },
  };

  return devServerConfig;
};

// Visual-edits wrapper removed for production / open-source build.

module.exports = webpackConfig;
