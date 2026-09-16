"""Turn database rows into template-ready structures for the review screens.

Kept separate from routes so the shape of what a template renders is testable
without spinning up the app.
"""

from __future__ import annotations

import datetime as dt
from calendar import monthrange
from dataclasses import dataclass

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from server.config import get_holidays, get_settings, get_shift_config, get_wage_mapping
from server.core.roster import is_holiday
from server.models import (
    DayRecord,
    Employee,
    EmployeeStatus,
    Exception_,
    ExceptionKind,
    ExceptionState,
    ExportRun,
    Holiday,
    Punch,
)

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON_SHORT = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

# From the design canvas PAL_BASE.
PALETTE = {
    "P":  {"bg": "#dcf5e4", "fg": "#146c39", "bd": "#a9e0bd"},
    "A":  {"bg": "#fde0dd", "fg": "#a4271b", "bd": "#f4b7b0"},
    "MP": {"bg": "#fde8cf", "fg": "#96500a", "bd": "#f3c58a"},
    "SS": {"bg": "#fbf3c4", "fg": "#7a6108", "bd": "#eadd8a"},
    "WO": {"bg": "#ece1fb", "fg": "#5b3ba8", "bd": "#d3c0f3"},
    "H":  {"bg": "#dbe9fd", "fg": "#1a4f96", "bd": "#b3cef7"},
    "":   {"bg": "#f1f2f4", "fg": "#9aa1ab", "bd": "#e2e5ea"},
}
# Exception-kind chips.
KIND_PALETTE = {
    "MP": PALETTE["MP"],
    "SS": PALETTE["SS"],
    "A": PALETTE["A"],
    "LONG": {"bg": "#e7e0f6", "fg": "#5b3ba8", "bd": "#d0c2ee"},
    "DUP": {"bg": "#e2e8f2", "fg": "#40506a", "bd": "#c9d3e3"},
}

PER_PAGE = 15


def period_bounds(period: str) -> tuple[dt.date, dt.date, int]:
    year, month = (int(p) for p in period.split("-"))
    n = monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, n), n


def available_periods(session: Session) -> list[dict]:
    # extract() rather than strftime(): the latter is SQLite-only and does not
    # exist in Postgres, which this runs on in production.
    year = extract("year", DayRecord.work_date)
    month = extract("month", DayRecord.work_date)
    rows = session.execute(
        select(year.label("y"), month.label("m"))
        .group_by(year, month)
        .order_by(year.desc(), month.desc())
    ).all()
    periods = [f"{int(r.y):04d}-{int(r.m):02d}" for r in rows if r.y and r.m]
    if not periods:
        periods = [dt.date.today().strftime("%Y-%m")]
    out = []
    for p in periods:
        y, m = (int(x) for x in p.split("-"))
        out.append({"value": p, "label": f"{MON_SHORT[m]} {y}"})
    return out


def talenta_periods(session: Session) -> list[dict]:
    """26→25 payroll periods that overlap the day-record data, newest first."""
    from server.core.period import talenta_period_bounds, talenta_period_for

    span = session.execute(
        select(func.min(DayRecord.work_date), func.max(DayRecord.work_date))
    ).one()
    lo, hi = span
    if lo is None:
        today = dt.date.today()
        labels = [talenta_period_for(today)]
    else:
        labels = []
        label = talenta_period_for(lo)
        seen = set()
        cursor = lo
        while cursor <= hi:
            lbl = talenta_period_for(cursor)
            if lbl not in seen:
                seen.add(lbl)
                labels.append(lbl)
            cursor += dt.timedelta(days=1)
    out = []
    for lbl in sorted(set(labels), reverse=True):
        y, m = (int(x) for x in lbl.split("-"))
        s, e = talenta_period_bounds(lbl)
        out.append({
            "value": lbl,
            "label": f"{MON_SHORT[m]} {y}",
            "range": f"{s.strftime('%d %b')} – {e.strftime('%d %b %Y')}",
        })
    return out


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def month_days(session: Session, period: str) -> list[dict]:
    start, _, n = period_bounds(period)
    db_hol = set(session.execute(select(Holiday.day)).scalars().all())
    cfg_hol = get_holidays()
    days = []
    for d in range(1, n + 1):
        day = start.replace(day=d)
        wd = day.weekday()
        days.append(
            {
                "num": d,
                "iso": day.isoformat(),
                "dow": DOW[wd],
                "weekend": wd >= 5,
                "holiday": day in db_hol or day in cfg_hol,
            }
        )
    return days


