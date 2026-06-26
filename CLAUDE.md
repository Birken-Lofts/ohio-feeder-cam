# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A 24/7 live-stream pipeline: an Axis Q6318 traffic camera (RTSP) → ffmpeg → Restream (RTMP/FLV), with an optional branded weather overlay driven by a Davis WeatherLink Live (WLL) station on the LAN. There is no application server or test suite — the repo is a Dockerized ffmpeg pipeline plus a single Python overlay renderer.

## Commands

There is no build/lint/test tooling. Everything runs through Docker Compose, and all configuration + credentials live in a gitignored `.env` (not committed; `chmod 600 .env`).

```bash
# 1. Validate the camera→Restream path first (copy relay, no overlay, no re-encode)
docker compose -f docker-compose.min.yml up -d
docker compose -f docker-compose.min.yml logs -f      # look for climbing frame=/bitrate=
docker compose -f docker-compose.min.yml down

# 2. Full overlay pipeline (renders overlay + re-encodes with libx264)
docker compose up -d --build
docker compose logs -f

# Iterate on the overlay without rebuilding the image:
# overlay_render.py and assets/ are bind-mounted read-only, so just restart:
docker compose restart camera-restream

# Test the renderer in isolation (writes raw PNG frames to stdout):
WLL_HOST=<wll-ip> python3 overlay_render.py | head -c 1000 > /dev/null
```

## Architecture

Two independent compose files share the same `.env`:

- **`docker-compose.min.yml`** — `relay-test` service. Pulls a prebuilt ffmpeg image and does a stream `copy` (no decode/encode, no overlay). Use this to confirm credentials, RTSP reachability, and Restream ingest before touching the full pipeline.
- **`docker-compose.yml`** — `camera-restream` service. Built from `Dockerfile` (python:3.12-slim + ffmpeg/libx264 + DejaVu fonts + Pillow). The `command` is a shell loop that runs `python3 overlay_render.py | ffmpeg ...` and restarts after 10s on any exit, so the stream self-heals.

### The render → encode → push data flow (the core thing to understand)

`overlay_render.py` does **not** touch the video. It only produces overlay PNG frames and writes them to **stdout**. ffmpeg consumes that stdout as an `image2pipe` input and composites it over the camera feed. Specifically inside `docker-compose.yml`:

- Input 0: camera RTSP, scaled to 1920×1080.
- Input 1: the Python script's stdout (`-f image2pipe -framerate $OVERLAY_FPS -i -`).
- Input 2: `anullsrc` silent audio (Restream/FLV expects an audio track).
- `filter_complex` overlays input 1 on input 0 with `eof_action=repeat` so the last overlay frame persists if the renderer stalls.
- Re-encoded `libx264` high profile, `-g 60 -r 30`, CBR-ish (`-b:v`/`-maxrate`/`-bufsize`), AAC audio, FLV out to `$RESTREAM_URL/$RESTREAM_KEY`.
- `-t 82800` (23h) caps each ffmpeg run; the outer shell loop then restarts it — a deliberate daily recycle.

### overlay_render.py internals

A two-thread design (see the module docstring): a background `updater` thread re-fetches WLL data and re-renders the PNG every `POLL_SECONDS`; the main thread re-emits the current PNG to stdout at `OVERLAY_FPS`. This decouples slow weather polling from the steady frame cadence ffmpeg needs.

- `_last` holds last-good readings so a failed/partial WLL poll never blanks the panel.
- `_state["png"]` is the single shared frame buffer between the two threads.
- `BrokenPipeError` (ffmpeg gone) exits cleanly so the compose loop restarts the whole pipeline.
- `render_overlay()` is explicitly "the canvas" — it always draws on a fixed **1920×1080 grid** and only resizes at the very end if `WIDTH/HEIGHT` differ. Keep new layout math in 1920×1080 coordinates.
- WLL data comes from `http://$WLL_HOST/v1/current_conditions`; values are pulled out of `conditions[]` entries by `data_structure_type` (1 = ISS sensor: temp/hum/wind/dew; 3 = barometer).
- Missing logo files (`CONSTRUCTION_LOGO`, `PROPERTY_LOGO`) fall back to a dashed placeholder box, so the pipeline runs before art is added. `assets/construction_logo.png` exists; `property_logo.png` is expected but optional.

## Configuration (.env)

All knobs are env vars read by compose and the renderer — there are no config files to edit in code. Key groups:

- **Camera/Restream**: `CAM_USER`, `CAM_PASS`, `CAM_IP`, `CAM_PATH`, `RESTREAM_URL`, `RESTREAM_KEY`. Note: a key containing `_event` is a temporary Event key — use the channel's persistent RTMP key for an always-on feed.
- **Encode**: `OVERLAY_FPS`, `X264_PRESET`, `VBITRATE`, `VBUFSIZE`, `TZ` (drives the on-screen clock).
- **Overlay look** (renderer): `BAR_RGB`, `ACCENT_RGB`, `BAR_ALPHA`, `PANEL_W`, `PANEL_SIDE` (`left`/`right`), `PROP_NAME`, `PROP_URL`, `WLL_HOST`, `WLL_PORT`, `POLL_SECONDS`, `FONT_BOLD/REG/COND`.

## Gotchas

- `WIDTH`/`HEIGHT` and the `OVERLAY_FPS` used for the `image2pipe` `-framerate` must stay consistent across `.env`, the renderer, and the camera's `restream` profile (set the camera to 1920×1080 H.264), or compositing misaligns or stutters.
- Always validate with `docker-compose.min.yml` before debugging the full pipeline — it isolates camera/credential/ingest problems from overlay/encode problems.
