#!/usr/bin/env python3
"""
Overlay renderer for the camera -> Restream pipeline.

Renders the FULL branded weather lower-third (logos + live Davis data) to a
transparent PNG and streams it to ffmpeg's image2pipe input on stdout. A
background thread re-fetches the WeatherLink Live data and re-renders every
POLL_SECONDS; the main thread re-sends the current PNG at OVERLAY_FPS so
ffmpeg always has a fresh frame to composite.

This file is your canvas. Edit COLORS, fonts, layout, and render_overlay()
freely -- whatever you draw shows up on the stream.

Logos: drop PNGs (transparent bg preferred) in ./assets and point the env
vars CONSTRUCTION_LOGO / PROPERTY_LOGO at them. Missing files fall back to a
dashed placeholder box, so the pipeline runs before you add art.
"""
import io
import os
import sys
import json
import time
import threading
import datetime
import urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ------------------------------------------------------------------ config ---
def env(k, d=None): return os.environ.get(k, d)

WIDTH       = int(env("WIDTH", "1920"))      # MUST match the camera profile resolution
HEIGHT      = int(env("HEIGHT", "1080"))
OVERLAY_FPS = float(env("OVERLAY_FPS", "4"))
POLL        = int(env("POLL_SECONDS", "30"))

WLL_HOST = env("WLL_HOST")
WLL_PORT = env("WLL_PORT", "80")
WLL_URL  = f"http://{WLL_HOST}:{WLL_PORT}/v1/current_conditions" if WLL_HOST else None

# Free fallback data source (Open-Meteo, no API key) used when WLL_HOST is unset.
# Set WX_LAT/WX_LON to the camera's location. WeatherLink takes priority once
# WLL_HOST is configured, so the switch back to the local station is automatic.
WX_LAT = env("WX_LAT")
WX_LON = env("WX_LON")
OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={WX_LAT}&longitude={WX_LON}"
    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
    "dew_point_2m,wind_speed_10m,wind_direction_10m,pressure_msl"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph"
) if (WX_LAT and WX_LON) else None

PROP_NAME = env("PROP_NAME", "Your Property")
PROP_URL  = env("PROP_URL", "yourproperty.com")

# Vehicle counts written by vehicle_counter.py (today's snapshot). Relative
# default works for native preview; the container sets /app/data/counts.json.
COUNTS_FILE = env("COUNTS_FILE", "data/counts.json")
DIR_A_LABEL = env("DIR_A_LABEL", "NB")   # near carriageway (count line A)
DIR_B_LABEL = env("DIR_B_LABEL", "SB")   # far  carriageway (count line B)
# Spell out compass abbreviations for the overlay; unknown labels pass through.
_DIR_FULL = {"NB": "NORTHBOUND", "SB": "SOUTHBOUND",
             "EB": "EASTBOUND",  "WB": "WESTBOUND"}
def full_dir(lbl): return _DIR_FULL.get((lbl or "").upper(), lbl or "")

CONSTRUCTION_LOGO = env("CONSTRUCTION_LOGO", "/app/assets/construction_logo.png")
PROPERTY_LOGO     = env("PROPERTY_LOGO",     "/app/assets/property_logo.png")
# When no PROPERTY_LOGO image exists, the property slot renders this text as a
# serif wordmark instead. Words are stacked on separate lines (e.g. "Birken
# Lofts" -> BIRKEN / LOFTS). Defaults to PROP_NAME.
PROP_BRAND = env("PROP_BRAND", PROP_NAME)

