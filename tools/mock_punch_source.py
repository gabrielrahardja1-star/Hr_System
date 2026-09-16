"""Generate synthetic punches and POST them through the REAL ingest API.

This is the stand-in for the site agent during development. It exercises the
exact same code path a real sync will: HTTP -> /api/v1/punches -> idempotency
check -> recompute -> exceptions.

Usage:
    # in one terminal:
    uvicorn server.main:app
    # in another:
    python -m tools.seed_employees
    python -m tools.mock_punch_source --days 30 --period 2026-08

It deliberately injects the edge cases the exceptions report must catch:
    ~4% of shifts   -> only a clock-in, no clock-out   (MP)
    ~3% of shifts   -> a very short shift               (SS)
    ~1% of shifts   -> no punches at all                (A)
    ~1% of shifts   -> a duplicate scan 20s later       (chatter, absorbed)
    ~1% of shifts   -> forgot to clock out, picked up next morning (LONG)
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
from calendar import monthrange
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from server.config import get_shift_config
from server.db import session_scope
from server.models import Employee

WIB = ZoneInfo("Asia/Jakarta")


def _shift_times(shift_key: str) -> tuple[dt.time, dt.time, bool]:
    s = get_shift_config().get(shift_key)
    return s.start, s.end, s.crosses_midnight


def generate_punches(days: int, period: str, seed: int) -> tuple[str, list[dict]]:
    rng = random.Random(seed)
    year, month = (int(p) for p in period.split("-"))
    last_day = monthrange(year, month)[1]
    end_day = min(days, last_day)

    with session_scope() as session:
        employees = list(session.execute(select(Employee)).scalars())

    device_id = "MOCK-DELI-01"
    punches: list[dict] = []

    for emp in employees:
        if not emp.shift_key:
            continue  # no shift assigned yet — nothing to generate against
        start, end, crosses = _shift_times(emp.shift_key)
        for day in range(1, end_day + 1):
            work_date = dt.date(year, month, day)
            # weekend-ish skip for 5-day roster
            if emp.roster_pattern == "five_day_weekend" and work_date.weekday() >= 5:
                if rng.random() > 0.1:
                    continue
            if emp.roster_pattern == "six_day_sun_off" and work_date.weekday() == 6:
                if rng.random() > 0.05:
                    continue

            roll = rng.random()
            jitter_in = dt.timedelta(minutes=rng.randint(-8, 12))
            in_dt = dt.datetime.combine(work_date, start, tzinfo=WIB) + jitter_in
            out_date = work_date + dt.timedelta(days=1) if crosses else work_date
            out_dt = dt.datetime.combine(out_date, end, tzinfo=WIB) + dt.timedelta(
                minutes=rng.randint(-5, 40)
            )

            if roll < 0.01:
                continue  # A: no punches
            if roll < 0.05:
                punches.append(_p(device_id, emp, in_dt))  # MP: in only
                continue
            if roll < 0.08:
                short_out = in_dt + dt.timedelta(hours=rng.uniform(2.0, 4.5))
                punches += [_p(device_id, emp, in_dt), _p(device_id, emp, short_out)]
                continue
            if roll < 0.09:
                # LONG: forgot to clock out; next-morning scan absorbed as out
                late_out = in_dt + dt.timedelta(hours=rng.uniform(17, 22))
                punches += [_p(device_id, emp, in_dt), _p(device_id, emp, late_out)]
                continue

            punches.append(_p(device_id, emp, in_dt))
            if roll < 0.10:
                punches.append(_p(device_id, emp, in_dt + dt.timedelta(seconds=20)))
            punches.append(_p(device_id, emp, out_dt))

    punches.sort(key=lambda p: p["punched_at"])
    return device_id, punches


def _p(device_id: str, emp: Employee, when: dt.datetime) -> dict:
    return {
        "device_user_id": emp.device_user_id,
        "punched_at": when.astimezone(dt.timezone.utc).isoformat(),
        "raw_punch_type": 0,
        "raw_status": 1,
    }


def post_in_batches(
    base_url: str, api_key: str, agent_id: str, device_id: str, punches: list[dict], size: int
) -> None:
    client = httpx.Client(base_url=base_url, headers={"X-API-Key": api_key}, timeout=60)
    totals = {"submitted": 0, "accepted": 0, "duplicate": 0}
    for i in range(0, len(punches), size):
        chunk = punches[i : i + size]
        resp = client.post(
            "/api/v1/punches",
            json={
                "agent_id": agent_id,
                "device_id": device_id,
                "client_batch_ref": f"mock-{i // size:04d}",
                "punches": chunk,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        for k in totals:
            totals[k] += body[k]
        print(f"  batch {i // size:>3}: {body['accepted']} accepted, {body['duplicate']} dup")
    print(f"done: {totals}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="dev-local-key")
    parser.add_argument("--agent-id", default="mock-agent")
    parser.add_argument("--period", default=dt.date.today().strftime("%Y-%m"))
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--twice", action="store_true", help="POST every batch twice to prove idempotency"
    )
    args = parser.parse_args()

    device_id, punches = generate_punches(args.days, args.period, args.seed)
    print(f"generated {len(punches)} punches for {args.period} (through day {args.days})")
    post_in_batches(
        args.url, args.api_key, args.agent_id, device_id, punches, args.batch_size
    )
    if args.twice:
        print("re-posting identical batches (expect 0 accepted, all duplicate):")
        post_in_batches(
            args.url, args.api_key, args.agent_id, device_id, punches, args.batch_size
        )
