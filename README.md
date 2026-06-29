# 401 Live Stream

Axis Q6318 traffic camera → ffmpeg → [Restream](https://restream.io), with an
optional branded weather overlay (live temperature, wind, and humidity) driven
by a Davis WeatherLink Live station on the LAN. Runs 24/7 in Docker and
self-heals on any failure.

```
 ┌──────────────┐   RTSP    ┌─────────────────────────────┐   RTMPS    ┌──────────┐
 │ Axis Q6318   │──────────▶│ ffmpeg (decode → overlay →   │───────────▶│ Restream │
 │ camera       │           │ libx264 re-encode)          │            └──────────┘
 └──────────────┘           │        ▲ image2pipe         │
                            │        │ (PNG frames)        │
 ┌──────────────┐   HTTP    │ ┌──────┴───────────────────┐│
 │ WeatherLink  │◀──────────┼─│ overlay_render.py (Pillow)││
 │ Live         │           │ └──────────────────────────┘│
 └──────────────┘           └─────────────────────────────┘
```

## Requirements

- Docker + Docker Compose (v2)
- Axis camera reachable over RTSP, with a stream profile named `restream`
- A Restream channel (or any RTMP/RTMPS ingest) and its stream key
- (Optional, for weather data) a Davis WeatherLink Live on the same LAN

## Quick start

```bash
git clone https://github.com/Birken-Lofts/ohio-feeder-cam.git
cd ohio-feeder-cam

cp .env.example .env && chmod 600 .env   # then edit .env with your real values

# 1. Validate the camera → Restream path first (copy relay, no overlay)
docker compose -f docker-compose.min.yml up -d
docker compose -f docker-compose.min.yml logs -f      # watch for climbing frame=/bitrate=
docker compose -f docker-compose.min.yml down

# 2. Run the full overlay pipeline
docker compose up -d --build
docker compose logs -f
```

> **Always validate with the copy relay first.** It isolates camera reachability,
> credentials, and Restream ingest from any overlay/encode problems. Look for
> ffmpeg's climbing `frame= … bitrate=` line, then confirm an incoming feed on
> the Restream dashboard.

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Full pipeline: renders the overlay and re-encodes (libx264) before pushing to Restream. Also defines the `vehicle-counter` (under the `gpu` profile) and `postgres` services. |
| `docker-compose.min.yml` | Minimal copy relay (camera → Restream, no overlay, no re-encode). For validation. |
| `Dockerfile` | Image for the full pipeline (ffmpeg + Python + Pillow + DejaVu fonts). |
| `Dockerfile.counter` | Prod/NVIDIA image for the vehicle counter (Ultralytics CUDA base + supervision). |
| `overlay_render.py` | The overlay renderer — your canvas. Renders a vertical panel and streams PNG frames to ffmpeg. |
| `vehicle_counter.py` | YOLO vehicle counter: detects + tracks + counts line crossings → SQLite + `counts.json`. |
| `calibrate_line.py` | Helper to pick the counting line on a camera frame and print `COUNT_LINE`. |
| `requirements-counter.txt` | Python deps for the counter (ultralytics, supervision, opencv) — dev/native install. |
| `preview.sh` | Render one overlay frame locally on macOS (no Docker) and open it. |
| `.env.example` | Template for all settings/credentials. Copy to `.env` (gitignored). |
| `assets/` | Logos (`construction_logo.png`; add your own `property_logo.png`) and optional brand `.ttf` fonts. |
| `data/` | `counts.json` snapshot + calibration frames (+ a SQLite fallback DB if no Postgres). Gitignored. Postgres data lives in the `pgdata` Docker volume. |

## Configuration (`.env`)

All settings are environment variables — there is nothing to edit in code.
Copy `.env.example` to `.env` and fill in the values below.

**Camera (Axis RTSP)**

| Var | Description |
|-----|-------------|
| `CAM_USER` / `CAM_PASS` | Camera login. |
| `CAM_IP` | Camera LAN IP. |
| `CAM_PATH` | RTSP path. Default targets the `restream` profile: `/axis-media/media.amp?streamprofile=restream` |

**Restream (ingest)**

| Var | Description |
|-----|-------------|
| `RESTREAM_URL` | e.g. `rtmps://live.restream.io:1937/live` |
| `RESTREAM_KEY` | Channel stream key. Use the channel's **persistent** RTMP key for an always-on feed — a key containing `_event` is a temporary Event key. |

**Weather data (overlay)**

The renderer pulls live conditions from one of two sources. WeatherLink takes
priority; if `WLL_HOST` is blank it falls back to Open-Meteo, so the switch back
to the local station is automatic once it's installed.

| Var | Description |
|-----|-------------|
| `WLL_HOST` | Davis WeatherLink Live's LAN IP (preferred source). Leave blank until installed. |
| `WLL_PORT` | Default `80`. |
| `WX_LAT` / `WX_LON` | Camera location in decimal degrees. Used for the free [Open-Meteo](https://open-meteo.com) fallback (no API key) when `WLL_HOST` is blank. |
| `POLL_SECONDS` | How often to re-fetch weather data (default `30`). |

If neither source is configured (or both are unreachable), the panel still
renders and shows `--` for the affected values.

**Vehicle counting**

| Var | Description |
|-----|-------------|
| `COUNT_LINE_A` | Counting line for the **near** carriageway as `x1,y1,x2,y2` in 1920×1080 space → direction A. **At least one line required** — produce it with `calibrate_line.py`. |
| `COUNT_LINE_B` | Counting line for the **far** carriageway → direction B. Omit on an undivided road. |
| `DIR_A_LABEL` / `DIR_B_LABEL` | Labels for the two lines/directions (e.g. `WB` / `EB`). |
| `SITE` | Camera/site id stored on every crossing row (for multi-camera analysis later). |
| `YOLO_MODEL` | Ultralytics model (`yolo11n.pt` for dev/MPS/CPU; `yolo11m.pt`+ on the GPU box). Auto-downloads. |
| `DETECT_CONF` | Detection confidence threshold (default `0.3`). |
| `DETECT_STRIDE` | Process 1 of every N frames (default `6` ≈ 5 detections/s at 30fps). |
| `ENABLE_COLOR` | Classify dominant vehicle color (default `true`). |
| `ENABLE_MMR` | **Experimental** make/model classifier (default `false`, off). Unreliable on a traffic cam — see caveat below. Needs `pip install transformers pillow` if turned on. |
| `MMR_MODEL` / `MMR_MIN_PX` | Only used when `ENABLE_MMR=true`. HF model id and the min box width to bother classifying. |

**Postgres (durable crossing history)**

| Var | Description |
|-----|-------------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Credentials for the `postgres` compose service. |
| `DATABASE_URL` | Counter's connection string. Native dev uses `…@localhost:5432…`; the **container overrides** it to `…@postgres:5432…` via compose. Leave blank to fall back to local SQLite (`data/counts.db`). |

On a **divided road**, draw one line per carriageway — each line *is* a direction,
which is more reliable than a single line when the carriageways carry opposite
traffic or the road curves. If no line is set the counter exits with a hint; the
overlay simply shows `--` for traffic until a snapshot exists, so the stream is
unaffected either way.

**Encode**

| Var | Description |
|-----|-------------|
| `X264_PRESET` | libx264 preset (`veryfast` default; lower = less CPU). |
| `VBITRATE` / `VBUFSIZE` | Output bitrate target/cap and buffer (`8000k` / `16000k` for 1080p30). |
| `OVERLAY_FPS` | How often ffmpeg pulls a fresh overlay frame (default `4`). |
| `WIDTH` / `HEIGHT` | Output resolution. **Must match the camera profile** (`1920` × `1080`). |
| `TZ` | Timezone for the on-screen clock (e.g. `America/Chicago`). |

**Overlay branding / look**

| Var | Description |
|-----|-------------|
| `PROP_NAME` / `PROP_URL` | Property name and marketing URL shown at the bottom. |
| `CONSTRUCTION_LOGO` / `PROPERTY_LOGO` | In-container paths to logo PNGs (under `/app/assets`). |
| `BAR_RGB` / `ACCENT_RGB` | Panel color and accent color (`R,G,B`). |
| `BAR_ALPHA` | Panel opacity `0–255` (lower = more see-through). |
| `PANEL_W` / `PANEL_SIDE` | Panel width in px and side (`right` or `left`). |

## Camera profile settings

Configure the Axis `restream` stream profile (the one `CAM_PATH` points at):

- **Codec:** H.264
- **Resolution:** 1920×1080 (16:9) — must match `WIDTH`/`HEIGHT`
- **Frame rate:** 30 fps
- **GOP length:** 60 (= a keyframe every 2s at 30fps)
- **Zipstream:** Off

The pipeline additionally forces a keyframe every 2 seconds
(`-force_key_frames "expr:gte(t,n_forced*2)"`), which is what Restream and most
downstream platforms expect — so the "keyframe interval" warning stays clear
even if the frame rate jitters.

## How it works

`overlay_render.py` never touches the video. It renders the branded panel to a
transparent PNG and writes frames to **stdout**; ffmpeg reads that as an
`image2pipe` input and composites it over the camera feed, then re-encodes to
H.264 and pushes FLV to Restream. The renderer uses two threads: a background
thread re-fetches WeatherLink data and re-renders every `POLL_SECONDS`, while
the main thread re-emits the current frame at `OVERLAY_FPS`. Last-good readings
are cached, so a failed poll never blanks the panel.

The full pipeline runs inside a shell loop that restarts ffmpeg after any exit
(10s backoff), and each ffmpeg run is capped at 23h (`-t 82800`) for a
deliberate daily recycle. A healthcheck `ffprobe`s the camera every 60s.

## Vehicle counting

A separate process, `vehicle_counter.py`, pulls the **same** RTSP feed and uses
[Ultralytics YOLO](https://github.com/ultralytics/ultralytics) (detection +
built-in ByteTrack tracking) with [Roboflow Supervision](https://github.com/roboflow/supervision)'s
`LineZone` to count vehicles crossing a counting line. Each crossing is recorded
with **direction** (one line per carriageway), **type** (car / truck / bus /
motorcycle), **color**, and a full **timestamp**. It is fully decoupled from the
encode loop, so a detector crash never touches the live feed.

```
                ┌──────────────────────────┐  writes  Postgres `crossings`  (1 row/vehicle, full history)
 RTSP ─────────▶│ vehicle_counter.py        │────────▶ data/counts.json       (today snapshot)
 (same camera)  │  YOLO track + LineZone +  │                 │
                │  color + make/model       │                 │ reads
                └──────────────────────────┘                 │
 RTSP ─────────▶ camera-restream ── overlay_render.py ────────┘ ──▶ "TRAFFIC TODAY" on stream
```

Every crossing is written as a row to **Postgres** (the `postgres` compose
service) for durable history/analysis, and today's totals are mirrored to
`data/counts.json`, which the overlay reads (last-good cached, like weather).
Today's totals are rehydrated from the DB on startup, so restarting the counter
mid-day doesn't lose the count. The day boundary follows `TZ`. (With no
`DATABASE_URL` set, it falls back to a local SQLite file so it still runs without
Docker.)

**`crossings` schema** — one row per vehicle, built for analysis:

```sql
-- crossings(id, ts timestamptz, local_date, local_hour, direction, direction_label,
--           cls, color, make_model, mmr_conf, confidence, tracker_id,
--           bbox_x, bbox_y, bbox_w, bbox_h, site)
-- Hourly volume by direction today:
SELECT local_hour, direction_label, count(*)
FROM crossings WHERE local_date = to_char(now(),'YYYY-MM-DD')
GROUP BY 1,2 ORDER BY 1;

-- Color mix this week:
SELECT color, count(*) FROM crossings
WHERE ts > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC;
```

Connect with `psql`: `docker exec -it feedercam-postgres psql -U feedercam -d feedercam`.

> **Vehicle type is COCO-coarse.** YOLO's `truck` class means *large commercial
> trucks*; **pickups (F-150/F-450) are labeled `car`** by COCO-trained models.
> That's expected behavior, not a bug. Distinguishing pickups would need a
> body-type classifier or a detector trained on a richer taxonomy.
>
> **Make/model is off by default** (`ENABLE_MMR=false`). The only open option
> (Stanford-Cars ViT) is trained on clean ~2012 catalog photos and is unreliable
> on a moving traffic cam — the `make_model`/`mmr_conf` columns exist but stay
> `NULL`. Reliable make/model needs a commercial recognition service. Color is
> reliable for near lanes, weaker for distant ones.

### Two deployments

The Mac dev box and the prod GPU box run the **same script**; the device
(`cuda` → `mps` → `cpu`) is auto-detected. Both write to Postgres and share the
repo-local `./data/` directory (how counts reach the overlay).

**Dev — Apple Silicon (native, MPS).** Docker can't reach the Mac GPU, so run the
counter natively, but start Postgres in Docker first:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-counter.txt
docker compose up -d postgres             # DB the native counter writes to (localhost:5432)
.venv/bin/python calibrate_line.py        # draw a line per carriageway; writes COUNT_LINE_A/B to .env
.venv/bin/python vehicle_counter.py        # logs device=mps, db=pg; writes Postgres + counts.json
```

**Prod — NVIDIA (Docker, CUDA).** Everything runs in Docker, including the
GPU-accelerated counter — see **[Production deploy](#production-deploy-server-with-nvidia-gpu)** below.

## Production deploy (server with NVIDIA GPU)

On the prod box, all three services run in Docker and the counter uses the GPU
(the counter auto-detects `cuda`; the `ultralytics/ultralytics` base image is
CUDA-enabled). All services use `restart: unless-stopped`, so they come back on
reboot (ensure Docker starts on boot: `sudo systemctl enable docker`).

**One-time host setup (Docker can't do this for you):**

1. Install the NVIDIA driver for the GPU.
2. Install the **[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)** and restart Docker. Verify GPU-in-Docker works *before* deploying:

   ```bash
   docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   # must list the GPU
   ```

   Without the toolkit, startup fails with *"could not select device driver nvidia with capabilities: [[gpu]]"*.

**Deploy:**

```bash
git clone <repo> && cd <repo>
cp .env.example .env && chmod 600 .env     # then fill in real values
# (copy your calibrated COUNT_LINE_A/B over too — no GUI/recalibration on the server)

docker compose --profile gpu up -d --build   # starts postgres + camera-restream + vehicle-counter
docker compose logs -f vehicle-counter        # expect: device=cuda model=... db=pg mmr=off
```

> Tip: set `COMPOSE_PROFILES=gpu` in the server's `.env` (see `.env.example`) and a
> plain `docker compose up -d` then starts the whole stack — convenient for ops.

**Notes:**

- **Network:** the server must reach the camera over RTSP (`CAM_IP`) — verify from the server.
- **Restream key:** use the channel's **persistent** RTMP key, not an `_event` key, for a 24/7 feed.
- **Model weights** persist in the `weights` Docker volume (downloaded once). On the GPU you can raise accuracy with `YOLO_MODEL=yolo11m.pt` and `DETECT_STRIDE=3`.
- **Postgres data** persists in the `pgdata` volume across restarts/recreates.
- The `camera-restream` container picks up `counts.json` automatically via the shared `./data` mount — no restart needed when counts update.

## Operations

```bash
docker compose logs -f                 # follow logs
docker compose restart camera-restream # apply overlay_render.py / assets edits
docker compose up -d                    # apply .env or docker-compose.yml changes (recreates)
docker compose up -d --build            # rebuild after editing the Dockerfile
docker compose down                     # stop the stream
```

`overlay_render.py` and `assets/` are bind-mounted read-only, so edits to the
overlay take effect on `docker compose restart`. **`.env` changes need
`docker compose up -d`** — `restart` reuses the container's existing
environment and won't pick them up.

## Preview the overlay locally (macOS, no Docker)

Iterate on the panel design without touching the live stream:

```bash
./preview.sh        # renders one frame to preview.png and opens it
```

First run creates a `.venv/` and installs Pillow. The script reads look +
location settings from `.env`, overrides the fonts/logo paths for macOS, pulls
live weather (WeatherLink or Open-Meteo, same as the stream), and writes
`preview.png`. Edit `overlay_render.py` and re-run to see changes.

## Logos & fonts

Drop files in `assets/` and point the env vars at them:

- `construction_logo.png` → `CONSTRUCTION_LOGO=/app/assets/construction_logo.png`
- `property_logo.png` → `PROPERTY_LOGO=/app/assets/property_logo.png`
- Brand fonts: drop a `.ttf` and set e.g. `FONT_BOLD=/app/assets/YourFont-Bold.ttf`

Transparent-background PNGs work best (export SVGs to PNG first). The renderer
scales each logo to fit its slot. If `construction_logo.png` is missing it falls
back to a dashed placeholder box; if `property_logo.png` is missing it renders
`PROP_BRAND` (default `PROP_NAME`) as a serif wordmark — one word per line —
using `FONT_SERIF`. So the stream looks finished even before you have art.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Weather shows `--` | `WLL_HOST` is unset/wrong, or the station is unreachable. Set it to the WeatherLink Live's LAN IP and `docker compose restart`. |
| No feed on Restream | Wrong/expired `RESTREAM_KEY` (use the persistent RTMP key, not an `_event` key), or the camera is unreachable — validate with `docker-compose.min.yml`. |
| Restream "keyframe interval" warning | Confirm the camera GOP is 60 @ 30fps; the pipeline already forces 2s keyframes. |
| Container keeps restarting | Check `docker compose logs` for `pipeline exited` lines — usually a bad RTSP URL/credentials or the camera being offline. |
| Overlay misaligned/stuttering | `WIDTH`/`HEIGHT` must match the camera profile resolution. |

## Security

Credentials live only in `.env`, which is gitignored — never commit it. The
repo ships `.env.example` with placeholders. Keep `.env` at `chmod 600`.
