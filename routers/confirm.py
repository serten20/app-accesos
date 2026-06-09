"""
Confirmación rápida de rotación desde el email (magic link), sin login.

GET  /confirm/{token}  → valida el token y muestra una página de confirmación.
POST /confirm/{token}  → registra la rotación (igual que en la consola).

La página intermedia (GET sin acción) evita que un escáner de correo confirme
solo por previsualizar el enlace. El token va firmado y caduca según el ajuste
'confirm_token_hours' (Configuración → General).
"""
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from auth import decode_confirm_token
from database import SessionLocal, get_setting
from tmpl import templates
import audit as audit_mod

router = APIRouter()


def _token_max_age() -> int:
    try:
        return max(1, int(get_setting("confirm_token_hours") or "3")) * 3600
    except (ValueError, TypeError):
        return 3 * 3600


def _render(request, **ctx):
    return templates.TemplateResponse("confirm.html", {"request": request, **ctx})


@router.get("/confirm/{token}", response_class=HTMLResponse)
async def confirm_get(request: Request, token: str):
    tid, cid, reason = decode_confirm_token(token, _token_max_age())
    if reason:
        return _render(request, state=reason)
    db = SessionLocal()
    try:
        from models import TechnicianCompany
        tc = db.query(TechnicianCompany).filter_by(technician_id=tid, company_id=cid).first()
        if not tc or not tc.company or not tc.technician:
            return _render(request, state="notfound")
        return _render(request, state="confirm", token=token,
                       tech=tc.technician.username, company=tc.company.name,
                       days=tc.days_remaining, status=tc.status)
    finally:
        db.close()


@router.post("/confirm/{token}", response_class=HTMLResponse)
async def confirm_post(request: Request, token: str):
    tid, cid, reason = decode_confirm_token(token, _token_max_age())
    if reason:
        return _render(request, state=reason)
    db = SessionLocal()
    try:
        from models import TechnicianCompany, RotationHistory
        tc = db.query(TechnicianCompany).filter_by(technician_id=tid, company_id=cid).first()
        if not tc or not tc.company or not tc.technician:
            return _render(request, state="notfound")

        days_late = max(0, -tc.days_remaining)
        company_name = tc.company.name
        tech_name = tc.technician.username
        db.add(RotationHistory(
            technician_id=tid, company_id=cid,
            technician_name=tech_name, company_name=company_name,
            rotated_on=date.today(), days_late=days_late,
        ))
        tc.last_changed = date.today()
        tc.alert_count = 0
        tc.escalated_at = None
        audit_mod.log(
            db,
            f"Rotación confirmada desde email: {company_name} por {tech_name}"
            + (f" (con {days_late}d de retraso)" if days_late else ""),
            user_id=tid, company_id=cid,
        )
        db.commit()
        return _render(request, state="done", company=company_name, late=days_late)
    finally:
        db.close()