def _employee_query(dept: str | None, search: str | None = None):
    q = select(Employee).where(Employee.status != EmployeeStatus.inactive)
    if dept and dept != "*":
        q = q.where(Employee.department == dept)
    if search:
        like = f"%{search.strip()}%"
        q = q.where(
            Employee.name.ilike(like)
            | Employee.emp_code.ilike(like)
            | Employee.device_user_id.ilike(like)
        )
    return q.order_by(Employee.emp_code)


def departments(session: Session) -> list[str]:
    return session.execute(
        select(Employee.department).distinct().order_by(Employee.department)
    ).scalars().all()


def employee_options(session: Session) -> list[dict]:
    """Every active employee, for the search box's autocomplete list."""
    rows = session.execute(
        select(Employee)
        .where(Employee.status != EmployeeStatus.inactive)
        .order_by(Employee.name)
    ).scalars().all()
    return [
        {
            "name": e.name,
            "emp_code": e.emp_code,
            "department": e.department,
            "device_user_id": e.device_user_id,
        }
        for e in rows
    ]


def shift_options() -> list[dict]:
    """Every shift declared in config/shifts.yaml, for the employee form."""
    cfg = get_shift_config()
    return [
        {
            "key": s.key,
            "name": s.name,
            "hours": f"{s.start:%H:%M}–{s.end:%H:%M}",
            "crosses_midnight": s.crosses_midnight,
        }
        for s in sorted(cfg.shifts.values(), key=lambda s: s.key)
    ]


def roster_options() -> list[str]:
    return sorted(get_shift_config().roster_patterns)


def unmatched_device_ids(session: Session) -> list[dict]:
    """Device IDs that have sent punches but match no employee.

    The kiosk stamps a punch with whatever ID it was enrolled under, and ingest
    stores it without checking. A typo'd or auto-generated ID therefore lands
    here rather than on anyone's timesheet — silently, until this surfaces it.
    """
    known = select(Employee.device_user_id)
    rows = session.execute(
        select(
            Punch.device_user_id,
            func.count(Punch.id).label("punches"),
            func.min(Punch.punched_at).label("first_seen"),
            func.max(Punch.punched_at).label("last_seen"),
        )
        .where(Punch.device_user_id.not_in(known))
        .group_by(Punch.device_user_id)
        .order_by(func.max(Punch.punched_at).desc())
    ).all()
    tz = get_settings().timezone
    return [
        {
            "device_user_id": r.device_user_id,
            "punches": r.punches,
            "first_seen": r.first_seen.astimezone(tz).strftime("%d %b %H:%M"),
            "last_seen": r.last_seen.astimezone(tz).strftime("%d %b %H:%M"),
        }
        for r in rows
    ]


def employee_admin_view(session: Session) -> dict:
    rows = session.execute(select(Employee).order_by(Employee.name)).scalars().all()
    shifts = {s["key"]: s for s in shift_options()}
    return {
        "employees": [
            {
                "id": e.id,
                "name": e.name,
                "device_user_id": e.device_user_id,
                "emp_code": e.emp_code,
                "talenta_id": e.talenta_id or "",
                "department": e.department,
                "status": e.status.value,
                "shift_key": e.shift_key,
                "shift_label": shifts[e.shift_key]["name"]
                if e.shift_key in shifts
                else f"{e.shift_key} (unknown)",
                "shift_known": e.shift_key in shifts,
                "roster_pattern": e.roster_pattern,
                "active_from": e.active_from.isoformat() if e.active_from else "",
                "active_to": e.active_to.isoformat() if e.active_to else "",
            }
            for e in rows
        ],
        "shifts": shift_options(),
        "rosters": roster_options(),
        "statuses": [s.value for s in EmployeeStatus],
        "unmatched": unmatched_device_ids(session),
    }


@dataclass
class Page:
    number: int
    total: int
    per_page: int

    @property
    def start(self) -> int:
        return (self.number - 1) * self.per_page

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def range_label(self) -> str:
        lo = self.start + 1
        hi = min(self.start + self.per_page, self.total)
        return f"{lo}–{hi}"


