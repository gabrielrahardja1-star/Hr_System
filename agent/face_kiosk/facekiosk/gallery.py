"""The enrolled-face store: {device_user_id -> embedding vectors}.

Held on disk as one Fernet-encrypted .npz next to a chmod-600 key file. Face
embeddings are biometric data ("data pribadi spesifik" under UU PDP 27/2022) —
they never leave this machine in the prototype, and we store vectors, not
images. Enrol only with the employee's consent.

`device_user_id` is the same key the roster uses (Employee.device_user_id), so a
face punch flows through HQ's coding engine exactly like a fingerprint punch.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os

import numpy as np
from cryptography.fernet import Fernet

from .config import DATA_DIR, GALLERY_PATH, KEY_PATH
from .engine import FaceEngine


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    os.chmod(KEY_PATH, 0o600)
    return key


class Person:
    __slots__ = ("uid", "name", "emp_id", "enrolled_at", "embeddings")

    def __init__(
        self,
        uid: str,
        name: str,
        enrolled_at: str,
        embeddings: np.ndarray,
        emp_id: str = "",
    ) -> None:
        self.uid = uid
        self.name = name
        self.emp_id = emp_id                   # optional free-text staff/badge number
        self.enrolled_at = enrolled_at
        self.embeddings = embeddings          # (N, 128) float32

    @property
    def shots(self) -> int:
        return int(self.embeddings.shape[0])

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "name": self.name,
            "emp_id": self.emp_id,
            "enrolled_at": self.enrolled_at,
            "shots": self.shots,
        }


class Match:
    __slots__ = ("uid", "name", "emp_id", "similarity")

    def __init__(
        self, uid: str | None, name: str | None, similarity: float, emp_id: str = ""
    ) -> None:
        self.uid = uid
        self.name = name
        self.emp_id = emp_id
        self.similarity = similarity

    @property
    def ok(self) -> bool:
        return self.uid is not None


class Gallery:
    def __init__(self) -> None:
        self._fernet = Fernet(_load_key())
        self.people: dict[str, Person] = {}
        self._load()

    # --- persistence ----------------------------------------------------- #

    def _load(self) -> None:
        if not GALLERY_PATH.exists():
            return
        plain = self._fernet.decrypt(GALLERY_PATH.read_bytes())
        with np.load(io.BytesIO(plain), allow_pickle=False) as blob:
            meta = json.loads(bytes(blob["_meta"]).decode("utf-8"))
            for i, m in enumerate(meta):
                self.people[m["uid"]] = Person(
                    uid=m["uid"],
                    name=m["name"],
                    emp_id=m.get("emp_id", ""),
                    enrolled_at=m["enrolled_at"],
                    embeddings=blob[f"emb_{i}"],
                )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, np.ndarray] = {}
        meta = []
        for i, person in enumerate(self.people.values()):
            arrays[f"emb_{i}"] = person.embeddings
            meta.append(
                {
                    "uid": person.uid,
                    "name": person.name,
                    "emp_id": person.emp_id,
                    "enrolled_at": person.enrolled_at,
                }
            )
        arrays["_meta"] = np.frombuffer(
            json.dumps(meta).encode("utf-8"), dtype=np.uint8
        )
        buf = io.BytesIO()
        np.savez(buf, **arrays)
        GALLERY_PATH.write_bytes(self._fernet.encrypt(buf.getvalue()))
        os.chmod(GALLERY_PATH, 0o600)

    # --- mutation ------------------------------------------------------- #

    def enroll(
        self,
        uid: str,
        name: str,
        embeddings: list[np.ndarray],
        *,
        emp_id: str = "",
        replace: bool = False,
    ) -> Person:
        new = np.vstack(embeddings).astype(np.float32)
        if not replace and uid in self.people:
            existing = self.people[uid]
            new = np.vstack([existing.embeddings, new])
            emp_id = emp_id or existing.emp_id
        person = Person(uid=uid, name=name, enrolled_at=_now_iso(), embeddings=new, emp_id=emp_id)
        self.people[uid] = person
        return person

    def remove(self, uid: str) -> bool:
        return self.people.pop(uid, None) is not None

    # --- query -------------------------------------------------------- #

    def identify(self, embedding: np.ndarray, threshold: float) -> Match:
        best = Match(None, None, -1.0)
        for person in self.people.values():
            sim = float(FaceEngine.cosine_batch(person.embeddings, embedding).max())
            if sim > best.similarity:
                best = Match(person.uid, person.name, sim, person.emp_id)
        if best.similarity >= threshold:
            return best
        return Match(None, None, best.similarity)

    def __len__(self) -> int:
        return len(self.people)
