#!/usr/bin/env bash
# Render ONE overlay frame locally (macOS) and open it — no Docker needed.
#
#   ./preview.sh
#
# First run creates .venv/ and installs Pillow (gitignored). Reads the look +
# location settings from .env, then overrides the fonts/logo paths for macOS
# (the container's DejaVu font paths don't exist here). Pulls live weather the
# same way the stream does: WeatherLink if WLL_HOST is set, else Open-Meteo
# from WX_LAT/WX_LON.
set -euo pipefail
cd "$(dirname "$0")"

# 1. venv with Pillow (created on first run)
if [ ! -x .venv/bin/python ]; then
  echo "Setting up .venv (one-time)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip pillow
fi

# 2. Pull rendering-relevant settings from .env (ignores secrets; strips inline comments)
envget() {
  [ -f .env ] || return 0
  grep -E "^$1=" .env 2>/dev/null | head -1 | sed -E "s/^$1=//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//" || true
}
for k in WLL_HOST WLL_PORT WX_LAT WX_LON PROP_NAME PROP_URL \
         BAR_RGB ACCENT_RGB BAR_ALPHA PANEL_W PANEL_SIDE WIDTH HEIGHT TZ; do
  v="$(envget "$k")"
  if [ -n "$v" ]; then export "$k=$v"; fi
done

# 3. macOS overrides: fonts (Linux DejaVu paths don't exist here) + local asset paths
export FONT_BOLD="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
export FONT_REG="/System/Library/Fonts/Supplemental/Arial.ttf"
export FONT_COND="/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf"
export FONT_SERIF="/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
export CONSTRUCTION_LOGO="assets/construction_logo.png"
export PROPERTY_LOGO="assets/property_logo.png"

# 4. Render a single frame with live data and open it.
#    Falls back to placeholders if the weather source is unreachable, so the
#    layout still previews.
.venv/bin/python - <<'PY'
import sys, overlay_render as o
try:
    v = o.fetch_values()
except Exception as e:
    sys.stderr.write(f"weather fetch failed ({e}); rendering with placeholders\n")
    v = dict(o._last)
o.render_overlay(v).save("preview.png")
print("wrote preview.png")
PY
open preview.png
