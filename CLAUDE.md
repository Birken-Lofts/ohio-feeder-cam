# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A 24/7 live-stream pipeline: an Axis Q6318 traffic camera (RTSP) → ffmpeg → Restream (RTMP/FLV), with an optional branded weather overlay driven by a Davis WeatherLink Live (WLL) station on the LAN. There is no application server or test suite — the repo is a Dockerized ffmpeg pipeline plus a single Python overlay renderer.

A second, **independent** Python process (`vehicle_counter.py`) pulls the same RTSP feed and uses Ultralytics YOLO + Roboflow Supervision to count vehicles crossing a line (by direction and type), persists them to a local SQLite DB, and writes a `counts.json` snapshot the overlay renders as "TRAFFIC TODAY". It is decoupled from the encode loop on purpose — a detector crash never affects the live stream.

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

### Vehicle counter (separate from the streaming pipeline)

```bash
# Dev (Apple Silicon, native — Docker can't reach the Mac GPU; auto-uses MPS):
python3 -m venv .venv && .venv/bin/pip install -r requirements-counter.txt
docker compose up -d postgres          # native counter writes to it on localhost:5432
.venv/bin/python calibrate_line.py     # draw a line per carriageway -> writes COUNT_LINE_A/B to .env
.venv/bin/python vehicle_counter.py    # writes Postgres `crossings` + data/counts.json

# Prod (NVIDIA, Docker, CUDA) — gated behind the "gpu" compose profile (postgres starts as a dep):
docker compose --profile gpu up -d --build vehicle-counter

# Inspect counts (Postgres):
docker exec -it feedercam-postgres psql -U feedercam -d feedercam \
  -c "SELECT local_date, direction_label, cls, color, COUNT(*) FROM crossings GROUP BY 1,2,3,4"
```

## Architecture

Two independent compose files share the same `.env`:

- **`docker-compose.min.yml`** — `relay-test` service. Pulls a prebuilt ffmpeg image and does a stream `copy` (no decode/encode, no overlay). Use this to confirm credentials, RTSP reachability, and Restream ingest before touching the full pipeline.
- **`docker-compose.yml`** — `camera-restream` service. Built from `Dockerfile` (python:3.12-slim + ffmpeg/libx264 + DejaVu fonts + Pillow). The `command` is a shell loop that runs `python3 overlay_render.py | ffmpeg ...` and restarts after 10s on any exit, so the stream self-heals. Also defines **`vehicle-counter`** (built from `Dockerfile.counter`, Ultralytics CUDA base + supervision), gated behind the `gpu` profile so a plain `docker compose up` on a non-GPU host never starts it.

### Vehicle-count data flow (the second thing to understand)

`vehicle_counter.py` runs as its **own** process (native on the dev Mac, a `--profile gpu` container in prod). It does NOT write video. It drives the RTSP capture with **OpenCV directly** (`cv2.VideoCapture` + cheap `grab()` to skip `DETECT_STRIDE-1` frames, then `read()` + `model.track(frame, persist=True)`) — NOT Ultralytics' built-in stream loader, which is unreliable on IP cameras ("Waiting for stream..."). Tracked detections feed one `supervision.LineZone` **per carriageway** (`COUNT_LINE_A`/`COUNT_LINE_B`); the line's identity is the direction (A/B), not the in/out sense. On each crossing it crops the bbox, classifies **color** (HSV heuristic) and optionally **make/model** (`ENABLE_MMR`, experimental HF ViT), and writes a full `crossings` row via the `Store` class — **Postgres** when `DATABASE_URL` is set, else a local **SQLite** fallback. A throttled `data/counts.json` snapshot (incl. `by_color`) is what the overlay reads. Device is auto-detected (`cuda` → `mps` → `cpu`). Today's totals are rehydrated from the DB on startup; the day boundary follows `TZ`. `vehicle_counter.py` also has a tiny built-in `.env` loader (`load_dotenv()`) so it works natively; real env vars always win, so Docker's `env_file` is unaffected.

`overlay_render.py` reads `counts.json` in `read_counts()` (last-good cached like the weather `_last`) and draws the "TRAFFIC TODAY" block in `render_overlay()`. The streaming container mounts `./data` **read-only**; the counter mounts it read-write. `./data/` (relative defaults resolve to `/app/data` inside the containers via WORKDIR) is the only coupling between the two halves — keep it that way so the stream never depends on the detector being up.

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
- `render_overlay()` is explicitly "the canvas" — it always draws on a fixed **1920×1080 grid** and only resizes at the very end if `WIDTH/HEIGHT` differ. Keep new layout math in 1920×1080 coordinates. The panel stacks: clock → construction logo → 3 weather cells → **TRAFFIC TODAY** block → property wordmark/url.
- `read_counts()` loads the vehicle `counts.json` snapshot with a `_lastcounts` last-good cache (mirrors `_last`); called inside `render_overlay()` so the traffic block updates each re-render and shows `--` if no snapshot exists yet.
- WLL data comes from `http://$WLL_HOST/v1/current_conditions`; values are pulled out of `conditions[]` entries by `data_structure_type` (1 = ISS sensor: temp/hum/wind/dew; 3 = barometer).
- Missing logo files (`CONSTRUCTION_LOGO`, `PROPERTY_LOGO`) fall back to a dashed placeholder box, so the pipeline runs before art is added. `assets/construction_logo.png` exists; `property_logo.png` is expected but optional.

