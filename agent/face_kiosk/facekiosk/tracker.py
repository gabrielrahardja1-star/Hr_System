"""A minimal IOU tracker so each face keeps one identity across frames.

Borrowed in spirit from the DeepSORT stage in the CAP6411 reference, minus the
neural re-identification model — at kiosk range (one or two people, close to the
camera, ~15-30 fps) greedy IOU matching is enough. The point of tracking here is
to (a) accumulate recognition votes over many frames before committing a punch
and (b) attach the liveness challenge to one specific face.
"""

from __future__ import annotations

from collections import Counter, deque

from .config import T
from .engine import Face


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    return inter / (aw * ah + bw * bh - inter)


class Track:
    def __init__(self, track_id: int, face: Face) -> None:
        self.id = track_id
        self.face = face
        self.box = face.box
        self.hits = 1
        self.misses = 0
        self.votes: deque[str] = deque(maxlen=T.vote_window)
        # lifecycle: recognizing -> awaiting_liveness -> committed
        # recognizing -> awaiting_liveness -> (ready | committed)
        self.stage = "recognizing"
        self.identity: tuple[str, str] | None = None   # (uid, name)
        self.best_similarity = 0.0
        self.challenge = None                           # liveness.Challenge | None
        self.liveness_tries = 0
        self.committed_uid: str | None = None
        self.greet_until = 0.0                          # monotonic time to show the tick
        self.greet_text = "OK"
        self.stage_until = 0.0                          # monotonic deadline for a terminal stage

    def vote(self, name: str | None) -> None:
        self.votes.append(name or "?")

    def leader(self) -> tuple[str | None, int]:
        if not self.votes:
            return None, 0
        name, count = Counter(self.votes).most_common(1)[0]
        return (None if name == "?" else name), count

    def vote_settled(self) -> bool:
        _, count = self.leader()
        return count >= T.vote_frames


class IOUTracker:
    def __init__(self) -> None:
        self._tracks: dict[int, Track] = {}
        self._next_id = 1

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def update(self, faces: list[Face]) -> list[tuple[Track, Face]]:
        """Greedy-match detections to tracks; age or spawn the rest."""
        pairs: list[tuple[Track, Face]] = []
        unmatched = set(self._tracks)
        used_faces: set[int] = set()

        candidates = sorted(
            (
                (_iou(t.box, f.box), tid, fi)
                for tid, t in self._tracks.items()
                for fi, f in enumerate(faces)
            ),
            reverse=True,
        )
        for score, tid, fi in candidates:
            if score < T.track_iou or tid not in unmatched or fi in used_faces:
                continue
            track = self._tracks[tid]
            face = faces[fi]
            track.face = face
            track.box = face.box
            track.hits += 1
            track.misses = 0
            unmatched.discard(tid)
            used_faces.add(fi)
            pairs.append((track, face))

        for tid in unmatched:
            self._tracks[tid].misses += 1

        for fi, face in enumerate(faces):
            if fi in used_faces:
                continue
            track = Track(self._next_id, face)
            self._tracks[self._next_id] = track
            self._next_id += 1
            pairs.append((track, face))

        for tid in [t for t, tr in self._tracks.items() if tr.misses > T.track_max_misses]:
            del self._tracks[tid]

        return pairs
