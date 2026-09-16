"""HTTP-level smoke tests for the review UI.

These exist because a SQLite-only `strftime()` in `available_periods` survived
the Postgres migration and took every page down — the suite tested viewmodels
directly and never once asked the app for a response.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import unquote

import pytest

WIB = dt.timezone(dt.timedelta(hours=7))

PAGES = ["/dashboard", "/daily", "/monthly", "/exceptions", "/payroll-export", "/employees"]


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient

    from server.db import get_session
    from server.main import app

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, make_employee, path):
    make_employee()
    assert client.get(path).status_code == 200


def test_employee_can_be_created_on_the_night_shift(client, session):
    from server.models import Employee

    resp = client.post(
        "/employees",
        data={
            "device_user_id": "7001", "name": "Malam Tester", "emp_code": "KM-9001",
            "talenta_id": "", "department": "Operasi Tambang", "shift_key": "S3",
            "roster_pattern": "continuous", "status": "active",
            "active_from": "2026-09-17", "active_to": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    emp = session.query(Employee).filter_by(device_user_id="7001").one()
    assert emp.shift_key == "S3"


def test_unknown_shift_is_rejected(client, session):
    from server.models import Employee

    resp = client.post(
        "/employees",
        data={
            "device_user_id": "7002", "name": "Bad Shift", "emp_code": "KM-9002",
            "talenta_id": "", "department": "Ops", "shift_key": "B",
            "roster_pattern": "continuous", "status": "active",
            "active_from": "", "active_to": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Unknown shift" in unquote(resp.headers["location"])
    assert session.query(Employee).filter_by(device_user_id="7002").one_or_none() is None


def test_duplicate_device_id_is_rejected(client, make_employee, session):
    from server.models import Employee

    make_employee(device_user_id="7003", name="First Claim", emp_code="KM-1")
    resp = client.post(
        "/employees",
        data={
            "device_user_id": "7003", "name": "Second Claim", "emp_code": "KM-2",
            "talenta_id": "", "department": "Ops", "shift_key": "S1",
            "roster_pattern": "continuous", "status": "active",
            "active_from": "", "active_to": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "already belongs to First Claim" in unquote(resp.headers["location"])
    assert session.query(Employee).filter_by(emp_code="KM-2").one_or_none() is None


def test_punches_with_no_employee_are_surfaced(client, add_punches, make_employee):
    """A kiosk enrolment with a typo'd ID must not vanish silently."""
    make_employee(device_user_id="1001")
    add_punches("K0001", [
        dt.datetime(2026, 9, 16, 23, 10, tzinfo=WIB),
        dt.datetime(2026, 9, 17, 7, 5, tzinfo=WIB),
    ])
    body = client.get("/employees").text
    assert "K0001" in body


def test_utc_datetime_survives_a_non_utc_db_session(session, add_punches):
    """replace(tzinfo=utc) on an already-aware value would shift the instant."""
    from sqlalchemy import text

    from server.models import Punch

    session.execute(text("SET TIME ZONE 'Asia/Jakarta'"))
    add_punches("9100", [dt.datetime(2026, 9, 16, 23, 10, tzinfo=WIB)])
    stored = session.query(Punch).filter_by(device_user_id="9100").one().punched_at
    assert stored == dt.datetime(2026, 9, 16, 16, 10, tzinfo=dt.timezone.utc)
