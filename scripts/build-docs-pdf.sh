#!/usr/bin/env bash
# Render docs/setup-guide.html -> docs/LAMA-Setup-Guide.pdf via headless Chrome.
# Chrome is used because it supports the same CSS the app's UI uses (web fonts,
# flexbox, print colour) so the PDF matches the product's visual identity.
#   ./scripts/build-docs-pdf.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC="docs/setup-guide.html"
OUT="docs/LAMA-Setup-Guide.pdf"

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
