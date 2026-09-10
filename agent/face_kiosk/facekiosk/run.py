"""The prototype recognition loop.

    python -m facekiosk.run --camera 1
    python -m facekiosk.run --camera 1 --no-liveness --match-cosine 0.42
    python -m facekiosk.run --camera 1 --no-window --seconds 60      # headless

For each face it tracks across frames, votes on the identity, then (unless
--no-liveness) runs a randomized head-turn challenge before writing one event to
data/events.jsonl and printing a line. Same person again within the debounce
window is ignored. Press Q in the window, or Ctrl-C headless, to stop.

This does NOT talk to HQ yet. Each event line is already shaped like a punch;
Phase 2 maps it to PunchIn and POSTs a batch to /api/v1/punches.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sys
import time
from collections import deque

import cv2

from . import viz
from .camera import open_camera, robust_read
from .config import EVENT_LOG, DATA_DIR, SOURCE_TAG, T
from .engine import FaceEngine
from .gallery import Gallery
from .liveness import Challenge
from .tracker import IOUTracker, Track


def _now_local() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


class Kiosk:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.engine = FaceEngine(detect_score=args.conf_thres)
        self.gallery = Gallery()
        self.tracker = IOUTracker()
        self.debounce: dict[str, float] = {}     # uid -> monotonic time of last event
        self.events = 0
        if not self.gallery.people:
            print("! gallery is empty — enrol someone first (python -m facekiosk.enroll)", file=sys.stderr)

    # --- event sink ---------------------------------------------------- #

    def _emit(self, track: Track, uid: str, name: str, similarity: float, liveness: str) -> None:
        mono = time.monotonic()
        last = self.debounce.get(uid)
        if last is not None and mono - last < T.debounce_seconds:
            track.stage = "committed"
            track.committed_uid = uid
            track.greet_until = mono + 1.5
            track.identity = (uid, f"{name} (already in)")
            return

        self.debounce[uid] = mono
        self.events += 1
        record = {
            "ts": _now_local().isoformat(timespec="seconds"),
            "device_user_id": uid,
            "name": name,
            "similarity": round(similarity, 4),
            "liveness": liveness,
            "track_id": track.id,
            "source": SOURCE_TAG,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(
            f"[{record['ts']}]  {name:<24} uid={uid:<8} "
            f"sim={similarity:.3f} liveness={liveness}"
        )
        track.stage = "committed"
        track.committed_uid = uid
        track.identity = (uid, name)
        track.greet_until = mono + 3.0

    # --- per-track state machine ------------------------------------- #

    def _step_track(self, track: Track, frame) -> None:
        face = track.face
        stage = track.stage

        if stage == "recognizing":
            emb = self.engine.embed(frame, face)
            match = self.gallery.identify(emb, self.args.match_cosine)
            track.vote(match.uid if match.ok else None)
            track.best_similarity = max(track.best_similarity, match.similarity)

            leader_uid, count = track.leader()
            label = self.gallery.people[leader_uid].name if leader_uid else "?"
            viz.draw_box(frame, face.box, viz.AMBER, f"{label}  {match.similarity:.2f}")
            _vote_bar(frame, face.box, count)

            if track.vote_settled() and leader_uid:
                name = self.gallery.people[leader_uid].name
                track.identity = (leader_uid, name)
                if self.args.liveness:
                    track.stage = "awaiting_liveness"
                    track.challenge = Challenge()
                else:
                    self._emit(track, leader_uid, name, track.best_similarity, "skipped")

        elif stage == "awaiting_liveness":
            uid, name = track.identity
            result = track.challenge.update(face)
            viz.draw_box(frame, face.box, viz.AMBER, name)
            viz.banner(frame, track.challenge.prompt, viz.AMBER)
            viz.progress_bar(frame, track.challenge.progress, viz.AMBER)
            if result == "pass":
                self._emit(track, uid, name, track.best_similarity, "pass")
            elif result == "timeout":
                track.stage = "recognizing"
                track.votes.clear()
                track.challenge = None

        elif stage == "committed":
            uid, name = track.identity
            done = time.monotonic() > track.greet_until
            color = viz.GREY if done else viz.GREEN
            tick = "" if done else "  OK"
            viz.draw_box(frame, face.box, color, f"{name}{tick}")

    # --- main loop -------------------------------------------------- #

    def process(self, frame) -> list:
        """Run one frame through the whole pipeline; returns the faces detected
        this frame. Camera-free entry point — used by the loop and by selftest."""
        faces = [f for f in self.engine.detect(frame) if f.size >= T.min_face_px]
        for track, _face in self.tracker.update(faces):
            self._step_track(track, frame)
        return faces

    def run(self, frames=None) -> int:
        cap = open_camera(self.args.camera) if frames is None else None
        frames = iter(frames) if frames is not None else None
        show = not self.args.no_window and cap is not None
        stop_at = time.monotonic() + self.args.seconds if self.args.seconds else None
        frame_times: deque[float] = deque(maxlen=30)

        stopping = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stopping.update(v=True))
        print(f"kiosk running — {len(self.gallery)} enrolled, "
              f"liveness={'on' if self.args.liveness else 'OFF'}, "
              f"{'window' if show else 'headless'}. Ctrl-C to stop.")

        try:
            while not stopping["v"]:
                t0 = time.monotonic()
                frame = robust_read(cap) if cap is not None else next(frames, None)
                if frame is None:
                    if cap is not None:
                        print("camera stopped responding", file=sys.stderr)
                    break

                faces = self.process(frame)

                frame_times.append(time.monotonic() - t0)
                if show:
                    fps = len(frame_times) / max(sum(frame_times), 1e-6)
                    viz.banner(
                        frame,
                        f"{len(self.gallery)} enrolled   {len(faces)} face(s)   {self.events} events",
                        viz.WHITE,
                        top=False,
                    )
                    viz.fps_tag(frame, fps)
                    cv2.imshow("facekiosk", frame)
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        break

                if stop_at and time.monotonic() > stop_at:
                    break
        finally:
            if cap is not None:
                cap.release()
            cv2.destroyAllWindows()

        print(f"stopped — {self.events} event(s) written to {EVENT_LOG}")
        return 0


def _vote_bar(frame, box, count: int) -> None:
    x, y, w, h = box
    frac = min(1.0, count / T.vote_frames)
    cv2.rectangle(frame, (x, y + h + 4), (x + w, y + h + 9), viz.GREY, 1)
    cv2.rectangle(frame, (x, y + h + 4), (x + int(w * frac), y + h + 9), viz.GREEN, -1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--conf-thres", type=float, default=T.detect_score, help="YuNet face-box score cut")
    ap.add_argument("--match-cosine", type=float, default=T.match_cosine, help="same-identity cosine cut")
    ap.add_argument("--no-liveness", dest="liveness", action="store_false", help="skip the head-turn challenge")
    ap.add_argument("--no-window", action="store_true", help="headless — no preview window")
    ap.add_argument("--seconds", type=float, default=0.0, help="auto-stop after N seconds (0 = run until stopped)")
    args = ap.parse_args(argv)
    return Kiosk(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
