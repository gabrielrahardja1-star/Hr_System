"""Review UI routes — Monthly Review, Daily Attendance, Exceptions, plus the
HTMX partials for cell detail / manual correction / exception resolve.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import get_settings
from server.core.exceptions import resolve_exception
from server.core.export import run_export
from server.core.recompute import recompute_employee
from server.db import get_session
from server.models import Correction, DayRecord, Employee
from server.web import viewmodels as vm
from server.web.i18n import AVAILABLE, DEFAULT_LANG, translator_for

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = WEB_DIR / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

router = APIRouter(tags=["web"])
STATIC_DIR = WEB_DIR / "static"


def _lang(request: Request) -> str:
    value = request.cookies.get("lang", DEFAULT_LANG)
    return value if value in AVAILABLE else DEFAULT_LANG


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    t = translator_for(_lang(request))
    base = {
        "t": t,
        "lang": t.lang,
        "other_lang": t.other_lang,
        "request": request,
        "PALETTE": vm.PALETTE,
        "now": dt.datetime.now(get_settings().timezone),
    }
    base.update(ctx)
    html = _env.get_template(template).render(**base)
    return HTMLResponse(html)


def _current_period(session: Session, requested: str | None) -> str:
    periods = [p["value"] for p in vm.available_periods(session)]
    if requested in periods:
        return requested
    return periods[0]


def _default_day(session: Session, period: str, requested: str | None) -> str:
    start, end, _ = vm.period_bounds(period)
    if requested:
        try:
            d = dt.date.fromisoformat(requested)
            if start <= d <= end:
                return requested
        except ValueError:
            pass
    latest = session.execute(
        select(DayRecord.work_date)
        .where(DayRecord.work_date >= start, DayRecord.work_date <= end)
        .order_by(DayRecord.work_date.desc())
    ).scalars().first()
    return (latest or end).isoformat()


# --------------------------------------------------------------------------- #
# Full pages                                                                   #
# --------------------------------------------------------------------------- #


@router.get("/", response_class=HTMLResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/monthly")


@router.get("/monthly", response_class=HTMLResponse)
def monthly(
    request: Request,
    period: str | None = None,
    dept: str = "*",
    page: int = 1,
    view: str = "detail",
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    grid = vm.monthly_grid(session, period, dept=dept, page=page, view=view)
    t = translator_for(_lang(request))
    return render(
        request,
        "monthly.html",
        active_nav="monthly",
        period=period,
        periods=vm.available_periods(session),
        departments=vm.departments(session),
        dept=dept,
        grid=grid,
        stat_cards=vm.stat_cards_monthly(grid, t),
        open_exc_badge=vm.exceptions_view(session, period)["open_total"],
    )


@router.get("/daily", response_class=HTMLResponse)
def daily(
    request: Request,
    period: str | None = None,
    date: str | None = None,
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    day_iso = _default_day(session, period, date)
    roster = vm.daily_roster(session, period, day_iso)
    return render(
        request,
        "daily.html",
        active_nav="daily",
        period=period,
        periods=vm.available_periods(session),
        days=vm.month_days(session, period),
        day_iso=day_iso,
        roster=roster,
        open_exc_badge=vm.exceptions_view(session, period)["open_total"],
    )


@router.get("/exceptions", response_class=HTMLResponse)
def exceptions(
    request: Request,
    period: str | None = None,
    kind: str = "*",
    resolved: int = 0,
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    data = vm.exceptions_view(
        session, period, kind=kind, show_resolved=bool(resolved)
    )
    unfiltered = vm.exceptions_view(session, period)
    return render(
        request,
        "exceptions.html",
        active_nav="exceptions",
        period=period,
        periods=vm.available_periods(session),
        data=data,
        open_exc_badge=unfiltered["open_total"],
    )


# --------------------------------------------------------------------------- #
# HTMX partials + actions                                                      #
# --------------------------------------------------------------------------- #


@router.get("/cell/{employee_id}/{date}", response_class=HTMLResponse)
def cell(
    request: Request,
    employee_id: int,
    date: str,
    session: Session = Depends(get_session),
):
    detail = vm.cell_detail(session, employee_id, date)
    return render(request, "partials/cell_detail.html", d=detail, editing=False)


@router.get("/cell/{employee_id}/{date}/edit", response_class=HTMLResponse)
def cell_edit(
    request: Request,
    employee_id: int,
    date: str,
    session: Session = Depends(get_session),
):
    detail = vm.cell_detail(session, employee_id, date)
    return render(request, "partials/cell_detail.html", d=detail, editing=True)


@router.post("/cell/{employee_id}/{date}/correct", response_class=HTMLResponse)
def cell_correct(
    request: Request,
    employee_id: int,
    date: str,
    reason: str = Form(...),
    actor: str = Form("HR"),
    new_in: str = Form(""),
    new_out: str = Form(""),
    force_code: str = Form(""),
    session: Session = Depends(get_session),
):
    day = dt.date.fromisoformat(date)
    rec = session.execute(
        select(DayRecord).where(
            DayRecord.employee_id == employee_id, DayRecord.work_date == day
        )
    ).scalar_one_or_none()
    emp = session.get(Employee, employee_id)
    if rec is None or emp is None:
        return render(request, "partials/cell_detail.html", d=None, editing=False)

    def _ts(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        # Accept "HH:MM" (same day) or a full ISO datetime.
        if len(value) <= 5 and ":" in value:
            return f"{date} {value}"
        return value

    added = 0
    if _ts(new_in):
        session.add(Correction(day_record_id=rec.id, field="first_in",
                               old_value=str(rec.first_in), new_value=_ts(new_in),
                               reason=reason, actor=actor))
        added += 1
    if _ts(new_out):
        session.add(Correction(day_record_id=rec.id, field="last_out",
                               old_value=str(rec.last_out), new_value=_ts(new_out),
                               reason=reason, actor=actor))
        added += 1
    if force_code:
        session.add(Correction(day_record_id=rec.id, field="code",
                               old_value=rec.code, new_value=force_code,
                               reason=reason, actor=actor))
        added += 1

    if added:
        session.flush()
        recompute_employee(session, emp, day, day)
        session.commit()

    detail = vm.cell_detail(session, employee_id, date)
    resp = render(request, "partials/cell_detail.html", d=detail, editing=False)
    resp.headers["HX-Trigger"] = "recordChanged"
    return resp


@router.post("/exceptions/{exception_id}/resolve", response_class=HTMLResponse)
def exception_resolve(
    request: Request,
    exception_id: int,
    period: str = Form(...),
    resolution: str = Form("Reviewed — no punch change needed"),
    actor: str = Form("HR"),
    session: Session = Depends(get_session),
):
    try:
        resolve_exception(session, exception_id, actor, resolution)
        session.commit()
    except LookupError:
        pass
    return RedirectResponse(f"/exceptions?period={period}", status_code=303)


@router.post("/export/{period}", response_class=HTMLResponse)
def export(
    request: Request,
    period: str,
    kind: str = Form("final"),
    actor: str = Form("HR"),
    session: Session = Depends(get_session),
):
    outcome = run_export(session, period, kind=kind, generated_by=actor)
    return render(
        request,
        "partials/export_result.html",
        outcome=outcome,
        period=period,
        kind=kind,
    )


@router.post("/lang")
def set_lang(to: str = Form(...), back: str = Form("/monthly")):
    resp = RedirectResponse(back, status_code=303)
    lang = to if to in AVAILABLE else DEFAULT_LANG
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp
