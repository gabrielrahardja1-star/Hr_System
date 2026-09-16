"""Verify the pipeline without a camera.

    python -m facekiosk.selftest

Checks detection, embedding match/separation, the encrypted gallery round-trip,
the tracker, the liveness challenge, the auto/manual event paths, the sightings
roll-up, and the web routes. Runs entirely inside a throwaway temp directory —
it never touches data/faces.gallery or data/sightings.db. Exits non-zero on the
first failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from .config import KIOSK_DIR, T
from .engine import Face, FaceEngine
from .liveness import Challenge
from .tracker import IOUTracker

# Public sample images only (no PII) — cached persistently so repeat runs don't
# hit the network, but kept out of data/ (the sandbox below never touches that).
_CACHE = KIOSK_DIR / ".selftest_cache"
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
    from . import config as cmod
    from . import gallery as gmod
    from . import run as rmod
    from . import store as smod

    orig = {
        # Without this the app's own camera-switch checks write the selftest's
        # fake camera name into the real settings file, and the next real run
        # boots looking for a device that never existed.
        (cmod, "SETTINGS_PATH"): cmod.SETTINGS_PATH,
        (gmod, "GALLERY_PATH"): gmod.GALLERY_PATH,
        (gmod, "KEY_PATH"): gmod.KEY_PATH,
        (gmod, "DATA_DIR"): gmod.DATA_DIR,
        (smod, "DB_PATH"): smod.DB_PATH,
        (rmod, "EVENT_LOG"): rmod.EVENT_LOG,
        (rmod, "DATA_DIR"): rmod.DATA_DIR,
    }
    cmod.SETTINGS_PATH = box / "kiosk_settings.json"
    gmod.GALLERY_PATH = box / "faces.gallery"
    gmod.KEY_PATH = box / "faces.key"
    gmod.DATA_DIR = box
    smod.DB_PATH = box / "sightings.db"
    rmod.EVENT_LOG = box / "events.jsonl"
    rmod.DATA_DIR = box
    return orig


def main() -> int:
    print("facekiosk selftest")
    box = Path(tempfile.mkdtemp(prefix="facekiosk-selftest-"))
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

    # Detection and display run on separate threads: process() must only
    # RECORD what to draw, and the display thread draws it. If process() drew
    # directly, boxes would appear on the few frames detection happened to
    # touch and be missing from every other one.
    check("process() records overlays for the display thread",
          len(auto.overlays) >= 1, f"overlays={len(auto.overlays)}")
    _clean = imgs["person_a.jpg"].copy()
    _before = _clean.copy()
    auto.replay_overlays(_clean)
    check("replay_overlays actually draws them",
          not np.array_equal(_clean, _before))

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

    # --- overnight shift spans two calendar dates ------------------- #
    night = SightingStore(box / "night.db")
    for ts, direction in [
        ("2026-01-02T23:10:00+07:00", "in"),   # Shift 3 starts
        ("2026-01-03T07:05:00+07:00", "out"),  # ...and ends the next morning
    ]:
        night.record({"ts": ts, "device_user_id": "N1", "emp_id": "43", "name": "Malam",
                      "direction": direction, "similarity": 0.8, "liveness": "pass"})
    n2 = night.roster_for_date("2026-01-02")
    check(
        "overnight shift is rolled up on the date it started",
        len(n2) == 1 and n2[0]["check_out"][11:16] == "07:05" and abs(n2[0]["hours"] - 7.92) < 0.02,
        str(n2),
    )
    check(
        "overnight clock-out does not also appear as its own next-day shift",
        night.roster_for_date("2026-01-03") == [],
        str(night.roster_for_date("2026-01-03")),
    )
    night.close()

    # --- dshow device parsing (Windows camera discovery) ------------ #
    from .camera import _parse_dshow_devices

    # Verbatim from ffmpeg 9.0.1 on Windows 11. Note the log context is
    # "in#0", not "dshow" — pinning the prefix to "dshow" hid both cameras.
    _dshow_new = (
        '[in#0 @ 000001d2] "USB2.0 camera" (video)\n'
        '[in#0 @ 000001d2]   Alternative name "@device_pnp_\\\\?\\usb#vid_0bda&pid_5830"\n'
        '[in#0 @ 000001d2] "Logi C615 HD WebCam" (video)\n'
        '[in#0 @ 000001d2]   Alternative name "@device_pnp_\\\\?\\usb#vid_046d&pid_082c"\n'
        '[in#0 @ 000001d2] "Microphone (Logi C615 HD WebCam)" (audio)\n'
        '[in#0 @ 000001d2] "Microphone Array (2- Realtek(R) Audio)" (audio)\n'
    )
    _dshow_old = (
        "[dshow @ 0001] DirectShow video devices (some may be both video and audio devices)\n"
        '[dshow @ 0001]  "Integrated Camera"\n'
        '[dshow @ 0001]     Alternative name "@device_pnp_\\\\?\\usb#vid_0bda"\n'
        "[dshow @ 0001] DirectShow audio devices\n"
        '[dshow @ 0001]  "Microphone Array (Realtek)"\n'
    )
    check(
        "dshow parser reads ffmpeg 9 output, cameras only, mics excluded",
        _parse_dshow_devices(_dshow_new) == ["USB2.0 camera", "Logi C615 HD WebCam"],
        str(_parse_dshow_devices(_dshow_new)),
    )
    check(
        "dshow parser still reads older sectioned ffmpeg output",
        _parse_dshow_devices(_dshow_old) == ["Integrated Camera"],
        str(_parse_dshow_devices(_dshow_old)),
    )

    # --- the chosen camera survives a restart ----------------------- #
    from . import config as _cfg

    _orig_settings = _cfg.SETTINGS_PATH
    _cfg.SETTINGS_PATH = box / "kiosk_settings.json"
    try:
        check("no settings file yet reads as empty", _cfg.load_settings() == {})
        _cfg.save_setting("camera", "Logi C615 HD WebCam")
        check(
            "the picked camera is written and read back",
            _cfg.load_settings().get("camera") == "Logi C615 HD WebCam",
            str(_cfg.load_settings()),
        )
        _cfg.save_setting("camera", "USB2.0 camera")
        check("re-picking overwrites rather than appends",
              _cfg.load_settings() == {"camera": "USB2.0 camera"},
              str(_cfg.load_settings()))
    finally:
        _cfg.SETTINGS_PATH = _orig_settings

    # --- frame pipe reading, with no camera present ----------------- #
    # This runs on the Windows CI runner, which has no camera. It stands in a
    # plain subprocess for ffmpeg so the real pipe-reading path is exercised
    # per platform: select() accepts sockets only on Windows, and waiting on a
    # pipe with it raised WinError 10038 on real hardware while every
    # camera-free check here still passed.
    from .camera import FFmpegCamera
    from .config import FROZEN

    if FROZEN:
        check("frame pipe read (skipped: sys.executable is the bundled exe)", True)
    else:
        _w, _h, _n = 4, 2, 3
        producer = subprocess.Popen(
            [sys.executable, "-c",
             "import sys\n"
             f"for i in range({_n}):\n"
             f"    sys.stdout.buffer.write(bytes([i + 1]) * {_w * _h * 3})\n"
             "    sys.stdout.buffer.flush()\n"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        fake = FFmpegCamera("fake", width=_w, height=_h, fps=1)
        fake._proc = producer
        fake._frame_thread = threading.Thread(target=fake._pump_frames, daemon=True)
        fake._frame_thread.start()

        first = fake.read(timeout=10.0)
        check(
            "reads a whole frame off the pipe (the WinError 10038 path)",
            first is not None and first.shape == (_h, _w, 3),
            f"got {None if first is None else first.shape}",
        )
        # The kiosk draws boxes and labels straight onto the frame, so a
        # read-only array (np.frombuffer over bytes) kills the camera thread
        # the moment a face is detected.
        check(
            "the frame is writable — overlays are drawn onto it in place",
            first is not None and first.flags.writeable,
            f"writeable={None if first is None else first.flags.writeable}",
        )
        deadline = time.time() + 10
        while not fake._eof and time.time() < deadline:
            if fake.read(timeout=1.0) is None and fake._eof:
                break
        check("read() reports EOF instead of hanging when the producer exits",
              fake.read(timeout=1.0) is None)
        producer.wait(timeout=5)

    # --- web app routes (camera intentionally absent) --------------- #
    try:
        from fastapi.testclient import TestClient

        from . import app as app_mod

        app_mod._SETTINGS.camera = "no such camera"
        # A PIN via env var short-circuits _admin_pin() before it ever touches
        # disk — admin.pin's path is derived from config.DATA_DIR at app.py's
        # own import time, which the sandbox above doesn't reach (it only
        # rebinds the copies gallery.py/store.py/run.py already hold).
        os.environ["FACEKIOSK_ADMIN_PIN"] = "135790"
        with TestClient(app_mod.create_app()) as client:
            codes = {p: client.get(p).status_code for p in ("/", "/api/status", "/api/candidate")}
            check("kiosk routes serve without a camera", all(v == 200 for v in codes.values()), str(codes))
            check("web app reports the camera failure", client.get("/api/status").json()["camera_ok"] is False)
            check("no candidate when nobody is on camera", client.get("/api/candidate").json()["candidate"] is None)
            check("stamp with no candidate is refused", client.post("/api/stamp", json={"direction": "in"}).status_code == 409)

            # --- admin pages/APIs are gated until a PIN session exists ---- #
            check("admin index without a session redirects to login",
                  client.get("/admin/log", follow_redirects=False).status_code == 302)
            check("admin register without a session redirects to login",
                  client.get("/admin/register", follow_redirects=False).status_code == 302)
            check("admin API without a session is refused",
                  client.get("/api/roster").status_code == 401)
            check("camera list is public — the kiosk has its own picker too",
                  client.get("/api/cameras").status_code == 200)
            check("camera switch is public",
                  client.post("/api/camera", json={"name": "no such camera"}).status_code == 200)
            check("wrong PIN is refused",
                  client.post("/admin/login", data={"pin": "000000", "next": "/admin/log"},
                              follow_redirects=False).status_code == 401)

            login = client.post("/admin/login", data={"pin": "135790", "next": "/admin/log"},
                                 follow_redirects=False)
            check("correct PIN starts an admin session", login.status_code == 302 and "fk_admin" in client.cookies)

            check("admin pages now load", client.get("/admin/log").status_code == 200)
            check("admin roster API now works", client.get("/api/roster").status_code == 200)

            cams = client.get("/api/cameras").json()
            check("camera list is by NAME, not index",
                  "cameras" in cams and cams.get("current") == "no such camera"
                  and all("name" in c and "index" not in c for c in cams["cameras"]), str(cams))
            check("switching to an absent camera is accepted (async)",
                  client.post("/api/camera", json={"name": "Nonexistent Cam"}).status_code == 200)
            check("camera switch requires a name", client.post("/api/camera", json={}).status_code == 422)
            check("empty camera name rejects", client.post("/api/camera", json={"name": "  "}).status_code == 422)
            time.sleep(0.5)  # let the camera thread act on the switch request
            check("camera thread survives a bad switch", client.get("/api/status").status_code == 200)

            client.post("/admin/logout")
            check("logout ends the admin session", client.get("/api/roster").status_code == 401)
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
