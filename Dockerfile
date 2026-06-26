FROM python:3.12-slim

# ffmpeg (with libx264 + rtmps), fonts, and tzdata for the local clock
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pillow

WORKDIR /app
# overlay_render.py and assets/ are mounted at runtime by docker-compose