def _records_by_key(session: Session, emp_ids: list[int], start: dt.date, end: dt.date):
    if not emp_ids:
        return {}
    rows = session.execute(
        select(DayRecord).where(
            DayRecord.employee_id.in_(emp_ids),
            DayRecord.work_date >= start,
            DayRecord.work_date <= end,
        )
    ).scalars().all()
    out: dict[tuple[int, str], DayRecord] = {}
    for r in rows:
        out[(r.employee_id, r.work_date.isoformat())] = r
    return out


def _open_exc_kinds(session: Session, day_record_ids: list[int]) -> dict[int, set[str]]:
    if not day_record_ids:
        return {}
    rows = session.execute(
        select(Exception_.day_record_id, Exception_.kind).where(
            Exception_.day_record_id.in_(day_record_ids),
            Exception_.state == ExceptionState.open,
        )
    ).all()
    out: dict[int, set[str]] = {}
    for drid, kind in rows:
        out.setdefault(drid, set()).add(kind.value)
    return out


def monthly_grid(
    session: Session,
    period: str,
    *,
    dept: str | None = None,
    search: str | None = None,
    page: int = 1,
    view: str = "detail",
) -> dict:
    start, end, _ = period_bounds(period)
    days = month_days(session, period)

    all_emps = session.execute(_employee_query(dept, search)).scalars().all()
    pager = Page(number=max(1, page), total=len(all_emps), per_page=PER_PAGE)
    emps = all_emps[pager.start : pager.start + pager.per_page]
    emp_ids = [e.id for e in emps]

    rec_map = _records_by_key(session, emp_ids, start, end)
    exc_map = _open_exc_kinds(session, [r.id for r in rec_map.values()])

    wage = get_wage_mapping()
    tz = get_settings().timezone
    rows = []
    for e in emps:
        cells = []
        counts = {k: 0 for k in ("P", "A", "MP", "SS", "WO", "H")}
        total_minutes = 0
        flagged = 0
        for day in days:
            rec = rec_map.get((e.id, day["iso"]))
            code = rec.code if rec else ""
            if code in counts:
                counts[code] += 1
            if rec and rec.worked_minutes:
                total_minutes += rec.worked_minutes
            open_kinds = exc_map.get(rec.id, set()) if rec else set()
            blocking = any(
                wage.code_blocks_export(k) or wage.flag_blocks_export(k) for k in open_kinds
            )
            if open_kinds:
                flagged += 1
            pal = PALETTE.get(code, PALETTE[""])
            cells.append(
                {
                    "code": code or "·",
                    "date": day["iso"],
                    "bg": pal["bg"],
                    "fg": pal["fg"],
                    "bd": pal["bd"],
                    "weekend": day["weekend"],
                    "has_exc": bool(open_kinds),
                    "blocking": blocking,
                    "clickable": rec is not None,
                    "title": f'{e.name} · {day["dow"]} {day["num"]} — {code or "no record"}',
                }
            )
        rows.append(
            {
                "id": e.id,
                "name": e.name,
                "emp_code": e.emp_code,
                "initials": _initials(e.name),
                "department": e.department,
                "status": e.status.value,
                "cells": cells,
                "counts": counts,
                "total_hours": round(total_minutes / 60, 1),
                "present_days": counts["P"] + counts["SS"],
                "flagged": flagged,
            }
        )

    total_flagged = sum(r["flagged"] for r in rows)
    return {
        "days": days,
        "rows": rows,
        "pager": pager,
        "view": view,
        "search": search or "",
        "col_span": len(days) + 2,
        "cells_need_review": total_flagged,
        "summary_cols": ["P", "A", "MP", "SS", "WO", "H"],
    }


