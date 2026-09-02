"""Assign a status code (P/A/MP/SS/WO/H) and any exception flags to one day.

This is the heart of the "never a silent zero" rule:

    | code | when                                    | hours   | exception |
    | H    | declared holiday, scheduled day         | -       | -         |
    | WO   | not scheduled to work, no punches       | -       | -         |
    | A    | scheduled day, zero punches             | 0       | A         |
    | MP   | odd number of punches (usually 1)       | NULL    | MP        |
    | SS   | 2+ punches, worked hours < short_hours   | actual  | SS        |
    | P    | worked hours between short and long      | actual  | (LONG if > long_hours) |

LONG is an exception flag on a P cell, not its own code — a >12-16h span almost
always means a missed clock-out, and that must be reviewed before it inflates
payroll, but the grid still shows P.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from server.config import ShiftDef
from server.core.attendance import DayPunchSummary
from server.models import ExceptionKind


@dataclass(frozen=True)
class CodingResult:
    code: str
    worked_minutes: int | None
    exceptions: list[tuple[ExceptionKind, str]] = field(default_factory=list)


def code_day(
    summary: DayPunchSummary,
    shift: ShiftDef,
    *,
    scheduled_working: bool,
    is_holiday: bool,
) -> CodingResult:
    count = summary.punch_count
    worked_minutes = summary.worked_minutes

    # --- non-working outcomes ---------------------------------------------- #
    if is_holiday and count == 0:
        # A declared holiday with no punches is H whether or not it was a
        # rostered working day — H is paid, WO is not.
        return CodingResult(code="H", worked_minutes=None)

    if not scheduled_working:
        if count == 0:
            return CodingResult(code="WO", worked_minutes=None)
        # Punched on a rostered day off — pay it as worked but flag for review.
        # Falls through to the working path below with a LONG/normal decision,
        # but we tag it so HR sees it in the queue.
        # (Represented as a P/SS with an extra note; simplest is to treat like a
        # normal working day and add a DUP-style note.)

    if count == 0:
        # Scheduled, not a holiday, nobody scanned.
        return CodingResult(
            code="A",
            worked_minutes=0,
            exceptions=[(ExceptionKind.A, "No punches on a scheduled working day")],
        )

    if summary.is_odd:
        # 1 punch (forgot to clock out), or 3 (a missed pair). No honest span.
        return CodingResult(
            code="MP",
            worked_minutes=None,
            exceptions=[
                (
                    ExceptionKind.MP,
                    f"{count} punch(es) — need an even count for a clock-in/out pair",
                )
            ],
        )

    # --- even count, real span -------------------------------------------- #
    assert worked_minutes is not None
    hours = worked_minutes / 60
    short_h = shift.short_hours
    long_h = shift.long_hours

    exceptions: list[tuple[ExceptionKind, str]] = []

    if not scheduled_working:
        exceptions.append(
            (ExceptionKind.DUP, "Worked on a rostered week-off — confirm before paying")
        )

    if hours < short_h:
        exceptions.append(
            (
                ExceptionKind.SS,
                f"Worked {hours:.2f}h, below the {short_h:.2f}h full-shift threshold",
            )
        )
        return CodingResult(code="SS", worked_minutes=worked_minutes, exceptions=exceptions)

    if hours > long_h:
        exceptions.append(
            (
                ExceptionKind.LONG,
                f"Worked {hours:.2f}h, above the {long_h:.2f}h ceiling — likely a "
                f"missed clock-out",
            )
        )
        return CodingResult(code="P", worked_minutes=worked_minutes, exceptions=exceptions)

    return CodingResult(code="P", worked_minutes=worked_minutes, exceptions=exceptions)
