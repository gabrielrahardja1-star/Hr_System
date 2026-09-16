"""Paths and tuning constants for the kiosk.

Every threshold that might need an on-site tweak lives here. The defaults are a
starting point for indoor light and a face ~0.5-1 m from a 720p webcam; expect
to adjust `match_cosine` and `detect_score` during the on-site pass.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"

PKG_DIR = Path(__file__).resolve().parent
KIOSK_DIR = PKG_DIR.parent                 # agent/face_kiosk/
MODELS_DIR = KIOSK_DIR / "models"

# A vendored, static ffmpeg (fetch_ffmpeg.py) so the built exe needs no
# separate ffmpeg install on the target machine. Read-only bundled resource —
# resolves correctly under PyInstaller automatically, same as MODELS_DIR.
VENDOR_DIR = KIOSK_DIR / "vendor"
FFMPEG_VENDORED = VENDOR_DIR / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg")

# Under a PyInstaller onefile build, KIOSK_DIR resolves inside the per-launch
# extraction temp dir — writing the gallery/db/key there would silently lose
# everything on the next run. Redirect writable state to sit next to the
# built executable instead; read-only bundled files (models, templates) are
# fine to resolve from KIOSK_DIR as normal.
FROZEN = bool(getattr(sys, "frozen", False))
DATA_DIR = (Path(sys.executable).resolve().parent / "data") if FROZEN else (KIOSK_DIR / "data")

YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"

GALLERY_PATH = DATA_DIR / "faces.gallery"  # Fernet-encrypted .npz of embeddings
KEY_PATH = DATA_DIR / "faces.key"          # Fernet key, chmod 600
EVENT_LOG = DATA_DIR / "events.jsonl"      # one JSON object per recognition

# Identifies which component wrote an event — carried through to the punch later.
SOURCE_TAG = "face-kiosk-proto"

# --- HQ sync (Phase 2) --------------------------------------------------- #
# Biometric data (the gallery above) never leaves this device — only sighting
# events (uid, timestamp, direction) sync out, and only when the Sync button
# is pressed.
#
# Configured by data/hq.json next to the executable, so a kiosk is set up by
# editing a file rather than by getting Windows environment variables right:
#
#     {"url": "http://10.0.0.5:8001", "api_key": "...", "device_id": "FACE-KIOSK-01"}
#
# An environment variable still wins, for dev machines and overrides.
HQ_CONFIG_PATH = DATA_DIR / "hq.json"


def _hq_file() -> dict:
    try:
        loaded = json.loads(HQ_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


_HQ = _hq_file()


def _hq_setting(key: str, env_var: str, default: str) -> str:
    return os.environ.get(env_var) or str(_HQ.get(key) or "") or default


HQ_BASE_URL = _hq_setting("url", "FACEKIOSK_HQ_URL", "http://localhost:8000").rstrip("/")
HQ_API_KEY = _hq_setting("api_key", "FACEKIOSK_HQ_API_KEY", "")
DEVICE_ID = _hq_setting("device_id", "FACEKIOSK_DEVICE_ID", "FACE-KIOSK-01")

# Written by the app, unlike hq.json which a human edits. Keeps the chosen
# camera across restarts — otherwise a reboot silently falls back to whichever
# device enumerates first, which on a laptop is the built-in lid camera rather
# than the one aimed at the queue.
SETTINGS_PATH = DATA_DIR / "kiosk_settings.json"


def load_settings() -> dict:
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_setting(key: str, value) -> None:
    """Best effort — a kiosk must keep running even if its settings file can't
    be written."""
    current = load_settings()
    if current.get(key) == value:
        return
    current[key] = value
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
        tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
    except OSError:
        pass


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

    # --- Liveness (head turn, direction-agnostic) ------------------------ #
    liveness_yaw_delta: float = 0.10     # nose-vs-eyeline shift (÷ face width) that counts as a turn
    liveness_return_frac: float = 0.4    # must reverse at least this fraction of the turn
    liveness_timeout_s: float = 8.0
    liveness_retries: int = 2            # re-arm the challenge this many times before giving up

    # --- Terminal-state dwell times (kiosk UI) ------------------------------ #
    # How long "Didn't catch that" / "Not recognised" stays on screen before the
    # track resets and tries again on its own. A visible dead end beats a silent one.
    liveness_fail_hold_s: float = 4.0
    unrecognized_hold_s: float = 4.0
    unrecognized_vote_frames: int = 15   # frames of no-match votes before declaring a stranger


T = Thresholds()


def env_index(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
