# 401 Live Stream

Axis Q6318 traffic camera → ffmpeg → Restream, with an optional branded
weather overlay driven by a Davis WeatherLink Live station.

## Files
- `docker-compose.min.yml` — minimal copy relay (camera → Restream, no overlay,
  no re-encode). Use this first to validate the path.
- `docker-compose.yml` — full pipeline: renders the branded weather overlay and
  re-encodes (libx264) before pushing to Restream.
- `Dockerfile` — image for the full pipeline (ffmpeg + Python + Pillow + fonts).
- `overlay_render.py` — the overlay renderer (vertical right panel; your canvas).
- `.env` — all settings and credentials (keep private; `chmod 600 .env`).
- `assets/` — logos (`construction_logo.png` = 3F badge; add `property_logo.png`).

## Validate first (copy relay)
    docker compose -f docker-compose.min.yml up -d
    docker compose -f docker-compose.min.yml logs -f
    docker compose -f docker-compose.min.yml down

Look for ffmpeg's climbing `frame= … bitrate=` line, then check the Restream
dashboard for an incoming feed.

## Full overlay pipeline (after validation)
    docker compose up -d --build
    docker compose logs -f

## Notes
- Set the camera `restream` profile to 1920x1080 (H.264).
- Set `WLL_HOST` in `.env` to your WeatherLink Live's LAN IP.
- The `RESTREAM_KEY` looks like an Event key (`..._event...`); for an always-on
  feed use the channel's persistent RTMP key instead.
- Overlay look is tuned in `.env`: `BAR_ALPHA`, `PANEL_W`, `PANEL_SIDE`,
  `ACCENT_RGB`, `PROP_URL`.
