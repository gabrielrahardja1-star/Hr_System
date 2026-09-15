# Attendance → Talenta (PT Cinta Kerja Indonesia)

Turns fingerprint-terminal punches into an **accurate, reviewed daily attendance
file that imports into Mekari Talenta**. Talenta is the payroll engine — base
pay, overtime, tax, BPJS all stay there. This system only produces trustworthy
Check In / Check Out / Attendance Code values and flags what a human must fix.

```
SITE (offline-tolerant)                       HQ
┌──────────────────────────┐                 ┌────────────────────────────┐
│ Deli E-13750 ──eth──▶ PC │                 │ management system          │
│   agent (Phase 4)        │   POST batch    │  FastAPI + Postgres         │
│    ├ pyzk poll           │ ─ ─ ─ ─ ─ ─ ─ ▶ │  /api/v1/punches            │
│    └ local spool ◀ retry │  when link up   │  web UI :8000               │
└──────────────────────────┘                 └────────────┬───────────────┘
                                       Talenta export (skeleton)  │
                                            ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ▼
                                      fill Check In / Check Out / Code
                                                             │
                                              re-import  ────▶  Talenta
```

**Source-of-truth rule:** `punches` is an immutable log of exactly what the
device reported. Day records (hours + status code) are *derived* and can be
rebuilt any time. HR fixes are `corrections` rows — the raw punch is never
edited. That's what makes an adjusted hours figure defensible at payroll time.

---

## Status — Phase 1 complete (core domain, headless)

| | |
|---|---|
| ✅ Phase 1 | ingest API, hours engine, status coding, exceptions, CSV export, CLI tools, device probe, tests |
| ✅ Phase 2 | review web UI — Dashboard rollup, Monthly grid, Daily roster, Exceptions queue, HTMX cell detail + manual correction, id/en toggle |
| 🟡 Phase 5 | **Talenta export** — enrich a Talenta "Import Attendance" skeleton with reviewed Check In / Check Out / Attendance Code (`/payroll-export`, `tools.manage talenta`). Working; pending the client's full Attendance-Code list |
| ⬜ Phase 3 | Wage Mapping screen, export-run history |
| ⬜ Phase 4 | site agent (pyzk poller + spool + PyInstaller build) |

### Talenta export (Phase 5)

The payroll period is **26th → 25th** (period `2026-08` = 2026-07-26 … 2026-08-25).

1. HR exports the scheduled attendance template from Talenta for the period.
2. Upload it at **`/payroll-export`** (or `python -m tools.manage talenta
   --skeleton <file> --period 2026-08 --kind draft`). The system fills only
   `Attendance Code`, `Check In`, `Check Out` from reviewed punch data; every
   other column passes through byte-for-byte.
3. A `final` run is refused while any working-day row is still *held* (missing
   punch, unresolved short shift, employee not mapped, no punch data). A `draft`
   run fills what it can and lists the rest.
4. Download the result, re-import to Talenta.

`config/talenta_export.yaml` holds the column map and the code map
(`P`/`SS` → `H` today; extend when the client sends the full Talenta code list).
Employees are matched by `Employee.talenta_id` ↔ `device_user_id`.

### The web UI

```bash
make serve            # or: uvicorn server.main:app --port 8000
# open http://localhost:8000  → redirects to /dashboard
```

Screens: **/dashboard** (period rollup — headcount, attendance-mix bar,
latest-day snapshot, exceptions by kind, per-department totals, recent export
runs, and an export-readiness banner), **/monthly** (employee × day grid, click
any cell for punch detail + "Edit punch"), **/daily** (single-day roster),
**/exceptions** (the review queue that gates a final export — filter by kind,
resolve, or run the export from here). Language toggle top-right, Indonesian by
default. Editing a punch writes a `corrections` row and recomputes that one day;
the raw device punch is untouched.

