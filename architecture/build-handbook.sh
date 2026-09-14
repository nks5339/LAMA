#!/usr/bin/env bash
# Render architecture/handbook.html -> architecture/handbook.pdf via headless Chrome.
# Mirrors scripts/build-docs-pdf.sh: Chrome is used because it supports the same
# CSS the app's UI uses (web fonts, flexbox, print colour) so the PDF matches the
# product's visual identity. The stylesheet is shared with docs/setup-guide.html.
#   ./architecture/build-handbook.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SRC="architecture/handbook.html"
OUT="architecture/handbook.pdf"

CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium" \
         "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
         "$(command -v google-chrome 2>/dev/null || true)" \
         "$(command -v chromium 2>/dev/null || true)"; do
  [ -n "$c" ] && [ -x "$c" ] && { CHROME="$c"; break; }
done
[ -n "$CHROME" ] || { echo "No Chrome/Chromium found — install one to rebuild the PDF."; exit 1; }

# The handbook embeds one light-theme render per flow: architecture/diagrams/<name>.png.
# Regenerate those first if a diagram source changed:
#   node .claude/skills/archify/bin/archify.mjs deliver      <type> src/<f>.json diagrams/<f>.html --quality showcase
#   node .claude/skills/archify/bin/archify.mjs visual-check diagrams/<f>.html --json
#
# visual-check writes four screenshots per diagram (1440x900 and 2048x1320, each
# light and dark) plus a contact sheet. Four near-identical renders of the same
# structure is confusing, so we promote the 1440x900 light one to <name>.png and
# drop the rest. The JSON receipt in evidence/ keeps the full browser evidence.
prune_renders() {
  local d="architecture/diagrams" n
  for f in "$d"/*.visual-check.1440x900.light.png; do
    [ -e "$f" ] || return 0                       # already pruned
    n="$(basename "$f" .visual-check.1440x900.light.png)"
    mv -f "$f" "$d/$n.png"
    echo "  promoted $n.png"
  done
  rm -f "$d"/*.visual-check.*.png \
        "$d"/*.visual-check.html \
        "$d"/*.visual-check.json   # byte-identical to the copy in evidence/
}
prune_renders

# --virtual-time-budget lets the Google Fonts request finish before printing,
# otherwise the PDF falls back to system fonts.
"$CHROME" --headless --disable-gpu --no-sandbox \
  --no-pdf-header-footer \
  --virtual-time-budget=25000 \
  --print-to-pdf="$OUT" \
  "file://$(pwd)/$SRC" 2>/dev/null

[ -s "$OUT" ] || { echo "Render produced no output."; exit 1; }
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
