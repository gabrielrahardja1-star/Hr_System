"""Employee search on the attendance screens."""

from __future__ import annotations

import datetime as dt


def _seed(make_employee):
    make_employee(device_user_id="1001", emp_code="KM-1400", name="Budi Santoso",
                  department="Operasi Tambang", shift_key="S1", roster_pattern="continuous")
    make_employee(device_user_id="1002", emp_code="KM-1403", name="Siti Wijaya",
                  department="Hauling", shift_key="S1", roster_pattern="continuous")
    make_employee(device_user_id="1003", emp_code="KM-1406", name="Budi Hartono",
                  department="Gudang", shift_key="S1", roster_pattern="continuous")


def test_monthly_grid_search_by_name(make_employee, session):
    from server.web.viewmodels import monthly_grid

    _seed(make_employee)
    g = monthly_grid(session, "2026-08", search="budi")
    names = {r["name"] for r in g["rows"]}
    assert names == {"Budi Santoso", "Budi Hartono"}
    assert g["pager"].total == 2
    assert g["search"] == "budi"


def test_monthly_grid_search_by_code_and_device_id(make_employee, session):
    from server.web.viewmodels import monthly_grid

    _seed(make_employee)
    assert {r["name"] for r in monthly_grid(session, "2026-08", search="KM-1403")["rows"]} == {"Siti Wijaya"}
    assert {r["name"] for r in monthly_grid(session, "2026-08", search="1001")["rows"]} == {"Budi Santoso"}


def test_search_no_match_is_empty(make_employee, session):
    from server.web.viewmodels import daily_roster, monthly_grid

    _seed(make_employee)
    assert monthly_grid(session, "2026-08", search="zzz")["rows"] == []
    assert daily_roster(session, "2026-08", "2026-08-10", search="zzz")["rows"] == []


def test_daily_roster_search(make_employee, session):
    from server.web.viewmodels import daily_roster

    _seed(make_employee)
    r = daily_roster(session, "2026-08", "2026-08-10", search="wijaya")
    assert [row["name"] for row in r["rows"]] == ["Siti Wijaya"]
    assert r["total"] == 1
