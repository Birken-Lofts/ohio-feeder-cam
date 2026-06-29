#!/usr/bin/env python3
"""
Count-line calibration helper.

Grabs one frame from the camera (scaled to the 1920x1080 stream space) and lets
you draw the counting line(s) by clicking endpoints. On a divided road you draw
TWO lines -- one per carriageway -- and each line becomes a direction:

    COUNT_LINE_A=x1,y1,x2,y2   # near carriageway  -> DIR_A_LABEL (e.g. NB)
    COUNT_LINE_B=x1,y1,x2,y2   # far  carriageway  -> DIR_B_LABEL (e.g. SB)

It writes these straight into .env for you. Draw just one line (press ENTER after
two clicks) for an undivided road.

    pip install -r requirements-counter.txt
    python3 calibrate_line.py            # interactive (needs a display, e.g. macOS)

Headless? It still saves data/calib_frame.jpg -- open it in any image editor, read
the pixel coordinates, and set COUNT_LINE_A / COUNT_LINE_B manually.
"""
import os
import sys
import cv2

# Reuse the streamer's RTSP URL + resolution + output dir (no heavy imports).
# Importing vehicle_counter also loads .env into os.environ.
import vehicle_counter as vc

RTSP_URL = vc.RTSP_URL
WIDTH, HEIGHT = vc.WIDTH, vc.HEIGHT
DATA_DIR = os.path.dirname(vc.COUNTS_DB) or "data"
FRAME_PATH   = os.path.join(DATA_DIR, "calib_frame.jpg")
PREVIEW_PATH = os.path.join(DATA_DIR, "calib_preview.jpg")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(vc.__file__)), ".env")

DIR_A = os.environ.get("DIR_A_LABEL", "A")
DIR_B = os.environ.get("DIR_B_LABEL", "B")
# BGR colors for line A / line B.
COL_A = (0, 215, 246)   # amber
COL_B = (80, 220, 80)   # green


def grab_frame():
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        sys.exit(f"Could not open camera stream: {RTSP_URL}\n"
                 "Is this machine on the same network as the camera? "
                 "(try: ping " + (vc.CAM_IP or "<cam-ip>") + ")")
    ok, frame = None, None
    for _ in range(30):              # let the stream warm up; grab a clean frame
        ok, frame = cap.read()
        if ok:
            break
    cap.release()
    if not ok or frame is None:
        sys.exit("Could not read a frame from the camera.")
    frame = cv2.resize(frame, (WIDTH, HEIGHT))
    os.makedirs(DATA_DIR, exist_ok=True)
    cv2.imwrite(FRAME_PATH, frame)
    return frame


def _draw_lines(view, pts):
    """Draw whatever points/lines exist so far onto `view`."""
    specs = [(pts[0:2], COL_A, DIR_A), (pts[2:4], COL_B, DIR_B)]
    for pair, col, name in specs:
        for p in pair:
            cv2.circle(view, p, 10, col, -1)
        if len(pair) == 2:
            cv2.line(view, pair[0], pair[1], col, 4)
            mx = (pair[0][0] + pair[1][0]) // 2
            my = (pair[0][1] + pair[1][1]) // 2
            cv2.putText(view, name, (mx + 12, my), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 6)
            cv2.putText(view, name, (mx + 12, my), cv2.FONT_HERSHEY_SIMPLEX, 1.2, col, 2)


def _label(view, msg):
    for color, thick in (((0, 0, 0), 6), ((255, 255, 255), 2)):  # outline + fill
        cv2.putText(view, msg, (30, 56), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, thick)


PROMPTS = {
    0: f"Line {DIR_A} (NEAR carriageway): click FIRST endpoint",
    1: f"Line {DIR_A}: click SECOND endpoint",
    2: f"Line {DIR_A} set. Click line {DIR_B} (FAR carriageway), or ENTER to finish",
    3: f"Line {DIR_B}: click SECOND endpoint",
    4: "Both lines set.  ENTER = save   r = redo   q = cancel",
}


def interactive(frame):
    pts = []

    def on_mouse(event, x, y, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN and len(pts) < 4:
            pts.append((x, y))

    win = "Count lines"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1280, 720)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        view = frame.copy()
        _draw_lines(view, pts)
        _label(view, PROMPTS[len(pts)])
        cv2.imshow(win, view)
        key = cv2.waitKey(20) & 0xFF
        # Closing the window accepts whatever full lines exist, else cancels.
        if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
            break
        if key == ord("q"):
            cv2.destroyAllWindows()
            sys.exit("Cancelled -- nothing saved.")
        if key == ord("r"):
            pts.clear()
        if key in (13, 10, ord(" ")) and len(pts) in (2, 4):  # enter/space confirms
            break
    cv2.destroyAllWindows()
    if len(pts) < 2:
        sys.exit("Need at least one full line (two points). Re-run calibrate_line.py.")
    line_a = f"{pts[0][0]},{pts[0][1]},{pts[1][0]},{pts[1][1]}"
    line_b = f"{pts[2][0]},{pts[2][1]},{pts[3][0]},{pts[3][1]}" if len(pts) == 4 else ""
    return pts, line_a, line_b


def set_env_keys(path, mapping):
    """Update/insert each KEY=value in .env, in place. Returns the path or None."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    remaining = dict(mapping)
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(ln)
    for key, val in remaining.items():       # keys not already present
        out.append(f"{key}={val}\n")
    with open(path, "w") as f:
        f.writelines(out)
    return path


def main():
    frame = grab_frame()
    print(f"Saved frame -> {FRAME_PATH}")
    try:
        pts, line_a, line_b = interactive(frame)
    except cv2.error:
        # No GUI backend (headless server). Fall back to manual coordinate entry.
        print("\nNo display available. Open the saved frame, read the pixel "
              "coordinates of your line(s), and set them manually in .env:")
        print(f"    {FRAME_PATH}  ({WIDTH}x{HEIGHT})")
        print("    COUNT_LINE_A=x1,y1,x2,y2")
        print("    COUNT_LINE_B=x1,y1,x2,y2   # omit for an undivided road")
        return

    preview = frame.copy()
    _draw_lines(preview, pts)
    cv2.imwrite(PREVIEW_PATH, preview)
    print(f"Saved preview -> {PREVIEW_PATH}")

    mapping = {"COUNT_LINE_A": line_a}
    if line_b:
        mapping["COUNT_LINE_B"] = line_b
    if set_env_keys(ENV_PATH, mapping):
        print(f"\n✓ Set COUNT_LINE_A={line_a}" + (f"  COUNT_LINE_B={line_b}" if line_b else ""))
        print(f"  in {ENV_PATH}")
        print("  Now run:  .venv/bin/python vehicle_counter.py")
    else:
        print("\nNo .env found. Add these manually:")
        for k, v in mapping.items():
            print(f"    {k}={v}")


if __name__ == "__main__":
    main()
