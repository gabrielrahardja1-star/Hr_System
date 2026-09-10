"""Paths and tuning constants for the kiosk.

Every threshold that might need an on-site tweak lives here. The defaults are a
starting point for indoor light and a face ~0.5-1 m from a 720p webcam; expect
to adjust `match_cosine` and `detect_score` during the on-site pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
KIOSK_DIR = PKG_DIR.parent                 # agent/face_kiosk/
MODELS_DIR = KIOSK_DIR / "models"
DATA_DIR = KIOSK_DIR / "data"              # gallery + key + event log (gitignored)

YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"

GALLERY_PATH = DATA_DIR / "faces.gallery"  # Fernet-encrypted .npz of embeddings
KEY_PATH = DATA_DIR / "faces.key"          # Fernet key, chmod 600
EVENT_LOG = DATA_DIR / "events.jsonl"      # one JSON object per recognition

# Identifies which component wrote an event — carried through to the punch later.
SOURCE_TAG = "face-kiosk-proto"


@dataclass(frozen=True)
class Thresholds:
    # --- SFace matching ---------------------------------------------------- #
    # Cosine similarity of two 128-d embeddings. OpenCV's SFace card quotes
    # 0.363 as the same-identity cut on LFW; 0.38 is a slightly safer default.
    match_cosine: float = 0.38

    # --- YuNet detection ------------------------------------------------------ #
    detect_score: float = 0.85            # keep only confident face boxes
    detect_nms: float = 0.30
    detect_topk: int = 50
    min_face_px: int = 90                 # ignore faces smaller than this (too far)

    # --- Track-level voting ------------------------------------------------- #
    # A recognition only becomes an event after the same identity wins the vote
    # on one tracked face for this many frames. Kills single-frame misfires.
    vote_window: int = 15
    vote_frames: int = 12                 # >= this many agreeing votes in window
    track_iou: float = 0.30
    track_max_misses: int = 15            # drop a track after this many blank frames

    # --- Re-punch debounce ------------------------------------------------- #
    debounce_seconds: int = 120          # auto-log: same person within this = no new event
    capture_debounce_seconds: int = 8    # manual: ignore a second Check In/Out tap this soon

    # --- Liveness (randomized head turn) --------------------------------- #
    liveness_yaw_delta: float = 0.16     # normalised nose shift that counts as a turn
    liveness_return_frac: float = 0.45   # must come back within this fraction of it
    liveness_timeout_s: float = 6.0
    liveness_invert: bool = False        # flip if "turn left" reads as right on-site


T = Thresholds()


def env_index(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