Known Phase-2 gaps: the exception *detail sentence* ("1 punch — need an even
count…") is still English only (the labels, codes, and legend are localised);
the Payroll Export screen is still a stub marked "soon".

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # adjust DATABASE_URL if you're not using docker compose

docker compose up -d postgres      # local Postgres 16 on :5434
alembic upgrade head               # apply the schema
```

No Docker? A Homebrew `postgresql@14` (or any local Postgres) works too — create
a role/db yourself and point `DATABASE_URL` at it:
```bash
psql -d postgres -c "CREATE ROLE hr_system WITH LOGIN PASSWORD 'hr_system' CREATEDB;"
psql -d postgres -c "CREATE DATABASE hr_system OWNER hr_system;"
DATABASE_URL=postgresql+psycopg://hr_system:hr_system@localhost:5432/hr_system alembic upgrade head
```

> **macOS note (this dev machine):** the Homebrew Python 3.12–3.14 builds on this
> machine have a broken `pyexpat` after the OS upgrade (`Symbol not found:
> _XML_SetAllocTrackerActivationThreshold`). Until it's fixed, every Python
> command needs:
> ```bash
> export DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
> ```
> Permanent fix: `brew update && brew reinstall python@3.14` (or use pyenv /
> python.org). This does **not** affect the Windows site PC.

---

## Run it now (`--mock`, no device, fully offline)

Two terminals.

**Terminal 1 — the HQ server:**
```bash
uvicorn server.main:app --port 8000
```

**Terminal 2 — seed a roster, then push synthetic punches through the real API:**
```bash
python -m tools.seed_employees --count 60 --reset
python -m tools.mock_punch_source --period 2026-08 --days 28 --twice
```
`--twice` re-sends every batch to prove idempotency (2nd pass: 0 accepted, all
duplicate). The mock deliberately injects missing punches, short shifts, absences,
reader chatter, and a missed clock-out so the exceptions report has something to
catch.

**Then, from the CLI (stands in for the Phase 2 UI):**
```bash
python -m tools.manage recompute  --period 2026-08
python -m tools.manage exceptions --period 2026-08          # the review queue
python -m tools.manage export     --period 2026-08 --kind draft
python -m tools.manage export     --period 2026-08 --kind final   # BLOCKED while MP/SS open

# fix a forgotten clock-out (time is site-local):
python -m tools.manage correct --emp KM-1402 --date 2026-08-12 \
    --set-out "2026-08-12 14:40" --reason "supervisor shift log" --actor rina

python -m tools.manage export --period 2026-08 --kind final   # now succeeds
```

CSVs land in `data/exports/` — `hours_daily_<period>.csv` (one row per
employee-day) and `hours_pivot_<period>.csv` (employee × day-of-month grid).

---

## On site (`--ip`, real device) — 3-day validation

**Day 1, before anything else — test the one unproven assumption** (that the Deli
E-13750 speaks the ZK protocol):
```bash
pip install pyzk
python agent/probe_device.py --ip 192.168.1.201
```
- **exit 0** → ZK works. Proceed to build the Phase 4 agent against this device.
- **exit 2** → ZK doesn't work here. Fall back to USB/CSV export; a CSV importer
  POSTs the *same* `/api/v1/punches` payload, so nothing downstream changes.

Save the full probe output either way — it tells us the firmware quirks the agent
must handle.

The HQ server and the review workflow above are identical whether punches arrive
from the live agent or a CSV import.

---

## Status codes

| Code | Meaning | Hours | Blocks final export? |
|---|---|---|---|
| `P`  | Present, full shift | recorded | no |
| `SS` | Short shift — under the per-shift threshold | recorded | **yes** |
| `MP` | Missing punch (odd count, usually a forgotten clock-out) | **null, never 0** | **yes** |
| `A`  | Absent on a scheduled day | 0 | no |
| `WO` | Rostered week-off | — | no |
| `H`  | Declared site holiday | — | no |

`LONG` is an extra flag on a `P` day (worked hours over the ceiling — likely a
missed clock-out); reviewed but does not block. `DUP` flags punches on a rostered
week-off.

---

## Config (`config/`)

| file | what |
|---|---|
| `shifts.yaml` | shift windows, per-shift short/long thresholds, weekly roster patterns |
| `holidays.yaml` | declared holidays (2026 Indonesian national days pre-filled) |
| `wage_mapping.yaml` | code → payroll component + multiplier (carried to CSV; **never applied here**) |
| `export_mapping.yaml` | CSV column names / order / formats — **this is where the real Excel template gets matched in Phase 5** |

---

## Tests

Needs a real Postgres (`TEST_DATABASE_URL`, separate db from dev — each test
runs in its own rolled-back transaction, nothing persists):
```bash
psql -d postgres -c "CREATE DATABASE hr_system_test OWNER hr_system;"   # once
TEST_DATABASE_URL=postgresql+psycopg://hr_system:hr_system@localhost:5432/hr_system_test \
    pytest -q
```
Covers the coding fixture table (night shift, midnight cross, single punch,
duplicate scans, out-of-order arrival, zero punches, >ceiling), the
"MP hours are null never zero" guarantee, recompute idempotency, ingest
idempotency (including partial-overlap batches), and the export gate
(blocked → resolve → succeeds).

---

## Deploy (VPS, Docker)

```bash
# local
git add -A && git commit -m "..." && git push

# server — /opt/hr_system, separate compose project from any other app on the box
ssh <user>@<vps-host>
cd /opt/hr_system
git pull
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec backend alembic upgrade head   # if schema changed
```

`.env` on the server is not committed — `cp .env.example .env` and fill in a
real `DATABASE_URL`/`POSTGRES_PASSWORD` pair and `HR_INGEST_API_KEYS` (not
`dev-local-key`). Logs: `docker compose -f docker-compose.prod.yml logs -f backend`.
