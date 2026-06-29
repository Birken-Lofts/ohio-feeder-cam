#!/usr/bin/env python3
"""
Vehicle counter for the camera -> Restream pipeline.

Pulls the SAME Axis RTSP feed the streamer uses, runs Ultralytics YOLO with
built-in ByteTrack tracking, and counts vehicles crossing a virtual line with
Roboflow Supervision's LineZone. Each crossing is classified by *direction* (one
count line per carriageway), *vehicle type* (car/truck/bus/motorcycle), dominant
*color*, and -- experimentally -- *make/model*. Every crossing is written as a
row to Postgres for durable history/analysis, and today's totals are mirrored to
a tiny counts.json snapshot that overlay_render.py reads to show on the stream.

This runs as a SEPARATE process from the ffmpeg encode loop -- YOLO is far too
heavy to run inline, and keeping it separate means a detector crash never
touches the live feed. See CLAUDE.md for the data-flow diagram.

Dev (Apple Silicon): start Postgres (`docker compose up -d postgres`) then run
this natively in the .venv (uses the MPS GPU; Docker can't reach the Mac GPU).
Prod (NVIDIA): run via the `vehicle-counter` compose service (uses CUDA). Device
and DB are auto-detected either way.

    pip install -r requirements-counter.txt
    python3 vehicle_counter.py
"""
import os
import re
import sys
import json
import time
import sqlite3
import datetime

# Force RTSP over TCP for the OpenCV/FFmpeg capture backend (matches the
# streamer's -rtsp_transport tcp). Must be set before cv2 loads.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

