# MergeCoal — Attendance → Hours

Turns fingerprint-terminal punches into **accurate daily hours-worked data** in a
shape the existing Excel payroll formulas can consume. It does **not** do payroll
math (tax, BPJS, THR, overtime rates) — that stays in Excel.

```
SITE (offline-tolerant)                       HQ
┌──────────────────────────┐                 ┌────────────────────────────┐
│ Deli E-13750 ──eth──▶ PC │                 │ management system          │
│   agent (Phase 4)        │   POST batch    │  FastAPI + SQLite          │
│    ├ pyzk poll           │ ─ ─ ─ ─ ─ ─ ─ ▶ │  /api/v1/punches           │
│    └ local spool ◀ retry │  when link up   │  web UI :8000 (Phase 2)    │
└──────────────────────────┘                 └────────────┬───────────────┘
                                                          │ CSV export
                                                          ▼   existing Excel
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
| ⬜ Phase 2 | review web UI (Monthly grid, Daily roster, Exceptions queue, manual edit) |
| ⬜ Phase 3 | Export Runs screen, Wage Mapping screen |
| ⬜ Phase 4 | site agent (pyzk poller + spool + PyInstaller build) |
| ⬜ Phase 5 | Excel template integration — **blocked until the real template is shared** |

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults work as-is for local dev
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

```bash
pytest -q
```
Covers the coding fixture table (night shift, midnight cross, single punch,
duplicate scans, out-of-order arrival, zero punches, >ceiling), the
"MP hours are null never zero" guarantee, recompute idempotency, ingest
idempotency (including partial-overlap batches), and the export gate
(blocked → resolve → succeeds).
