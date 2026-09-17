"""Ingest idempotency and the recompute hand-off.

Calls the endpoint function directly with a Session — no HTTP layer needed to
prove the contract.
"""

from __future__ import annotations

import datetime as dt

import pytest

WIB = dt.timezone(dt.timedelta(hours=7))


def _batch(punches, ref="b1"):
    from server.schemas import PunchBatch

    return PunchBatch(
        agent_id="agent-1", device_id="DEV-1", client_batch_ref=ref, punches=punches
    )


def _punch(uid, when):
    from server.schemas import PunchIn

    return PunchIn(device_user_id=uid, punched_at=when)


def test_batch_is_idempotent(make_employee, session):
    from server.api.ingest import ingest_punches
    from server.models import Punch

    make_employee(device_user_id="1001")
    punches = [
        _punch("1001", dt.datetime(2026, 8, 3, 6, 0, tzinfo=WIB)),
        _punch("1001", dt.datetime(2026, 8, 3, 14, 30, tzinfo=WIB)),
    ]

    r1 = ingest_punches(_batch(punches), session=session, _key="test-key")
    assert (r1.accepted, r1.duplicate) == (2, 0)

    r2 = ingest_punches(_batch(punches), session=session, _key="test-key")
    assert (r2.accepted, r2.duplicate) == (0, 2)

    assert session.query(Punch).count() == 2


def test_partial_overlap_batch(make_employee, session):
    from server.api.ingest import ingest_punches
    from server.models import Punch

    make_employee(device_user_id="1001")
    a = _punch("1001", dt.datetime(2026, 8, 3, 6, 0, tzinfo=WIB))
    b = _punch("1001", dt.datetime(2026, 8, 3, 14, 30, tzinfo=WIB))
    c = _punch("1001", dt.datetime(2026, 8, 4, 6, 0, tzinfo=WIB))

    ingest_punches(_batch([a, b]), session=session, _key="test-key")
    r = ingest_punches(_batch([b, c], ref="b2"), session=session, _key="test-key")

    assert (r.accepted, r.duplicate) == (1, 1)
    assert session.query(Punch).count() == 3


def test_duplicates_within_one_batch(make_employee, session):
    from server.api.ingest import ingest_punches

    make_employee(device_user_id="1001")
    p = _punch("1001", dt.datetime(2026, 8, 3, 6, 0, tzinfo=WIB))
    r = ingest_punches(_batch([p, p, p]), session=session, _key="test-key")
    assert (r.accepted, r.duplicate) == (1, 2)


def test_ingest_triggers_recompute(make_employee, session):
    from server.api.ingest import ingest_punches
    from server.models import DayRecord

    make_employee(device_user_id="1001", shift_key="KERJA", roster_pattern="continuous")
    ingest_punches(
        _batch(
            [
                _punch("1001", dt.datetime(2026, 8, 3, 8, 0, tzinfo=WIB)),
                _punch("1001", dt.datetime(2026, 8, 3, 16, 30, tzinfo=WIB)),
            ]
        ),
        session=session,
        _key="test-key",
    )
    rec = session.query(DayRecord).filter_by(work_date=dt.date(2026, 8, 3)).one()
    assert rec.code == "P"
    assert rec.worked_hours == 8.5


def test_naive_timestamp_treated_as_utc():
    from server.schemas import PunchIn

    p = PunchIn(device_user_id="1001", punched_at=dt.datetime(2026, 8, 3, 6, 0))
    assert p.punched_at.tzinfo is dt.timezone.utc

    p2 = PunchIn(
        device_user_id="1001",
        punched_at=dt.datetime(2026, 8, 3, 13, 0, tzinfo=WIB),
    )
    assert p2.punched_at.utcoffset() == dt.timedelta(0)
    assert p2.punched_at.hour == 6
