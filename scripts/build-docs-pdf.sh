#!/usr/bin/env bash
# Build docs/LAMA-Setup-Guide.pdf from docs/LAMA-USER-GUIDE.md.
#
#   markdown -> docs/setup-guide.html   (build-user-guide-html.py, LAMA theme)
#            -> docs/LAMA-Setup-Guide.pdf (headless Chrome)
#
# Chrome is used because it supports the same CSS the app's UI uses (web fonts,
# flexbox, print colour) so the PDF matches the product's visual identity.
#
# setup-guide.html is GENERATED. Edit docs/LAMA-USER-GUIDE.md for content, or
# scripts/build-user-guide-html.py for styling — never the HTML itself.
#   ./scripts/build-docs-pdf.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC="docs/setup-guide.html"
OUT="docs/LAMA-Setup-Guide.pdf"

# ---- 1. Regenerate the HTML so the PDF can never lag the markdown. ----
PY_BIN=""
for p in "./.venv/bin/python" "$(command -v python3 2>/dev/null || true)"; do
  [ -n "$p" ] && [ -x "$p" ] && { PY_BIN="$p"; break; }
done
[ -n "$PY_BIN" ] || { echo "No python found."; exit 1; }

if ! "$PY_BIN" -c "import markdown" 2>/dev/null; then
  echo "The 'markdown' package is missing. Install it with:"
  echo "  $PY_BIN -m pip install markdown"
  exit 1
fi
"$PY_BIN" scripts/build-user-guide-html.py

# ---- 2. Render. ----

CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium" \
         "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
         "$(command -v google-chrome 2>/dev/null || true)" \
         "$(command -v chromium 2>/dev/null || true)"; do
  [ -n "$c" ] && [ -x "$c" ] && { CHROME="$c"; break; }
done
[ -n "$CHROME" ] || { echo "No Chrome/Chromium found — install one to rebuild the PDF."; exit 1; }

# --virtual-time-budget lets the Google Fonts request finish before printing,
# otherwise the PDF falls back to system fonts.
"$CHROME" --headless --disable-gpu --no-sandbox \
  --no-pdf-header-footer \
  --virtual-time-budget=20000 \
  --print-to-pdf="$OUT" \
  "file://$(pwd)/$SRC" 2>/dev/null

[ -s "$OUT" ] || { echo "Render produced no output."; exit 1; }
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
