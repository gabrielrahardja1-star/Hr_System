"""Webcam selection and capture, by DEVICE NAME — via ffmpeg (macOS: AVFoundation,
Windows: DirectShow).

Cameras are addressed the way Zoom or your browser addresses them: by name
("HD Webcam C615"), never by a positional index.

Why not OpenCV's VideoCapture? It accepts only an integer index and exposes no
device-name API, so any name shown next to an index has to come from a separate
enumeration and be zipped on by position. That mapping is not dependable: on
this machine `system_profiler` returned (MacBook, C615) in one run and
(C615, MacBook) in another, so the same index was labelled differently minutes
apart, and picking "the Logitech" opened the built-in camera instead. Indices
also reshuffle when a webcam drops off the USB bus or a Continuity Camera comes
and goes, which silently repoints a saved index at a different physical device.

ffmpeg's avfoundation (macOS) and dshow (Windows) inputs both take the device
name directly, so nothing ever translates a name into a number. A name that
isn't currently connected fails loudly instead of quietly opening the wrong
camera.

First run triggers the macOS camera-permission prompt for whichever app launched
this process (Terminal/iTerm/VS Code). If capture returns all-black frames, grant
it in System Settings > Privacy & Security > Camera and restart that app.

**Windows (dshow) support is untested** — written by mirroring the avfoundation
path exactly (ffmpeg device-by-name semantics are the same shape on both), but
this codebase has only ever run on macOS. Verify on real Windows hardware
before relying on it: `python -m facekiosk.camera` there should list connected
cameras the same way it does here.
"""

from __future__ import annotations

import os
import re
import select
import stat
import subprocess
import threading
from collections import deque

import numpy as np

from .config import FFMPEG_VENDORED, IS_WINDOWS

FFMPEG_FORMAT = "dshow" if IS_WINDOWS else "avfoundation"