# ------------------------------------------------------------------ config ---
def load_dotenv():
    """Load .env next to this script into os.environ when running natively
    (e.g. on the dev Mac). Existing env vars win, so Docker's `env_file: .env`
    and explicit exports are never overridden. No external dependency."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in os.environ:
            continue                       # don't override real env (Docker/exports)
        os.environ[key] = re.sub(r"\s+#.*$", "", val).strip().strip('"').strip("'")

load_dotenv()

def env(k, d=None): return os.environ.get(k, d)
def env_bool(k, d):
    v = env(k)
    return d if v is None else v.strip().lower() in ("1", "true", "yes", "on")

# Local clock / day boundary -- match the on-screen overlay clock.
TZ = env("TZ")
if TZ:
    os.environ["TZ"] = TZ
    try:
        time.tzset()
    except AttributeError:
        pass  # Windows; dev/prod are macOS/Linux

CAM_USER = env("CAM_USER", "")
CAM_PASS = env("CAM_PASS", "")
CAM_IP   = env("CAM_IP", "")
CAM_PATH = env("CAM_PATH", "")
RTSP_URL = env("RTSP_URL") or f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}{CAM_PATH}"

WIDTH  = int(env("WIDTH", "1920"))      # count line coords are in this space
HEIGHT = int(env("HEIGHT", "1080"))
SITE   = env("SITE", "401")             # camera/site id, stored on every row

YOLO_MODEL  = env("YOLO_MODEL", "yolo11n.pt")
# When set (prod mounts a volume here), a bare model name downloads into this dir
# so weights persist across container recreates instead of re-downloading.
WEIGHTS_DIR = env("WEIGHTS_DIR", "")
DETECT_CONF = float(env("DETECT_CONF", "0.3"))
# Process 1 of every DETECT_STRIDE frames. The camera runs ~30fps, so the
# default 6 yields ~5 detections/sec -- plenty for counting, far cheaper to run.
DETECT_STRIDE = int(env("DETECT_STRIDE", "6"))

# One count line per carriageway on a divided road -> each line IS a direction.
# "x1,y1,x2,y2" in 1920x1080 space, from calibrate_line.py. COUNT_LINE (singular)
# is still honored as line A for backward compatibility.
COUNT_LINE_A = env("COUNT_LINE_A", env("COUNT_LINE", ""))
COUNT_LINE_B = env("COUNT_LINE_B", "")
DIR_LABELS = {"A": env("DIR_A_LABEL", "A"), "B": env("DIR_B_LABEL", "B")}

# Postgres connection. When DATABASE_URL is unset we fall back to a local SQLite
# file so the counter still runs without Docker.
DATABASE_URL = env("DATABASE_URL", "")
COUNTS_FILE  = env("COUNTS_FILE", "data/counts.json")
COUNTS_DB    = env("COUNTS_DB",   "data/counts.db")   # SQLite fallback path only
SNAPSHOT_SECS = float(env("SNAPSHOT_SECS", "2"))

# Classification toggles.
ENABLE_COLOR = env_bool("ENABLE_COLOR", True)
ENABLE_MMR   = env_bool("ENABLE_MMR", True)           # experimental make/model
MMR_MODEL    = env("MMR_MODEL", "therealcyberlord/stanford-car-vit-patch16")
MMR_MIN_PX   = int(env("MMR_MIN_PX", "64"))           # skip MMR on tiny/distant boxes

# COCO class ids we care about -> friendly names used in the DB / overlay.
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

def log(msg):
    sys.stderr.write(f"[counter] {msg}\n"); sys.stderr.flush()

def now_local():
    return datetime.datetime.now().astimezone()   # tz-aware, for timestamptz

# ----------------------------------------------------------------- store ---
CROSS_COLS = ["ts", "local_date", "local_hour", "direction", "direction_label",
              "cls", "color", "make_model", "mmr_conf", "confidence",
              "tracker_id", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "site"]

class Store:
    """Durable per-crossing store. Postgres when DATABASE_URL is set, else SQLite.
    Same `crossings` schema and API for both."""
    def __init__(self):
        if DATABASE_URL:
            import psycopg
            self.kind = "pg"
            self.con = psycopg.connect(DATABASE_URL, autocommit=True)
            self.ph = "%s"
            ts_type, id_type = "TIMESTAMPTZ", "BIGSERIAL PRIMARY KEY"
        else:
            self.kind = "sqlite"
            os.makedirs(os.path.dirname(COUNTS_DB) or ".", exist_ok=True)
            self.con = sqlite3.connect(COUNTS_DB, timeout=10)
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
            self.ph = "?"
            ts_type, id_type = "TEXT", "INTEGER PRIMARY KEY"
        self.con.cursor().execute(f"""
            CREATE TABLE IF NOT EXISTS crossings (
                id {id_type},
                ts {ts_type} NOT NULL,
                local_date TEXT NOT NULL,
                local_hour SMALLINT NOT NULL,
                direction TEXT NOT NULL,
                direction_label TEXT,
                cls TEXT NOT NULL,
                color TEXT,
                make_model TEXT,
                mmr_conf REAL,
                confidence REAL,
                tracker_id INTEGER,
                bbox_x INTEGER, bbox_y INTEGER, bbox_w INTEGER, bbox_h INTEGER,
                site TEXT
            )""")
        for ix in ("local_date", "ts"):
            self.con.cursor().execute(
                f"CREATE INDEX IF NOT EXISTS idx_crossings_{ix} ON crossings({ix})")
        self._commit()

    def _commit(self):
        if self.kind == "sqlite":
            self.con.commit()

    def _coerce(self, v):
        if self.kind == "sqlite" and isinstance(v, (datetime.date, datetime.datetime)):
            return v.isoformat()
        return v

    def insert(self, row):
        vals = [self._coerce(row.get(c)) for c in CROSS_COLS]
        ph = ",".join([self.ph] * len(CROSS_COLS))
        self.con.cursor().execute(
            f"INSERT INTO crossings ({','.join(CROSS_COLS)}) VALUES ({ph})", vals)
        self._commit()

    def load_today(self, date):
        """Rehydrate today's aggregates from the DB (survives a mid-day restart)."""
        agg = {"date": date, "total": 0,
               "by_dir": {"A": 0, "B": 0},
               "by_cls": {c: 0 for c in VEHICLE_CLASSES.values()},
               "by_color": {}}
        cur = self.con.cursor()
        cur.execute(f"SELECT direction, cls, color, COUNT(*) FROM crossings "
                    f"WHERE local_date={self.ph} GROUP BY direction, cls, color", (date,))
        for direction, cls, color, n in cur.fetchall():
            agg["total"] += n
            agg["by_dir"][direction] = agg["by_dir"].get(direction, 0) + n
            agg["by_cls"][cls] = agg["by_cls"].get(cls, 0) + n
            if color:
                agg["by_color"][color] = agg["by_color"].get(color, 0) + n
        return agg

# ----------------------------------------------------------- snapshot file ---
def write_snapshot(agg):
    agg = dict(agg, updated=now_local().isoformat(timespec="seconds"))
    os.makedirs(os.path.dirname(COUNTS_FILE) or ".", exist_ok=True)
    tmp = COUNTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(agg, f)
    os.replace(tmp, COUNTS_FILE)   # atomic; the overlay never sees a half-written file

