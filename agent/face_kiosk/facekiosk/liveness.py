"""Head-turn challenge — the v1 anti-spoofing check.

A held-up photo passes an embedding match. Requiring a live head turn (a swing
away from frontal and back, within a few seconds) defeats the casual attack: a
still photo can't move. This is a deterrent, not liveness-grade defence against a
determined video replay — that needs a dedicated anti-spoof model (a later option).

Direction-agnostic on purpose: a left/right *specific* turn depends on camera
mirroring and is fiddly to calibrate on-site, for little extra security. We just
need "the head moved, then came back".

Yaw proxy from YuNet's 5 landmarks: how far the nose sits from the eye midline,
normalised by the face-box width (stable as the head turns; inter-eye distance
is not). ~0 frontal, grows as the head yaws either way.
"""

from __future__ import annotations

import time
from collections import deque

from .config import T
from .engine import Face

_RIGHT_EYE, _LEFT_EYE, _NOSE = 0, 1, 2


def yaw_proxy(face: Face) -> float:
    lmk = face.landmarks
    eye_mid_x = (float(lmk[_RIGHT_EYE][0]) + float(lmk[_LEFT_EYE][0])) / 2.0
    nose_x = float(lmk[_NOSE][0])
    box_w = max(face.box[2], 1)
    return (nose_x - eye_mid_x) / box_w


class Challenge:
    """One "turn your head" ask, driven frame by frame with `update()`."""

    prompt = "Turn your head, then look back"

    def __init__(self, _direction: str | None = None) -> None:
        self.baseline: float | None = None
        self._samples: deque[float] = deque(maxlen=5)
        self.peak = 0.0
        self.turned = False
        self.result: str | None = None          # None | "pass" | "timeout"
        self._deadline = time.monotonic() + T.liveness_timeout_s

    def update(self, face: Face) -> str | None:
        if self.result is not None:
            return self.result

        self._samples.append(yaw_proxy(face))
        smooth = sum(self._samples) / len(self._samples)

        if self.baseline is None or len(self._samples) < 3:
            self.baseline = smooth
            return None

        delta = abs(smooth - self.baseline)
        self.peak = max(self.peak, delta)
        if not self.turned and self.peak >= T.liveness_yaw_delta:
            self.turned = True
        elif self.turned and delta <= self.peak * (1.0 - T.liveness_return_frac):
            # came back at least `return_frac` of the way toward frontal — relative
            # to how far they actually turned, so a landmark offset can't block it
            self.result = "pass"

        if self.result is None and time.monotonic() > self._deadline:
            self.result = "timeout"
        return self.result

    @property
    def progress(self) -> float:
        """0..1 for an on-screen bar."""
        if self.result == "pass":
            return 1.0
        if self.turned:
            return 0.75
        return min(0.7, self.peak / max(T.liveness_yaw_delta, 1e-6) * 0.7)
