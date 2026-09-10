"""Enrol, list, or remove employees in the local face gallery.

    # capture from the webcam (press SPACE for each shot, or --auto)
    python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --camera 1

    # enrol from a folder of photos instead of the camera
    python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --from-images ./budi/

    python -m facekiosk.enroll --list
    python -m facekiosk.enroll --remove 1001

`--uid` must be the employee's Employee.device_user_id in HR (so a face punch and
a fingerprint punch land on the same person). Get consent before enrolling.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from .camera import open_camera, robust_read
from .config import T
from .engine import Face, FaceEngine
from .gallery import Gallery
from . import viz

_MIN_SHOTS = 3


def _biggest_single_face(faces: list[Face]) -> Face | None:
    faces = [f for f in faces if f.size >= T.min_face_px]
    if len(faces) != 1:
        return None
    return faces[0]


def _capture_from_camera(engine: FaceEngine, cam: int, want: int, auto: bool) -> list[np.ndarray]:
    cap = open_camera(cam)
    shots: list[np.ndarray] = []
    last_auto = 0.0
    print("SPACE = take a shot   |   A = toggle auto   |   Q = done/cancel")
    try:
        while len(shots) < want:
            frame = robust_read(cap)
            if frame is None:
                print("camera stopped responding", file=sys.stderr)
                break
            face = _biggest_single_face(engine.detect(frame))
            ready = face is not None
            color = viz.GREEN if ready else viz.AMBER
            if face:
                viz.draw_box(frame, face.box, color, f"score {face.score:.2f}")
            viz.banner(frame, f"shot {len(shots)}/{want}   {'READY' if ready else 'hold still, one face'}", color)
            viz.banner(frame, f"auto:{'on' if auto else 'off'}   SPACE shot  A auto  Q done", viz.GREY, top=False)
            cv2.imshow("enroll", frame)

            key = cv2.waitKey(1) & 0xFF
            take = False
            if key == ord("q"):
                break
            if key == ord("a"):
                auto = not auto
            if key == ord(" "):
                take = True
            if auto and ready and time.monotonic() - last_auto > 0.6:
                take = True
                last_auto = time.monotonic()

            if take and ready:
                shots.append(engine.embed(frame, face))
                print(f"  captured {len(shots)}/{want}")
            elif take:
                print("  skipped — need exactly one clear face")
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return shots


def _capture_from_images(engine: FaceEngine, folder: Path) -> list[np.ndarray]:
    shots: list[np.ndarray] = []
    files = sorted(
        p for p in folder.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )
    if not files:
        print(f"no images in {folder}", file=sys.stderr)
        return shots
    for path in files:
        img = cv2.imread(str(path))
        if img is None:
            print(f"  {path.name}: unreadable")
            continue
        face = _biggest_single_face(engine.detect(img))
        if face is None:
            print(f"  {path.name}: need exactly one clear face — skipped")
            continue
        shots.append(engine.embed(img, face))
        print(f"  {path.name}: ok")
    return shots


def _cmd_list(gallery: Gallery) -> int:
    if not gallery.people:
        print("gallery is empty")
        return 0
    print(f"{'uid':>10}  {'shots':>5}  enrolled_at              name")
    for p in gallery.people.values():
        print(f"{p.uid:>10}  {p.shots:>5}  {p.enrolled_at:<22}  {p.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uid", help="employee device_user_id")
    ap.add_argument("--name", help="display name")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--shots", type=int, default=7)
    ap.add_argument("--auto", action="store_true", help="auto-capture when a face is steady")
    ap.add_argument("--from-images", type=Path, metavar="DIR")
    ap.add_argument("--replace", action="store_true", help="overwrite instead of appending shots")
    ap.add_argument("--min-shots", type=int, default=_MIN_SHOTS, help=f"refuse to save fewer than this (default {_MIN_SHOTS})")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove", metavar="UID")
    args = ap.parse_args(argv)

    gallery = Gallery()

    if args.list:
        return _cmd_list(gallery)
    if args.remove:
        ok = gallery.remove(args.remove)
        gallery.save()
        print(f"removed {args.remove}" if ok else f"{args.remove} not in gallery")
        return 0 if ok else 1

    if not args.uid or not args.name:
        ap.error("--uid and --name are required to enrol")

    engine = FaceEngine()
    if args.from_images:
        shots = _capture_from_images(engine, args.from_images)
    else:
        shots = _capture_from_camera(engine, args.camera, args.shots, args.auto)

    if len(shots) < args.min_shots:
        print(f"got {len(shots)} usable shots, need >= {args.min_shots} — nothing saved", file=sys.stderr)
        return 1

    person = gallery.enroll(args.uid, args.name, shots, replace=args.replace)
    gallery.save()
    print(f"enrolled {person.name} (uid {person.uid}) — {person.shots} shots total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
