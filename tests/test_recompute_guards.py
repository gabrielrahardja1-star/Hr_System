"""Guards added after the first on-screen review:
  * days after the last ingested punch are blank, not 'Absent'
  * a correction that makes clock-out <= clock-in never pays a negative shift
"""

from __future__ import annotations

import datetime as dt

WIB = dt.timezone(dt.timedelta(hours=7))


def _wib(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=WIB)


def test_days_after_frontier_are_not_absent(make_employee, add_punches, session):
    from server.core.recompute import recompute_all
    from server.models import DayRecord

    emp = make_employee(shift_key="A", roster_pattern="continuous")
    # punches only through Aug 5
    for d in range(3, 6):
        add_punches(emp.device_user_id, [_wib(2026, 8, d, 6, 0), _wib(2026, 8, d, 14, 30)])

    recompute_all(session, dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    session.commit()

    dates = {r.work_date for r in session.query(DayRecord).all()}
    assert max(dates) == dt.date(2026, 8, 5)          # nothing past the frontier
    assert not session.query(DayRecord).filter(
        DayRecord.work_date > dt.date(2026, 8, 5)
    ).count()


def test_frontier_can_be_overridden(make_employee, add_punches, session):
    from server.core.recompute import recompute_all
    from server.models import DayRecord

    emp = make_employee(shift_key="A", roster_pattern="continuous")
    add_punches(emp.device_user_id, [_wib(2026, 8, 3, 6, 0), _wib(2026, 8, 3, 14, 30)])

    recompute_all(session, dt.date(2026, 8, 3), dt.date(2026, 8, 6), respect_frontier=False)
    session.commit()
    # Aug 4-6 now exist and are Absent (scheduled, no punches)
    a = session.query(DayRecord).filter_by(work_date=dt.date(2026, 8, 5)).one()
    assert a.code == "A"


def test_correction_never_pays_negative(make_employee, add_punches, session):
    from server.core.recompute import recompute_employee
    from server.models import Correction, DayRecord, Employee

    emp = make_employee(shift_key="B", roster_pattern="continuous")
    # night shift: single clock-in at 18:10 -> MP
    add_punches(emp.device_user_id, [_wib(2026, 8, 3, 18, 10)])
    recompute_employee(session, emp, dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    session.commit()
    rec = session.query(DayRecord).filter_by(work_date=dt.date(2026, 8, 3)).one()

    # HR enters a clock-out of 02:00 but forgets it is the NEXT day -> 02:00 same day
    session.add(Correction(
        day_record_id=rec.id, field="last_out",
        new_value="2026-08-03 02:00", reason="typo", actor="hr",
    ))
    session.flush()
    recompute_employee(session, emp, dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    session.commit()

    session.refresh(rec)
    assert rec.worked_minutes is None            # not negative, not zero
    assert rec.code == "MP"                      # still flagged for a real fix

    # now the correct next-day value
    session.add(Correction(
        day_record_id=rec.id, field="last_out",
        new_value="2026-08-04 02:00", reason="fixed", actor="hr",
    ))
    session.flush()
    recompute_employee(session, emp, dt.date(2026, 8, 3), dt.date(2026, 8, 3))
    session.commit()
    session.refresh(rec)
    assert rec.code == "P"
    assert rec.worked_hours == round((8 * 60 - 10) / 60, 2)  # 18:10 -> 02:00
