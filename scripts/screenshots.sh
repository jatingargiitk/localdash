#!/usr/bin/env bash
# Re-render the example dashboards and capture the PNGs in docs/ with headless Chrome.
set -euo pipefail
cd "$(dirname "$0")/.."

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
shot() {  # shot <html> <png> <height> [fragment]
  local path="$1"
  [[ "$path" = /* ]] || path="$PWD/$path"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1440,"$3" --virtual-time-budget=5000 --screenshot="$2" "file://$path${4:-}" 2>/dev/null
  echo "wrote $2"
}

for name in saas github x_posts; do
  localdash render "examples/$name/dash.json" --strict 2>/dev/null
  shot "examples/$name/dash.html" "docs/$name.png" 1600
done

shot examples/saas/dash.html docs/sql.png 1000 "#sql"

tmp="$(mktemp -d)"
localdash render examples/saas/dash.json --theme light -o "$tmp/light.html" 2>/dev/null
shot "$tmp/light.html" docs/light.png 1000
