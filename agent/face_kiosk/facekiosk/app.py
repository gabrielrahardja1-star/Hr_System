"""Standalone kiosk web app.

    python -m facekiosk.app --camera 1
    open http://localhost:8770

Three pages:
  /           dashboard — live camera, today's roll-up (first seen / last seen),
              day picker, enrolled people
  /register   capture a few webcam shots, give a name, save
  (JSON API under /api/* backs both)

One background thread owns the camera and runs the recognition pipeline; every
confirmed sighting goes to data/sightings.db. This app does NOT talk to HQ.
"""

from __future__ import annotations

import argparse
import datetime as dt
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .camera import open_camera, robust_read
from .config import T
from .gallery import Gallery
from .run import Kiosk
from .store import SightingStore

WEB_DIR = Path(__file__).parent / "web"
MIN_SHOTS = 4
MAX_SHOTS = 12

# Set by main() before the server starts.
_SETTINGS = SimpleNamespace(camera=0, match_cosine=T.match_cosine, liveness=True, auto_log=False)


# --------------------------------------------------------------------------- #
# Camera + recognition, on its own thread                                      #
# --------------------------------------------------------------------------- #


class CameraWorker(threading.Thread):
    def __init__(self, camera: int, match_cosine: float, liveness: bool, auto_log: bool = False) -> None:
        super().__init__(name="camera-worker", daemon=True)
        self.camera = camera
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
        self.started_ok = threading.Event()

        args = SimpleNamespace(
            camera=camera,
            conf_thres=T.detect_score,
            match_cosine=match_cosine,
            liveness=liveness,
            no_window=True,
            seconds=0,
        )
        self.kiosk = Kiosk(args, on_event=self.store.record, gallery=self.gallery, auto_log=auto_log)

    # --- thread body -------------------------------------------------- #

    def run(self) -> None:
        try:
            cap = open_camera(self.camera)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.started_ok.set()
            return
        self.started_ok.set()
        times: deque[float] = deque(maxlen=30)
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                frame = robust_read(cap)
                if frame is None:
                    self.error = "camera stopped responding"
                    break
                with self._raw_lock:
                    self._latest_raw = frame.copy()
                with self.engine_lock, self.gallery_lock:
                    self.kiosk.process(frame)  # draws overlays onto `frame`
                ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok:
                    self._latest_jpeg = buf.tobytes()
                times.append(time.monotonic() - t0)
                self.fps = len(times) / max(sum(times), 1e-6)
        finally:
            cap.release()

    def stop(self) -> None:
        self._stop.set()

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

    def candidate(self) -> dict | None:
        with self.engine_lock:
            return self.kiosk.current_candidate()

    def stamp(self, direction: str) -> dict:
        if direction not in ("in", "out"):
            raise HTTPException(422, "direction must be 'in' or 'out'")
        with self.engine_lock, self.gallery_lock:
            record = self.kiosk.capture(direction)
        if record is None:
            raise HTTPException(409, "no one is recognised right now — step up to the camera")
        return record

    def status(self) -> dict:
        return {
            "camera_index": self.camera,
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
        yield
        worker.stop()

    app = FastAPI(title="Face Kiosk", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def _today(date: str | None) -> str:
        return date or dt.date.today().isoformat()

    @app.get("/", response_class=HTMLResponse)
    def kiosk_page(request: Request):
        return templates.TemplateResponse(request, "kiosk.html", {"status": worker.status()})

    @app.get("/log", response_class=HTMLResponse)
    def log_page(request: Request, date: str | None = None):
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

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        return templates.TemplateResponse(
            request,
            "register.html",
            {"status": worker.status(), "min_shots": MIN_SHOTS},
        )

    @app.get("/stream")
    def stream():
        return StreamingResponse(
            worker.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @app.get("/api/status")
    def api_status():
        return worker.status()

    @app.get("/api/roster")
    def api_roster(date: str | None = None):
        day = _today(date)
        return {"date": day, "roster": worker.store.roster_for_date(day)}

    @app.get("/api/sightings")
    def api_sightings(date: str | None = None):
        day = _today(date)
        return {"date": day, "sightings": worker.store.on_date(day)}

    @app.get("/api/people")
    def api_people():
        return {"people": [p.as_dict() for p in worker.gallery.people.values()]}

    @app.get("/api/candidate")
    def api_candidate():
        return {"candidate": worker.candidate()}

    @app.post("/api/stamp")
    def api_stamp(body: dict = Body(...)):
        record = worker.stamp((body.get("direction") or "").strip().lower())
        return {"ok": True, "record": record}

    @app.post("/api/capture")
    def api_capture(body: dict = Body(default={})):
        token, shots = worker.capture(body.get("token"))
        return {"token": token, "shots": shots}

    @app.post("/api/enroll")
    def api_enroll(body: dict = Body(...)):
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(422, "name is required")
        person = worker.enroll(body.get("token", ""), name, body.get("emp_id", ""))
        return {"ok": True, "person": person}

    @app.post("/api/enroll/cancel")
    def api_cancel(body: dict = Body(default={})):
        worker.cancel(body.get("token", ""))
        return {"ok": True}

    @app.post("/api/people/{uid}/forget")
    def api_forget(uid: str):
        return worker.forget(uid)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0)
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

    print(f"face kiosk on http://{args.host}:{args.port}  (camera {args.camera})")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