# -------------------------------------------------------------- classifiers ---
def classify_color(crop):
    """Dominant color name from the vehicle crop, via robust HSV medians on the
    box center (edges are windows/road). Returns None on an empty crop."""
    import cv2
    import numpy as np
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    roi = crop[int(h * 0.25):int(h * 0.75), int(w * 0.25):int(w * 0.75)]
    if roi.size == 0:
        roi = crop
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    H, S, V = (hsv[..., i].reshape(-1).astype(float) for i in range(3))
    s_med, v_med = float(np.median(S)), float(np.median(V))
    if v_med < 50:            return "black"
    if s_med < 40:            return "white" if v_med > 180 else "gray"  # gray ~ silver
    sat = S > 60
    if sat.sum() < 10:        return "gray"
    hue = float(np.median(H[sat]))            # OpenCV hue is 0-179
    for hi, name in ((10, "red"), (20, "orange"), (33, "yellow"),
                     (85, "green"), (130, "blue"), (160, "purple")):
        if hue < hi:
            return name
    return "red"                              # wraps back around to red

_mmr = {"loaded": False, "pipe": None}
def get_mmr(device):
    """Lazy-load the experimental make/model classifier once. Returns None (and
    logs why) if disabled or it fails to load, so counting continues regardless."""
    if _mmr["loaded"]:
        return _mmr["pipe"]
    _mmr["loaded"] = True
    if not ENABLE_MMR:
        return None
    try:
        from transformers import pipeline
        dev = 0 if device.startswith("cuda") else ("mps" if device == "mps" else -1)
        _mmr["pipe"] = pipeline("image-classification", model=MMR_MODEL, device=dev)
        log(f"make/model classifier loaded: {MMR_MODEL} (experimental)")
    except Exception as e:
        log(f"make/model disabled (could not load {MMR_MODEL}): {e}")
    return _mmr["pipe"]

def classify_mmr(pipe, crop):
    if pipe is None or crop is None or crop.size == 0:
        return None, None
    try:
        import cv2
        from PIL import Image
        out = pipe(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), top_k=1)
        if out:
            return str(out[0]["label"]), float(out[0]["score"])
    except Exception as e:
        log(f"make/model inference error: {e}")
    return None, None

# ----------------------------------------------------------------- helpers ---
def pick_device():
    import torch
    if torch.cuda.is_available():
        return "cuda:0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"

def parse_line(spec):
    try:
        x1, y1, x2, y2 = (int(float(v)) for v in spec.split(","))
        return (x1, y1), (x2, y2)
    except Exception:
        return None

def model_path():
    """Resolve a bare model name into WEIGHTS_DIR so the auto-download persists
    on the mounted volume. A path-like YOLO_MODEL (custom weights) is used as-is."""
    if WEIGHTS_DIR and os.sep not in YOLO_MODEL and "/" not in YOLO_MODEL:
        os.makedirs(WEIGHTS_DIR, exist_ok=True)
        return os.path.join(WEIGHTS_DIR, YOLO_MODEL)
    return YOLO_MODEL

