from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from auth import require_login
import audit as audit_mod
from tmpl import templates

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
@require_login
async def dashboard(request: Request):
    user = request.state.current_user
    db   = request.state.db

    if user.role in ("admin", "auditor"):
        return RedirectResponse("/admin", status_code=302)

    # Usamos tc_assocs para tener el estado POR técnico (last_changed individual)
    tc_list = sorted(user.tc_assocs, key=lambda tc: tc.days_remaining)

    critical = sum(1 for tc in tc_list if tc.status == "critical")
    warning  = sum(1 for tc in tc_list if tc.status == "warning")
    ok       = sum(1 for tc in tc_list if tc.status == "ok")

    from thresholds import get_thresholds
    crit_days, warn_days = get_thresholds()

    return templates.TemplateResponse("dashboard.html", {
        "request":  request,
        "user":     user,
        "tc_list":  tc_list,
        "total":    len(tc_list),
        "critical": critical,
        "warning":  warning,
        "ok":       ok,
        "warn_days": warn_days,
        "crit_days": crit_days,
    })


@router.post("/company/{company_id}/confirm", response_class=HTMLResponse)
@require_login
async def confirm_change(request: Request, company_id: int):
    user = request.state.current_user
    db   = request.state.db

    from models import TechnicianCompany
    # Buscar la fila concreta de ESTE técnico con ESTA empresa
    tc = db.query(TechnicianCompany).filter(
        TechnicianCompany.technician_id == user.id,
        TechnicianCompany.company_id    == company_id,
    ).first()

    # Destino tras confirmar: los admins con doble rol vuelven a "Mis Empresas"
    back_url = "/admin/my-companies" if user.role == "admin" else "/dashboard"

    if not tc:
        return RedirectResponse(back_url, status_code=302)

    from models import RotationHistory

    # Calcular si la rotación llega tarde ANTES de actualizar el estado
    days_remaining = tc.days_remaining
    days_late = max(0, -days_remaining)

    # Registrar el evento inmutable en el historial
    db.add(RotationHistory(
        technician_id=user.id,
        company_id=company_id,
        technician_name=user.username,
        company_name=tc.company.name,
        rotated_on=date.today(),
        days_late=days_late,
    ))

    # Actualizar estado y resetear el escalado de alertas
    tc.last_changed = date.today()
    tc.alert_count = 0
    tc.escalated_at = None

    audit_mod.log(
        db,
        f"Contraseña confirmada: {tc.company.name}" + (f" (con {days_late}d de retraso)" if days_late else ""),
        user_id=user.id,
        company_id=company_id,
    )
    db.commit()

    return RedirectResponse(back_url, status_code=302)