def _resolve_ffmpeg() -> str:
    """Prefer the vendored static binary (fetch_ffmpeg.py) so a built exe needs
    no separate ffmpeg install; fall back to PATH for dev machines that haven't
    fetched it."""
    if FFMPEG_VENDORED.exists():
        if not IS_WINDOWS:
            mode = FFMPEG_VENDORED.stat().st_mode
            if not mode & stat.S_IXUSR:
                os.chmod(FFMPEG_VENDORED, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(FFMPEG_VENDORED)
    return "ffmpeg"


FFMPEG = _resolve_ffmpeg()

# avfoundation: "[AVFoundation indev @ 0x...] [0] HD Webcam C615"
_DEVICE_LINE = re.compile(r"\]\s*\[(\d+)\]\s+(.+?)\s*$")

# dshow: `[dshow @ 0x...]  "Integrated Camera"` (video devices section only;
# an indented `Alternative name "..."` line follows each device — skip those).
# ffmpeg >= 5 annotates the kind after the name: `"Integrated Camera" (video)`.
# Without the optional group the name never matches on a current ffmpeg and the
# kiosk reports "no cameras found" on a machine that has one.
_DSHOW_DEVICE_LINE = re.compile(
    r'^\[dshow[^\]]*\]\s+"([^"]+)"(?:\s+\((?P<kind>video|audio)\))?\s*$'
)


class CameraError(RuntimeError):
    """Raised when a named camera can't be listed, opened, or read."""


def list_cameras(timeout: float = 15.0) -> list[str]:
    """Connected video capture devices, by name, in the platform's enumeration order.

    Screen-capture pseudo-devices are filtered out — they're not cameras and
    picking one would just record the kiosk's own display.
    """
    if IS_WINDOWS:
        cmd = [FFMPEG, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"]
    else:
        cmd = [FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []

    return _parse_dshow_devices(proc.stderr) if IS_WINDOWS else _parse_avfoundation_devices(proc.stderr)


def _parse_avfoundation_devices(stderr: str) -> list[str]:
    names: list[str] = []
    in_video = False
    for line in stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            break
        if not in_video:
            continue
        match = _DEVICE_LINE.search(line)
        if match:
            name = match.group(2).strip()
            if not name.lower().startswith("capture screen"):
                names.append(name)
    return names


def _parse_dshow_devices(stderr: str) -> list[str]:
    """Video device names from `ffmpeg -f dshow -list_devices true`.

    Handles both layouts: older ffmpeg groups devices under "DirectShow video
    devices" / "DirectShow audio devices" headers, newer ffmpeg drops the
    headers and tags each device "(video)" or "(audio)" instead. Relying on
    either one alone finds nothing on half the ffmpeg builds in the wild.
    """
    names: list[str] = []
    in_video = False
    for line in stderr.splitlines():
        if "DirectShow video devices" in line:
            in_video = True
            continue
        if "DirectShow audio devices" in line:
            in_video = False
            continue
        if "Alternative name" in line:
            continue  # a secondary identifier for the device just above, not a device
        match = _DSHOW_DEVICE_LINE.search(line)
        if not match:
            continue
        kind = match.group("kind")
        if kind == "video" or (kind is None and in_video):
            names.append(match.group(1))
    return names


class FFmpegCamera:
    """One ffmpeg process streaming raw BGR frames from a named device.

    Quacks like the subset of cv2.VideoCapture the rest of the package uses:
    `.read()`, `.release()`.
    """

    def __init__(
        self, name: str, width: int = 800, height: int = 448, fps: int = 30,
        request_size: bool = True,
    ) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.fps = fps
        self.request_size = request_size
        self._frame_bytes = width * height * 3
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", FFMPEG_FORMAT]
        if self.request_size:
            # Ask the device for this exact mode. It matters: uncompressed
            # capture is USB-bandwidth-bound, and letting avfoundation pick got
            # 1280x720 at a real 10 fps (5 fps at 1080p) off this C615, while
            # 800x448 runs 36. It's also 16:9, so a widescreen kiosk shows the
            # whole frame instead of cropping the sides off a 4:3 one — what
            # the person sees is then exactly what the detector sees.
            cmd += ["-video_size", f"{self.width}x{self.height}"]
        # dshow identifies a video device with a "video=" prefix on -i;
        # avfoundation takes the bare device name.
        device_arg = f"video={self.name}" if IS_WINDOWS else self.name
        cmd += ["-framerate", str(self.fps), "-i", device_arg]
        if not self.request_size:
            cmd += ["-vf", f"scale={self.width}:{self.height}"]
        # passthrough or ffmpeg pads to constant frame rate by DUPLICATING
        # frames — it emitted ~200k dupes as fast as the pipe drained, which
        # looked like 1500 fps of identical images.
        cmd += ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        # stderr must be drained or a chatty ffmpeg fills the pipe and blocks
        # the whole capture.
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    @property
    def error_tail(self) -> str:
        return " / ".join(self._stderr_tail)

    def read(self, timeout: float = 2.0):
        """One frame, or None if the device stopped producing within `timeout`."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        buf = bytearray(self._frame_bytes)
        view = memoryview(buf)
        got = 0
        while got < self._frame_bytes:
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if not ready:
                return None                      # hung — caller decides what next
            chunk = proc.stdout.readinto(view[got:])
            if not chunk:
                return None                      # ffmpeg exited / device gone
            got += chunk
        return np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width, 3)

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - never let teardown kill the caller
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


def robust_read(cam: FFmpegCamera, retries: int = 2):
    """A single frame, tolerating the odd dropped grab mid-stream. None only if
    it stays dead across every retry."""
    for _ in range(1 + retries):
        frame = cam.read()
        if frame is not None:
            return frame
    return None


def open_camera(
    name: str, width: int = 800, height: int = 448, fps: int = 30, warmup: float = 6.0
) -> FFmpegCamera:
    """Open the camera called `name` and wait for its first frame.

    Pass an empty name to take the first connected camera. Raises CameraError
    if the name isn't connected or produces no frames — deliberately loud,
    since the alternative is silently recording from the wrong device.
    """
    available = list_cameras()
    if not available:
        permission = (
            "Settings > Privacy & security > Camera > 'Let desktop apps access your camera'"
            if IS_WINDOWS
            else "macOS camera permission for this app"
        )
        raise CameraError(
            f"no cameras found. Check {permission}, and that no other app is "
            f"holding the camera. Run `{FFMPEG} -f {FFMPEG_FORMAT} -list_devices "
            "true -i dummy` to see what ffmpeg sees."
        )
    if not name:
        name = available[0]
    elif name not in available:
        raise CameraError(
            f"camera {name!r} is not connected. Available: {', '.join(available)}"
        )

    last_detail = ""
    # Ask for the exact mode first; fall back to whatever the device offers
    # (scaled to size) for cameras that don't advertise it.
    for request_size in (True, False):
        cam = FFmpegCamera(name, width=width, height=height, fps=fps, request_size=request_size)
        cam.start()
        for _ in range(max(1, int(warmup / 0.5))):   # ffmpeg needs ~1s for frame one
            frame = cam.read(timeout=0.5)
            if frame is not None:
                return cam
        last_detail = cam.error_tail
        cam.release()

    raise CameraError(
        f"camera {name!r} opened but produced no frames"
        + (f": {last_detail}" if last_detail else ". It may be held by another app.")
    )


_PERMISSION_HELP_WINDOWS = """\
No cameras found.

If ffmpeg is missing, install it (https://ffmpeg.org/download.html, or
`winget install ffmpeg`) and make sure ffmpeg.exe is on PATH — capture goes
through it so devices can be addressed by name instead of a shuffling index.

Otherwise Windows may be blocking camera access:

  Settings > Privacy & security > Camera > Let apps access your camera > on.

Also check the webcam isn't held by another app (Zoom, Teams, the Camera app)."""

_PERMISSION_HELP_MACOS = """\
No cameras found.

If ffmpeg is missing, install it (brew install ffmpeg) — capture goes through it
so devices can be addressed by name instead of a shuffling index.

Otherwise macOS may be blocking camera access for the app that launched this
process (Terminal, iTerm, or VS Code):

  System Settings > Privacy & Security > Camera > enable it for that app,
  then FULLY QUIT and reopen the app (a reload is not enough).

Also check the webcam isn't held by another app (Zoom, Photo Booth, FaceTime)."""

_PERMISSION_HELP = _PERMISSION_HELP_WINDOWS if IS_WINDOWS else _PERMISSION_HELP_MACOS


def main() -> int:
    names = list_cameras()
    if not names:
        print(_PERMISSION_HELP)
        return 1
    print("Connected cameras (pass one to --camera, quoted):\n")
    for n in names:
        print(f"  {n!r}")
    print("\nNames are stable; positions are not — always pass the name.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
