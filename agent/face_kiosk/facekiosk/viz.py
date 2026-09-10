"""Small OpenCV drawing helpers shared by enroll and run."""

from __future__ import annotations

import cv2
import numpy as np

GREEN = (80, 220, 100)
AMBER = (60, 180, 250)
RED = (70, 70, 240)
GREY = (180, 180, 180)
WHITE = (245, 245, 245)

_FONT = cv2.FONT_HERSHEY_SIMPLEX


def draw_box(frame: np.ndarray, box: tuple[int, int, int, int], color, label: str = "") -> None:
    x, y, w, h = box
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
    if label:
        (tw, th), _ = cv2.getTextSize(label, _FONT, 0.6, 2)
        cv2.rectangle(frame, (x, y - th - 8), (x + tw + 8, y), color, -1)
        cv2.putText(frame, label, (x + 4, y - 6), _FONT, 0.6, (20, 20, 20), 2, cv2.LINE_AA)


def banner(frame: np.ndarray, text: str, color=WHITE, *, top: bool = True) -> None:
    h, w = frame.shape[:2]
    y0 = 0 if top else h - 46
    strip = frame[y0 : y0 + 46]
    cv2.addWeighted(strip, 0.35, np.zeros_like(strip), 0.65, 0, strip)
    cv2.putText(frame, text, (16, y0 + 30), _FONT, 0.8, color, 2, cv2.LINE_AA)


def progress_bar(frame: np.ndarray, frac: float, color=GREEN) -> None:
    h, w = frame.shape[:2]
    frac = max(0.0, min(1.0, frac))
    cv2.rectangle(frame, (16, h - 12), (w - 16, h - 6), GREY, 1)
    cv2.rectangle(frame, (16, h - 12), (16 + int((w - 32) * frac), h - 6), color, -1)


def fps_tag(frame: np.ndarray, fps: float) -> None:
    h, w = frame.shape[:2]
    cv2.putText(frame, f"{fps:4.1f} fps", (w - 108, h - 20), _FONT, 0.6, GREY, 2, cv2.LINE_AA)
