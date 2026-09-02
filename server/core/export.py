"""CSV export, driven entirely by config/export_mapping.yaml.

Two files per run:
  * daily long  — one row per employee per day
  * monthly pivot — employee x day-of-month grid (mirrors the Monthly Review screen)

Export gate
-----------
A `final` run refuses to write while the period still has open exceptions of a
kind flagged `blocks_export` in config/wage_mapping.yaml (MP, SS by default). It
records itself as `blocked` with a reason. A `draft` run always writes.
"""

from __future__ import annotations

import csv
import datetime as dt
from calendar import monthrange
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import (
    get_export_mapping,
    get_settings,
    get_shift_config,
    get_wage_mapping,
)
from server.core.exceptions import open_exceptions_for_period
from server.models import (
    DayRecord,
    Employee,
    ExportRun,
    ExportStatus,
)

CODE_NAMES = {
    "P": "Hadir",
    "A": "Tidak Hadir",
    "MP": "Absen Tidak Lengkap",
    "SS": "Shift Pendek",
    "WO": "Libur Mingguan",
    "H": "Libur",
}


@dataclass
class ExportOutcome:
    run: ExportRun
    blocked: bool
    blocking_exceptions: int
    daily_path: Path | None
    pivot_path: Path | None


def _period_bounds(period: str) -> tuple[dt.date, dt.date, int]:
    year, month = (int(p) for p in period.split("-"))
    days_in_month = monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, days_in_month), days_in_month


def _row_fields(session: Session, rec: DayRecord, emp: Employee, mapping: dict) -> dict:
    tz = get_settings().timezone
    wage = get_wage_mapping()
    shift = get_shift_config().shifts.get(rec.shift_key)

    def local(v: dt.datetime | None) -> str:
        if v is None:
            return mapping.get("missing_value", "")
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return v.astimezone(tz).strftime(mapping.get("time_format", "%H:%M"))

    hours = rec.worked_hours
    hours_str = (
        mapping.get("missing_value", "")
        if hours is None
        else f"{hours:.{mapping.get('hours_decimals', 2)}f}"
    )
    open_flags = sorted({e.kind.value for e in rec.exceptions if e.state.value == "open"})

    mult = wage.multiplier(rec.code)
    return {
        "emp_code": emp.emp_code,
        "emp_name": emp.name,
        "department": emp.department,
        "work_date": rec.work_date.strftime(mapping.get("date_format", "%Y-%m-%d")),
        "shift": rec.shift_key,
        "shift_name": shift.name if shift else rec.shift_key,
        "clock_in": local(rec.first_in),
        "clock_out": local(rec.last_out),
        "hours": hours_str,
        "code": rec.code,
        "code_name": CODE_NAMES.get(rec.code, rec.code),
        "exception_flags": "|".join(open_flags),
        "wage_component": wage.component(rec.code),
        "wage_multiplier": "" if mult is None else f"{mult:.2f}",
        "state": rec.state.value,
    }


def _records_for_period(
    session: Session, period: str
) -> list[tuple[DayRecord, Employee]]:
    start, end, _ = _period_bounds(period)
    rows = session.execute(
        select(DayRecord, Employee)
        .join(Employee, DayRecord.employee_id == Employee.id)
        .where(DayRecord.work_date >= start, DayRecord.work_date <= end)
        .order_by(Employee.emp_code, DayRecord.work_date)
    ).all()
    return [(r[0], r[1]) for r in rows]


def check_export_gate(session: Session, period: str) -> tuple[bool, int, str]:
    """Returns (blocked, count, reason)."""
    wage = get_wage_mapping()
    blocking = [
        e
        for e in open_exceptions_for_period(session, period)
        if wage.code_blocks_export(e.kind.value) or wage.flag_blocks_export(e.kind.value)
    ]
    if not blocking:
        return False, 0, ""
    by_kind: dict[str, int] = {}
    for e in blocking:
        by_kind[e.kind.value] = by_kind.get(e.kind.value, 0) + 1
    summary = ", ".join(f"{k}×{v}" for k, v in sorted(by_kind.items()))
    return True, len(blocking), f"{len(blocking)} open blocking exception(s): {summary}"


