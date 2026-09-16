"""Rebuild day records from the immutable punch log.

`recompute_employee` / `recompute_all` are safe to run any number of times over
any date range. They are the ONLY writers of day_records and exceptions. The
ingest endpoint calls this for the dates a sync touched; HR edits call it for the
one day they changed; a nightly job can call it for the whole open period.

Correction handling
-------------------
Active `corrections` rows are applied on top of the punch-derived values:
  * first_in / last_out  -> override the timestamp, worked span recomputed
  * code                 -> force the code, skip auto-coding, clear its exception
A day with any active correction is marked `has_correction=True` and its state is
lifted to `resolved` (HR has touched it) unless it is already `final`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.config import get_shift_config, get_settings
from server.core.attendance import shift_window_utc, summarise_day
from server.core.coding import code_day
from server.core.exceptions import sync_exceptions
from server.core.roster import is_holiday, is_scheduled_working
from server.models import (
    Correction,
    DayRecord,
    DayState,
    Employee,
    Punch,
)


def _daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def _active_corrections(session: Session, day_record_id: int) -> dict[str, Correction]:
    rows = session.execute(
        select(Correction).where(
            Correction.day_record_id == day_record_id,
            Correction.active.is_(True),
        )
    ).scalars().all()
    # Last write wins per field.
    out: dict[str, Correction] = {}
    for row in sorted(rows, key=lambda r: r.created_at):
        out[row.field] = row
    return out


def recompute_employee(
    session: Session,
    employee: Employee,
    start: dt.date,
    end: dt.date,
) -> list[dt.date]:
    """Recompute every day in [start, end] for one employee. Returns the dates
    that ended up with a day record (i.e. the employee was employed then)."""
    # A worker can be registered before anyone knows their shift. Without one
    # there is no window to attribute punches to, so skip them rather than
    # guess — and never raise, or one unassigned worker would break ingest for
    # the whole site. Their punches keep accruing and land once a shift is set.
    shift = get_shift_config().shifts.get(employee.shift_key or "")
    if shift is None:
        return []
    touched: list[dt.date] = []

    for work_date in _daterange(start, end):
        if not employee.employed_on(work_date):
            continue

        window = shift_window_utc(shift, work_date)
        punch_rows = session.execute(
            select(Punch.punched_at).where(
                Punch.device_user_id == employee.device_user_id,
                Punch.punched_at >= window.start_utc,
                Punch.punched_at < window.end_utc,
            )
        ).scalars().all()
        punch_times = [
            p if p.tzinfo else p.replace(tzinfo=dt.timezone.utc) for p in punch_rows
        ]

        summary = summarise_day(shift, work_date, punch_times)

        record = session.execute(
            select(DayRecord).where(
                DayRecord.employee_id == employee.id,
                DayRecord.work_date == work_date,
            )
        ).scalar_one_or_none()
        if record is None:
            record = DayRecord(
                employee_id=employee.id,
                work_date=work_date,
                shift_key=shift.key,
                code="",
            )
            session.add(record)
            session.flush()  # need the id for corrections + exceptions

        corrections = _active_corrections(session, record.id)

        first_in = summary.first_in
        last_out = summary.last_out
        if "first_in" in corrections:
            first_in = _parse_dt(corrections["first_in"].new_value)
        if "last_out" in corrections:
            last_out = _parse_dt(corrections["last_out"].new_value)

        time_corrected = "first_in" in corrections or "last_out" in corrections
        worked_minutes = summary.worked_minutes
        if time_corrected and first_in and last_out:
            if last_out <= first_in:
                # Impossible span — e.g. a night-shift clock-out entered without
                # rolling to the next day. Refuse the pairing; keep the day
                # flagged rather than pay a negative or zero shift.
                last_out = None
                worked_minutes = None
            else:
                worked_minutes = round((last_out - first_in).total_seconds() / 60)

        scheduled = is_scheduled_working(
            session, employee.id, employee.roster_pattern, work_date
        )
        holiday = is_holiday(session, work_date)

        if "code" in corrections and corrections["code"].new_value:
            record.code = corrections["code"].new_value
            record.worked_minutes = worked_minutes
            wanted_exceptions = []
        else:
            # Feed corrected times back through the coder.
            result = code_day(
                _summary_with(
                    summary, first_in, last_out, worked_minutes, time_corrected
                ),
                shift,
                scheduled_working=scheduled,
                is_holiday=holiday,
            )
            record.code = result.code
            record.worked_minutes = result.worked_minutes
            wanted_exceptions = result.exceptions

        record.shift_key = shift.key
        record.first_in = first_in
        record.last_out = last_out
        record.punch_count = summary.punch_count
        record.has_correction = bool(corrections)
        record.computed_at = dt.datetime.now(dt.timezone.utc)
        if record.state != DayState.final:
            record.state = DayState.resolved if corrections else DayState.open

        session.flush()
        sync_exceptions(session, record, wanted_exceptions)
        touched.append(work_date)

    return touched


def _summary_with(summary, first_in, last_out, worked_minutes, time_corrected):
    """Return a DayPunchSummary reflecting HR's corrected in/out.

    When HR has supplied a time correction, the corrected timestamps ARE the
    events — an MP day where HR fills in the missing clock-out becomes an
    even-count day and codes as P/SS on hours. Without a time correction the
    device-derived events stand, so an untouched single punch still codes MP.
    """
    from dataclasses import replace

    if time_corrected:
        events = [t for t in (first_in, last_out) if t is not None]
    else:
        events = list(summary.events)

    return replace(
        summary,
        first_in=first_in,
        last_out=last_out,
        worked_minutes=worked_minutes,
        events=events,
    )


def _parse_dt(value: str | None) -> dt.datetime | None:
    """Parse a correction timestamp. A naive value is site-local time — that is
    how HR enters it ('left at 14:40') — so it is localised to the configured
    timezone, not UTC."""
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_settings().timezone)
    return parsed.astimezone(dt.timezone.utc)


def data_frontier(session: Session) -> dt.date | None:
    """The most recent local date any punch has been ingested for. Days after
    this have no data yet — they must not be coded 'Absent', only left blank."""
    latest = session.execute(select(func.max(Punch.punched_at))).scalar_one_or_none()
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=dt.timezone.utc)
    return latest.astimezone(get_settings().timezone).date()


def recompute_all(
    session: Session,
    start: dt.date,
    end: dt.date,
    *,
    device_user_ids: list[str] | None = None,
    respect_frontier: bool = True,
) -> dict[str, list[dt.date]]:
    if respect_frontier:
        frontier = data_frontier(session)
        if frontier is not None:
            end = min(end, frontier)
        if end < start:
            return {}
    q = select(Employee)
    if device_user_ids is not None:
        q = q.where(Employee.device_user_id.in_(device_user_ids))
    employees = session.execute(q).scalars().all()
    return {
        e.device_user_id: recompute_employee(session, e, start, end)
        for e in employees
    }


def recompute_for_dates(session: Session, dates: Iterable[dt.date]) -> None:
    """Convenience for the ingest path: recompute the affected span for everyone.
    A punch window can reach one day back, so pad the low end."""
    date_list = sorted(set(dates))
    if not date_list:
        return
    recompute_all(
        session,
        start=date_list[0] - dt.timedelta(days=1),
        end=date_list[-1] + dt.timedelta(days=1),
    )
