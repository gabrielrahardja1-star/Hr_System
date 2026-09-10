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
    liveness   TEXT
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
        check-out (last 'out'), and total taps."""
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
            out.append(d)
        return out

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
