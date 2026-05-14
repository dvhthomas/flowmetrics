#!/usr/bin/env bash
# Generate a screenshot of a flowmetrics HTML report.
#
# Renders the rust-lang/rust Aging report using cached data, then
# uses Chrome headless to capture a PNG of the full page. Output:
# samples/aging_sample.png.
#
# Usage:
#     scripts/screenshot_sample.sh
#
# Requires:
#     - A populated cache at .cache/github (run once online to seed).
#     - Google Chrome installed (macOS path hard-coded; adjust for Linux).
#     - `uv` on PATH.

set -euo pipefail

cd "$(dirname "$0")/.."

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [[ ! -x "$CHROME" ]]; then
  # Try Linux fallbacks.
  for candidate in google-chrome chromium chrome; do
    if command -v "$candidate" > /dev/null; then
      CHROME="$candidate"
      break
    fi
  done
fi

if [[ ! -x "$CHROME" && ! "$(command -v "$CHROME")" ]]; then
  echo "error: Chrome not found. Edit \$CHROME in this script." >&2
  exit 1
fi

HTML_OUT="samples/aging_sample.html"
PNG_OUT="samples/aging_sample.png"

# Re-render against the cached rust-lang/rust data with the 180-day
# max-age filter so the chart shows a meaningful slice.
uv run flow aging \
    --repo rust-lang/rust \
    --wip-labels "S-waiting-on-author,S-waiting-on-review,S-waiting-on-bors" \
    --history-start 2026-04-30 --history-end 2026-05-13 \
    --max-age-days 180 \
    --cache-dir .cache/github --offline \
    --format html --output "$HTML_OUT"

# Capture full-page screenshot. window-size needs to be tall enough to
# fit the report; Chrome will render the page at the given viewport
# and capture it. 1440x3000 fits the canonical content with margin.
"$CHROME" \
    --headless \
    --disable-gpu \
    --hide-scrollbars \
    --no-sandbox \
    --screenshot="$PNG_OUT" \
    --window-size=1440,3000 \
    --virtual-time-budget=2000 \
    "file://$PWD/$HTML_OUT"

echo "Wrote $PNG_OUT ($(du -h "$PNG_OUT" | cut -f1))"