# fonts (drop custom .ttf in ./assets and point these at them for brand type)
FONT_BOLD  = env("FONT_BOLD",  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_REG   = env("FONT_REG",   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_COND  = env("FONT_COND",  "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")
FONT_SERIF = env("FONT_SERIF", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")

# ---- EDIT THESE: brand palette (RGB) -------------------------------------- #
BAR_RGB    = tuple(int(x) for x in env("BAR_RGB",    "13,27,42").split(","))   # panel
ACCENT_RGB = tuple(int(x) for x in env("ACCENT_RGB", "246,166,9").split(","))  # strip + highlights
BAR_ALPHA  = int(env("BAR_ALPHA", "150"))                                      # 0-255 panel opacity
PANEL_W    = int(env("PANEL_W", "330"))                                        # vertical panel width (px @1080)
PANEL_SIDE = env("PANEL_SIDE", "right")                                        # "right" or "left"
WHITE = (255, 255, 255); MUTE = (159, 176, 196); LIGHT = (205, 214, 226)
# --------------------------------------------------------------------------- #

def F(path, sz): return ImageFont.truetype(path, sz)
DIRS16 = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
def compass(deg):
    return "" if deg is None else DIRS16[int((deg % 360) / 22.5 + 0.5) % 16]

# --------------------------------------------------------------- data fetch ---
_last = {"temp": None, "hum": None, "dew": None, "wind": None, "wdir": None,
         "baro": None, "hidx": None, "wchill": None}

def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())

def fetch_wll():
    """Local Davis WeatherLink Live (preferred source)."""
    doc = _get_json(WLL_URL)
    conds = (doc.get("data") or {}).get("conditions") or []
    iss = next((c for c in conds if c.get("data_structure_type") == 1), {})
    bar = next((c for c in conds if c.get("data_structure_type") == 3), {})
    return {
        "temp": iss.get("temp"), "hum": iss.get("hum"), "dew": iss.get("dew_point"),
        "wind": iss.get("wind_speed_last"), "wdir": iss.get("wind_dir_last"),
        "baro": bar.get("bar_sea_level"),
        "hidx": iss.get("heat_index"), "wchill": iss.get("wind_chill"),
    }

def fetch_openmeteo():
    """Free, keyless fallback. apparent_temperature already blends heat index /
    wind chill, so it maps to whichever 'feels like' branch render uses."""
    cur = (_get_json(OPENMETEO_URL).get("current") or {})
    feels = cur.get("apparent_temperature")
    return {
        "temp": cur.get("temperature_2m"), "hum": cur.get("relative_humidity_2m"),
        "dew": cur.get("dew_point_2m"),
        "wind": cur.get("wind_speed_10m"), "wdir": cur.get("wind_direction_10m"),
        "baro": cur.get("pressure_msl"),
        "hidx": feels, "wchill": feels,
    }

def fetch_values():
    vals = fetch_wll() if WLL_URL else fetch_openmeteo()
    for k, v in vals.items():
        if v is not None:
            _last[k] = v          # keep last-good readings on partial/failed polls
    return dict(_last)

# Vehicle counts: read the snapshot the counter writes. Last-good cache so a
# missing/half-written file (counter restarting) never blanks the panel.
_lastcounts = {"total": None, "by_dir": {}, "by_cls": {}}
def read_counts():
    try:
        with open(COUNTS_FILE) as f:
            doc = json.load(f)
        _lastcounts["total"]  = doc.get("total", 0)
        _lastcounts["by_dir"] = doc.get("by_dir", {}) or {}
        _lastcounts["by_cls"] = doc.get("by_cls", {}) or {}
    except Exception:
        pass                      # keep last-good (or None before first read)
    return dict(_lastcounts)

# ----------------------------------------------------------------- helpers ---
def load_logo(path, box_w, box_h):
    try:
        im = Image.open(path).convert("RGBA")
        im.thumbnail((box_w, box_h), Image.LANCZOS)
        return im
    except Exception:
        return None

def placeholder(d, box, label="LOGO"):
    d.rounded_rectangle(box, radius=14, fill=WHITE + (26,))
    d.rounded_rectangle(box, radius=14, outline=WHITE + (90,), width=2)
    cx = (box[0] + box[2]) // 2; cy = (box[1] + box[3]) // 2
    d.text((cx, cy), label, font=F(FONT_REG, 18), fill=MUTE, anchor="mm")

def paste_logo(base, logo, box):
    bx0, by0, bx1, by1 = box
    cx = (bx0 + bx1) // 2 - logo.width // 2
    cy = (by0 + by1) // 2 - logo.height // 2
    base.alpha_composite(logo, (cx, cy))

def fit_line(d, text, cx, cy, left, right, font_path=FONT_COND, fill=WHITE, cap=22):
    """Draw one centered line, shrinking the font until it fits left..right."""
    max_w = right - left
    size = cap
    while size > 12 and d.textlength(text, font=F(font_path, size)) > max_w:
        size -= 1
    d.text((cx, cy), text, font=F(font_path, size), fill=fill, anchor="mm")

def wordmark(d, box, text, font_path=FONT_SERIF, fill=WHITE, cap=58):
    """Render `text` as a centered serif wordmark inside `box`, one word per
    line. Auto-shrinks to fit the box width. Used when no logo image exists."""
    bx0, by0, bx1, by1 = box
    cx = (bx0 + bx1) // 2; cy = (by0 + by1) // 2
    lines = text.upper().split() or ["LOGO"]
    max_w = (bx1 - bx0)
    size = cap
    while size > 14:
        f = F(font_path, size)
        if max(d.textlength(ln, font=f) for ln in lines) <= max_w:
            break
        size -= 2
    f = F(font_path, size)
    line_h = size * 1.06
    y = cy - line_h * (len(lines) - 1) / 2
    for ln in lines:
        d.text((cx, y), ln, font=f, fill=fill, anchor="mm")
        y += line_h

# ------------------------------------------------------------ render canvas ---
# Designed on a 1920x1080 grid; resized to WIDTH/HEIGHT at the end if different.
def render_overlay(v):
    ov = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d0 = ImageDraw.Draw(ov)
    bold = lambda s: F(FONT_BOLD, s); reg = lambda s: F(FONT_REG, s)

    margin, rad, pad = 28, 22, 24
    if PANEL_SIDE == "left":
        x0 = margin; x1 = x0 + PANEL_W
    else:
        x1 = 1920 - margin; x0 = x1 - PANEL_W
    y0, y1 = 28, 1052
    cx = (x0 + x1) // 2

    # soft shadow behind the panel
    sh = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=(0, 0, 0, 140))
    ov.alpha_composite(sh.filter(ImageFilter.GaussianBlur(16)))

    d = ImageDraw.Draw(ov)
    d.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=BAR_RGB + (BAR_ALPHA,))
    d.rounded_rectangle([x0, y0, x1, y0 + 7], radius=4, fill=ACCENT_RGB + (255,))

    def fmt(x, dp=0):
        if x is None: return "--"
        return f"{x:.{dp}f}" if dp else f"{round(x)}"
    def hr(yy): d.line([(x0 + pad, yy), (x1 - pad, yy)], fill=WHITE + (40,), width=2)

    # clock (LIVE removed)
    d.text((cx, y0 + 34), datetime.datetime.now().strftime("%a %-I:%M %p").upper(),
           font=bold(24), fill=LIGHT, anchor="mm")

    # construction logo (3F badge)
    lbox = [cx - 84, 86, cx + 84, 186]
    clogo = load_logo(CONSTRUCTION_LOGO, lbox[2]-lbox[0], lbox[3]-lbox[1])
    if clogo: paste_logo(ov, clogo, lbox)
    else:     placeholder(d, lbox, "CONSTRUCTION")
    hr(204)

    # three equally-spaced stacked weather sections
    top, bot = 222, 606
    bh = (bot - top) / 3.0
    centers = [top + bh * (i + 0.5) for i in range(3)]
    hr(top + bh); hr(top + 2 * bh)

    def cell(cy, label, value, sub, subc):
        d.text((cx, cy - 50), label, font=bold(21), fill=MUTE, anchor="mm")
        d.text((cx, cy),      value, font=bold(54), fill=WHITE, anchor="mm")
        if sub: d.text((cx, cy + 44), sub, font=bold(24), fill=subc, anchor="mm")

    feels = v.get("hidx") if (v["temp"] is not None and v["temp"] >= 70) else v.get("wchill")
    temp_sub = ("Feels " + fmt(feels) + "\u00b0") if feels is not None else ""
    dew_sub  = ("Dew pt " + fmt(v["dew"]) + "\u00b0") if v["dew"] is not None else ""

    cell(centers[0], "TEMPERATURE", fmt(v["temp"]) + "\u00b0F", temp_sub, LIGHT)
    cell(centers[1], "WIND",        fmt(v["wind"]) + " mph", compass(v["wdir"]) or "--", ACCENT_RGB)
    cell(centers[2], "HUMIDITY",    fmt(v["hum"]) + "%", dew_sub, LIGHT)
    hr(bot)

    # traffic today: per-direction daily vehicle counts (from counts.json).
    # Resets to 0 each day (the counter rolls over at midnight in TZ).
    c = read_counts()
    by_dir = c.get("by_dir") or {}
    d.text((cx, 636), "VEHICLES TODAY", font=bold(21), fill=MUTE, anchor="mm")

    def dir_cell(cy, label, n):
        d.text((cx, cy), full_dir(label), font=bold(20), fill=MUTE, anchor="mm")
        fit_line(d, f"{n:,}" if n is not None else "--", cx, cy + 36,
                 x0 + pad, x1 - pad, FONT_BOLD, WHITE, cap=48)

    dir_cell(672, DIR_A_LABEL, by_dir.get("A"))   # near carriageway
    dir_cell(752, DIR_B_LABEL, by_dir.get("B"))   # far carriageway
    hr(810)

    # property brand (logo image if present, else serif wordmark) over the url
    pbox = [cx - 100, 822, cx + 100, 946]
    plogo = load_logo(PROPERTY_LOGO, pbox[2]-pbox[0]-16, pbox[3]-pbox[1]-12)
    if plogo: paste_logo(ov, plogo, pbox)
    else:     wordmark(d, [x0 + pad, 828, x1 - pad, 940], PROP_BRAND)
    d.text((cx, 1016), PROP_URL, font=bold(22), fill=ACCENT_RGB, anchor="mm")

    if (WIDTH, HEIGHT) != (1920, 1080):
        ov = ov.resize((WIDTH, HEIGHT), Image.LANCZOS)
    return ov

