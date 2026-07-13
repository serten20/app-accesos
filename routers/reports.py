from datetime import date, datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, case
from auth import require_viewer
from models import Company, User
from compliance import compute_compliance
from tmpl import templates

router = APIRouter(prefix="/admin")

PERIOD_DAYS = {"30": 30, "90": 90, "365": 365}
PERIOD_LABELS = {"30": "Últimos 30 días", "90": "Últimos 90 días",
                 "365": "Último año", "all": "Todo el histórico"}


def _build_report_context(db, request):
    kpis = compute_compliance(db)

    technicians = kpis["technicians"]
    tech_summary = []
    for t in technicians:
        t_companies = t.companies
        t_critical = sum(1 for c in t_companies if c.status == "critical")
        t_warning  = sum(1 for c in t_companies if c.status == "warning")
        tech_summary.append({
            "tech": t,
            "total": len(t_companies),
            "critical": t_critical,
            "warning": t_warning,
            "pending": t_critical + t_warning,
            "is_compliant": t_critical == 0,
        })

    return {
        "request": request,
        "user": getattr(request.state, "current_user", None),
        "generated_at": date.today().strftime("%d/%m/%Y"),
        "companies": sorted(kpis["companies"], key=lambda c: c.days_remaining),
        "critical": [c for c in kpis["companies"] if c.status == "critical"],
        "warning":  [c for c in kpis["companies"] if c.status == "warning"],
        "ok":       [c for c in kpis["companies"] if c.status == "ok"],
        "tech_summary": sorted(tech_summary, key=lambda t: -t["pending"]),
        **kpis,
    }


@router.get("/reports", response_class=HTMLResponse)
@require_viewer
async def reports_index(request: Request):
    """Página de índice de reportes disponibles."""
    db = request.state.db
    kpis = compute_compliance(db)

    from models import Company
    companies = db.query(Company).all()
    n_critical = sum(1 for c in companies if c.status == "critical")
    n_warning  = sum(1 for c in companies if c.status == "warning")

    return templates.TemplateResponse("admin_reports_index.html", {
        "request": request,
        "user": request.state.current_user,
        "compliance_pct":   kpis["compliance_pct"],
        "compliance_color": kpis["compliance_color"],
        "compliance_label": kpis["compliance_label"],
        "n_critical":  n_critical,
        "n_warning":   n_warning,
        "total_companies": kpis["total_companies"],
        "generated_at": date.today().strftime("%d/%m/%Y"),
    })


@router.get("/reports/ranking", response_class=HTMLResponse)
@require_viewer
async def reports_ranking(request: Request):
    """Ranking de puntualidad por técnico, agregando el histórico de rotaciones."""
    from models import RotationHistory
    db = request.state.db
    period = request.query_params.get("period", "all")
    if period not in PERIOD_LABELS:
        period = "all"

    q = db.query(
        RotationHistory.technician_name.label("name"),
        func.count(RotationHistory.id).label("total"),
        func.sum(case((RotationHistory.days_late > 0, 1), else_=0)).label("late"),
        func.avg(RotationHistory.days_late).label("avg_late"),
        func.max(RotationHistory.days_late).label("max_late"),
        func.max(RotationHistory.confirmed_at).label("last_at"),
    )
    if period in PERIOD_DAYS:
        cutoff = datetime.utcnow() - timedelta(days=PERIOD_DAYS[period])
        q = q.filter(RotationHistory.confirmed_at >= cutoff)
    rows = q.group_by(RotationHistory.technician_name).all()

    # Mapa nombre → id de técnico aún existente (para enlazar a su ficha)
    existing = {u.username: u.id for u in db.query(User).filter(
        (User.role == "technician") | (User.also_technician == True)).all()}

    ranking = []
    for r in rows:
        total = r.total or 0
        late = int(r.late or 0)
        ontime = total - late
        pct = round(ontime / total * 100) if total else 0
        ranking.append({
            "name": r.name or "—",
            "total": total,
            "ontime": ontime,
            "late": late,
            "pct": pct,
            "avg_late": round(r.avg_late or 0, 1),
            "max_late": int(r.max_late or 0),
            "last_at": r.last_at,
            "tech_id": existing.get(r.name),
        })
    # Orden: mayor puntualidad primero; a igualdad, más rotaciones
    ranking.sort(key=lambda x: (-x["pct"], -x["total"]))

    # Totales globales del periodo
    g_total = sum(x["total"] for x in ranking)
    g_late = sum(x["late"] for x in ranking)
    g_pct = round((g_total - g_late) / g_total * 100) if g_total else 0

    return templates.TemplateResponse("admin_ranking.html", {
        "request": request,
        "user": request.state.current_user,
        "ranking": ranking,
        "period": period,
        "period_label": PERIOD_LABELS[period],
        "period_labels": PERIOD_LABELS,
        "g_total": g_total,
        "g_late": g_late,
        "g_pct": g_pct,
        "n_techs": len(ranking),
    })


