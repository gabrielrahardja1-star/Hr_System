"""Verify the pipeline without a camera.

    python -m facekiosk.selftest

Checks detection, embedding match/separation, the encrypted gallery round-trip,
the tracker, the liveness challenge, the auto/manual event paths, the sightings
roll-up, and the web routes. Runs entirely inside a throwaway temp directory —
it never touches data/faces.gallery or data/sightings.db. Exits non-zero on the
first failure.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from .config import T
from .engine import Face, FaceEngine
from .liveness import Challenge
from .tracker import IOUTracker

_CACHE = Path()  # set to a temp dir by _sandbox()
_IMAGES = {
    "person_a.jpg": "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg",
    "person_b.jpg": "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/messi5.jpg",
}

_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        sys.exit(1)
    _passed += 1


def _fetch_images() -> dict[str, "np.ndarray"]:
    import cv2

    _CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for fname, url in _IMAGES.items():
        path = _CACHE / fname
        if not path.exists():
            req = urllib.request.Request(url, headers={"User-Agent": "facekiosk/selftest"})
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                path.write_bytes(resp.read())
        out[fname] = cv2.imread(str(path))
    return out


def _sandbox(box: Path) -> dict:
    """Point every on-disk path at `box` so the real data/ is never touched.
    Returns the originals for restoration."""
    from . import gallery as gmod
    from . import run as rmod
    from . import store as smod

    orig = {
        (gmod, "GALLERY_PATH"): gmod.GALLERY_PATH,
        (gmod, "KEY_PATH"): gmod.KEY_PATH,
        (gmod, "DATA_DIR"): gmod.DATA_DIR,
        (smod, "DB_PATH"): smod.DB_PATH,
        (rmod, "EVENT_LOG"): rmod.EVENT_LOG,
        (rmod, "DATA_DIR"): rmod.DATA_DIR,
    }
    gmod.GALLERY_PATH = box / "faces.gallery"
    gmod.KEY_PATH = box / "faces.key"
    gmod.DATA_DIR = box
    smod.DB_PATH = box / "sightings.db"
    rmod.EVENT_LOG = box / "events.jsonl"
    rmod.DATA_DIR = box
    return orig


def main() -> int:
    global _CACHE
    print("facekiosk selftest")
    box = Path(tempfile.mkdtemp(prefix="facekiosk-selftest-"))
    _CACHE = box / "_images"
    orig = _sandbox(box)
    try:
        return _run(box)
    finally:
        for (mod, attr), value in orig.items():
            setattr(mod, attr, value)
        shutil.rmtree(box, ignore_errors=True)


def _run(box: Path) -> int:
    from .gallery import Gallery

    engine = FaceEngine()
    imgs = _fetch_images()

    # --- detection + embedding ---------------------------------------- #
    faces_a = engine.detect(imgs["person_a.jpg"])
    faces_b = engine.detect(imgs["person_b.jpg"])
    check("detect finds a face in image A", len(faces_a) >= 1, f"{len(faces_a)} found")
    check("detect finds a face in image B", len(faces_b) >= 1, f"{len(faces_b)} found")

    emb_a1 = engine.embed(imgs["person_a.jpg"], faces_a[0])
    emb_a2 = engine.embed(imgs["person_a.jpg"], faces_a[0])
    emb_b = engine.embed(imgs["person_b.jpg"], faces_b[0])
    check("embedding is 128-d", emb_a1.shape == (128,), str(emb_a1.shape))
    same = FaceEngine.cosine(emb_a1, emb_a2)
    diff = FaceEngine.cosine(emb_a1, emb_b)
    check("same face -> high cosine", same > 0.9, f"{same:.3f}")
    check("different faces -> below match cut", diff < T.match_cosine, f"{diff:.3f}")

    # --- gallery round-trip through encryption ----------------------- #
    g = Gallery()
    g.enroll("_st_a", "Selftest A", [emb_a1])
    g.enroll("_st_b", "Selftest B", [emb_b])
    g.save()
    check("first save leaves no stale .bak", not (box / "faces.gallery.bak").exists())
    g.enroll("_st_c", "Selftest C", [emb_b])
    g.save()
    check("save keeps the previous file as .bak", (box / "faces.gallery.bak").exists())
    reloaded = Gallery()
    check("gallery reload keeps enrolled people", {"_st_a", "_st_b", "_st_c"} <= set(reloaded.people))
    m = reloaded.identify(emb_a2, T.match_cosine)
    check("identify returns the right uid", m.uid == "_st_a", f"{m.uid} sim={m.similarity:.3f}")
    m_none = reloaded.identify(np.random.default_rng(0).standard_normal(128).astype("float32"), T.match_cosine)
    check("random vector does not match", not m_none.ok, f"sim={m_none.similarity:.3f}")

    # --- tracker keeps identity across frames ----------------------- #
    tr = IOUTracker()
    fbox = (100, 100, 120, 120)
    ids = set()
    for dx in range(0, 40, 4):
        f = Face((fbox[0] + dx, fbox[1], fbox[2], fbox[3]), 0.99, _landmarks(fbox[0] + dx, fbox[1]), _row(fbox, dx))
        pairs = tr.update([f])
        ids.add(pairs[0][0].id)
    check("one moving face stays one track", len(ids) == 1, f"track ids seen: {ids}")

    # --- liveness (direction-agnostic: turn away from frontal, then back) --- #
    def _face_at(proxy: float) -> Face:
        return Face(fbox, 0.99, _landmarks_nose(160, 160 + proxy * fbox[2]), np.zeros(15, "float32"))

    ch = Challenge()
    result = None
    for proxy in [0.0] * 4 + [0.16] * 8 + [0.0] * 8:      # frontal, turn+hold, return
        result = ch.update(_face_at(proxy))
    check("liveness passes on a head turn and return", result == "pass", str(result))

    ch_left = Challenge()
    result = None
    for proxy in [0.0] * 4 + [-0.16] * 8 + [0.0] * 8:     # the other way works too
        result = ch_left.update(_face_at(proxy))
    check("liveness is direction-agnostic", result == "pass", str(result))

    ch2 = Challenge()
    for _ in range(5):
        ch2.update(_face_at(0.0))
    ch2._deadline = time.monotonic() - 1
    check("liveness times out with no motion", ch2.update(_face_at(0.0)) == "timeout")

    # --- end-to-end: track -> vote -> event -------------------------- #
    from types import SimpleNamespace

    from .run import Kiosk

    g4 = Gallery()
    g4.people.clear()
    g4.enroll("_st_e2e", "E2E Tester", [emb_a1, emb_a2])
    g4.save()
    args = SimpleNamespace(
        camera=0, conf_thres=T.detect_score, match_cosine=T.match_cosine,
        liveness=False, no_window=True, seconds=0,
    )
    auto = Kiosk(args, auto_log=True)
    for _ in range(T.vote_frames + 4):
        auto.process(imgs["person_a.jpg"].copy())
    check("auto-log emits one event for the enrolled face", auto.events == 1, f"events={auto.events}")

    manual = Kiosk(args, auto_log=False)
    for _ in range(T.vote_frames + 4):
        manual.process(imgs["person_a.jpg"].copy())
    check("manual mode logs nothing until a capture", manual.events == 0, f"events={manual.events}")
    cand = manual.current_candidate()
    check("manual mode surfaces the recognised person as a candidate", cand and cand["uid"] == "_st_e2e", str(cand))
    rec = manual.capture("in")
    check("capture('in') logs one event with direction", rec and rec["direction"] == "in" and manual.events == 1, str(rec))
    check("second capture within debounce is ignored", manual.capture("in") is None)

    # --- sightings store roll-up ----------------------------------- #
    from .store import SightingStore

    st = SightingStore(box / "roll.db")
    for ts, sim in [("2026-01-02T08:03:00+07:00", 0.7), ("2026-01-02T17:31:00+07:00", 0.8)]:
        st.record({"ts": ts, "device_user_id": "K1", "emp_id": "42", "name": "Tester",
                   "direction": "in", "similarity": sim, "liveness": "pass"})
    roll = st.roster_for_date("2026-01-02")
    check(
        "store rolls sightings into first/last per person",
        len(roll) == 1 and roll[0]["first_seen"][11:16] == "08:03"
        and roll[0]["last_seen"][11:16] == "17:31" and roll[0]["sightings"] == 2,
        str(roll),
    )
    st.close()

    # --- web app routes (camera intentionally absent) --------------- #
    try:
        from fastapi.testclient import TestClient

        from . import app as app_mod

        app_mod._SETTINGS.camera = 999
        with TestClient(app_mod.create_app()) as client:
            codes = {p: client.get(p).status_code
                     for p in ("/", "/log", "/register", "/api/status", "/api/roster", "/api/candidate")}
            check("web app serves its pages without a camera", all(v == 200 for v in codes.values()), str(codes))
            check("web app reports the camera failure", client.get("/api/status").json()["camera_ok"] is False)
            check("no candidate when nobody is on camera", client.get("/api/candidate").json()["candidate"] is None)
            check("stamp with no candidate is refused", client.post("/api/stamp", json={"direction": "in"}).status_code == 409)
    except ImportError:
        print("  [skip] web app checks (fastapi/httpx not installed)")

    print(f"\n{_passed} checks passed.")
    return 0


def _landmarks(x: int, y: int) -> np.ndarray:
    return np.array(
        [[x + 35, y + 45], [x + 85, y + 45], [x + 60, y + 70], [x + 40, y + 95], [x + 80, y + 95]],
        dtype=np.float32,
    )


def _landmarks_nose(eye_center_x: float, nose_x: float) -> np.ndarray:
    return np.array(
        [[eye_center_x - 25, 145], [eye_center_x + 25, 145], [nose_x, 170], [nose_x - 15, 195], [nose_x + 15, 195]],
        dtype=np.float32,
    )


def _row(box, dx) -> np.ndarray:
    r = np.zeros(15, dtype=np.float32)
    r[:4] = [box[0] + dx, box[1], box[2], box[3]]
    r[-1] = 0.99
    return r


if __name__ == "__main__":
    raise SystemExit(main())
