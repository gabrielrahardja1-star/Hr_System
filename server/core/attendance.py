"""Turn a pile of raw punches into (first_in, last_out, worked_minutes) for one
employee on one work_date.

Two things this module is careful about:

* **Work-date attribution.** A night-shift punch at 01:30 belongs to the shift
  that *started* the previous evening. The shift window in config/shifts.yaml
  defines the believable range; a punch is attributed to the date the window
  opens on.
* **Reader chatter.** Fingerprint/face readers frequently register the same
  person 2-3 times within a few seconds. Punches on the same device within
  `chatter_window_seconds` collapse to one event, keeping the earliest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from server.config import ShiftDef, get_settings


@dataclass(frozen=True)
class ShiftWindow:
    start_utc: dt.datetime
    end_utc: dt.datetime

    def contains(self, moment: dt.datetime) -> bool:
        return self.start_utc <= moment < self.end_utc


def shift_window_utc(shift: ShiftDef, work_date: dt.date) -> ShiftWindow:
    """The UTC interval within which a punch counts toward `work_date` for this
    shift. `window_end` earlier than `window_start` (or crosses_midnight) rolls
    the end into the next calendar day."""
    tz = get_settings().timezone
    start_local = dt.datetime.combine(work_date, shift.window_start, tzinfo=tz)

    end_date = work_date
    if shift.crosses_midnight or shift.window_end <= shift.window_start:
        end_date = work_date + dt.timedelta(days=1)
    end_local = dt.datetime.combine(end_date, shift.window_end, tzinfo=tz)

    return ShiftWindow(
        start_utc=start_local.astimezone(dt.timezone.utc),
        end_utc=end_local.astimezone(dt.timezone.utc),
    )


def collapse_chatter(
    timestamps: list[dt.datetime], window_seconds: int | None = None
) -> list[dt.datetime]:
    """De-duplicate near-simultaneous scans. Input need not be sorted."""
    if window_seconds is None:
        window_seconds = get_settings().chatter_window_seconds
    if not timestamps:
        return []
    ordered = sorted(timestamps)
    kept = [ordered[0]]
    for ts in ordered[1:]:
        if (ts - kept[-1]).total_seconds() > window_seconds:
            kept.append(ts)
    return kept


@dataclass(frozen=True)
class DayPunchSummary:
    work_date: dt.date
    shift_key: str
    events: list[dt.datetime]          # chatter-collapsed, sorted
    first_in: dt.datetime | None
    last_out: dt.datetime | None
    worked_minutes: int | None

    @property
    def punch_count(self) -> int:
        return len(self.events)

    @property
    def is_odd(self) -> bool:
        return self.punch_count % 2 == 1


def summarise_day(
    shift: ShiftDef,
    work_date: dt.date,
    punch_times: list[dt.datetime],
) -> DayPunchSummary:
    """`punch_times` must already be filtered to this shift window."""
    events = collapse_chatter(punch_times)

    first_in = events[0] if events else None
    last_out = events[-1] if len(events) >= 2 else None

    worked_minutes: int | None
    if first_in is not None and last_out is not None:
        worked_minutes = round((last_out - first_in).total_seconds() / 60)
    else:
        # 0 or 1 real events -> no defensible span. Leave NULL, never 0.
        worked_minutes = None

    return DayPunchSummary(
        work_date=work_date,
        shift_key=shift.key,
        events=events,
        first_in=first_in,
        last_out=last_out,
        worked_minutes=worked_minutes,
    )
