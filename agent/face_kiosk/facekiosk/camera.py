"""Webcam selection and capture, macOS-first.

OpenCV can't name capture devices, and on a Mac the list is muddled by the
built-in camera plus any Continuity Camera (iPhone). `probe()` opens each index
and reports resolution so you can tell them apart (the Logitech C615 tops out at
1280x720; the MacBook camera goes higher). Pick the index and pass it to the
other commands with --camera.

First run triggers the macOS camera-permission prompt for whichever app launched
Python (Terminal/iTerm/VS Code). If capture returns all-black frames, grant it in
System Settings > Privacy & Security > Camera and restart that app.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import cv2

_BACKEND = cv2.CAP_AVFOUNDATION


def _warm_read(cap: cv2.VideoCapture, tries: int = 15, pause: float = 0.06):
    """Many UVC webcams (the C615 included) return nothing on the first few
    grabs while the sensor spins up. Poll briefly before deciding it's dead.
    Returns the first good frame, or None."""
    for _ in range(tries):
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        time.sleep(pause)
    return None


def robust_read(cap: cv2.VideoCapture, retries: int = 6, pause: float = 0.05):
    """A single frame, tolerating the odd dropped grab the C615 throws mid-stream.
    Returns None only if it stays dead across every retry."""
    for _ in range(1 + retries):
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        time.sleep(pause)
    return None


@dataclass
class CamInfo:
    index: int
    width: int
    height: int
    readable: bool


def probe(max_index: int = 6) -> list[CamInfo]:
    """Open indices 0..max_index-1 and report what answers. AVFoundation numbers
    cameras contiguously from 0, so the first index that won't open means we're
    past the end — stop there rather than let OpenCV spam 'out device of bound'."""
    found: list[CamInfo] = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, _BACKEND)
        if not cap.isOpened():
            cap.release()
            if found:
                break
            continue
        frame = _warm_read(cap)
        if frame is not None:
            h, w = frame.shape[:2]
        else:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        found.append(CamInfo(index=i, width=w, height=h, readable=frame is not None))
        cap.release()
    return found


def system_camera_names() -> list[str]:
    """Human-readable camera names from `system_profiler`, in its order.

    Not guaranteed to line up with OpenCV indices, but usually close and handy
    as a hint. Empty list on non-macOS or any failure.
    """
    try:
        out = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("Model") and stripped != "Camera:":
            names.append(stripped[:-1])
    return names


def open_camera(index: int, width: int = 1280, height: int = 720, fps: int = 30) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(index, _BACKEND)
    if not cap.isOpened():
        raise RuntimeError(
            f"camera index {index} would not open. Run `python -m facekiosk.camera` "
            "to list what's available."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    if _warm_read(cap) is None:
        cap.release()
        raise RuntimeError(
            f"camera index {index} opened but produced no frames. It may be held by "
            "another app, or macOS camera permission is still pending — see "
            "`python -m facekiosk.camera`."
        )
    return cap


_PERMISSION_HELP = """\
No cameras opened.

If you saw "not authorized to capture video" above, macOS is blocking camera
access for the app that launched this process (Terminal, iTerm, or VS Code):

  System Settings > Privacy & Security > Camera > enable it for that app,
  then FULLY QUIT and reopen the app (a reload is not enough).

Otherwise: check the webcam is plugged in and not held by another app (Zoom,
Photo Booth, FaceTime)."""


def main() -> int:
    cams = probe()
    names = system_camera_names()
    if not cams:
        print(_PERMISSION_HELP)
        if names:
            print("\nmacOS sees these cameras (names only, not usable until permitted):")
            for n in names:
                print(f"  - {n}")
        return 1
    print(f"{'idx':>3}  {'resolution':>12}  {'reads?':>6}  hint")
    for c in cams:
        hint = names[c.index] if c.index < len(names) else ""
        print(f"{c.index:>3}  {c.width:>5} x {c.height:<4}  {str(c.readable):>6}  {hint}")
    print("\nPick the index that matches your Logitech webcam and pass it as --camera.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
