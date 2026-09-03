"""Dashboard rollup viewmodel."""

from __future__ import annotations

import datetime as dt

WIB = dt.timezone(dt.timedelta(hours=7))


def _wib(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=WIB)


def _seed_month(make_employee, add_punches, session):
    """Two employees on shift A; the second forgets to clock out on the 5th."""
    from server.core.recompute import recompute_all

    make_employee(
        device_user_id="1001", emp_code="KM-1400", name="Budi Santoso",
        department="Operasi Tambang", shift_key="A", roster_pattern="continuous",
    )
    make_employee(
        device_user_id="1002", emp_code="KM-1403", name="Siti Wijaya",
        department="Hauling", shift_key="A", roster_pattern="continuous",
    )
    for day in range(1, 11):
        add_punches("1001", [_wib(2026, 8, day, 6, 0), _wib(2026, 8, day, 14, 30)])
        if day == 5:
            add_punches("1002", [_wib(2026, 8, 5, 6, 0)])  # missing out -> MP
        else:
            add_punches("1002", [_wib(2026, 8, day, 6, 0), _wib(2026, 8, day, 14, 30)])

    recompute_all(session, dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    session.commit()


def test_dashboard_headcount_and_hours(make_employee, add_punches, session):
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")

    assert v["total_employees"] == 2
    assert v["gross_hours"] > 0
    # P days carry hours; the MP day contributes none.
    assert v["code_counts"]["P"] == 19
    assert v["code_counts"]["MP"] == 1
    assert v["total_records"] == sum(v["code_counts"].values())


def test_dashboard_mix_percentages_sum_to_100(make_employee, add_punches, session):
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")

    assert round(sum(m["pct"] for m in v["mix"]), 0) == 100
    assert {m["code"] for m in v["mix"]} == {"P", "A", "MP", "SS", "WO", "H"}


def test_dashboard_open_exception_blocks_export(make_employee, add_punches, session):
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")

    assert v["open_exceptions"] >= 1
    assert v["blocking"] >= 1
    assert v["export_clear"] is False
    assert any(k["kind"] == "MP" for k in v["exc_kinds"])
    # the MP belongs to the Hauling employee
    hauling = next(d for d in v["departments"] if d["department"] == "Hauling")
    assert hauling["open_exceptions"] >= 1


def test_dashboard_departments_cover_every_active_employee(make_employee, add_punches, session):
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")

    assert sum(d["employees"] for d in v["departments"]) == 2
    assert {d["department"] for d in v["departments"]} == {"Operasi Tambang", "Hauling"}


def test_dashboard_snapshot_is_latest_day_with_records(make_employee, add_punches, session):
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")

    assert v["snapshot"]["date_iso"] == "2026-08-10"
    assert v["snapshot"]["total"] == 2


def test_dashboard_recent_runs_listed_newest_first(make_employee, add_punches, session):
    from server.core.export import run_export
    from server.web.viewmodels import dashboard_view

    _seed_month(make_employee, add_punches, session)
    run_export(session, "2026-08", kind="draft", generated_by="rina")
    run_export(session, "2026-08", kind="final", generated_by="rina")

    v = dashboard_view(session, "2026-08")
    assert len(v["runs"]) == 2
    assert v["runs"][0]["status"] == "blocked"  # final, held by the open MP


def test_stat_cards_dashboard_shape(make_employee, add_punches, session):
    from server.web.i18n import translator_for
    from server.web.viewmodels import dashboard_view, stat_cards_dashboard

    _seed_month(make_employee, add_punches, session)
    v = dashboard_view(session, "2026-08")
    cards = stat_cards_dashboard(v, translator_for("en"))

    assert [c["value"] for c in cards][0] == 2
    assert len(cards) == 4
    assert all("label" in c and "value" in c for c in cards)
