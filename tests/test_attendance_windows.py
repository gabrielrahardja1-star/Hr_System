"""Shift-window attribution and chatter collapse, tested as pure functions."""

from __future__ import annotations

import datetime as dt

WIB = dt.timezone(dt.timedelta(hours=7))


def test_day_shift_window_same_calendar_day(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("S1")
    win = shift_window_utc(shift, dt.date(2026, 8, 3))
    # 05:00 WIB -> 20:00 WIB on the 3rd == 22:00 UTC the 2nd .. 13:00 UTC the 3rd
    assert win.start_utc == dt.datetime(2026, 8, 2, 22, 0, tzinfo=dt.timezone.utc)
    assert win.end_utc == dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)


def test_evening_shift_window_rolls_into_next_day(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("S2")
    win = shift_window_utc(shift, dt.date(2026, 8, 3))
    # 13:00 WIB on the 3rd -> 04:00 WIB on the 4th
    assert win.start_utc == dt.datetime(2026, 8, 3, 6, 0, tzinfo=dt.timezone.utc)
    assert win.end_utc == dt.datetime(2026, 8, 3, 21, 0, tzinfo=dt.timezone.utc)
    assert win.contains(dt.datetime(2026, 8, 3, 17, 0, tzinfo=dt.timezone.utc))  # 00:00 WIB the 4th


def test_night_shift_window_rolls_into_next_day(env):
    from server.config import get_shift_config
    from server.core.attendance import shift_window_utc

    shift = get_shift_config().get("S3")
    win = shift_window_utc(shift, dt.date(2026, 8, 3))
    # 20:00 WIB on the 3rd -> 12:00 WIB on the 4th
    assert win.start_utc == dt.datetime(2026, 8, 3, 13, 0, tzinfo=dt.timezone.utc)
    assert win.end_utc == dt.datetime(2026, 8, 4, 5, 0, tzinfo=dt.timezone.utc)
    assert win.contains(dt.datetime(2026, 8, 3, 16, 0, tzinfo=dt.timezone.utc))  # 23:00 WIB the 3rd
    assert win.contains(dt.datetime(2026, 8, 4, 0, 0, tzinfo=dt.timezone.utc))   # 07:00 WIB the 4th
    # the next day's own window must not swallow this shift's clock-out
    assert not shift_window_utc(shift, dt.date(2026, 8, 4)).contains(
        dt.datetime(2026, 8, 4, 0, 0, tzinfo=dt.timezone.utc)
    )


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
