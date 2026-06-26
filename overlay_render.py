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

PROP_NAME = env("PROP_NAME", "Your Property")
PROP_URL  = env("PROP_URL", "yourproperty.com")

CONSTRUCTION_LOGO = env("CONSTRUCTION_LOGO", "/app/assets/construction_logo.png")
PROPERTY_LOGO     = env("PROPERTY_LOGO",     "/app/assets/property_logo.png")

# fonts (drop custom .ttf in ./assets and point these at them for brand type)
FONT_BOLD = env("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_REG  = env("FONT_REG",  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_COND = env("FONT_COND", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")

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

def fetch_values():
    with urllib.request.urlopen(WLL_URL, timeout=10) as r:
        doc = json.loads(r.read().decode())
    conds = (doc.get("data") or {}).get("conditions") or []
    iss = next((c for c in conds if c.get("data_structure_type") == 1), {})
    bar = next((c for c in conds if c.get("data_structure_type") == 3), {})
    vals = {
        "temp": iss.get("temp"), "hum": iss.get("hum"), "dew": iss.get("dew_point"),
        "wind": iss.get("wind_speed_last"), "wdir": iss.get("wind_dir_last"),
        "baro": bar.get("bar_sea_level"),
        "hidx": iss.get("heat_index"), "wchill": iss.get("wind_chill"),
    }
    for k, v in vals.items():
        if v is not None:
            _last[k] = v          # keep last-good readings on partial/failed polls
    return dict(_last)

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
    lbox = [cx - 92, 92, cx + 92, 210]
    clogo = load_logo(CONSTRUCTION_LOGO, lbox[2]-lbox[0], lbox[3]-lbox[1])
    if clogo: paste_logo(ov, clogo, lbox)
    else:     placeholder(d, lbox, "CONSTRUCTION")
    hr(232)

    # three equally-spaced stacked sections
    top, bot = 252, 824
    bh = (bot - top) / 3.0
    centers = [top + bh * (i + 0.5) for i in range(3)]
    hr(top + bh); hr(top + 2 * bh)

    def cell(cy, label, value, sub, subc):
        d.text((cx, cy - 58), label, font=bold(22), fill=MUTE, anchor="mm")
        d.text((cx, cy),      value, font=bold(62), fill=WHITE, anchor="mm")
        if sub: d.text((cx, cy + 50), sub, font=bold(26), fill=subc, anchor="mm")

    feels = v.get("hidx") if (v["temp"] is not None and v["temp"] >= 70) else v.get("wchill")
    temp_sub = ("Feels " + fmt(feels) + "\u00b0") if feels is not None else ""
    dew_sub  = ("Dew pt " + fmt(v["dew"]) + "\u00b0") if v["dew"] is not None else ""

    cell(centers[0], "TEMPERATURE", fmt(v["temp"]) + "\u00b0F", temp_sub, LIGHT)
    cell(centers[1], "WIND",        fmt(v["wind"]) + " mph", compass(v["wdir"]) or "--", ACCENT_RGB)
    cell(centers[2], "HUMIDITY",    fmt(v["hum"]) + "%", dew_sub, LIGHT)

    hr(bot)

    # property marketing site (logo over url, bottom)
    pbox = [cx - 100, 850, cx + 100, 978]
    plogo = load_logo(PROPERTY_LOGO, pbox[2]-pbox[0]-16, pbox[3]-pbox[1]-12)
    if plogo: paste_logo(ov, plogo, pbox)
    else:     placeholder(d, pbox, "LOGO")
    d.text((cx, 1014), PROP_URL, font=bold(22), fill=ACCENT_RGB, anchor="mm")

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
            vals = fetch_values() if WLL_URL else dict(_last)
            _state["png"] = encode_png(render_overlay(vals))
        except Exception as e:
            sys.stderr.write(f"[overlay] render error: {e}\n"); sys.stderr.flush()
            if _state["png"] is None:                      # ensure we always have a frame
                _state["png"] = encode_png(render_overlay(dict(_last)))
        time.sleep(POLL)

def main():
    if not WLL_HOST:
        sys.stderr.write("[overlay] WLL_HOST not set\n")
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
