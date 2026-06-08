from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from auth import require_login, hash_password, verify_password
from database import SessionLocal
from models import User
import audit as audit_mod
from tmpl import templates

router = APIRouter(prefix="/profile")


@router.get("", response_class=HTMLResponse)
@require_login
async def profile_get(request: Request):
    return templates.TemplateResponse("profile.html", {
        "request": request,
        "user": request.state.current_user,
        "success": None,
        "error": None,
    })


@router.post("/update", response_class=HTMLResponse)
@require_login
async def profile_update(
    request: Request,
    email: str = Form(default=""),
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    receive_reports: str = Form(default="off"),
):
    user = request.state.current_user

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user.id).first()

        # ── Email (opcional) ──────────────────────────────────────────────────
        email_clean = email.strip() or None
        if email_clean:
            existing_email = db.query(User).filter(
                User.email == email_clean, User.id != user.id
            ).first()
            if existing_email:
                return templates.TemplateResponse("profile.html", {
                    "request": request, "user": user,
                    "error": "Ese email ya está en uso por otro usuario.", "success": None,
                })
        u.email = email_clean

        # ── Receive reports (solo admins) ────────────────────────────────────
        if u.role == "admin":
            wants_reports = (receive_reports == "on")
            if wants_reports and not email_clean:
                # Quiere reportes pero no tiene email → desactivar silenciosamente
                u.receive_reports = False
            else:
                u.receive_reports = wants_reports

        # ── Cambio de contraseña (opcional) ──────────────────────────────────
        if new_password:
            if not current_password:
                return templates.TemplateResponse("profile.html", {
                    "request": request, "user": user,
                    "error": "Debes introducir tu contraseña actual para cambiarla.", "success": None,
                })
            if not verify_password(current_password, u.hashed_password):
                return templates.TemplateResponse("profile.html", {
                    "request": request, "user": user,
                    "error": "La contraseña actual es incorrecta.", "success": None,
                })
            if new_password != confirm_password:
                return templates.TemplateResponse("profile.html", {
                    "request": request, "user": user,
                    "error": "Las contraseñas nuevas no coinciden.", "success": None,
                })
            if len(new_password) < 6:
                return templates.TemplateResponse("profile.html", {
                    "request": request, "user": user,
                    "error": "La contraseña debe tener al menos 6 caracteres.", "success": None,
                })
            u.hashed_password = hash_password(new_password)
            u.must_change_password = False
            audit_mod.log(db, f"Contraseña cambiada por el propio usuario — {user.username}", user_id=user.id)

        db.commit()
        audit_mod.log(db, f"Perfil actualizado — {user.username}", user_id=user.id)

        # Recargar user para la plantilla
        u = db.query(User).filter(User.id == user.id).first()
        return templates.TemplateResponse("profile.html", {
            "request": request, "user": u,
            "success": "Perfil actualizado correctamente.", "error": None,
        })
    finally:
        db.close()
