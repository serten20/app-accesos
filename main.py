import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()


def _setup_logging():
    """Configura logging a consola + fichero rotativo (logs/app.log)."""
    os.makedirs("logs", exist_ok=True)
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    # Evitar duplicar handlers si se reimporta el módulo
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(
            os.path.join("logs", "app.log"),
            maxBytes=10 * 1024 * 1024,   # 10 MB por fichero
            backupCount=5,               # 5 ficheros históricos
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


_setup_logging()
logger = logging.getLogger("accesos")

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from database import engine, SessionLocal, run_migrations, init_database
from models import Base
from auth import (
    verify_password, hash_password, create_session_token,
    get_current_user, seed_admin, SESSION_COOKIE, require_login,
    create_reset_token, decode_reset_token, password_fingerprint,
)
from models import User
from routers import technician, admin, reports, confirm
from routers import profile as profile_router
import audit as audit_mod
from tmpl import templates          # instancia compartida con filtros fmt_date / fmt_datetime
from security import CSRFMiddleware, too_many_login_attempts, COOKIE_SECURE


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data", exist_ok=True)
    os.makedirs("uploads", exist_ok=True)
    os.makedirs("static", exist_ok=True)
    # Inicialización de esquema (Alembic con fallback clásico)
    init_database()
    db = SessionLocal()
    seed_admin(db)
    db.close()

    from scheduler import start_scheduler
    scheduler = start_scheduler()  # None en los workers que no obtienen el lock

    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="Gestión de Accesos", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(technician.router)
app.include_router(admin.router)
app.include_router(reports.router)
app.include_router(confirm.router)
app.include_router(profile_router.router)


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")


@app.get("/healthz")
async def healthz():
    """Health check ligero para monitores externos / balanceadores / Docker.
    Devuelve 200 si la app y la BD responden, 503 en caso contrario."""
    from sqlalchemy import text
    from fastapi.responses import JSONResponse
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return JSONResponse({"status": "ok", "database": "up"})
    except Exception as e:
        logger.error("Healthcheck FALLÓ: %s", e)
        return JSONResponse(
            {"status": "error", "database": "down", "detail": str(e)[:120]},
            status_code=503,
        )


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    db = SessionLocal()
    user = get_current_user(request, db)
    db.close()
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role in ("admin", "auditor"):
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


def _login_context(request, **extra):
    """Contexto base para login.html. Incluye el nombre de empresa configurable
    (Configuración → General), vacío por defecto."""
    from database import get_setting
    ctx = {"request": request, "error": None, "company_name": get_setting("company_name") or ""}
    ctx.update(extra)
    return ctx


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    db = SessionLocal()
    user = get_current_user(request, db)
    db.close()
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _login_context(request))


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    db = SessionLocal()
    ip = _client_ip(request)
    try:
        # Anti fuerza bruta: bloquear tras demasiados intentos fallidos desde la IP
        if too_many_login_attempts(db, ip):
            audit_mod.log(db, f"Login bloqueado por fuerza bruta — IP {ip} (usuario: '{username}')", level="warning", ip=ip)
            return templates.TemplateResponse("login.html", _login_context(
                request, error="⛔ Demasiados intentos fallidos. Espera 15 minutos e inténtalo de nuevo.",
            ), status_code=429)

        user = db.query(User).filter(User.username == username).first()

        if not user or not verify_password(password, user.hashed_password):
            audit_mod.log(db, f"Login fallido — usuario: '{username}'", level="warning", ip=ip)
            return templates.TemplateResponse("login.html", _login_context(
                request, error="Usuario o contraseña incorrectos",
            ), status_code=401)

        if not user.is_active:
            audit_mod.log(db, f"Login denegado — cuenta deshabilitada: {user.username}", user_id=user.id, level="warning", ip=ip)
            return templates.TemplateResponse("login.html", _login_context(
                request, error="⛔ Tu cuenta está deshabilitada. Contacta con el administrador.",
                error_type="disabled",
            ))

        audit_mod.log(db, f"Login correcto — {user.username} ({user.role})", user_id=user.id, level="info", ip=ip)

        token = create_session_token(user.id)
        if user.must_change_password:
            response = RedirectResponse("/change-password", status_code=302)
        elif user.role in ("admin", "auditor"):
            response = RedirectResponse("/admin", status_code=302)
        else:
            response = RedirectResponse("/dashboard", status_code=302)

        response.set_cookie(
            SESSION_COOKIE, token,
            httponly=True, samesite="strict", secure=COOKIE_SECURE,
            max_age=60 * 60 * 8,
        )
        return response
    finally:
        db.close()


@app.get("/logout")
async def logout(request: Request):
    db = SessionLocal()
    user = get_current_user(request, db)
    if user:
        # Invalida en servidor todos los tokens emitidos hasta ahora para este usuario
        u = db.query(User).filter(User.id == user.id).first()
        if u:
            u.tokens_valid_from = datetime.utcnow()
            db.commit()
        audit_mod.log(db, f"Logout — {user.username}", user_id=user.id)
    db.close()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Reseteo de contraseña (autoservicio por email, enlace firmado de 1 uso) ──
RESET_MAX_AGE = 60 * 60  # 1 hora


def _reset_throttled(db, ip: str) -> bool:
    """Limita solicitudes de reseteo por IP (anti-abuso/enumeración)."""
    if not ip:
        return False
    from models import AuditLog
    since = datetime.utcnow() - timedelta(minutes=15)
    n = (db.query(AuditLog).filter(
            AuditLog.action.like("Solicitud de reseteo%"),
            AuditLog.ip_address == ip,
            AuditLog.timestamp >= since).count())
    return n >= 5


