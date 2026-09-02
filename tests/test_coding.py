"""The coding engine's contract, expressed as a fixture table.

Each case seeds punches for one Shift-A employee on one day, runs recompute, and
asserts (code, hours, open-exception-kinds). WIB = UTC+7, no DST.
"""

from __future__ import annotations

import datetime as dt

import pytest

WIB = dt.timezone(dt.timedelta(hours=7))


def _wib(y, m, d, hh, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=WIB)


# A Monday (working day for six_day_sun_off) with nothing special in config.
DAY = dt.date(2026, 8, 3)


CASES = [
    # name,                punch times (WIB),                       code, hours, exceptions
    ("full shift",         [_wib(2026, 8, 3, 6, 2), _wib(2026, 8, 3, 14, 35)], "P",  8.55, set()),
    ("short shift",        [_wib(2026, 8, 3, 6, 0), _wib(2026, 8, 3, 9, 30)],  "SS", 3.5,  {"SS"}),
    ("single punch / MP",  [_wib(2026, 8, 3, 6, 5)],                            "MP", None, {"MP"}),
    ("three punches / MP", [_wib(2026, 8, 3, 6, 0), _wib(2026, 8, 3, 10, 0), _wib(2026, 8, 3, 14, 0)], "MP", None, {"MP"}),
    ("no punches / absent",[],                                                  "A",  0.0,  {"A"}),
    ("chatter absorbed",   [_wib(2026, 8, 3, 6, 0), _wib(2026, 8, 3, 6, 0, 20), _wib(2026, 8, 3, 14, 30)], "P", 8.5, set()),
    # 06:00 -> 19:45 is 13.75h, over Shift A's 12.0h ceiling: still P, flagged LONG.
    ("long / missed out",  [_wib(2026, 8, 3, 6, 0), _wib(2026, 8, 3, 19, 45)],  "P",  13.75, {"LONG"}),
    ("out-of-order punches",[_wib(2026, 8, 3, 14, 30), _wib(2026, 8, 3, 6, 0)], "P",  8.5,  set()),
]


@pytest.mark.parametrize("name,times,exp_code,exp_hours,exp_exc", CASES, ids=[c[0] for c in CASES])
def test_coding_case(make_employee, add_punches, session, name, times, exp_code, exp_hours, exp_exc):
    from server.core.recompute import recompute_employee
    from server.models import DayRecord, Exception_, ExceptionState

    emp = make_employee()
    add_punches(emp.device_user_id, times, device_id="DEV-1")

    recompute_employee(session, emp, DAY, DAY)
    session.commit()

    rec = (
        session.query(DayRecord)
        .filter_by(employee_id=emp.id, work_date=DAY)
        .one()
    )
    assert rec.code == exp_code, name
    assert rec.worked_hours == exp_hours, name

    open_kinds = {
        e.kind.value
        for e in session.query(Exception_).filter_by(
            day_record_id=rec.id, state=ExceptionState.open
        )
    }
    assert open_kinds == exp_exc, name


def test_mp_hours_are_null_never_zero(make_employee, add_punches, session):
    """The core guarantee: a forgotten clock-out never becomes a payable 0."""
    from server.core.recompute import recompute_employee
    from server.models import DayRecord

    emp = make_employee()
    add_punches(emp.device_user_id, [_wib(2026, 8, 3, 6, 5)])
    recompute_employee(session, emp, DAY, DAY)
    session.commit()

    rec = session.query(DayRecord).filter_by(employee_id=emp.id, work_date=DAY).one()
    assert rec.worked_minutes is None
    assert rec.worked_hours is None
    assert rec.code == "MP"


def test_week_off_and_holiday(make_employee, add_punches, session):
    from server.core.recompute import recompute_employee
    from server.models import DayRecord, Holiday

    emp = make_employee(roster_pattern="six_day_sun_off")

    sunday = dt.date(2026, 8, 2)     # week-off for this pattern
    recompute_employee(session, emp, sunday, sunday)
    session.commit()
    rec = session.query(DayRecord).filter_by(employee_id=emp.id, work_date=sunday).one()
    assert rec.code == "WO"

    holiday = dt.date(2026, 8, 17)   # Hari Kemerdekaan, a Monday
    session.add(Holiday(day=holiday, label="Kemerdekaan"))
    session.commit()
    recompute_employee(session, emp, holiday, holiday)
    session.commit()
    rec = session.query(DayRecord).filter_by(employee_id=emp.id, work_date=holiday).one()
    assert rec.code == "H"


def test_recompute_is_idempotent(make_employee, add_punches, session):
    from server.core.recompute import recompute_employee
    from server.models import DayRecord, Exception_

    emp = make_employee()
    add_punches(emp.device_user_id, [_wib(2026, 8, 3, 6, 5)])

    for _ in range(3):
        recompute_employee(session, emp, DAY, DAY)
        session.commit()

    assert session.query(DayRecord).filter_by(employee_id=emp.id).count() == 1
    assert session.query(Exception_).count() == 1


def test_night_shift_attribution(make_employee, add_punches, session):
    """A Shift-B punch after midnight belongs to the date the shift started."""
    from server.core.recompute import recompute_employee
    from server.models import DayRecord

    emp = make_employee(shift_key="B", roster_pattern="continuous")
    # in 18:10 on the 3rd, out 02:20 on the 4th
    add_punches(
        emp.device_user_id,
        [_wib(2026, 8, 3, 18, 10), _wib(2026, 8, 4, 2, 20)],
    )
    recompute_employee(session, emp, dt.date(2026, 8, 3), dt.date(2026, 8, 4))
    session.commit()

    rec_3 = session.query(DayRecord).filter_by(employee_id=emp.id, work_date=dt.date(2026, 8, 3)).one()
    assert rec_3.code == "P"
    assert rec_3.worked_hours == pytest.approx(8.17, abs=0.02)
    # the 4th should have no punches attributed to it from that pair
    rec_4 = session.query(DayRecord).filter_by(employee_id=emp.id, work_date=dt.date(2026, 8, 4)).one()
    assert rec_4.code in ("A", "MP")
