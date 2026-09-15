"""Test fixtures.

Needs a real Postgres (`TEST_DATABASE_URL`, default points at the Homebrew
postgresql@14 instance — `docker compose up -d postgres` + a `hr_system_test`
db works too). One engine/schema for the whole run; each test gets its own
transaction that's rolled back at teardown (SQLAlchemy 2.0 "join savepoint"
pattern — `session.commit()` inside app code like `ingest_punches` nests as a
savepoint instead of actually persisting), so tests stay isolated without
recreating all tables per test the way the old per-test SQLite file did.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://hr_system:hr_system@localhost:5432/hr_system_test",
    ),
)
os.environ.setdefault("HR_TIMEZONE", "Asia/Jakarta")
os.environ.setdefault("HR_INGEST_API_KEYS", "test-key")
os.environ.setdefault("HR_SHORT_SHIFT_HOURS", "6.0")
os.environ.setdefault("HR_LONG_SHIFT_HOURS", "16.0")
os.environ.setdefault("HR_CHATTER_WINDOW_SECONDS", "90")


@pytest.fixture(scope="session")
def _engine():
    from server.models import Base

    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def env(tmp_path, monkeypatch, _engine):
    monkeypatch.setenv("HR_EXPORT_DIR", str(tmp_path / "exports"))

    import server.config as config

    config.reset_caches()

    import server.db as db

    yield db

    config.reset_caches()


@pytest.fixture()
def session(env, _engine):
    connection = _engine.connect()
    trans = connection.begin()
    TestSession = sessionmaker(
        bind=connection, autoflush=False, expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    s = TestSession()
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        connection.close()


@pytest.fixture()
def make_employee(session):
    from server.models import Employee, EmployeeStatus

    created = []

    def _make(
        device_user_id="1001",
        emp_code="KM-1400",
        name="Budi Santoso",
        department="Operasi Tambang",
        shift_key="A",
        roster_pattern="six_day_sun_off",
        status=EmployeeStatus.active,
        active_from=dt.date(2025, 1, 1),
        talenta_id=None,
    ):
        emp = Employee(
            device_user_id=device_user_id,
            emp_code=emp_code,
            talenta_id=talenta_id,
            name=name,
            department=department,
            shift_key=shift_key,
            roster_pattern=roster_pattern,
            status=status,
            active_from=active_from,
        )
        session.add(emp)
        session.commit()
        created.append(emp)
        return emp

    return _make


@pytest.fixture()
def add_punches(session):
    from server.models import Punch, PunchSource

    def _add(device_user_id: str, times: list[dt.datetime], device_id="DEV-1"):
        for t in times:
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            session.add(
                Punch(
                    device_id=device_id,
                    device_user_id=device_user_id,
                    punched_at=t.astimezone(dt.timezone.utc),
                    source=PunchSource.device,
                )
            )
        session.commit()

    return _add