@app.get("/forgot", response_class=HTMLResponse)
async def forgot_get(request: Request):
    from database import get_setting
    return templates.TemplateResponse("forgot.html", {
        "request": request, "sent": False,
        "company_name": get_setting("company_name") or "",
    })


@app.post("/forgot", response_class=HTMLResponse)
async def forgot_post(request: Request, identifier: str = Form(...)):
    from database import get_setting
    db = SessionLocal()
    ip = _client_ip(request)
    try:
        ctx = {"request": request, "sent": True, "company_name": get_setting("company_name") or ""}
        if _reset_throttled(db, ip):
            return templates.TemplateResponse("forgot.html", ctx)  # mismo mensaje, no revela nada
        ident = (identifier or "").strip()
        if ident:
            user = db.query(User).filter(
                (User.username == ident) | (User.email == ident),
                User.is_active == True,
            ).first()
            # No revelamos si existe o no. Solo enviamos si hay cuenta con email.
            if user and user.email:
                base = (get_setting("app_base_url") or "").rstrip("/")
                token = create_reset_token(user.id, user.hashed_password)
                reset_url = f"{base}/reset/{token}" if base else f"/reset/{token}"
                try:
                    from scheduler import send_password_reset_email
                    send_password_reset_email(user.username, user.email, reset_url, hours=RESET_MAX_AGE // 3600)
                except Exception:
                    logger.exception("No se pudo enviar el email de reseteo")
                audit_mod.log(db, f"Solicitud de reseteo de contraseña para {user.username}",
                              user_id=user.id, level="warning", ip=ip)
        return templates.TemplateResponse("forgot.html", ctx)
    finally:
        db.close()


def _reset_render(request, state, **extra):
    from database import get_setting
    return templates.TemplateResponse("reset.html", {
        "request": request, "state": state,
        "company_name": get_setting("company_name") or "", **extra,
    })


@app.get("/reset/{token}", response_class=HTMLResponse)
async def reset_get(request: Request, token: str):
    uid, fp, reason = decode_reset_token(token, RESET_MAX_AGE)
    if reason:
        return _reset_render(request, reason)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid, User.is_active == True).first()
        if not user or password_fingerprint(user.hashed_password) != fp:
            return _reset_render(request, "invalid")  # enlace ya usado o caducado
        return _reset_render(request, "form", token=token, username=user.username)
    finally:
        db.close()


@app.post("/reset/{token}", response_class=HTMLResponse)
async def reset_post(request: Request, token: str,
                     new_password: str = Form(...), confirm_password: str = Form(...)):
    uid, fp, reason = decode_reset_token(token, RESET_MAX_AGE)
    if reason:
        return _reset_render(request, reason)
    db = SessionLocal()
    ip = _client_ip(request)
    try:
        user = db.query(User).filter(User.id == uid, User.is_active == True).first()
        if not user or password_fingerprint(user.hashed_password) != fp:
            return _reset_render(request, "invalid")
        if len(new_password) < 8:
            return _reset_render(request, "form", token=token, username=user.username,
                                 error="La contraseña debe tener al menos 8 caracteres.")
        if new_password != confirm_password:
            return _reset_render(request, "form", token=token, username=user.username,
                                 error="Las contraseñas no coinciden.")
        user.hashed_password = hash_password(new_password)
        user.must_change_password = False
        user.tokens_valid_from = datetime.utcnow()  # cierra sesiones activas
        audit_mod.log(db, f"Contraseña restablecida vía enlace por {user.username}",
                      user_id=user.id, level="warning", ip=ip)
        db.commit()
        return _reset_render(request, "done")
    finally:
        db.close()


@app.post("/onboarding/done")
@require_login
async def onboarding_done(request: Request, tour: str = Form(...)):
    """Marca el wizard de bienvenida como visto para el usuario actual."""
    user = request.state.current_user
    db = request.state.db
    u = db.query(User).filter(User.id == user.id).first()
    if u:
        if tour == "admin":
            u.onboarding_admin_done = True
        elif tour == "tech":
            u.onboarding_tech_done = True
        db.commit()
    return JSONResponse({"ok": True})


@app.get("/change-password", response_class=HTMLResponse)
@require_login
async def change_password_get(request: Request):
    return templates.TemplateResponse("change_password.html", {
        "request": request,
        "user": request.state.current_user,
        "error": None,
    })


@app.post("/change-password", response_class=HTMLResponse)
@require_login
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = request.state.current_user

    if new_password != confirm_password:
        return templates.TemplateResponse("change_password.html", {
            "request": request, "user": user, "error": "Las contraseñas no coinciden",
        })
    if len(new_password) < 6:
        return templates.TemplateResponse("change_password.html", {
            "request": request, "user": user, "error": "Mínimo 6 caracteres",
        })

    db2 = SessionLocal()
    try:
        u = db2.query(User).filter(User.id == user.id).first()
        u.hashed_password = hash_password(new_password)
        u.must_change_password = False
        db2.commit()
        audit_mod.log(db2, f"Contraseña cambiada (forzado) — {user.username}", user_id=user.id)
    finally:
        db2.close()

    redirect_url = "/admin" if user.role == "admin" else "/dashboard"
    return RedirectResponse(redirect_url, status_code=302)
