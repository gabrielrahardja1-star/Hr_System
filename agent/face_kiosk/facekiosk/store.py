"""SQLite log of confirmed sightings — the "seen on camera at HH:MM" record.

One row per debounced recognition (a re-appearance after the debounce window is
a new row), so first/last per person per day fall out of a GROUP BY. Standalone:
this file is the kiosk's own attendance store, independent of the HQ database.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "sightings.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id         INTEGER PRIMARY KEY,
    uid        TEXT NOT NULL,
    emp_id     TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    direction  TEXT NOT NULL DEFAULT '',  -- 'in' | 'out' | 'auto'
    seen_at    TEXT NOT NULL,          -- ISO 8601, local time with offset
    seen_date  TEXT NOT NULL,          -- YYYY-MM-DD, local
    similarity REAL,
    liveness   TEXT,
    synced_at  TEXT               -- set once POSTed to HQ; NULL = pending
);
CREATE INDEX IF NOT EXISTS ix_sightings_date ON sightings(seen_date);
CREATE INDEX IF NOT EXISTS ix_sightings_uid  ON sightings(uid);
"""


class SightingStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sightings)")}
        if "direction" not in cols:  # db created before Check In/Out
            self._conn.execute("ALTER TABLE sightings ADD COLUMN direction TEXT NOT NULL DEFAULT ''")
        if "synced_at" not in cols:  # db created before HQ sync
            self._conn.execute("ALTER TABLE sightings ADD COLUMN synced_at TEXT")

    def record(self, event: dict) -> int:
        """Insert one sighting from a Kiosk event record. Returns the row id."""
        seen_at = event["ts"]
        seen_date = seen_at[:10]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sightings (uid, emp_id, name, direction, seen_at, seen_date, similarity, liveness) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["device_user_id"],
                    event.get("emp_id", ""),
                    event["name"],
                    event.get("direction", ""),
                    seen_at,
                    seen_date,
                    event.get("similarity"),
                    event.get("liveness"),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def on_date(self, day: str | None = None) -> list[dict]:
        """Every sighting on `day` (default today), newest first."""
        day = day or dt.date.today().isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sightings WHERE seen_date = ? ORDER BY seen_at DESC",
                (day,),
            ).fetchall()
        return [dict(r) for r in rows]

    def roster_for_date(self, day: str | None = None) -> list[dict]:
        """Per-person roll-up for `day`: check-in (first 'in', else first sighting),
        check-out (last 'out'), hours between them, and total records."""
        day = day or dt.date.today().isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT uid, emp_id, name,
                       MIN(CASE WHEN direction = 'in'  THEN seen_at END) AS check_in,
                       MAX(CASE WHEN direction = 'out' THEN seen_at END) AS check_out,
                       MIN(seen_at) AS first_seen,
                       MAX(seen_at) AS last_seen,
                       COUNT(*)     AS sightings
                FROM sightings WHERE seen_date = ?
                GROUP BY uid ORDER BY first_seen
                """,
                (day,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["check_in"] = d["check_in"] or d["first_seen"]
            d["hours"] = _hours_between(d["check_in"], d["check_out"])
            out.append(d)
        return out

    def last_stamp(self, uid: str, day: str | None = None) -> dict | None:
        """The most recent Check In/Out tap for `uid` on `day` — used to decide
        which button the kiosk should lead with next."""
        day = day or dt.date.today().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT direction, seen_at FROM sightings "
                "WHERE uid = ? AND seen_date = ? AND direction IN ('in', 'out') "
                "ORDER BY seen_at DESC, id DESC LIMIT 1",
                (uid, day),
            ).fetchone()
        return dict(row) if row else None

    def void_last(self, uid: str, day: str | None = None) -> bool:
        """Delete the most recent sighting for `uid` on `day` — correcting a
        mis-stamp without touching anyone else's records."""
        day = day or dt.date.today().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sightings WHERE uid = ? AND seen_date = ? "
                "ORDER BY seen_at DESC, id DESC LIMIT 1",
                (uid, day),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM sightings WHERE id = ?", (row["id"],))
            self._conn.commit()
            return True

    def unsynced(self, limit: int = 500) -> list[dict]:
        """Sightings never POSTed to HQ, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sightings WHERE synced_at IS NULL ORDER BY seen_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, ids: list[int]) -> None:
        if not ids:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                "UPDATE sightings SET synced_at = ? WHERE id = ?",
                [(now, i) for i in ids],
            )
            self._conn.commit()

    def recent_days(self, limit: int = 14) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT seen_date FROM sightings ORDER BY seen_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["seen_date"] for r in rows]

    def forget_person(self, uid: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sightings WHERE uid = ?", (uid,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _hours_between(check_in: str | None, check_out: str | None) -> float | None:
    if not check_in or not check_out:
        return None
    try:
        start = dt.datetime.fromisoformat(check_in)
        end = dt.datetime.fromisoformat(check_out)
    except ValueError:
        return None
    hours = (end - start).total_seconds() / 3600
    return round(hours, 2) if hours >= 0 else None
