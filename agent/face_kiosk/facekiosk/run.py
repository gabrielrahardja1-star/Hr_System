"""The recognition pipeline (`Kiosk`) and a self-contained OpenCV-window scanner.

    python -m facekiosk.run --camera "HD Webcam C615"
    python -m facekiosk.run --camera "HD Webcam C615" --no-liveness --match-cosine 0.42
    python -m facekiosk.run --camera "HD Webcam C615" --no-window --seconds 60      # headless

For each face it tracks across frames, votes on the identity, then (unless
--no-liveness) runs a randomized head-turn challenge. What happens on a confirmed
recognition depends on the mode:
  auto-log (default here)  -> write an event immediately (direction "auto")
  --no-auto-log            -> park the person as a "ready" candidate; something
                             else (the web app's Check In / Check Out buttons)
                             calls Kiosk.capture(direction) to log it

Events go to data/events.jsonl (+ any on_event sink). This does NOT talk to HQ.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sys
import time
from collections import deque
from collections.abc import Callable

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
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        on_event: Callable[[dict], None] | None = None,
        gallery: Gallery | None = None,
        auto_log: bool = True,
        debug_overlay: bool = True,
    ) -> None:
        self.args = args
        self.engine = FaceEngine(detect_score=args.conf_thres)
        self.gallery = gallery if gallery is not None else Gallery()
        self.tracker = IOUTracker()
        self.overlays: list[tuple] = []
        self._building_overlays: list[tuple] = []
        self.debounce: dict[str, float] = {}     # uid -> monotonic time of last event
        self.events = 0
        self.auto_log = auto_log                 # False = a Check In/Out tap is the only logger
        # Text/numbers on the video are a tuning aid, not user feedback — off by
        # default for the web kiosk, on by default for the standalone `run.py`
        # window (which is inherently a developer tool). Toggle live via ?debug=1.
        self.debug_overlay = debug_overlay
        self._candidate_track_id: int | None = None
        self._on_event = on_event
        self._pass = "skipped" if not args.liveness else "pass"
        if not self.gallery.people:
            print("! gallery is empty — enrol someone first (register in the app, "
                  "or python -m facekiosk.enroll)", file=sys.stderr)

    # --- event sink ---------------------------------------------------- #

    def _write_event(
        self,
        track: Track,
        uid: str,
        name: str,
        similarity: float,
        liveness: str,
        direction: str,
        debounce_s: float,
    ) -> dict | None:
        """Log one attendance event. Returns the record, or None if it fell
        inside the debounce window for this person."""
        mono = time.monotonic()
        last = self.debounce.get(uid)
        if last is not None and mono - last < debounce_s:
            return None

        self.debounce[uid] = mono
        self.events += 1
        person = self.gallery.people.get(uid)
        record = {
            "ts": _now_local().isoformat(timespec="seconds"),
            "device_user_id": uid,
            "emp_id": person.emp_id if person else "",
            "name": name,
            "direction": direction,          # "in" | "out" | "auto"
            "similarity": round(similarity, 4),
            "liveness": liveness,
            "track_id": track.id,
            "source": SOURCE_TAG,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(
            f"[{record['ts']}]  {name:<22} {direction:<4} uid={uid:<8} "
            f"sim={similarity:.3f} liveness={liveness}"
        )
        if self._on_event is not None:
            try:
                self._on_event(record)
            except Exception as exc:  # noqa: BLE001 - a sink error must not kill the loop
                print(f"on_event sink failed: {exc}", file=sys.stderr)
        return record

    # --- manual capture (the Check In / Check Out buttons) ----------- #

    def _pick_candidate_track(self) -> Track | None:
        """The track the app can stamp right now: a live, recognised, liveness-
        cleared track that isn't inside its post-stamp debounce. Best match wins."""
        mono = time.monotonic()
        best: tuple[float, Track] | None = None
        for tr in self.tracker.tracks:
            if tr.stage != "ready" or tr.identity is None or tr.misses > 3:
                continue
            uid = tr.identity[0]
            last = self.debounce.get(uid)
            if last is not None and mono - last < T.capture_debounce_seconds:
                continue
            if best is None or tr.best_similarity > best[0]:
                best = (tr.best_similarity, tr)
        return best[1] if best else None

    def current_candidate(self) -> dict | None:
        track = self._pick_candidate_track()
        if track is None:
            return None
        uid, name = track.identity
        person = self.gallery.people.get(uid)
        return {
            "uid": uid,
            "name": name,
            "emp_id": person.emp_id if person else "",
            "similarity": round(track.best_similarity, 3),
            "track_id": track.id,
        }

    def frontmost(self) -> dict | None:
        """What the biggest face on camera is doing right now — for the kiosk
        readout (recognising / turn your head / ready / just logged / a dead end)."""
        tracks = [t for t in self.tracker.tracks if t.misses == 0]
        if not tracks:
            return None
        tr = max(tracks, key=lambda t: t.face.size)
        name = tr.identity[1] if tr.identity else None
        if tr.stage == "recognizing":
            return {"stage": "recognizing", "name": name, "prompt": "Hold still…", "progress": 0.0}
        if tr.stage == "unrecognized":
            return {
                "stage": "unrecognized",
                "name": None,
                "prompt": "Not recognised. See HR to enrol.",
                "progress": 0.0,
                "retryable": True,
            }
        if tr.stage == "awaiting_liveness":
            ch = tr.challenge
            return {
                "stage": "liveness",
                "name": name,
                "prompt": "Turn your head, then look back",
                "progress": round(ch.progress, 2) if ch else 0.0,
            }
        if tr.stage == "liveness_failed":
            return {
                "stage": "liveness_failed",
                "name": name,
                "prompt": "Didn't catch that — try again",
                "progress": 0.0,
                "retryable": True,
            }
        if tr.stage == "ready":
            return {"stage": "ready", "name": name, "prompt": "Tap Check In or Check Out", "progress": 1.0}
        if tr.stage == "committed":
            return {"stage": "done", "name": name, "prompt": getattr(tr, "greet_text", "Done"), "progress": 1.0}
        return None

    def retry(self) -> bool:
        """Force the frontmost dead-ended track (liveness_failed / unrecognized)
        back to recognizing right now, instead of waiting out its hold timer —
        the kiosk's "Try again" button."""
        tracks = [t for t in self.tracker.tracks if t.misses == 0]
        if not tracks:
            return False
        tr = max(tracks, key=lambda t: t.face.size)
        if tr.stage not in ("liveness_failed", "unrecognized"):
            return False
        tr.stage = "recognizing"
        tr.votes.clear()
        tr.identity = None
        tr.liveness_tries = 0
        tr.stage_until = 0.0
        return True

    def capture(self, direction: str) -> dict | None:
        """Stamp the current candidate with a direction. None if nobody is ready."""
        cand = self.current_candidate()
        if cand is None:
            return None
        track = next((t for t in self.tracker.tracks if t.id == cand["track_id"]), None)
        if track is None:
            return None
        record = self._write_event(
            track, cand["uid"], cand["name"], track.best_similarity,
            self._pass, direction, T.capture_debounce_seconds,
        )
        if record is not None:
            track.stage = "committed"
            track.greet_until = time.monotonic() + 3.0
            track.greet_text = f"{direction.upper()}  OK"
        return record

    # --- per-track state machine ------------------------------------- #

    def _confirm(self, track: Track, uid: str, name: str) -> None:
        """Track is recognised (and liveness-cleared). Either auto-log it or park
        it as a candidate for a Check In / Check Out tap."""
        if self.auto_log:
            self._write_event(
                track, uid, name, track.best_similarity, self._pass, "auto", T.debounce_seconds
            )
            track.stage = "committed"
            track.greet_until = time.monotonic() + 3.0
            track.greet_text = "OK"
        else:
            track.stage = "ready"

    def _step_track(self, track: Track, frame) -> None:
        face = track.face
        stage = track.stage
        dbg = self.debug_overlay

        if stage == "recognizing":
            emb = self.engine.embed(frame, face)
            match = self.gallery.identify(emb, self.args.match_cosine)
            track.vote(match.uid if match.ok else None)
            track.best_similarity = max(track.best_similarity, match.similarity)

            leader_uid, count = track.leader()
            label = ""
            if dbg:
                name_lbl = self.gallery.people[leader_uid].name if leader_uid else "?"
                label = f"{name_lbl}  {match.similarity:.2f}"
            self._box(frame, face.box, viz.AMBER, label)
            if dbg:
                _vote_bar(frame, face.box, count)

            if track.vote_settled() and leader_uid:
                name = self.gallery.people[leader_uid].name
                track.identity = (leader_uid, name)
                if self.args.liveness:
                    track.stage = "awaiting_liveness"
                    track.challenge = Challenge()
                else:
                    self._confirm(track, leader_uid, name)
            elif len(track.votes) >= T.unrecognized_vote_frames and not leader_uid:
                # Votes are in and nobody won — a stranger, not a misfire. Say so
                # instead of leaving the box amber forever.
                track.stage = "unrecognized"
                track.stage_until = time.monotonic() + T.unrecognized_hold_s
                track.votes.clear()

        elif stage == "unrecognized":
            self._box(frame, face.box, viz.RED, "Not recognised" if dbg else "")
            if time.monotonic() > track.stage_until:
                track.stage = "recognizing"

        elif stage == "awaiting_liveness":
            uid, name = track.identity
            result = track.challenge.update(face)
            self._box(frame, face.box, viz.AMBER, f"{name} — turn your head" if dbg else "")
            if dbg:
                viz.progress_bar(frame, track.challenge.progress, viz.AMBER)
            if result is not None:
                print(f"liveness {result} for {name}: peak={track.challenge.peak:.3f} "
                      f"(need {T.liveness_yaw_delta})", file=sys.stderr)
            if result == "pass":
                self._confirm(track, uid, name)
            elif result == "timeout":
                track.liveness_tries += 1
                if track.liveness_tries >= T.liveness_retries:
                    # A silent reset here is the worst thing a kiosk can do to
                    # someone standing in front of it — say it failed and give
                    # a way out (auto-retry after a hold, or the Try Again button).
                    track.stage = "liveness_failed"
                    track.stage_until = time.monotonic() + T.liveness_fail_hold_s
                    track.challenge = None
                else:
                    track.challenge = Challenge()   # re-arm, keep the identity

        elif stage == "liveness_failed":
            uid, name = track.identity if track.identity else (None, None)
            self._box(frame, face.box, viz.RED, f"{name} — try again" if dbg and name else "")
            if time.monotonic() > track.stage_until:
                track.stage = "recognizing"
                track.votes.clear()
                track.identity = None
                track.liveness_tries = 0

        elif stage == "ready":
            uid, name = track.identity
            mono = time.monotonic()
            stamped = self.debounce.get(uid)
            label = f"{name}" if dbg else ""
            if stamped is not None and mono - stamped < T.capture_debounce_seconds:
                self._box(frame, face.box, viz.GREY, f"{label}  logged" if dbg else "")
            elif track.id == self._candidate_track_id:
                # The one box the Check In/Out button actually refers to.
                self._box(frame, face.box, viz.GREEN, f"{label}  — tap Check In / Out" if dbg else "")
            else:
                # Recognised but not the candidate the button would stamp — grey,
                # so a crowd never shows two boxes claiming the same tap.
                self._box(frame, face.box, viz.GREY, label)

        elif stage == "committed":
            uid, name = track.identity
            done = time.monotonic() > track.greet_until
            color = viz.GREY if done else viz.GREEN
            label = ""
            if dbg:
                tail = "" if done else f"  {getattr(track, 'greet_text', 'OK')}"
                label = f"{name}{tail}"
            self._box(frame, face.box, color, label)

    # --- main loop -------------------------------------------------- #

    def _box(self, frame, box, color, label: str = "") -> None:
        """Record an overlay instead of drawing it.

        Detection runs on its own thread, at whatever rate the machine manages,
        while the display thread shows every frame. So detection describes what
        should be drawn and the display thread draws it — otherwise overlays
        would appear only on the frames detection happened to touch.
        """
        self._building_overlays.append((box, color, label))

    def replay_overlays(self, frame) -> None:
        for box, color, label in self.overlays:
            viz.draw_box(frame, box, color, label)

    def process(self, frame) -> list:
        """Run one frame through the whole pipeline; returns the faces detected
        this frame. Camera-free entry point — used by the loop and by selftest."""
        self._building_overlays: list[tuple] = []
        faces = [f for f in self.engine.detect(frame) if f.size >= T.min_face_px]
        pairs = self.tracker.update(faces)
        cand = self._pick_candidate_track()
        self._candidate_track_id = cand.id if cand is not None else None
        for track, _face in pairs:
            self._step_track(track, frame)
        # Swap in one go. The display thread reads this list while we build the
        # next one, and a half-built list would flicker boxes on and off.
        self.overlays = self._building_overlays
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
                self.replay_overlays(frame)   # process() records; drawing is ours

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
    ap.add_argument(
        "--camera",
        default="",
        help='camera NAME, e.g. --camera "HD Webcam C615" (see python -m facekiosk.camera). '
             "Empty takes the first connected camera.",
    )
    ap.add_argument("--conf-thres", type=float, default=T.detect_score, help="YuNet face-box score cut")
    ap.add_argument("--match-cosine", type=float, default=T.match_cosine, help="same-identity cosine cut")
    ap.add_argument("--no-liveness", dest="liveness", action="store_false", help="skip the head-turn challenge")
    ap.add_argument("--no-window", action="store_true", help="headless — no preview window")
    ap.add_argument("--seconds", type=float, default=0.0, help="auto-stop after N seconds (0 = run until stopped)")
    ap.add_argument(
        "--no-auto-log",
        dest="auto_log",
        action="store_false",
        help="don't log on recognition; wait for a manual capture (the web app's mode)",
    )
    args = ap.parse_args(argv)
    return Kiosk(args, auto_log=args.auto_log).run()


if __name__ == "__main__":
    raise SystemExit(main())
