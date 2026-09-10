"""Randomized head-turn challenge — the v1 anti-spoofing check.

A held-up photo can pass an embedding match. Requiring a specific, randomly
chosen head turn (done live, within a few seconds) defeats the casual attack: a
still photo can't move, and a pre-recorded clip only matches the prompt half the
time. This is not liveness-grade defence against a determined replay attack —
that needs a dedicated anti-spoof model (a later option).

Yaw proxy from YuNet's 5 landmarks: how far the nose sits between the two eyes,
normalised by inter-eye distance. ~0 when frontal; swings toward one eye as the
head turns. Sign of the swing vs. the spoken direction can differ by camera
mirroring — flip config.T.liveness_invert if "turn left" registers as right.
"""

from __future__ import annotations

import random
import time

import numpy as np

from .config import T
from .engine import Face

# YuNet landmark indices.
_RIGHT_EYE, _LEFT_EYE, _NOSE = 0, 1, 2


def yaw_proxy(face: Face) -> float:
    lmk = face.landmarks
    re_x, le_x = float(lmk[_RIGHT_EYE][0]), float(lmk[_LEFT_EYE][0])
    nose_x = float(lmk[_NOSE][0])
    eye_span = abs(le_x - re_x)
    if eye_span < 1e-3:
        return 0.0
    return (nose_x - (re_x + le_x) / 2.0) / eye_span


class Challenge:
    """One head-turn ask, driven frame by frame with `update()`."""

    def __init__(self, direction: str | None = None) -> None:
        self.direction = direction or random.choice(("left", "right"))
        self.baseline: float | None = None
        self.turned = False
        self.result: str | None = None          # None | "pass" | "timeout"
        self._deadline = time.monotonic() + T.liveness_timeout_s

    @property
    def prompt(self) -> str:
        arrow = "<--" if self.direction == "left" else "-->"
        return f"Turn your head {self.direction}  {arrow}"

    def _target_sign(self) -> int:
        base = -1 if self.direction == "left" else 1
        return -base if T.liveness_invert else base

    def update(self, face: Face) -> str | None:
        if self.result is not None:
            return self.result

        offset = yaw_proxy(face)
        if self.baseline is None:
            self.baseline = offset
            return None

        delta = (offset - self.baseline) * self._target_sign()
        if not self.turned and delta > T.liveness_yaw_delta:
            self.turned = True
        elif self.turned and delta < T.liveness_yaw_delta * T.liveness_return_frac:
            self.result = "pass"

        if self.result is None and time.monotonic() > self._deadline:
            self.result = "timeout"
        return self.result

    @property
    def progress(self) -> float:
        """0..1 for an on-screen bar."""
        if self.result == "pass":
            return 1.0
        if self.baseline is None or not self.turned:
            return 0.0
        return 0.6
