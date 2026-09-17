"""The single work-day window, and chatter collapse, as pure functions.

There is one shift. A working day runs 07:30 to 07:30 the next morning — the
hour nobody is on site — so every pattern worked here falls inside one window
and is credited to the day it started.
"""

from __future__ import annotations

import datetime as dt

WIB = dt.timezone(dt.timedelta(hours=7))


def test_the_day_runs_0730_to_0730(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("KERJA")
    win = shift_window_utc(shift, dt.date(2026, 8, 3))
    # 07:30 local on the 3rd -> 07:30 local on the 4th
    assert win.start_utc == dt.datetime(2026, 8, 3, 0, 30, tzinfo=dt.timezone.utc)
    assert win.end_utc == dt.datetime(2026, 8, 4, 0, 30, tzinfo=dt.timezone.utc)
    assert (win.end_utc - win.start_utc) == dt.timedelta(hours=24)


def test_a_night_spans_one_window_not_two(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("KERJA")
    win = shift_window_utc(shift, dt.date(2026, 8, 3))
    clock_in = dt.datetime(2026, 8, 3, 23, 0, tzinfo=WIB)     # 23:00 on the 3rd
    clock_out = dt.datetime(2026, 8, 4, 7, 0, tzinfo=WIB)     # 07:00 on the 4th
    assert win.contains(clock_in.astimezone(dt.timezone.utc))
    assert win.contains(clock_out.astimezone(dt.timezone.utc))
    # ...and the next day must not also claim that clock-out
    assert not shift_window_utc(shift, dt.date(2026, 8, 4)).contains(
        clock_out.astimezone(dt.timezone.utc)
    )


def test_consecutive_days_neither_overlap_nor_leave_a_gap(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("KERJA")
    first = shift_window_utc(shift, dt.date(2026, 8, 3))
    second = shift_window_utc(shift, dt.date(2026, 8, 4))
    # Touching exactly: no punch can land in both windows or in neither.
    assert first.end_utc == second.start_utc


def test_chatter_collapse():
    from server.core.attendance import collapse_chatter

    base = dt.datetime(2026, 8, 3, 6, 0, tzinfo=dt.timezone.utc)
    times = [
        base,
        base + dt.timedelta(seconds=15),
        base + dt.timedelta(seconds=40),
        base + dt.timedelta(hours=8),
        base + dt.timedelta(hours=8, seconds=10),
    ]
    kept = collapse_chatter(times, window_seconds=90)
    assert kept == [base, base + dt.timedelta(hours=8)]


def test_chatter_collapse_unsorted_input():
    from server.core.attendance import collapse_chatter

    base = dt.datetime(2026, 8, 3, 6, 0, tzinfo=dt.timezone.utc)
    times = [base + dt.timedelta(hours=8), base, base + dt.timedelta(seconds=5)]
    assert collapse_chatter(times, window_seconds=90) == [base, base + dt.timedelta(hours=8)]
