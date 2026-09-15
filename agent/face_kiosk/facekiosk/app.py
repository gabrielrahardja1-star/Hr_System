"""Standalone kiosk web app.

    python -m facekiosk.app --camera "HD Webcam C615"
    open http://localhost:8770

Two audiences, two surfaces:
  /        the kiosk — fullscreen, no chrome, nothing an employee can break.
           Live camera + Check In/Out. Reachable by anyone standing at the device.
  /admin/* Log (today's roll-up, corrections, export) and Register (enrol a
           face) — behind a PIN. Anyone who can reach the wall screen should
           NOT be one click from enrolling a face or deleting someone's day.

(JSON API under /api/* backs both; admin-only endpoints require the same PIN
session as the /admin pages.)

One background thread owns the camera and runs the recognition pipeline; every
confirmed sighting goes to data/sightings.db. This app does NOT talk to HQ.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io as _io
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .camera import FFmpegCamera, list_cameras, open_camera, robust_read
from .config import DATA_DIR, T
from .gallery import Gallery
from .run import Kiosk
from .store import SightingStore
from .tracker import IOUTracker

WEB_DIR = Path(__file__).parent / "web"
MIN_SHOTS = 4
MAX_SHOTS = 12

# Set by main() before the server starts.
_SETTINGS = SimpleNamespace(camera="", match_cosine=T.match_cosine, liveness=True, auto_log=False)


# --------------------------------------------------------------------------- #
# Admin PIN gate — the kiosk (/) stays open to anyone; /admin/* does not.      #
# --------------------------------------------------------------------------- #

ADMIN_COOKIE = "fk_admin"
ADMIN_SESSION_MAX_AGE = 12 * 3600  # a shift, roughly
PIN_PATH = DATA_DIR / "admin.pin"

_admin_sessions: set[str] = set()
_admin_pin_cache: str | None = None


def _admin_pin() -> str:
    global _admin_pin_cache
    if _admin_pin_cache is not None:
        return _admin_pin_cache
    env_pin = os.environ.get("FACEKIOSK_ADMIN_PIN", "").strip()
    if env_pin:
        _admin_pin_cache = env_pin
        return env_pin
    if PIN_PATH.exists():
        _admin_pin_cache = PIN_PATH.read_text(encoding="utf-8").strip()
        return _admin_pin_cache
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pin = f"{secrets.randbelow(1_000_000):06d}"
    PIN_PATH.write_text(pin, encoding="utf-8")
    os.chmod(PIN_PATH, 0o600)
    print(f"! admin PIN generated: {pin}  (saved to {PIN_PATH}; set FACEKIOSK_ADMIN_PIN to override)")
    _admin_pin_cache = pin
    return pin


def _is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE)
    return bool(token) and token in _admin_sessions


def _require_admin_page(request: Request) -> RedirectResponse | None:
    """Use in a page route: `if (r := _require_admin_page(request)): return r`."""
    if _is_admin(request):
        return None
    return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=302)


def admin_required(request: Request) -> None:
    """Use as a Depends() on admin-only JSON endpoints."""
    if not _is_admin(request):
        raise HTTPException(401, "admin login required")


# --------------------------------------------------------------------------- #
# Camera + recognition, on its own thread                                      #
# --------------------------------------------------------------------------- #


class CameraWorker(threading.Thread):
    def __init__(self, camera: str, match_cosine: float, liveness: bool, auto_log: bool = False) -> None:
        super().__init__(name="camera-worker", daemon=True)
        self.camera = camera          # device NAME, never a positional index
        self.gallery = Gallery()
        self.store = SightingStore()
        self.gallery_lock = threading.RLock()
        self.engine_lock = threading.Lock()
        self._stop = threading.Event()
        self._latest_jpeg: bytes | None = None
        self._latest_raw: np.ndarray | None = None
        self._raw_lock = threading.Lock()
        self._pending: dict[str, list[np.ndarray]] = {}
        self._pending_lock = threading.Lock()
        self.fps = 0.0
        self.error: str | None = None
        self.frame_size: tuple[int, int] | None = None   # (h, w) of the last good frame
        self._requested_camera: str | None = None
        self.started_ok = threading.Event()

        args = SimpleNamespace(
            camera=camera,
            conf_thres=T.detect_score,
            match_cosine=match_cosine,
            liveness=liveness,
            no_window=True,
            seconds=0,
        )
        self.kiosk = Kiosk(
            args, on_event=self.store.record, gallery=self.gallery, auto_log=auto_log, debug_overlay=False
        )

    # --- thread body -------------------------------------------------- #

    def _open(self, name: str) -> FFmpegCamera | None:
        try:
            cap = open_camera(name)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return None
        self.camera = cap.name
        self.error = None
        self.frame_size = None
        self._latest_jpeg = None
        with self.engine_lock:
            self.kiosk.tracker = IOUTracker()   # drop tracks from the old feed
        return cap

    RECONNECT_INTERVAL_S = 2.0

    def run(self) -> None:
        cap = self._open(self.camera)
        self.started_ok.set()          # started_ok = "the thread is alive and took its shot",
        times: deque[float] = deque(maxlen=30)  # not "the camera is up" — check .error/.camera_ok
        last_reconnect_attempt = 0.0
        try:
            while not self._stop.is_set():
                if self._requested_camera is not None:
                    new_name, self._requested_camera = self._requested_camera, None
                    new_cap = self._open(new_name)
                    if new_cap is not None:
                        if cap is not None:
                            cap.release()
                        cap = new_cap
                        times.clear()
                    # on failure self.error is set; keep the old cap running if we had one

                if cap is None:
                    # Keep retrying the camera that was actually chosen. It must
                    # never quietly fall back to a different device: the C615
                    # drops frames often, and substituting whatever else answers
                    # meant an explicit pick silently became the built-in camera
                    # and stayed there. On an attendance kiosk, recording people
                    # from an unintended camera is worse than showing "down".
                    now = time.monotonic()
                    if now - last_reconnect_attempt >= self.RECONNECT_INTERVAL_S:
                        last_reconnect_attempt = now
                        cap = self._open(self.camera)
                        if cap is not None:
                            times.clear()
                            continue
                    time.sleep(0.3)
                    continue

                t0 = time.monotonic()
                frame = robust_read(cap)
                if frame is None:
                    self.error = "camera stopped responding"
                    self._latest_jpeg = None
                    cap.release()
                    cap = None
                    # Otherwise whoever was mid-recognition when the camera
                    # dropped (e.g. mid liveness-turn) stays frozen in
                    # /api/candidate — the kiosk showed stale text like
                    # "Turn your head" right underneath "Camera down".
                    with self.engine_lock:
                        self.kiosk.tracker = IOUTracker()
                    continue
                # A successful read here means frames are genuinely flowing —
                # clear any stale error (e.g. a failed switch attempt that
                # left the OLD camera running fine) instead of leaving
                # camera_ok stuck false, and the "Camera down" overlay stuck
                # covering the picker, forever after one bad switch.
                self.error = None
                self.frame_size = frame.shape[:2]
                with self._raw_lock:
                    self._latest_raw = frame.copy()
                with self.engine_lock, self.gallery_lock:
                    self.kiosk.process(frame)  # draws overlays onto `frame`
                # Mirror the DISPLAY copy only, after detection/overlays: aiming
                # your own face at a feed that doesn't mirror is disorienting,
                # worse so mid-liveness-turn. Box positions mirror correctly
                # because the flip applies to the whole annotated frame at once.
                frame = cv2.flip(frame, 1)
                ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok:
                    self._latest_jpeg = buf.tobytes()
                times.append(time.monotonic() - t0)
                self.fps = len(times) / max(sum(times), 1e-6)
        finally:
            if cap is not None:
                cap.release()

    def stop(self) -> None:
        self._stop.set()

    def switch_camera(self, name: str) -> None:
        """Ask the camera thread to switch devices, by name. Async — poll
        status()/error afterwards to see whether it took."""
        self._requested_camera = name

    def cameras(self) -> dict:
        """What's available to switch to, plus the one currently in use.

        Deliberately does NOT attach a device name to an index. system_profiler
        and OpenCV's AVFoundation backend enumerate cameras in *different*
        orders — verified on this machine, where system_profiler listed
        (C615, MacBook) while OpenCV's index 0 was the built-in camera and
        index 1 the C615. Zipping the two lists by position, which this used
        to do, mislabels every device and sends you to the wrong camera.
        Cameras are addressed by name instead, so nothing is ever translated.
        """
        names = list_cameras()
        if self.camera and self.camera not in names:
            names.append(self.camera)     # chosen device currently unplugged
        return {
            "cameras": [
                {"name": n, "active": n == self.camera and self.error is None} for n in names
            ],
            "current": self.camera,
        }

    # --- streaming -------------------------------------------------- #

    def mjpeg(self):
        while not self._stop.is_set():
            jpg = self._latest_jpeg
            if jpg is not None:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                )
            time.sleep(0.05)

    # --- enrollment ------------------------------------------------ #

    def capture(self, token: str | None) -> tuple[str, int]:
        token = token or _new_token()
        with self._raw_lock:
            frame = None if self._latest_raw is None else self._latest_raw.copy()
        if frame is None:
            raise HTTPException(503, "no camera frame yet — is the webcam working?")
        with self.engine_lock:
            faces = [f for f in self.kiosk.engine.detect(frame) if f.size >= T.min_face_px]
            if len(faces) != 1:
                raise HTTPException(
                    422,
                    f"need exactly one clear, close face — saw {len(faces)}. "
                    "Move closer, face the camera, one person only.",
                )
            emb = self.kiosk.engine.embed(frame, faces[0])
        with self._pending_lock:
            shots = self._pending.setdefault(token, [])
            if len(shots) >= MAX_SHOTS:
                raise HTTPException(409, f"already have {MAX_SHOTS} shots — save or cancel")
            shots.append(emb)
            count = len(shots)
        return token, count

    def delete_shot(self, token: str, index: int) -> int:
        with self._pending_lock:
            shots = self._pending.get(token)
            if not shots or not (0 <= index < len(shots)):
                raise HTTPException(404, "no such shot")
            shots.pop(index)
            return len(shots)

    def enroll(self, token: str, name: str, emp_id: str) -> dict:
        with self._pending_lock:
            shots = self._pending.pop(token, None)
        if not shots or len(shots) < MIN_SHOTS:
            raise HTTPException(422, f"need at least {MIN_SHOTS} shots, have {len(shots or [])}")
        with self.gallery_lock:
            uid = emp_id.strip() or _next_uid(self.gallery)
            if emp_id.strip() and emp_id.strip() in self.gallery.people:
                raise HTTPException(409, f"ID {emp_id!r} is already enrolled")
            person = self.gallery.enroll(uid, name.strip(), shots, emp_id=emp_id.strip())
            self.gallery.save()
        return person.as_dict()

    def cancel(self, token: str) -> None:
        with self._pending_lock:
            self._pending.pop(token, None)

    def forget(self, uid: str) -> dict:
        with self.gallery_lock:
            removed = self.gallery.remove(uid)
            if removed:
                self.gallery.save()
        rows = self.store.forget_person(uid)
        if not removed:
            raise HTTPException(404, f"{uid} not enrolled")
        return {"uid": uid, "sightings_deleted": rows}

    # --- manual capture (Check In / Check Out) --------------------- #

    def candidate(self) -> dict:
        with self.engine_lock:
            cand = self.kiosk.current_candidate()
            hint = self.kiosk.frontmost()
        if cand:
            last = self.store.last_stamp(cand["uid"])
            last_dir = last["direction"] if last else None
            cand["expected"] = "out" if last_dir == "in" else "in"
            cand["last_action_dir"] = last_dir
            cand["last_action_ts"] = last["seen_at"] if last else None
        return {"candidate": cand, "hint": hint}

    def stamp(self, direction: str) -> dict:
        if direction not in ("in", "out"):
            raise HTTPException(422, "direction must be 'in' or 'out'")
        with self.engine_lock, self.gallery_lock:
            record = self.kiosk.capture(direction)
        if record is None:
            raise HTTPException(409, "no one is recognised right now — step up to the camera")
        return record

    def retry(self) -> dict:
        with self.engine_lock:
            ok = self.kiosk.retry()
        return {"ok": ok}

    def void_last(self, uid: str, date: str | None) -> dict:
        if not self.store.void_last(uid, date):
            raise HTTPException(404, "no sighting to void")
        return {"ok": True}

    def status(self) -> dict:
        return {
            "camera": self.camera,
            "camera_ok": self.error is None and self._latest_jpeg is not None,
            "error": self.error,
            "fps": round(self.fps, 1),
            "enrolled": len(self.gallery),
            "liveness": self.kiosk.args.liveness,
            "auto_log": self.kiosk.auto_log,
            "match_cosine": self.kiosk.args.match_cosine,
        }


def _new_token() -> str:
    return dt.datetime.now().strftime("%H%M%S") + "-" + str(int(time.monotonic() * 1000) % 100000)


def _next_uid(gallery: Gallery) -> str:
    n = 0
    for uid in gallery.people:
        if uid.startswith("K") and uid[1:].isdigit():
            n = max(n, int(uid[1:]))
    return f"K{n + 1:04d}"


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def create_app() -> FastAPI:
    worker = CameraWorker(
        _SETTINGS.camera, _SETTINGS.match_cosine, _SETTINGS.liveness, _SETTINGS.auto_log
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker.start()
        worker.started_ok.wait(timeout=10)
        if worker.error:
            print(f"! camera did not start: {worker.error}")
        _admin_pin()  # print/generate the PIN up front, not on first login attempt
        yield
        worker.stop()

    app = FastAPI(title="Face Kiosk", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def _today(date: str | None) -> str:
        return date or dt.date.today().isoformat()

    # --- the kiosk (public, no chrome) -------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def kiosk_page(request: Request, debug: str | None = None):
        worker.kiosk.debug_overlay = debug == "1"
        return templates.TemplateResponse(request, "kiosk.html", {})

    @app.get("/stream")
    def stream():
        return StreamingResponse(
            worker.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @app.get("/api/status")
    def api_status():
        return worker.status()

    @app.get("/api/candidate")
    def api_candidate():
        return worker.candidate()

    @app.post("/api/stamp")
    def api_stamp(body: dict = Body(...)):
        record = worker.stamp((body.get("direction") or "").strip().lower())
        return {"ok": True, "record": record}

    @app.post("/api/retry")
    def api_retry():
        return worker.retry()

    # Camera selection is public — the kiosk itself carries a picker (by
    # request) alongside the admin one, since switching devices isn't a
    # data-mutating action the way enroll/forget/void are.
    @app.get("/api/cameras")
    def api_cameras():
        return worker.cameras()

    @app.post("/api/camera")
    def api_camera(body: dict = Body(...)):
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(422, "name (str) is required")
        worker.switch_camera(name.strip())
        return {"ok": True}

    # --- admin: login / logout ----------------------------------------- #

    @app.get("/admin", response_class=HTMLResponse)
    def admin_index(request: Request):
        target = "/admin/log" if _is_admin(request) else "/admin/login"
        return RedirectResponse(target, status_code=302)

    @app.get("/admin/login", response_class=HTMLResponse)
    def admin_login_page(request: Request, next: str = "/admin/log"):
        if _is_admin(request):
            return RedirectResponse(next)
        return templates.TemplateResponse(request, "admin_login.html", {"next": next, "error": None})

    @app.post("/admin/login", response_class=HTMLResponse)
    async def admin_login_submit(request: Request):
        form = await request.form()
        pin = str(form.get("pin") or "").strip()
        next_url = str(form.get("next") or "/admin/log")
        if not secrets.compare_digest(pin, _admin_pin()):
            return templates.TemplateResponse(
                request, "admin_login.html", {"next": next_url, "error": "Wrong PIN."}, status_code=401
            )
        token = secrets.token_urlsafe(32)
        _admin_sessions.add(token)
        resp = RedirectResponse(next_url, status_code=302)
        resp.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_SESSION_MAX_AGE, httponly=True, samesite="lax")
        return resp

    @app.post("/admin/logout")
    def admin_logout(request: Request):
        token = request.cookies.get(ADMIN_COOKIE)
        _admin_sessions.discard(token)
        resp = RedirectResponse("/admin/login", status_code=302)
        resp.delete_cookie(ADMIN_COOKIE)
        return resp

    # --- admin: pages ---------------------------------------------------- #

    @app.get("/admin/log", response_class=HTMLResponse)
    def log_page(request: Request, date: str | None = None):
        if (redirect := _require_admin_page(request)) is not None:
            return redirect
        day = _today(date)
        return templates.TemplateResponse(
            request,
            "log.html",
            {
                "day": day,
                "is_today": day == dt.date.today().isoformat(),
                "roster": worker.store.roster_for_date(day),
                "days": worker.store.recent_days(),
                "people": [p.as_dict() for p in worker.gallery.people.values()],
                "status": worker.status(),
            },
        )

    @app.get("/admin/register", response_class=HTMLResponse)
    def register_page(request: Request):
        if (redirect := _require_admin_page(request)) is not None:
            return redirect
        return templates.TemplateResponse(
            request,
            "register.html",
            {"status": worker.status(), "min_shots": MIN_SHOTS, "max_shots": MAX_SHOTS},
        )

    # --- admin: JSON API --------------------------------------------- #

    @app.get("/api/roster", dependencies=[Depends(admin_required)])
    def api_roster(date: str | None = None):
        day = _today(date)
        return {"date": day, "roster": worker.store.roster_for_date(day)}

    @app.post("/api/roster/void", dependencies=[Depends(admin_required)])
    def api_roster_void(body: dict = Body(...)):
        uid = str(body.get("uid") or "").strip()
        if not uid:
            raise HTTPException(422, "uid is required")
        return worker.void_last(uid, body.get("date"))

    @app.post("/api/sync", dependencies=[Depends(admin_required)])
    def api_sync():
        from . import sync

        return sync.run_sync(worker.store)

    @app.get("/api/export.csv", dependencies=[Depends(admin_required)])
    def api_export_csv(date: str | None = None):
        day = _today(date)
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "name", "id", "check_in", "check_out", "hours", "records"])
        for r in worker.store.roster_for_date(day):
            w.writerow([
                day, r["name"], r.get("emp_id") or r["uid"],
                r.get("check_in") or "", r.get("check_out") or "",
                r["hours"] if r.get("hours") is not None else "", r["sightings"],
            ])
        return PlainTextResponse(
            buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="attendance-{day}.csv"'},
        )

    @app.get("/api/sightings", dependencies=[Depends(admin_required)])
    def api_sightings(date: str | None = None):
        day = _today(date)
        return {"date": day, "sightings": worker.store.on_date(day)}

    @app.get("/api/people", dependencies=[Depends(admin_required)])
    def api_people():
        return {"people": [p.as_dict() for p in worker.gallery.people.values()]}

    @app.post("/api/capture", dependencies=[Depends(admin_required)])
    def api_capture(body: dict = Body(default={})):
        token, shots = worker.capture(body.get("token"))
        return {"token": token, "shots": shots}

    @app.post("/api/capture/delete", dependencies=[Depends(admin_required)])
    def api_capture_delete(body: dict = Body(...)):
        index = body.get("index")
        if not isinstance(index, int):
            raise HTTPException(422, "index (int) is required")
        shots = worker.delete_shot(str(body.get("token") or ""), index)
        return {"shots": shots}

    @app.post("/api/enroll", dependencies=[Depends(admin_required)])
    def api_enroll(body: dict = Body(...)):
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(422, "name is required")
        person = worker.enroll(body.get("token", ""), name, body.get("emp_id", ""))
        return {"ok": True, "person": person}

    @app.post("/api/enroll/cancel", dependencies=[Depends(admin_required)])
    def api_cancel(body: dict = Body(default={})):
        worker.cancel(body.get("token", ""))
        return {"ok": True}

    @app.post("/api/people/{uid}/forget", dependencies=[Depends(admin_required)])
    def api_forget(uid: str):
        return worker.forget(uid)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--camera",
        default="",
        help='camera NAME, e.g. --camera "HD Webcam C615" (see python -m facekiosk.camera). '
             "Empty takes the first connected camera.",
    )
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--match-cosine", type=float, default=T.match_cosine)
    ap.add_argument("--no-liveness", dest="liveness", action="store_false")
    ap.add_argument(
        "--auto-log",
        action="store_true",
        help="also log automatically on recognition (default: only a Check In/Out tap logs)",
    )
    args = ap.parse_args(argv)

    _SETTINGS.camera = args.camera
    _SETTINGS.match_cosine = args.match_cosine
    _SETTINGS.liveness = args.liveness
    _SETTINGS.auto_log = args.auto_log

    print(f"face kiosk on http://{args.host}:{args.port}  (camera {args.camera or 'first available'})")
    print(f"admin: http://{args.host}:{args.port}/admin")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
