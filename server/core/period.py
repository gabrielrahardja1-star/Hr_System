"""Payroll period arithmetic.

PT Cinta Kerja Indonesia runs a **26th → 25th** payroll cycle. A period is
labelled by the month it ENDS in: period "2026-08" covers 2026-07-26 through
2026-08-25 inclusive.
"""

from __future__ import annotations

import datetime as dt

CUTOVER_DAY = 26  # first day of a period


def talenta_period_bounds(label: str) -> tuple[dt.date, dt.date]:
    """label 'YYYY-MM' -> (start, end) inclusive, 26th of previous month to 25th."""
    year, month = (int(p) for p in label.split("-"))
    end = dt.date(year, month, CUTOVER_DAY - 1)
    if month == 1:
        start = dt.date(year - 1, 12, CUTOVER_DAY)
    else:
        start = dt.date(year, month - 1, CUTOVER_DAY)
    return start, end


def talenta_period_for(day: dt.date) -> str:
    """Which period label a given date falls in."""
    if day.day >= CUTOVER_DAY:
        # belongs to next month's period
        y, m = (day.year + (day.month == 12)), (day.month % 12) + 1
        return f"{y:04d}-{m:02d}"
    return f"{day.year:04d}-{day.month:02d}"


def period_label(start: dt.date, end: dt.date) -> str:
    return talenta_period_for(start)
