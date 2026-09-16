"""Review UI routes — Monthly Review, Daily Attendance, Exceptions, plus the
HTMX partials for cell detail / manual correction / exception resolve.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import get_settings, get_shift_config
from server.core.exceptions import resolve_exception
from server.core.export import run_export
from server.core.recompute import recompute_employee
from server.core.talenta_export import enrich_skeleton
from server.db import get_session
from server.models import Correction, DayRecord, Employee, EmployeeStatus
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
    return RedirectResponse("/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    period: str | None = None,
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    t = translator_for(_lang(request))
    view = vm.dashboard_view(session, period)
    return render(
        request,
        "dashboard.html",
        active_nav="dashboard",
        period=period,
        periods=vm.available_periods(session),
        view=view,
        stat_cards=vm.stat_cards_dashboard(view, t),
        open_exc_badge=view["open_exceptions"],
    )


@router.get("/monthly", response_class=HTMLResponse)
def monthly(
    request: Request,
    period: str | None = None,
    dept: str = "*",
    q: str = "",
    page: int = 1,
    view: str = "detail",
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    q = q.strip()
    grid = vm.monthly_grid(
        session, period, dept=dept, search=q or None, page=page, view=view
    )
    t = translator_for(_lang(request))
    return render(
        request,
        "monthly.html",
        active_nav="monthly",
        period=period,
        periods=vm.available_periods(session),
        departments=vm.departments(session),
        emp_options=vm.employee_options(session),
        dept=dept,
        q=q,
        grid=grid,
        stat_cards=vm.stat_cards_monthly(grid, t),
        open_exc_badge=vm.exceptions_view(session, period)["open_total"],
    )


@router.get("/daily", response_class=HTMLResponse)
def daily(
    request: Request,
    period: str | None = None,
    date: str | None = None,
    dept: str = "*",
    q: str = "",
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    q = q.strip()
    day_iso = _default_day(session, period, date)
    roster = vm.daily_roster(
        session, period, day_iso, dept=dept, search=q or None
    )
    return render(
        request,
        "daily.html",
        active_nav="daily",
        period=period,
        periods=vm.available_periods(session),
        departments=vm.departments(session),
        emp_options=vm.employee_options(session),
        dept=dept,
        q=q,
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


@router.get("/payroll-export", response_class=HTMLResponse)
def payroll_export(
    request: Request,
    period: str | None = None,
    session: Session = Depends(get_session),
):
    tperiods = vm.talenta_periods(session)
    period = period if period in {p["value"] for p in tperiods} else tperiods[0]["value"]
    return render(
        request,
        "payroll_export.html",
        active_nav="payroll",
        period=period,
        periods=vm.available_periods(session),
        tperiods=tperiods,
        result=None,
        open_exc_badge=vm.exceptions_view(session, period)["open_total"]
        if period in {p["value"] for p in vm.available_periods(session)}
        else 0,
    )


@router.post("/payroll-export", response_class=HTMLResponse)
async def payroll_export_run(
    request: Request,
    period: str = Form(...),
    kind: str = Form("draft"),
    skeleton: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    upload_dir = settings.export_dir / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    src = upload_dir / f"skeleton_{period}_{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    src.write_bytes(await skeleton.read())

    error = None
    result = None
    try:
        result = enrich_skeleton(session, src, period, kind=kind)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    return render(
        request,
        "payroll_export.html",
        active_nav="payroll",
        period=period,
        periods=vm.available_periods(session),
        tperiods=vm.talenta_periods(session),
        result=result,
        error=error,
        uploaded_name=skeleton.filename,
        open_exc_badge=0,
    )


@router.get("/payroll-export/download/{filename}")
def payroll_export_download(filename: str):
    settings = get_settings()
    path = (settings.export_dir / filename).resolve()
    if path.parent != settings.export_dir.resolve() or not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@router.get("/employees", response_class=HTMLResponse)
def employees(
    request: Request,
    period: str | None = None,
    error: str | None = None,
    saved: str | None = None,
    session: Session = Depends(get_session),
):
    period = _current_period(session, period)
    return render(
        request,
        "employees.html",
        active_nav="employees",
        period=period,
        periods=vm.available_periods(session),
        view=vm.employee_admin_view(session),
        error=error,
        saved=saved,
        open_exc_badge=vm.exceptions_view(session, period)["open_total"],
    )


def _employee_form(
    device_user_id: str,
    name: str,
    emp_code: str,
    talenta_id: str,
    department: str,
    shift_key: str,
    roster_pattern: str,
    status: str,
    active_from: str,
    active_to: str,
) -> dict:
    """Validate and normalise the employee form. Raises ValueError on bad input."""
    cfg = get_shift_config()
    fields = {
        "device_user_id": device_user_id.strip(),
        "name": name.strip(),
        "emp_code": emp_code.strip(),
        "department": department.strip(),
    }
    for label, value in fields.items():
        if not value:
            raise ValueError(f"{label.replace('_', ' ')} is required")
    if shift_key not in cfg.shifts:
        raise ValueError(f"Unknown shift {shift_key!r}. Defined: {sorted(cfg.shifts)}")
    if roster_pattern not in cfg.roster_patterns:
        raise ValueError(f"Unknown roster {roster_pattern!r}")
    try:
        status_enum = EmployeeStatus(status)
    except ValueError:
        raise ValueError(f"Unknown status {status!r}") from None

    def _date(value: str) -> dt.date | None:
        value = value.strip()
        return dt.date.fromisoformat(value) if value else None

    return {
        **fields,
        "talenta_id": talenta_id.strip() or None,
        "shift_key": shift_key,
        "roster_pattern": roster_pattern,
        "status": status_enum,
        "active_from": _date(active_from),
        "active_to": _date(active_to),
    }


@router.post("/employees")
def employee_create(
    device_user_id: str = Form(""),
    name: str = Form(""),
    emp_code: str = Form(""),
    talenta_id: str = Form(""),
    department: str = Form(""),
    shift_key: str = Form(""),
    roster_pattern: str = Form(""),
    status: str = Form("active"),
    active_from: str = Form(""),
    active_to: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        values = _employee_form(
            device_user_id, name, emp_code, talenta_id, department,
            shift_key, roster_pattern, status, active_from, active_to,
        )
    except ValueError as exc:
        return RedirectResponse(f"/employees?error={exc}", status_code=303)

    clash = session.execute(
        select(Employee).where(
            Employee.device_user_id == values["device_user_id"]
        )
    ).scalar_one_or_none()
    if clash is not None:
        return RedirectResponse(
            f"/employees?error=Device ID {values['device_user_id']!r} "
            f"already belongs to {clash.name}",
            status_code=303,
        )

    session.add(Employee(**values))
    session.commit()
    return RedirectResponse(f"/employees?saved={values['name']}", status_code=303)


@router.post("/employees/{employee_id}")
def employee_update(
    employee_id: int,
    device_user_id: str = Form(""),
    name: str = Form(""),
    emp_code: str = Form(""),
    talenta_id: str = Form(""),
    department: str = Form(""),
    shift_key: str = Form(""),
    roster_pattern: str = Form(""),
    status: str = Form("active"),
    active_from: str = Form(""),
    active_to: str = Form(""),
    session: Session = Depends(get_session),
):
    emp = session.get(Employee, employee_id)
    if emp is None:
        return RedirectResponse("/employees?error=No such employee", status_code=303)
    try:
        values = _employee_form(
            device_user_id, name, emp_code, talenta_id, department,
            shift_key, roster_pattern, status, active_from, active_to,
        )
    except ValueError as exc:
        return RedirectResponse(f"/employees?error={exc}", status_code=303)

    clash = session.execute(
        select(Employee).where(
            Employee.device_user_id == values["device_user_id"],
            Employee.id != employee_id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        return RedirectResponse(
            f"/employees?error=Device ID {values['device_user_id']!r} "
            f"already belongs to {clash.name}",
            status_code=303,
        )

    shift_changed = emp.shift_key != values["shift_key"]
    for field, value in values.items():
        setattr(emp, field, value)
    session.flush()

    # A different shift means a different attribution window, so previously
    # computed days for this employee no longer follow from their punches.
    if shift_changed:
        start, end, _ = vm.period_bounds(_current_period(session, None))
        recompute_employee(session, emp, start, end)
    session.commit()
    return RedirectResponse(f"/employees?saved={values['name']}", status_code=303)


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
