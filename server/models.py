"""Database schema.

Design rule that shapes everything below: `punches` is an immutable log of what
the device reported. `day_records` are *derived* and can be rebuilt from scratch
at any time by core.recompute. Manual HR fixes are `corrections` rows, never
edits to a punch. That separation is what makes an hours figure defensible when
payroll asks "why does this differ from the raw scan?".
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import DateTime as _SADateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """Always store naive UTC, always return tz-aware UTC.

    SQLite's DateTime is timezone-blind — without this, an aware value written
    now and an aware value compared later (idempotency checks, window filters)
    disagree. This makes the whole codebase see aware-UTC datetimes uniformly.
    """

    impl = _SADateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value  # assume already UTC
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.timezone.utc)


# Name kept as DateTime so the column declarations below read normally.
DateTime = UTCDateTime


# --------------------------------------------------------------------------- #
# Reference data                                                               #
# --------------------------------------------------------------------------- #


class EmployeeStatus(str, enum.Enum):
    active = "active"
    contract = "contract"
    notice = "notice"
    inactive = "inactive"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    emp_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # Talenta "Employee ID*" (e.g. CKI-A230065) — the key the enriched export
    # writes rows against. Nullable until the roster is mapped.
    talenta_id: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(128))
    department: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus, native_enum=False), default=EmployeeStatus.active
    )
    shift_key: Mapped[str] = mapped_column(String(16))
    roster_pattern: Mapped[str] = mapped_column(String(16))
    active_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    active_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    day_records: Mapped[list["DayRecord"]] = relationship(back_populates="employee")

    def employed_on(self, day: dt.date) -> bool:
        if self.active_from and day < self.active_from:
            return False
        if self.active_to and day > self.active_to:
            return False
        return self.status != EmployeeStatus.inactive


# --------------------------------------------------------------------------- #
# Immutable device log                                                         #
# --------------------------------------------------------------------------- #


class PunchSource(str, enum.Enum):
    device = "device"
    manual_import = "manual_import"


class Punch(Base):
    """One raw scan. Append-only. Never updated, never deleted."""

    __tablename__ = "punches"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "device_user_id", "punched_at", name="uq_punch_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(32), index=True)
    device_user_id: Mapped[str] = mapped_column(String(32), index=True)
    punched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw_punch_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[PunchSource] = mapped_column(
        Enum(PunchSource, native_enum=False), default=PunchSource.device
    )
    ingest_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingest_batches.id"), nullable=True
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IngestBatch(Base):
    """Audit row per sync from a site agent."""

    __tablename__ = "ingest_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    punches_submitted: Mapped[int] = mapped_column(Integer, default=0)
    punches_accepted: Mapped[int] = mapped_column(Integer, default=0)
    punches_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    client_batch_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    earliest_punch: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_punch: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Derived day records                                                          #
# --------------------------------------------------------------------------- #


class DayState(str, enum.Enum):
    open = "open"          # computed, may still change
    resolved = "resolved"  # HR looked at it / exception cleared
    final = "final"        # locked into an export run


class DayRecord(Base):
    __tablename__ = "day_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_dayrecord_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[dt.date] = mapped_column(Date, index=True)
    shift_key: Mapped[str] = mapped_column(String(16))

    first_in: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_out: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    punch_count: Mapped[int] = mapped_column(Integer, default=0)
    # NULL is meaningful: an MP day has no defensible hours figure. Never 0-as-unknown.
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    code: Mapped[str] = mapped_column(String(4), index=True)  # P/A/MP/SS/WO/H
    state: Mapped[DayState] = mapped_column(
        Enum(DayState, native_enum=False), default=DayState.open, index=True
    )
    has_correction: Mapped[bool] = mapped_column(Boolean, default=False)

    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    employee: Mapped[Employee] = relationship(back_populates="day_records")
    exceptions: Mapped[list["Exception_"]] = relationship(
        back_populates="day_record", cascade="all, delete-orphan"
    )
    corrections: Mapped[list["Correction"]] = relationship(
        back_populates="day_record", cascade="all, delete-orphan"
    )

    @property
    def worked_hours(self) -> float | None:
        return None if self.worked_minutes is None else round(self.worked_minutes / 60, 2)


class Correction(Base):
    """An HR override applied on top of the raw punches for one day record."""

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    day_record_id: Mapped[int] = mapped_column(ForeignKey("day_records.id"), index=True)
    field: Mapped[str] = mapped_column(String(32))       # first_in | last_out | code
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Corrections are kept even across recompute; superseded ones are marked stale.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    day_record: Mapped[DayRecord] = relationship(back_populates="corrections")


class ExceptionKind(str, enum.Enum):
    MP = "MP"       # missing punch (odd count)
    SS = "SS"       # short shift
    LONG = "LONG"   # implausibly long — likely missed clock-out
    A = "A"         # absent on a scheduled day
    DUP = "DUP"     # duplicate/ambiguous punches that could not be collapsed


class ExceptionState(str, enum.Enum):
    open = "open"
    resolved = "resolved"


class Exception_(Base):
    """A flagged day awaiting human review. Table name kept ORM-side as Exception_
    to avoid shadowing the builtin; SQL table is `exceptions`."""

    __tablename__ = "exceptions"
    __table_args__ = (
        UniqueConstraint("day_record_id", "kind", name="uq_exception_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    day_record_id: Mapped[int] = mapped_column(ForeignKey("day_records.id"), index=True)
    kind: Mapped[ExceptionKind] = mapped_column(Enum(ExceptionKind, native_enum=False), index=True)
    state: Mapped[ExceptionState] = mapped_column(
        Enum(ExceptionState, native_enum=False), default=ExceptionState.open, index=True
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    day_record: Mapped[DayRecord] = relationship(back_populates="exceptions")


# --------------------------------------------------------------------------- #
# Export runs                                                                  #
# --------------------------------------------------------------------------- #


class ExportStatus(str, enum.Enum):
    draft = "draft"
    complete = "complete"
    blocked = "blocked"


class ExportRun(Base):
    __tablename__ = "export_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    kind: Mapped[str] = mapped_column(String(8))                # draft | final
    status: Mapped[ExportStatus] = mapped_column(
        Enum(ExportStatus, native_enum=False), default=ExportStatus.draft
    )
    generated_by: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    gross_hours: Mapped[float] = mapped_column(Float, default=0.0)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    daily_file: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pivot_file: Mapped[str | None] = mapped_column(String(256), nullable=True)


class Holiday(Base):
    """DB-backed holidays layered on top of config/holidays.yaml so HR can add
    a site day without editing a file. Recompute reads the union of both."""

    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, unique=True)
    label: Mapped[str] = mapped_column(String(128), default="")


class RosterOverride(Base):
    """Explicit per-employee week-off / working-day override for a single date,
    for rotating crews whose pattern the weekly template can't express."""

    __tablename__ = "roster_overrides"
    __table_args__ = (
        UniqueConstraint("employee_id", "day", name="uq_roster_override"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    working: Mapped[bool] = mapped_column(Boolean)  # True = working, False = week-off
    note: Mapped[str] = mapped_column(String(128), default="")
