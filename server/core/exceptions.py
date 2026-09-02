"""Exception lifecycle.

`sync_exceptions` reconciles the exceptions attached to a day record with the
set the coder just produced. It is idempotent — running recompute twice never
duplicates a row and never re-opens something HR already resolved unless the
underlying condition materially changed.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import (
    DayRecord,
    Exception_,
    ExceptionKind,
    ExceptionState,
)


def sync_exceptions(
    session: Session,
    day_record: DayRecord,
    wanted: list[tuple[ExceptionKind, str]],
) -> None:
    wanted_by_kind = {kind: detail for kind, detail in wanted}

    existing = session.execute(
        select(Exception_).where(Exception_.day_record_id == day_record.id)
    ).scalars().all()
    existing_by_kind = {e.kind: e for e in existing}

    # Open or update the ones the coder wants.
    for kind, detail in wanted_by_kind.items():
        row = existing_by_kind.get(kind)
        if row is None:
            session.add(
                Exception_(
                    day_record_id=day_record.id,
                    kind=kind,
                    state=ExceptionState.open,
                    detail=detail,
                )
            )
        elif row.state == ExceptionState.open:
            row.detail = detail  # refresh wording, keep it open

    # Auto-close ones that are no longer applicable.
    for kind, row in existing_by_kind.items():
        if kind not in wanted_by_kind and row.state == ExceptionState.open:
            row.state = ExceptionState.resolved
            row.resolved_at = dt.datetime.now(dt.timezone.utc)
            row.resolver = "system"
            row.resolution = "Condition cleared on recompute"


def resolve_exception(
    session: Session,
    exception_id: int,
    resolver: str,
    resolution: str,
) -> Exception_:
    row = session.get(Exception_, exception_id)
    if row is None:
        raise LookupError(f"exception {exception_id} not found")
    row.state = ExceptionState.resolved
    row.resolved_at = dt.datetime.now(dt.timezone.utc)
    row.resolver = resolver
    row.resolution = resolution
    return row


def open_exceptions_for_period(session: Session, period: str) -> list[Exception_]:
    """period is YYYY-MM. Returns open exceptions whose day falls in that month."""
    year, month = (int(p) for p in period.split("-"))
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), (month % 12) + 1, 1)
    return session.execute(
        select(Exception_)
        .join(DayRecord, Exception_.day_record_id == DayRecord.id)
        .where(
            Exception_.state == ExceptionState.open,
            DayRecord.work_date >= start,
            DayRecord.work_date < end,
        )
        .order_by(DayRecord.work_date)
    ).scalars().all()
