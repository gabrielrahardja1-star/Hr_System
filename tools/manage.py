"""Headless operations for Phase 1 — everything the UI will later do, from the CLI.

    python -m tools.manage recompute --period 2026-08
    python -m tools.manage exceptions --period 2026-08
    python -m tools.manage export --period 2026-08 --kind draft      # legacy CSV
    python -m tools.manage talenta --skeleton talenta_aug.xlsx --period 2026-08 --kind draft
    python -m tools.manage talenta --skeleton talenta_aug.xlsx --period 2026-08 --kind final
    python -m tools.manage correct --emp KM-1402 --date 2026-08-12 \
        --set-out "2026-08-12 14:40" --reason "supervisor log confirms" --actor rina
"""

from __future__ import annotations

import argparse
import datetime as dt
from calendar import monthrange

from sqlalchemy import select

from server.core.export import run_export
from server.core.recompute import recompute_all
from server.db import init_db, session_scope
from server.models import (
    Correction,
    DayRecord,
    Employee,
    Exception_,
    ExceptionState,
)


def _period_range(period: str) -> tuple[dt.date, dt.date]:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1), dt.date(year, month, monthrange(year, month)[1])


def cmd_recompute(args) -> None:
    start, end = _period_range(args.period)
    with session_scope() as session:
        result = recompute_all(session, start, end)
    total = sum(len(v) for v in result.values())
    print(f"recompute {args.period}: {total} day-records across {len(result)} employees")


def cmd_exceptions(args) -> None:
    from server.config import get_wage_mapping

    start, end = _period_range(args.period)
    wage = get_wage_mapping()

    def blocks(kind: str) -> bool:
        return wage.code_blocks_export(kind) or wage.flag_blocks_export(kind)

    with session_scope() as session:
        rows = session.execute(
            select(Exception_, DayRecord, Employee)
            .join(DayRecord, Exception_.day_record_id == DayRecord.id)
            .join(Employee, DayRecord.employee_id == Employee.id)
            .where(
                Exception_.state == ExceptionState.open,
                DayRecord.work_date >= start,
                DayRecord.work_date <= end,
            )
        ).all()

        # Blocking kinds first (they hold up the payroll cut-off), then by date.
        rows.sort(key=lambda r: (not blocks(r[0].kind.value), r[0].kind.value, r[1].work_date))

        by_kind: dict[str, int] = {}
        blocking_total = 0
        print(f"\nOpen exceptions for {args.period} — {len(rows)} total\n" + "-" * 78)
        for exc, rec, emp in rows:
            k = exc.kind.value
            by_kind[k] = by_kind.get(k, 0) + 1
            if blocks(k):
                blocking_total += 1
            hrs = "  —  " if rec.worked_hours is None else f"{rec.worked_hours:>5.2f}h"
            mark = "!" if blocks(k) else " "
            print(
                f" {mark}{k:<4} {rec.work_date}  {emp.emp_code:<9} "
                f"{emp.name:<22} {rec.code:<3} {hrs}  {exc.detail}"
            )
        print("-" * 78)
        print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())))
        print(
            f"  {blocking_total} of {len(rows)} block a final export (marked !). "
            f"A / DUP / LONG do not."
        )


def cmd_export(args) -> None:
    with session_scope() as session:
        outcome = run_export(
            session, args.period, kind=args.kind, generated_by=args.actor
        )
    if outcome.blocked:
        print(
            f"EXPORT BLOCKED ({args.period}, final): {outcome.blocking_exceptions} "
            f"open blocking exception(s). Resolve them, then re-run.\n"
            f"  reason: {outcome.run.blocked_reason}"
        )
        return
    print(
        f"export {args.period} [{args.kind}] -> {outcome.run.status.value}\n"
        f"  records: {outcome.run.record_count}   gross hours: {outcome.run.gross_hours}\n"
        f"  daily : {outcome.daily_path}\n"
        f"  pivot : {outcome.pivot_path}"
    )