def run_export(
    session: Session,
    period: str,
    *,
    kind: str = "draft",
    generated_by: str = "system",
) -> ExportOutcome:
    settings = get_settings()
    mapping = get_export_mapping()
    settings.export_dir.mkdir(parents=True, exist_ok=True)

    blocked, blocking_count, reason = (False, 0, "")
    if kind == "final":
        blocked, blocking_count, reason = check_export_gate(session, period)

    run = ExportRun(
        period=period,
        kind=kind,
        generated_by=generated_by,
        status=ExportStatus.blocked if blocked else ExportStatus.draft,
        blocked_reason=reason or None,
    )
    session.add(run)
    session.flush()

    if blocked:
        session.commit()
        return ExportOutcome(run, True, blocking_count, None, None)

    pairs = _records_for_period(session, period)
    rows = [_row_fields(session, rec, emp, mapping) for rec, emp in pairs]

    daily_path = _write_daily_long(rows, period, mapping, settings.export_dir)
    pivot_path = _write_monthly_pivot(pairs, period, mapping, settings.export_dir)

    gross = sum(
        (rec.worked_minutes or 0) for rec, _ in pairs
    ) / 60.0

    run.record_count = len(rows)
    run.gross_hours = round(gross, 2)
    run.status = ExportStatus.complete if kind == "final" else ExportStatus.draft
    run.daily_file = daily_path.name
    run.pivot_file = pivot_path.name
    session.commit()

    return ExportOutcome(run, False, 0, daily_path, pivot_path)


def _write_daily_long(rows: list[dict], period: str, mapping: dict, out_dir: Path) -> Path:
    spec = mapping["daily_long"]
    path = out_dir / spec["filename"].format(period=period)
    headers = list(spec["columns"].keys())
    fields = list(spec["columns"].values())
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(f, "") for f in fields])
    return path


def _write_monthly_pivot(
    pairs: list[tuple[DayRecord, Employee]],
    period: str,
    mapping: dict,
    out_dir: Path,
) -> Path:
    spec = mapping["monthly_pivot"]
    _, _, days_in_month = _period_bounds(period)
    path = out_dir / spec["filename"].format(period=period)

    id_headers = list(spec["id_columns"].keys())
    id_fields = list(spec["id_columns"].values())
    trailing_headers = list(spec["trailing_columns"].keys())
    day_mode = spec.get("day_cell", "hours_or_code")
    missing = mapping.get("missing_value", "")
    hours_dec = mapping.get("hours_decimals", 2)

    # group by employee
    by_emp: dict[int, dict] = {}
    for rec, emp in pairs:
        bucket = by_emp.setdefault(
            emp.id,
            {"emp": emp, "days": {}, "total_minutes": 0, "present": 0, "open_exc": 0},
        )
        bucket["days"][rec.work_date.day] = rec
        if rec.worked_minutes:
            bucket["total_minutes"] += rec.worked_minutes
        if rec.code in ("P", "SS"):
            bucket["present"] += 1
        bucket["open_exc"] += sum(1 for e in rec.exceptions if e.state.value == "open")

    def cell(rec: DayRecord | None) -> str:
        if rec is None:
            return ""
        if day_mode == "code":
            return rec.code
        if day_mode == "hours":
            return "" if rec.worked_minutes is None else f"{rec.worked_minutes / 60:.{hours_dec}f}"
        # hours_or_code
        if rec.code in ("P", "SS") and rec.worked_minutes is not None:
            return f"{rec.worked_minutes / 60:.{hours_dec}f}"
        if rec.code == "MP":
            return missing or "MP"
        return rec.code

    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            id_headers + [str(d) for d in range(1, days_in_month + 1)] + trailing_headers
        )
        attr_for = {"emp_code": "emp_code", "emp_name": "name", "department": "department"}
        for bucket in sorted(by_emp.values(), key=lambda b: b["emp"].emp_code):
            emp = bucket["emp"]
            id_values = [getattr(emp, attr_for[f]) for f in id_fields]
            day_values = [cell(bucket["days"].get(d)) for d in range(1, days_in_month + 1)]
            trailing = [
                f"{bucket['total_minutes'] / 60:.{hours_dec}f}",
                bucket["present"],
                bucket["open_exc"],
            ]
            writer.writerow(id_values + day_values + trailing)
    return path