def daily_roster(
    session: Session,
    period: str,
    day_iso: str,
    *,
    dept: str | None = None,
    search: str | None = None,
) -> dict:
    day = dt.date.fromisoformat(day_iso)
    emps = session.execute(_employee_query(dept, search)).scalars().all()
    emp_by_id = {e.id: e for e in emps}
    recs = session.execute(
        select(DayRecord).where(
            DayRecord.employee_id.in_(list(emp_by_id)),
            DayRecord.work_date == day,
        )
    ).scalars().all()
    rec_by_emp = {r.employee_id: r for r in recs}
    exc_map = _open_exc_kinds(session, [r.id for r in recs])
    tz = get_settings().timezone
    shift_cfg = get_shift_config()

    def hhmm(v: dt.datetime | None) -> str:
        return "" if v is None else v.astimezone(tz).strftime("%H:%M")

    counts = {k: 0 for k in ("P", "A", "MP", "SS", "WO", "H")}
    rows = []
    for e in emps:
        rec = rec_by_emp.get(e.id)
        code = rec.code if rec else ""
        if code in counts:
            counts[code] += 1
        pal = PALETTE.get(code, PALETTE[""])
        shift = shift_cfg.shifts.get(e.shift_key)
        rows.append(
            {
                "id": e.id,
                "name": e.name,
                "emp_code": e.emp_code,
                "initials": _initials(e.name),
                "department": e.department,
                "shift": shift.name if shift else e.shift_key,
                "code": code or "·",
                "pal": pal,
                "in": hhmm(rec.first_in) if rec else "",
                "out": hhmm(rec.last_out) if rec else "",
                "hours": "" if not rec or rec.worked_hours is None else f"{rec.worked_hours:.2f}",
                "has_exc": bool(rec and exc_map.get(rec.id)),
                "date": day_iso,
                "has_record": rec is not None,
            }
        )
    return {
        "day": day,
        "day_iso": day_iso,
        "rows": rows,
        "counts": counts,
        "total": len(emps),
        "search": search or "",
        "dept": dept or "*",
    }


def cell_detail(session: Session, employee_id: int, day_iso: str) -> dict | None:
    day = dt.date.fromisoformat(day_iso)
    rec = session.execute(
        select(DayRecord).where(
            DayRecord.employee_id == employee_id, DayRecord.work_date == day
        )
    ).scalar_one_or_none()
    if rec is None:
        return None
    emp = session.get(Employee, employee_id)
    tz = get_settings().timezone
    shift = get_shift_config().shifts.get(rec.shift_key)

    def hhmm(v: dt.datetime | None) -> str | None:
        return None if v is None else v.astimezone(tz).strftime("%H:%M")

    def dtlocal(v: dt.datetime | None, fallback_time: dt.time, add_day: int = 0) -> str:
        if v is not None:
            return v.astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        base = day + dt.timedelta(days=add_day)
        return f"{base.isoformat()}T{fallback_time.strftime('%H:%M')}"

    open_excs = [e for e in rec.exceptions if e.state == ExceptionState.open]
    wage = get_wage_mapping()
    out_add_day = 1 if (shift and shift.crosses_midnight) else 0
    return {
        "record": rec,
        "employee": emp,
        "edit_in_default": dtlocal(rec.first_in, shift.start if shift else dt.time(6), 0),
        "edit_out_default": dtlocal(
            rec.last_out, shift.end if shift else dt.time(14, 30), out_add_day
        ),
        "day_iso": day_iso,
        "date_label": f'{DOW[day.weekday()]} {day.day} {MON_SHORT[day.month]} {day.year}',
        "shift_label": shift.name if shift else rec.shift_key,
        "clock_in": hhmm(rec.first_in),
        "clock_out": hhmm(rec.last_out),
        "hours": None if rec.worked_hours is None else f"{rec.worked_hours:.2f}",
        "code": rec.code,
        "pal": PALETTE.get(rec.code, PALETTE[""]),
        "punch_count": rec.punch_count,
        "state": rec.state.value,
        "has_correction": rec.has_correction,
        "corrections": sorted(rec.corrections, key=lambda c: c.created_at),
        "open_exceptions": [
            {
                "id": e.id,
                "kind": e.kind.value,
                "detail": e.detail,
                "pal": KIND_PALETTE.get(e.kind.value, PALETTE[""]),
                "blocks": wage.code_blocks_export(e.kind.value)
                or wage.flag_blocks_export(e.kind.value),
            }
            for e in open_excs
        ],
        "code_options": ["P", "A", "MP", "SS", "WO", "H"],
    }