@router.get("/reports/calendar", response_class=HTMLResponse)
@require_viewer
async def reports_calendar(request: Request):
    """Calendario de vencimientos próximos (por asignación) + heatmap de rotaciones."""
    from models import TechnicianCompany, RotationHistory
    db = request.state.db
    today = date.today()

    # ── Vencimientos por asignación técnico-empresa ──────────────────────────
    expiries = []
    for tc in db.query(TechnicianCompany).all():
        if not tc.company or not tc.technician:
            continue
        exp = tc._effective_last_changed + timedelta(days=tc.company.expiry_days)
        expiries.append({
            "date": exp.isoformat(),
            "company": tc.company.name,
            "tech": tc.technician.username,
            "status": tc.status,
            "days": tc.days_remaining,
        })

    from thresholds import get_thresholds
    crit_days, warn_days = get_thresholds()
    next_7 = sum(1 for e in expiries if 0 <= e["days"] <= warn_days)
    next_30 = sum(1 for e in expiries if 0 <= e["days"] <= 30)
    overdue = sum(1 for e in expiries if e["days"] < 0)

    # ── Heatmap de rotaciones (último año) ───────────────────────────────────
    cutoff = datetime.utcnow() - timedelta(days=365)
    rows = (db.query(func.date(RotationHistory.confirmed_at), func.count(RotationHistory.id))
            .filter(RotationHistory.confirmed_at >= cutoff)
            .group_by(func.date(RotationHistory.confirmed_at)).all())
    heat = {str(r[0]): int(r[1]) for r in rows}
    rotations_year = sum(heat.values())

    return templates.TemplateResponse("admin_calendar.html", {
        "request": request,
        "user": request.state.current_user,
        "expiries": expiries,
        "heat": heat,
        "today": today.isoformat(),
        "next_7": next_7,
        "next_30": next_30,
        "overdue": overdue,
        "rotations_year": rotations_year,
        "total_expiries": len(expiries),
        "warn_days": warn_days,
        "crit_days": crit_days,
    })


@router.get("/report/urgent", response_class=HTMLResponse)
@require_viewer
async def report_urgent(request: Request):
    """Reporte de solo empresas en atención y crítico con detalle de técnicos."""
    db = request.state.db
    from models import Company, User

    companies = db.query(Company).all()
    urgent = [c for c in companies if c.status in ("critical", "warning")]
    urgent_sorted = sorted(urgent, key=lambda c: (0 if c.status == "critical" else 1, c.days_remaining))

    # Para cada empresa urgente, listar SOLO los técnicos pendientes EN ESA empresa
    # (crítico/atención según su propio TechnicianCompany). Los que ya confirmaron
    # (OK en esta empresa) no son el problema y no se incluyen.
    urgent_detail = []
    for company in urgent_sorted:
        pend = [tc for tc in company.tc_assocs if tc.status in ("critical", "warning")]
        pend.sort(key=lambda tc: tc.days_remaining)
        tech_rows = [{
            "tech": tc.technician,
            "status": tc.status,                 # 'critical' | 'warning' (en esta empresa)
            "days_remaining": tc.days_remaining,
        } for tc in pend]
        urgent_detail.append({"company": company, "techs": tech_rows})

    # Stats para el resumen superior
    n_critical = sum(1 for c in urgent if c.status == "critical")
    n_warning  = sum(1 for c in urgent if c.status == "warning")

    # Técnicos afectados únicos: solo los pendientes en alguna empresa urgente
    affected_tech_ids = set()
    for company in urgent:
        for tc in company.tc_assocs:
            if tc.status in ("critical", "warning"):
                affected_tech_ids.add(tc.technician_id)

    return templates.TemplateResponse("report_urgent.html", {
        "request": request,
        "user": request.state.current_user,
        "generated_at": date.today().strftime("%d/%m/%Y"),
        "urgent_detail": urgent_detail,
        "n_critical": n_critical,
        "n_warning": n_warning,
        "n_affected_techs": len(affected_tech_ids),
        "total_urgent": len(urgent),
    })


@router.get("/report", response_class=HTMLResponse)
@require_viewer
async def report_html(request: Request):
    db = request.state.db
    ctx = _build_report_context(db, request)
    return templates.TemplateResponse("report.html", ctx)


@router.get("/report/pdf", response_class=HTMLResponse)
@require_viewer
async def report_pdf(request: Request):
    """Vista de reporte optimizada para impresión / exportar a PDF desde el navegador."""
    db = request.state.db
    ctx = _build_report_context(db, request)
    return templates.TemplateResponse("report_pdf.html", ctx)
