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
| `docker-compose.yml` | Full pipeline: renders the weather overlay and re-encodes (libx264) before pushing to Restream. |
| `docker-compose.min.yml` | Minimal copy relay (camera → Restream, no overlay, no re-encode). For validation. |
| `Dockerfile` | Image for the full pipeline (ffmpeg + Python + Pillow + DejaVu fonts). |
| `overlay_render.py` | The overlay renderer — your canvas. Renders a vertical panel and streams PNG frames to ffmpeg. |
| `.env.example` | Template for all settings/credentials. Copy to `.env` (gitignored). |
| `assets/` | Logos (`construction_logo.png`; add your own `property_logo.png`) and optional brand `.ttf` fonts. |

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

**WeatherLink Live (overlay data)**

| Var | Description |
|-----|-------------|
| `WLL_HOST` | WeatherLink Live's LAN IP. If unset/unreachable, the overlay still runs and shows `--`. |
| `WLL_PORT` | Default `80`. |
| `POLL_SECONDS` | How often to re-fetch weather data (default `30`). |

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

## Operations

```bash
docker compose logs -f                 # follow logs
docker compose restart camera-restream # apply .env changes (no rebuild needed)
docker compose up -d                    # recreate after editing docker-compose.yml
docker compose up -d --build            # rebuild after editing the Dockerfile
docker compose down                     # stop the stream
```

`overlay_render.py` and `assets/` are bind-mounted read-only, so edits to the
overlay take effect on `docker compose restart` — no image rebuild required.

## Logos & fonts

Drop files in `assets/` and point the env vars at them:

- `construction_logo.png` → `CONSTRUCTION_LOGO=/app/assets/construction_logo.png`
- `property_logo.png` → `PROPERTY_LOGO=/app/assets/property_logo.png`
- Brand fonts: drop a `.ttf` and set e.g. `FONT_BOLD=/app/assets/YourFont-Bold.ttf`

Transparent-background PNGs work best (export SVGs to PNG first). The renderer
scales each logo to fit its slot; missing files fall back to a dashed
placeholder box so the stream still runs.

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