def exceptions_view(
    session: Session, period: str, *, kind: str | None = None, show_resolved: bool = False
) -> dict:
    start, end, _ = period_bounds(period)
    wage = get_wage_mapping()

    state_filter = (
        [ExceptionState.open, ExceptionState.resolved]
        if show_resolved
        else [ExceptionState.open]
    )
    base = (
        select(Exception_, DayRecord, Employee)
        .join(DayRecord, Exception_.day_record_id == DayRecord.id)
        .join(Employee, DayRecord.employee_id == Employee.id)
        .where(
            Exception_.state.in_(state_filter),
            DayRecord.work_date >= start,
            DayRecord.work_date <= end,
        )
    )
    today = dt.date.today()

    def blocks(k: str) -> bool:
        return wage.code_blocks_export(k) or wage.flag_blocks_export(k)

    # Chip counts + totals are always over the unfiltered set for this period.
    counts: dict[str, int] = {}
    open_total = 0
    blocking_total = 0
    for exc, rec, _emp in session.execute(base).all():
        k = exc.kind.value
        counts[k] = counts.get(k, 0) + 1
        if exc.state == ExceptionState.open:
            open_total += 1
            if blocks(k):
                blocking_total += 1

    q = base
    if kind and kind != "*":
        q = q.where(Exception_.kind == ExceptionKind(kind))
    triples = session.execute(q).all()

    rows = []
    for exc, rec, emp in triples:
        k = exc.kind.value
        is_blocking = blocks(k)
        rows.append(
            {
                "id": exc.id,
                "kind": k,
                "state": exc.state.value,
                "pal": KIND_PALETTE.get(k, PALETTE[""]),
                "blocks": is_blocking,
                "detail": exc.detail,
                "resolution": exc.resolution,
                "employee_id": emp.id,
                "emp_code": emp.emp_code,
                "name": emp.name,
                "initials": _initials(emp.name),
                "department": emp.department,
                "date": rec.work_date.isoformat(),
                "date_label": f"{DOW[rec.work_date.weekday()]} {rec.work_date.day} {MON_SHORT[rec.work_date.month]}",
                "age_days": (today - rec.work_date).days,
                "code": rec.code,
                "hours": None if rec.worked_hours is None else f"{rec.worked_hours:.2f}",
            }
        )

    rows.sort(key=lambda r: (r["state"] != "open", not r["blocks"], r["kind"], r["date"]))

    oldest = max(
        (r["age_days"] for r in rows if r["state"] == "open"), default=0
    )
    return {
        "rows": rows,
        "counts": counts,
        "filtered_count": len(rows),
        "open_total": open_total,
        "blocking_total": blocking_total,
        "oldest_open": oldest,
        "kind_filter": kind or "*",
        "show_resolved": show_resolved,
        "kinds": ["MP", "SS", "A", "LONG", "DUP"],
        "export_clear": blocking_total == 0,
    }


