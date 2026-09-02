"""Is a given employee scheduled to work on a given date?

Answer comes from, in priority order:
  1. an explicit RosterOverride row for that employee+date
  2. the weekly roster pattern (config/shifts.yaml -> roster_patterns)
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import get_holidays, get_shift_config
from server.models import Holiday, RosterOverride


def is_holiday(session: Session, day: dt.date) -> bool:
    if day in get_holidays():
        return True
    return session.execute(
        select(Holiday.id).where(Holiday.day == day)
    ).first() is not None


def _pattern_says_working(pattern_key: str, day: dt.date) -> bool:
    patterns = get_shift_config().roster_patterns
    pattern = patterns.get(pattern_key)
    if not pattern:
        # Unknown pattern -> assume working so nothing is silently dropped.
        return True
    # Monday = index 0
    return pattern[day.weekday() % len(pattern)].upper() == "W"


def is_scheduled_working(
    session: Session, employee_id: int, roster_pattern: str, day: dt.date
) -> bool:
    override = session.execute(
        select(RosterOverride.working).where(
            RosterOverride.employee_id == employee_id,
            RosterOverride.day == day,
        )
    ).scalar_one_or_none()
    if override is not None:
        return bool(override)
    return _pattern_says_working(roster_pattern, day)
