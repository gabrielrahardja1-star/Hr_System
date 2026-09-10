"""Download the two pretrained ONNX nets the kiosk needs.

    python -m facekiosk.fetch_models

Both come from OpenCV's model zoo (Apache-2.0). They are weights only — no code
runs from them; OpenCV's DNN backend executes them. ~39 MB total, one-time.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request

from .config import MODELS_DIR, SFACE_PATH, YUNET_PATH

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    YUNET_PATH: (
        f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        232589,
    ),
    SFACE_PATH: (
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        38696353,
    ),
}


def _download(url: str, dest, expected_bytes: int) -> None:
    print(f"  {dest.name}  <-  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "facekiosk/fetch"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (trusted host)
        data = resp.read()
    if len(data) != expected_bytes:
        print(
            f"    ! size {len(data)} != expected {expected_bytes} — saving anyway, "
            "verify the model still loads",
            file=sys.stderr,
        )
    dest.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()[:16]
    print(f"    ok  {len(data):,} bytes  sha256:{sha}…")


def main() -> int:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"fetching models into {MODELS_DIR}")
    for dest, (url, size) in MODELS.items():
        if dest.exists() and dest.stat().st_size == size:
            print(f"  {dest.name}  already present, skipping")
            continue
        _download(url, dest, size)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
