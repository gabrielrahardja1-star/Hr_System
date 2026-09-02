"""Export gate + CSV shape.

The gate is the load-bearing rule: a FINAL export must refuse while the period
has open MP/SS exceptions, and must succeed once they are resolved.
"""

from __future__ import annotations

import csv
import datetime as dt

WIB = dt.timezone(dt.timedelta(hours=7))


def _wib(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=WIB)


def _seed_month(make_employee, add_punches, session):
    """One clean employee, one with a missing punch on the 5th."""
    from server.core.recompute import recompute_all

    clean = make_employee(
        device_user_id="1001", emp_code="KM-1400", name="Budi Santoso",
        shift_key="A", roster_pattern="continuous",
    )
    messy = make_employee(
        device_user_id="1002", emp_code="KM-1403", name="Siti Wijaya",
        shift_key="A", roster_pattern="continuous",
    )
    for day in range(1, 11):
        add_punches("1001", [_wib(2026, 8, day, 6, 0), _wib(2026, 8, day, 14, 30)])
        if day == 5:
            add_punches("1002", [_wib(2026, 8, 5, 6, 0)])  # missing out -> MP
        else:
            add_punches("1002", [_wib(2026, 8, day, 6, 0), _wib(2026, 8, day, 14, 30)])

    recompute_all(session, dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    session.commit()
    return clean, messy


def test_final_export_blocked_then_allowed(make_employee, add_punches, session):
    from server.core.exceptions import resolve_exception
    from server.core.export import run_export
    from server.models import DayRecord, Exception_, ExceptionState

    _seed_month(make_employee, add_punches, session)

    blocked = run_export(session, "2026-08", kind="final", generated_by="rina")
    assert blocked.blocked is True
    assert blocked.blocking_exceptions >= 1
    assert blocked.run.status.value == "blocked"

    # a draft is always allowed
    draft = run_export(session, "2026-08", kind="draft", generated_by="rina")
    assert draft.blocked is False
    assert draft.daily_path.exists()

    # resolve every open blocking exception (MP/SS) and try final again
    from server.models import ExceptionKind

    blocking = (
        session.query(Exception_)
        .filter(
            Exception_.state == ExceptionState.open,
            Exception_.kind.in_([ExceptionKind.MP, ExceptionKind.SS]),
        )
        .all()
    )
    assert blocking, "seed should have produced at least one MP"
    for exc in blocking:
        resolve_exception(session, exc.id, "rina", "supervisor log confirms")
    session.commit()

    ok = run_export(session, "2026-08", kind="final", generated_by="rina")
    assert ok.blocked is False
    assert ok.run.status.value == "complete"
    assert ok.daily_path.exists() and ok.pivot_path.exists()


def test_daily_csv_has_mapped_columns(make_employee, add_punches, session):
    from server.core.export import run_export

    _seed_month(make_employee, add_punches, session)
    out = run_export(session, "2026-08", kind="draft")

    with out.daily_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    assert rows, "expected data rows"
    assert set(rows[0]) >= {
        "Kode Karyawan", "Nama", "Tanggal", "Jam Kerja", "Kode", "Pengecualian"
    }
    mp_rows = [r for r in rows if r["Kode"] == "MP"]
    assert mp_rows, "the messy employee should have an MP day"
    # MP hours must be blank, never "0"
    assert mp_rows[0]["Jam Kerja"] == ""
    assert "MP" in mp_rows[0]["Pengecualian"]


def test_pivot_csv_is_employee_by_day(make_employee, add_punches, session):
    from server.core.export import run_export

    _seed_month(make_employee, add_punches, session)
    out = run_export(session, "2026-08", kind="draft")

    with out.pivot_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))

    header = rows[0]
    assert header[:3] == ["Kode Karyawan", "Nama", "Departemen"]
    assert "1" in header and "31" in header
    assert "Total Jam" in header
    # one row per employee
    assert len(rows) - 1 == 2