# --------------------------------------------------------------- counting ---
def run():
    import cv2
    import supervision as sv
    from ultralytics import YOLO

    # One LineZone per configured carriageway; the line's identity is the
    # direction (A = DIR_A_LABEL near road, B = DIR_B_LABEL far road).
    zones = {}
    for name, spec in (("A", COUNT_LINE_A), ("B", COUNT_LINE_B)):
        pts = parse_line(spec)
        if pts:
            zones[name] = sv.LineZone(start=sv.Point(*pts[0]), end=sv.Point(*pts[1]))
    if not zones:
        log("No count line set -- run calibrate_line.py to set COUNT_LINE_A "
            "(and COUNT_LINE_B for the second carriageway) in .env. Exiting.")
        sys.exit(2)

    device = pick_device()
    store = Store()
    mmr = get_mmr(device)
    log(f"device={device} model={YOLO_MODEL} lines={list(zones)} db={store.kind} "
        f"color={ENABLE_COLOR} mmr={'on' if mmr else 'off'} stride={DETECT_STRIDE}")
    model = YOLO(model_path())

    date = now_local().strftime("%Y-%m-%d")
    agg = store.load_today(date)
    write_snapshot(agg)
    log(f"resumed {date}: total={agg['total']} by_dir={agg['by_dir']}")

    last_snap = 0.0

    def maybe_rollover():
        """Reset the daily aggregates when the local date changes, even if no
        vehicle crossed right at midnight -- so the overlay shows 0 for the new
        day promptly. Called on every crossing and before every snapshot."""
        nonlocal date, agg
        d = now_local().strftime("%Y-%m-%d")
        if d != date:
            date = d
            agg = store.load_today(date)
            log(f"new day {date}; daily counts reset to 0")

    def record(direction, cls, crop, conf, tid, bbox):
        nonlocal date, agg
        maybe_rollover()
        dt = now_local()
        color = classify_color(crop) if ENABLE_COLOR else None
        make_model, mmr_conf = (classify_mmr(mmr, crop)
                                if (mmr and bbox[2] >= MMR_MIN_PX) else (None, None))
        x, y, w, h = bbox
        store.insert({
            "ts": dt, "local_date": date, "local_hour": dt.hour,
            "direction": direction, "direction_label": DIR_LABELS.get(direction),
            "cls": cls, "color": color, "make_model": make_model,
            "mmr_conf": mmr_conf, "confidence": conf, "tracker_id": tid,
            "bbox_x": x, "bbox_y": y, "bbox_w": w, "bbox_h": h, "site": SITE,
        })
        agg["total"] += 1
        agg["by_dir"][direction] = agg["by_dir"].get(direction, 0) + 1
        agg["by_cls"][cls] = agg["by_cls"].get(cls, 0) + 1
        if color:
            agg["by_color"][color] = agg["by_color"].get(color, 0) + 1
        extra = f" {color}" if color else ""
        extra += f" [{make_model}]" if make_model else ""
        log(f"{DIR_LABELS.get(direction, direction)} {cls}{extra}  total={agg['total']}")

    # Drive the RTSP capture with OpenCV directly (not Ultralytics' built-in
    # stream loader, which is unreliable on some IP cameras -- "Waiting for
    # stream..."). We cheaply grab() the frames we skip and only decode/track 1
    # of every DETECT_STRIDE, which keeps us real-time on CPU/MPS.
    imgsz = int(env("YOLO_IMGSZ", "960"))
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"cannot open RTSP stream (camera reachable from here?): {RTSP_URL}")
    log("stream opened; counting...")

    miss = 0
    while True:
        for _ in range(max(0, DETECT_STRIDE - 1)):
            cap.grab()                       # advance without decoding (cheap)
        ok, frame = cap.read()
        if not ok or frame is None:
            miss += 1
            if miss >= 50:                   # ~stream dropped -> bubble up to reconnect
                cap.release()
                raise RuntimeError("stream unresponsive (50 failed reads)")
            time.sleep(0.1)
            continue
        miss = 0
        if (frame.shape[1], frame.shape[0]) != (WIDTH, HEIGHT):
            frame = cv2.resize(frame, (WIDTH, HEIGHT))  # keep coords in line space

        result = model.track(
            frame, persist=True, classes=list(VEHICLE_CLASSES), conf=DETECT_CONF,
            tracker="bytetrack.yaml", device=device, imgsz=imgsz, verbose=False,
        )[0]
        det = sv.Detections.from_ultralytics(result)
        if det.tracker_id is not None and len(det):   # need tracker ids for LineZone
            for direction, zone in zones.items():
                crossed_in, crossed_out = zone.trigger(det)
                crossed = crossed_in | crossed_out     # either sense = a crossing
                for i in range(len(det)):
                    if not crossed[i]:
                        continue
                    cls = VEHICLE_CLASSES.get(int(det.class_id[i]), "car")
                    x1, y1, x2, y2 = (int(v) for v in det.xyxy[i])
                    x1, y1 = max(0, x1), max(0, y1)
                    x2, y2 = min(WIDTH, x2), min(HEIGHT, y2)
                    crop = frame[y1:y2, x1:x2]
                    conf = float(det.confidence[i]) if det.confidence is not None else None
                    tid = int(det.tracker_id[i])
                    record(direction, cls, crop, conf, tid, (x1, y1, x2 - x1, y2 - y1))
        now = time.time()
        if now - last_snap >= SNAPSHOT_SECS:
            maybe_rollover()             # roll to 0 at midnight even with no traffic
            write_snapshot(agg); last_snap = now

def main():
    # Self-healing: if the RTSP stream drops, run() raises -- sleep and reconnect
    # rather than dying (the compose service also restarts, but this keeps the
    # process warm across blips).
    backoff = 5
    while True:
        try:
            run()
            log("track stream ended; reconnecting in 5s")
        except KeyboardInterrupt:
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            log(f"error: {e}; reconnecting in {backoff}s")
        time.sleep(backoff)

if __name__ == "__main__":
    main()
