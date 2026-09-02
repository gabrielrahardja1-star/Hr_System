"""Test fixtures.

Each test gets a fresh temp SQLite database and clean config caches. Env vars
are set before any `server.*` import so config picks them up.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HR_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("HR_TIMEZONE", "Asia/Jakarta")
    monkeypatch.setenv("HR_INGEST_API_KEYS", "test-key")
    monkeypatch.setenv("HR_SHORT_SHIFT_HOURS", "6.0")
    monkeypatch.setenv("HR_LONG_SHIFT_HOURS", "16.0")
    monkeypatch.setenv("HR_CHATTER_WINDOW_SECONDS", "90")

    import server.config as config

    config.reset_caches()

    # db module binds an engine at import time — rebuild it against the temp path.
    import server.db as db

    importlib.reload(db)
    db.init_db()

    yield db

    config.reset_caches()


@pytest.fixture()
def session(env):
    s = env.SessionLocal()
    try:
        yield s
    finally:
        s.close()


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
    ):
        emp = Employee(
            device_user_id=device_user_id,
            emp_code=emp_code,
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