## Configuration (.env)

All knobs are env vars read by compose and the renderer — there are no config files to edit in code. Key groups:

- **Camera/Restream**: `CAM_USER`, `CAM_PASS`, `CAM_IP`, `CAM_PATH`, `RESTREAM_URL`, `RESTREAM_KEY`. Note: a key containing `_event` is a temporary Event key — use the channel's persistent RTMP key for an always-on feed.
- **Encode**: `OVERLAY_FPS`, `X264_PRESET`, `VBITRATE`, `VBUFSIZE`, `TZ` (drives the on-screen clock).
- **Overlay look** (renderer): `BAR_RGB`, `ACCENT_RGB`, `BAR_ALPHA`, `PANEL_W`, `PANEL_SIDE` (`left`/`right`), `PROP_NAME`, `PROP_URL`, `WLL_HOST`, `WLL_PORT`, `POLL_SECONDS`, `FONT_BOLD/REG/COND`.
- **Vehicle counter**: `COUNT_LINE_A`/`COUNT_LINE_B` (`x1,y1,x2,y2` in 1920×1080, one per carriageway; at least A required — generate with `calibrate_line.py`. `COUNT_LINE` singular is still honored as A), `DIR_A_LABEL`/`DIR_B_LABEL`, `SITE`, `YOLO_MODEL`, `DETECT_CONF`, `DETECT_STRIDE`. `COUNTS_FILE`/`COUNTS_DB` default to relative `data/...` paths (don't set them in `.env` — that file is shared with the native dev counter, where absolute `/app/...` paths would break).
- **Classification**: `ENABLE_COLOR`, `ENABLE_MMR` (experimental make/model), `MMR_MODEL` (HF image-classification id), `MMR_MIN_PX`.
- **Postgres**: `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` (the `postgres` service) and `DATABASE_URL`. `.env` sets `DATABASE_URL` to `@localhost:5432` for the native dev counter; the `vehicle-counter` container overrides it to `@postgres:5432` via compose `environment:` (which beats `env_file:`). Blank `DATABASE_URL` → SQLite fallback.
- **Prod/Docker only**: `WEIGHTS_DIR` (set to `/app/weights` by compose; a bare `YOLO_MODEL` name downloads/persists there via the `weights` volume). `COMPOSE_PROFILES=gpu` in the server's `.env` makes a plain `docker compose up -d` start the whole stack incl. `vehicle-counter`. Prod startup: `docker compose --profile gpu up -d --build` (requires the host NVIDIA driver + nvidia-container-toolkit). All services are `restart: unless-stopped`.

## Gotchas

- `WIDTH`/`HEIGHT` and the `OVERLAY_FPS` used for the `image2pipe` `-framerate` must stay consistent across `.env`, the renderer, and the camera's `restream` profile (set the camera to 1920×1080 H.264), or compositing misaligns or stutters.
- Always validate with `docker-compose.min.yml` before debugging the full pipeline — it isolates camera/credential/ingest problems from overlay/encode problems.
- `COUNT_LINE_A`/`COUNT_LINE_B` are in **1920×1080** coordinates (same grid as the overlay canvas), regardless of `WIDTH`/`HEIGHT` — `calibrate_line.py` resizes the grabbed frame to 1920×1080 before you click. On a divided/curved road, one line per carriageway is more reliable than one straight line spanning both.
- The `vehicle-counter` compose service is behind `profiles: ["gpu"]` — it only starts with `docker compose --profile gpu up`. On the dev Mac run `vehicle_counter.py` natively (the container has no access to the Mac GPU, and would fall back to slow CPU).
- The counter never blocks the stream: if it's down or no count line is set, the overlay just shows `--` for traffic. Don't add a hard dependency from `camera-restream` on `vehicle-counter`.
- `DATABASE_URL` differs by deployment ON PURPOSE: `.env` uses `localhost` (native dev counter → dockerized Postgres via the published 5432 port); the container overrides to host `postgres`. Don't "fix" the `.env` to use `postgres` — that host only resolves inside the compose network and would break native dev.
- Vehicle `cls` comes straight from YOLO/COCO: `truck` = large commercial trucks only; **pickups are labeled `car`**. Expected COCO behavior, not a bug. Fixing it would need a body-type classifier or a detector with a richer taxonomy (descoped — see chat history).
- Make/model (`ENABLE_MMR`) is **off by default** — it's unreliable on a traffic cam (Stanford-Cars ViT, 2012-era clean photos) and `transformers`/`pillow` aren't installed by default. The code stays (loads lazily, degrades gracefully if absent/failed), and the `make_model`/`mmr_conf` columns stay `NULL`. Don't re-enable expecting accuracy.
- The capture loop uses OpenCV, not Ultralytics' stream loader — if you ever switch it back to `model.track(source=RTSP_URL, stream=True)` you'll likely reintroduce the "Waiting for stream..." hangs on this camera.