def encode_png(img):
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

# --------------------------------------------------------------------- main ---
_state = {"png": None}

def updater():
    while True:
        try:
            vals = fetch_values() if (WLL_URL or OPENMETEO_URL) else dict(_last)
            _state["png"] = encode_png(render_overlay(vals))
        except Exception as e:
            sys.stderr.write(f"[overlay] render error: {e}\n"); sys.stderr.flush()
            if _state["png"] is None:                      # ensure we always have a frame
                _state["png"] = encode_png(render_overlay(dict(_last)))
        time.sleep(POLL)

def main():
    if WLL_URL:
        sys.stderr.write(f"[overlay] weather source: WeatherLink Live ({WLL_HOST})\n")
    elif OPENMETEO_URL:
        sys.stderr.write(f"[overlay] weather source: Open-Meteo ({WX_LAT},{WX_LON})\n")
    else:
        sys.stderr.write("[overlay] no weather source set (WLL_HOST or WX_LAT/WX_LON); showing --\n")
    _state["png"] = encode_png(render_overlay(dict(_last)))   # initial frame before streaming
    threading.Thread(target=updater, daemon=True).start()
    out = sys.stdout.buffer
    delay = 1.0 / OVERLAY_FPS
    try:
        while True:
            out.write(_state["png"]); out.flush()
            time.sleep(delay)
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)                                          # ffmpeg went away -> let the loop restart us

if __name__ == "__main__":
    main()
