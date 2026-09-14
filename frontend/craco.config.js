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

webpackConfig.devServer = (devServerConfig) => {
  // Add proxy configuration for /api routes
  devServerConfig.proxy = {
    '/api': {
      target: process.env.REACT_APP_BACKEND_URL || 'http://127.0.0.1:8382',
      changeOrigin: true,
      secure: false,
    },
  };

  return devServerConfig;
};

// Visual-edits wrapper removed for production / open-source build.

module.exports = webpackConfig;
