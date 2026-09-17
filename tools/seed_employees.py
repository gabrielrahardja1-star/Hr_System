"""Seed the employee roster with realistic Indonesian mining-site data.

Usage:
    python -m tools.seed_employees            # ~60 employees, idempotent
    python -m tools.seed_employees --count 150
    python -m tools.seed_employees --reset    # wipe employees first

This is a stand-in for a real roster import. The device_user_id values here
(1001, 1002, ...) are what tools/mock_punch_source.py and, later, the real
fingerprint terminal will report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random

from sqlalchemy import delete, select

from server.db import init_db, session_scope
from server.models import Employee, EmployeeStatus

FIRST = [
    "Budi", "Siti", "Agus", "Dewi", "Eko", "Rina", "Joko", "Wati", "Slamet",
    "Ani", "Bambang", "Yuni", "Hendra", "Sri", "Rudi", "Endang", "Dedi",
    "Nurul", "Fajar", "Lestari", "Wahyu", "Indah", "Rizki", "Puji", "Arif",
]
LAST = [
    "Santoso", "Wijaya", "Kusuma", "Pratama", "Nugroho", "Halim", "Saputra",
    "Hidayat", "Susanto", "Gunawan", "Setiawan", "Firmansyah", "Maulana",
    "Purnomo", "Hartono",
]
DEPARTMENTS = [
    ("Operasi Tambang", "KERJA"),
    ("Hauling", "KERJA"),
    ("Pengolahan", "KERJA"),
    ("Maintenance", "KERJA"),
    ("Survey & Geologi", "KERJA"),
    ("HSE", "KERJA"),
    ("Gudang", "KERJA"),
    ("Listrik", "KERJA"),
]
# Staff who keep office hours rather than rotating with the crews.
OFFICE_DEPTS = {"Survey & Geologi", "HSE"}
ROSTERS = ["six_day_sun_off", "five_day_weekend", "continuous"]


def build_employees(count: int) -> list[dict]:
    rng = random.Random(42)
    out = []
    for i in range(count):
        dept, shift = DEPARTMENTS[i % len(DEPARTMENTS)]
        first = FIRST[i % len(FIRST)]
        last = LAST[(i * 7 + 3) % len(LAST)]
        status = rng.choices(
            [EmployeeStatus.active, EmployeeStatus.contract, EmployeeStatus.notice],
            weights=[0.8, 0.15, 0.05],
        )[0]
        out.append(
            {
                "device_user_id": str(1001 + i),
                "emp_code": f"KM-{1400 + i * 3}",
                "talenta_id": f"CKI-A23{4000 + i * 3:04d}",
                "name": f"{first} {last}",
                "department": dept,
                "status": status,
                "shift_key": shift,
                "roster_pattern": "five_day_weekend"
                if dept in OFFICE_DEPTS
                else ROSTERS[i % 2],
                "active_from": dt.date(2025, 1, 1),
            }
        )
    return out


def seed(count: int, reset: bool) -> None:
    init_db()
    with session_scope() as session:
        if reset:
            session.execute(delete(Employee))
        existing = {
            e for e in session.execute(select(Employee.device_user_id)).scalars()
        }
        added = 0
        for row in build_employees(count):
            if row["device_user_id"] in existing:
                continue
            session.add(Employee(**row))
            added += 1
    print(f"seed_employees: {added} added, target {count}, reset={reset}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    seed(args.count, args.reset)