def dashboard_view(session: Session, period: str) -> dict:
    """Period-level rollup for the landing screen: headcount, attendance mix,
    the latest-day snapshot, exceptions, per-department totals, recent exports.
    """
    start, end, _ = period_bounds(period)

    active_emps = session.execute(
        select(Employee)
        .where(Employee.status != EmployeeStatus.inactive)
        .order_by(Employee.department)
    ).scalars().all()
    dept_headcount: dict[str, int] = {}
    for e in active_emps:
        dept_headcount[e.department] = dept_headcount.get(e.department, 0) + 1

    triples = session.execute(
        select(DayRecord, Employee)
        .join(Employee, DayRecord.employee_id == Employee.id)
        .where(DayRecord.work_date >= start, DayRecord.work_date <= end)
    ).all()

    codes = ["P", "A", "MP", "SS", "WO", "H"]
    code_counts = {c: 0 for c in codes}
    total_minutes = 0
    latest_day: dt.date | None = None
    attended_days: set[dt.date] = set()
    dept_roll: dict[str, dict] = {}
    for rec, emp in triples:
        if rec.code in code_counts:
            code_counts[rec.code] += 1
        if rec.worked_minutes:
            total_minutes += rec.worked_minutes
        if latest_day is None or rec.work_date > latest_day:
            latest_day = rec.work_date
        if rec.code in ("P", "SS", "MP"):
            attended_days.add(rec.work_date)
        b = dept_roll.setdefault(emp.department, {"present": 0, "minutes": 0})
        if rec.code in ("P", "SS"):
            b["present"] += 1
        if rec.worked_minutes:
            b["minutes"] += rec.worked_minutes

    total_records = sum(code_counts.values())
    mix = [
        {
            "code": c,
            "count": code_counts[c],
            "pct": round(100 * code_counts[c] / total_records, 1) if total_records else 0.0,
            "pal": PALETTE[c],
        }
        for c in codes
    ]

    exc = exceptions_view(session, period)
    dept_exc = dict(
        session.execute(
            select(Employee.department, func.count(Exception_.id))
            .join(DayRecord, Exception_.day_record_id == DayRecord.id)
            .join(Employee, DayRecord.employee_id == Employee.id)
            .where(
                Exception_.state == ExceptionState.open,
                DayRecord.work_date >= start,
                DayRecord.work_date <= end,
            )
            .group_by(Employee.department)
        ).all()
    )
    exc_kinds = [
        {"kind": k, "count": exc["counts"].get(k, 0), "pal": KIND_PALETTE.get(k, PALETTE[""])}
        for k in exc["kinds"]
        if exc["counts"].get(k, 0)
    ]

    departments_rows = []
    for d in sorted(dept_headcount):
        roll = dept_roll.get(d, {"present": 0, "minutes": 0})
        departments_rows.append(
            {
                "department": d,
                "employees": dept_headcount[d],
                "present_days": roll["present"],
                "hours": round(roll["minutes"] / 60, 1),
                "open_exceptions": dept_exc.get(d, 0),
            }
        )

    as_of = max(attended_days) if attended_days else (latest_day or end)
    as_of_iso = as_of.isoformat()
    roster = daily_roster(session, period, as_of_iso)
    snapshot = {
        "date_iso": as_of_iso,
        "date_label": f"{DOW[as_of.weekday()]} {as_of.day} {MON_SHORT[as_of.month]} {as_of.year}",
        "total": roster["total"],
        "present": roster["counts"]["P"] + roster["counts"]["SS"],
        "absent": roster["counts"]["A"],
        "pending": roster["counts"]["MP"] + roster["counts"]["SS"],
        "off": roster["counts"]["WO"] + roster["counts"]["H"],
    }

    tz = get_settings().timezone
    runs = session.execute(
        select(ExportRun).order_by(ExportRun.generated_at.desc()).limit(5)
    ).scalars().all()
    run_rows = [
        {
            "period": r.period,
            "kind": r.kind,
            "status": r.status.value,
            "generated_by": r.generated_by,
            "generated_at": r.generated_at.astimezone(tz).strftime("%d %b %Y · %H:%M"),
            "record_count": r.record_count,
            "gross_hours": r.gross_hours,
            "blocked_reason": r.blocked_reason,
        }
        for r in runs
    ]

    return {
        "period": period,
        "total_employees": len(active_emps),
        "gross_hours": round(total_minutes / 60, 1),
        "total_records": total_records,
        "mix": mix,
        "code_counts": code_counts,
        "open_exceptions": exc["open_total"],
        "blocking": exc["blocking_total"],
        "oldest_open": exc["oldest_open"],
        "export_clear": exc["export_clear"],
        "exc_kinds": exc_kinds,
        "departments": departments_rows,
        "snapshot": snapshot,
        "runs": run_rows,
    }


def stat_cards_dashboard(view: dict, t) -> list[dict]:
    exc = view["open_exceptions"]
    return [
        {"label": t("stat.total_employees"), "value": view["total_employees"],
         "fg": "#1f2430", "trend": ""},
        {"label": t("dash.present_snapshot"), "value": view["snapshot"]["present"],
         "fg": "#146c39", "trend": f'/ {view["snapshot"]["total"]}'},
        {"label": t("stat.open_exceptions"), "value": exc,
         "fg": "#96500a" if exc else "#146c39",
         "trend": f'{view["blocking"]} ⚠' if view["blocking"] else "—"},
        {"label": t("dash.gross_hours"), "value": view["gross_hours"],
         "fg": "#1f2430", "trend": t("dash.hours_unit")},
    ]


def stat_cards_monthly(grid: dict, t) -> list[dict]:
    total_emps = grid["pager"].total
    return [
        {"label": t("stat.total_employees"), "value": total_emps, "fg": "#1f2430", "trend": ""},
        {"label": t("stat.pending_review"), "value": grid["cells_need_review"],
         "fg": "#96500a", "trend": "⚠" if grid["cells_need_review"] else "—"},
    ]