def cmd_correct(args) -> None:
    with session_scope() as session:
        emp = session.execute(
            select(Employee).where(Employee.emp_code == args.emp)
        ).scalar_one_or_none()
        if emp is None:
            raise SystemExit(f"no employee {args.emp}")
        day = dt.date.fromisoformat(args.date)
        rec = session.execute(
            select(DayRecord).where(
                DayRecord.employee_id == emp.id, DayRecord.work_date == day
            )
        ).scalar_one_or_none()
        if rec is None:
            raise SystemExit(f"no day record for {args.emp} on {args.date} — recompute first")

        added = []
        if args.set_in:
            session.add(Correction(
                day_record_id=rec.id, field="first_in",
                old_value=str(rec.first_in), new_value=args.set_in,
                reason=args.reason, actor=args.actor,
            ))
            added.append("first_in")
        if args.set_out:
            session.add(Correction(
                day_record_id=rec.id, field="last_out",
                old_value=str(rec.last_out), new_value=args.set_out,
                reason=args.reason, actor=args.actor,
            ))
            added.append("last_out")
        if args.set_code:
            session.add(Correction(
                day_record_id=rec.id, field="code",
                old_value=rec.code, new_value=args.set_code,
                reason=args.reason, actor=args.actor,
            ))
            added.append("code")
        session.flush()

        from server.core.recompute import recompute_employee
        recompute_employee(session, emp, day, day)
        session.refresh(rec)
    print(
        f"correction applied to {args.emp} {args.date}: {', '.join(added)}\n"
        f"  now: code={rec.code} in={rec.first_in} out={rec.last_out} "
        f"hours={rec.worked_hours}"
    )


def cmd_talenta(args) -> None:
    from server.core.talenta_export import enrich_skeleton

    with session_scope() as session:
        r = enrich_skeleton(
            session, args.skeleton, args.period, kind=args.kind, out_path=args.out
        )
    print(
        f"\nTalenta enrich {args.period} [{args.kind}] — {r.skeleton_rows} skeleton rows\n"
        f"  enriched : {r.enriched}\n"
        f"  held     : {r.held}\n"
        f"  passthrough (day off / holiday / leave): {r.passthrough}\n"
        f"  skipped  : {r.skipped}"
    )
    if r.blocked:
        print(f"\nFINAL RUN BLOCKED — {r.held} row(s) not finalised. Sample:")
    elif r.held:
        print(f"\nDraft written with {r.held} row(s) left blank. Sample:")
    for o in r.held_rows[:15]:
        print(f"  row {o.row:>4}  {o.employee_id:14} {o.date}  {o.reason}")
    if len(r.held_rows) > 15:
        print(f"  … and {len(r.held_rows) - 15} more")
    if r.out_path:
        print(f"\n  wrote: {r.out_path}")


def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("recompute"); p.add_argument("--period", required=True); p.set_defaults(fn=cmd_recompute)
    p = sub.add_parser("exceptions"); p.add_argument("--period", required=True); p.set_defaults(fn=cmd_exceptions)

    p = sub.add_parser("export")
    p.add_argument("--period", required=True)
    p.add_argument("--kind", choices=["draft", "final"], default="draft")
    p.add_argument("--actor", default="cli")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("talenta", help="enrich a Talenta Import Attendance skeleton")
    p.add_argument("--skeleton", required=True, help="xlsx exported from Talenta for the period")
    p.add_argument("--period", required=True, help="YYYY-MM (period ending 25th of that month)")
    p.add_argument("--kind", choices=["draft", "final"], default="draft")
    p.add_argument("--out", default=None)
    p.set_defaults(fn=cmd_talenta)

    p = sub.add_parser("correct")
    p.add_argument("--emp", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--set-in", dest="set_in")
    p.add_argument("--set-out", dest="set_out")
    p.add_argument("--set-code", dest="set_code")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="cli")
    p.set_defaults(fn=cmd_correct)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
