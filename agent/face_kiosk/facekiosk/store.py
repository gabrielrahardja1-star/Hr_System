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

# Shifts run past midnight (the 23:00-07:00 crew especially), so "the last thing
# this person did" cannot be scoped to today's calendar date. 18h spans the
# longest shift plus overtime without reaching the previous day's same stamp.
STAMP_LOOKBACK_HOURS = 18

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
        check-out (last 'out'), hours between them, and total records.

        A shift is reported on the date it STARTED, matching how HQ attributes
        punches. So an overnight worker's clock-out is pulled forward from the
        next morning, and a lone early-morning 'out' is left to the previous
        day's row rather than shown here as a zero-hour shift.
        """
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
            # An 'out' with no 'in' before it closes a shift that started
            # yesterday, so it belongs to yesterday's row.
            if d["check_out"] is not None and (
                d["check_in"] is None or d["check_out"] < d["check_in"]
            ):
                if d["check_in"] is None:
                    continue
                d["check_out"] = None
            d["check_in"] = d["check_in"] or d["first_seen"]
            if d["check_out"] is None:
                d["check_out"] = self._checkout_next_morning(d["uid"], day)
            d["hours"] = _hours_between(d["check_in"], d["check_out"])
            out.append(d)
        return out

    def _checkout_next_morning(self, uid: str, day: str) -> str | None:
        """Clock-out for a shift that started on `day` but ended after midnight.

        Only counts an 'out' that precedes the person's next 'in', so the next
        shift's check-in is never mistaken for this shift's check-out.
        """
        nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT direction, seen_at FROM sightings "
                "WHERE uid = ? AND seen_date = ? AND direction IN ('in', 'out') "
                "ORDER BY seen_at ASC, id ASC",
                (uid, nxt),
            ).fetchall()
        for r in rows:
            if r["direction"] == "in":
                return None
            return r["seen_at"]
        return None

    def last_stamp(self, uid: str) -> dict | None:
        """The most recent Check In/Out tap for `uid` within the lookback window —
        used to decide which button the kiosk should lead with next. Deliberately
        not scoped to today: a night-shift worker who checked in at 23:00 checks
        out after midnight, and must still be offered Check Out."""
        with self._lock:
            row = self._conn.execute(
                "SELECT direction, seen_at FROM sightings "
                "WHERE uid = ? AND direction IN ('in', 'out') "
                "ORDER BY seen_at DESC, id DESC LIMIT 1",
                (uid,),
            ).fetchone()
        if row is None:
            return None
        seen = _parse(row["seen_at"])
        if seen is None or seen < _now() - dt.timedelta(hours=STAMP_LOOKBACK_HOURS):
            return None
        return dict(row)

    def void_last(self, uid: str, day: str | None = None) -> bool:
        """Delete the most recent sighting for `uid` — correcting a mis-stamp
        without touching anyone else's records. Scoped to `day` when given;
        otherwise to the lookback window, so a pre-midnight mis-tap is still
        voidable from the small hours of the next day."""
        if day:
            where, params = "uid = ? AND seen_date = ?", (uid, day)
        else:
            cutoff = (_now() - dt.timedelta(hours=STAMP_LOOKBACK_HOURS)).isoformat(
                timespec="seconds"
            )
            where, params = "uid = ? AND seen_at >= ?", (uid, cutoff)
        with self._lock:
            row = self._conn.execute(
                f"SELECT id FROM sightings WHERE {where} ORDER BY seen_at DESC, id DESC LIMIT 1",
                params,
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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone()


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _hours_between(check_in: str | None, check_out: str | None) -> float | None:
    start = _parse(check_in)
    end = _parse(check_out)
    if start is None or end is None:
        return None
    hours = (end - start).total_seconds() / 3600
    return round(hours, 2) if hours >= 0 else None
