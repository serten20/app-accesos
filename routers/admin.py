import csv
import io
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, FileResponse
from sqlalchemy import or_
from auth import require_admin, require_viewer, hash_password
from models import User, Company, TechnicianCompany, AuditLog, AppSettings
from database import SessionLocal, get_setting, set_setting
from compliance import compute_compliance
import audit as audit_mod
from tmpl import templates
from crypto import encrypt_secret, decrypt_secret

router = APIRouter(prefix="/admin")


def _assignable_tech_filter():
    """Condición SQLAlchemy para usuarios que pueden tener empresas asignadas:
    técnicos puros + admins marcados como 'también técnico'."""
    return or_(User.role == "technician", User.also_technician == True)


def _parse_date_flexible(s: str):
    """Parsea una fecha en 'AAAA-MM-DD' o 'DD/MM/AAAA'.
    Devuelve (date|None, ok). Cadena vacía → (None, True) = usar hoy.
    Formato inválido → (None, False)."""
    s = (s or "").strip()
    if not s:
        return None, True
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date(), True
        except ValueError:
            continue
    return None, False

# ── Almacén temporal para importación masiva (evita exponer contraseñas en el HTML) ──
import json
import os as _os
import secrets as _secrets

_IMPORTS_DIR = _os.path.join("data", "imports")


def _purge_old_imports(max_age_seconds: int = 3600):
    """Elimina ficheros de importación huérfanos (contienen contraseñas en claro)."""
    import time
    if not _os.path.isdir(_IMPORTS_DIR):
        return
    now = time.time()
    for fname in _os.listdir(_IMPORTS_DIR):
        path = _os.path.join(_IMPORTS_DIR, fname)
        try:
            if now - _os.path.getmtime(path) > max_age_seconds:
                _os.remove(path)
        except Exception:
            pass


def _store_import(valid_rows: list, mode: str) -> str:
    _os.makedirs(_IMPORTS_DIR, exist_ok=True)
    _purge_old_imports()
    token = _secrets.token_urlsafe(24)
    with open(_os.path.join(_IMPORTS_DIR, f"{token}.json"), "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "rows": valid_rows}, f)
    return token


def _load_import(token: str):
    # Sanea el token para evitar path traversal
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        return None
    path = _os.path.join(_IMPORTS_DIR, f"{token}.json")
    if not _os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _delete_import(token: str):
    if not token or not all(c.isalnum() or c in "-_" for c in token):
        return
    try:
        _os.remove(_os.path.join(_IMPORTS_DIR, f"{token}.json"))
    except Exception:
        pass


# ── Dashboard Global ────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@require_viewer
async def admin_dashboard(request: Request):
    db = request.state.db
    user = request.state.current_user

    kpis = compute_compliance(db)

    recent_logs = (
        db.query(AuditLog)
        .order_by(AuditLog.timestamp.desc())
        .limit(10)
        .all()
    )

    # Histórico de cumplimiento para la gráfica de tendencia (últimos 30 días)
    from models import ComplianceSnapshot
    snaps = (
        db.query(ComplianceSnapshot)
        .order_by(ComplianceSnapshot.snapshot_date.desc())
        .limit(30)
        .all()
    )
    snaps = list(reversed(snaps))  # cronológico ascendente
    history = {
        "labels": [s.snapshot_date.strftime("%d/%m") for s in snaps],
        "compliance": [s.compliance_pct for s in snaps],
        "critical": [s.critical_companies for s in snaps],
        "warning": [s.warning_companies for s in snaps],
    }

    # ── Próximos vencimientos por asignación técnico-empresa ─────────────────
    upcoming = []
    for tc in db.query(TechnicianCompany).all():
        if not tc.company or not tc.technician:
            continue
        upcoming.append({
            "company": tc.company.name,
            "company_id": tc.company.id,
            "tech": tc.technician.username,
            "days": tc.days_remaining,
            "status": tc.status,
            "status_color": tc.status_color,
        })
    upcoming.sort(key=lambda x: x["days"])
    exp_overdue = sum(1 for e in upcoming if e["days"] < 0)
    exp_7  = sum(1 for e in upcoming if 0 <= e["days"] <= 7)
    exp_30 = sum(1 for e in upcoming if 0 <= e["days"] <= 30)

    return templates.TemplateResponse("admin_panel.html", {
        "request": request,
        "user": user,
        "companies": sorted(kpis["companies"], key=lambda c: c.days_remaining),
        "technicians": kpis["technicians"],
        "recent_logs": recent_logs,
        "history": history,
        "upcoming": upcoming[:8],
        "exp_overdue": exp_overdue,
        "exp_7": exp_7,
        "exp_30": exp_30,
        **kpis,
    })


# ── Administradores ────────────────────────────────────────────────────────

@router.get("/admins", response_class=HTMLResponse)
@require_viewer
async def list_admins(request: Request):
    db = request.state.db
    admins = db.query(User).filter(
        User.role.in_(["admin", "auditor"])).order_by(User.created_at).all()
    return templates.TemplateResponse("admin_admins.html", {
        "request": request,
        "user": request.state.current_user,
        "admins": admins,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/my-companies", response_class=HTMLResponse)
@require_admin
async def admin_my_companies(request: Request):
    """Vista 'Mis Empresas' para administradores con doble rol (también técnicos).
    Reutiliza la plantilla del dashboard de técnico dentro del shell de admin."""
    db = request.state.db
    user = request.state.current_user
    if not user.also_technician:
        return RedirectResponse("/admin", status_code=302)
    tc_list = sorted(user.tc_assocs, key=lambda tc: tc.days_remaining)
    return templates.TemplateResponse("dashboard.html", {
        "request":  request,
        "user":     user,
        "tc_list":  tc_list,
        "total":    len(tc_list),
        "critical": sum(1 for tc in tc_list if tc.status == "critical"),
        "warning":  sum(1 for tc in tc_list if tc.status == "warning"),
        "ok":       sum(1 for tc in tc_list if tc.status == "ok"),
    })


@router.post("/admins/create")
@require_admin
async def create_admin(
    request: Request,
    username: str = Form(...),
    email: str = Form(default=""),
    password: str = Form(...),
    must_change_password: str = Form(default="off"),
    receive_reports: str = Form(default="off"),
    also_technician: str = Form(default="off"),
    user_type: str = Form(default="admin"),
):
    db = request.state.db
    email_clean = email.strip() or None
    new_role = "auditor" if user_type == "auditor" else "admin"

    # Un auditor es solo lectura: no recibe reportes ni actúa como técnico
    if new_role == "auditor":
        receive_reports = "off"
        also_technician = "off"

    # Email obligatorio si quiere recibir reportes
    if receive_reports == "on" and not email_clean:
        return RedirectResponse("/admin/admins?error=email_required", status_code=302)

    # Verificar duplicados (username siempre; email solo si se proporcionó)
    dup_query = db.query(User).filter(User.username == username)
    if email_clean:
        dup_query = db.query(User).filter(
            (User.username == username) | (User.email == email_clean)
        )
    if dup_query.first():
        return RedirectResponse("/admin/admins?error=duplicate", status_code=302)

    u = User(
        username=username,
        email=email_clean,
        hashed_password=hash_password(password),
        role=new_role,
        must_change_password=(must_change_password == "on"),
        receive_reports=(receive_reports == "on"),
        also_technician=(also_technician == "on"),
    )
    db.add(u)
    db.flush()
    if new_role == "auditor":
        detail = "Auditor (solo lectura)"
    else:
        detail = "Admin" + (" · también técnico" if u.also_technician else "")
    audit_mod.log(db, f"Usuario de consola creado: {username} — {detail} (por {request.state.current_user.username})",
                  user_id=request.state.current_user.id, level="warning")
    db.commit()
    return RedirectResponse("/admin/admins?success=created", status_code=302)


@router.post("/admins/{admin_id}/toggle-reports")
@require_admin
async def toggle_admin_reports(request: Request, admin_id: int):
    """Activa/desactiva recepción de reportes para un admin."""
    db = request.state.db
    u = db.query(User).filter(User.id == admin_id, User.role == "admin").first()
    if not u:
        return RedirectResponse("/admin/admins?error=notfound", status_code=302)
    if not u.email:
        return RedirectResponse("/admin/admins?error=no_email", status_code=302)
    u.receive_reports = not u.receive_reports
    state = "activada" if u.receive_reports else "desactivada"
    audit_mod.log(db, f"Recepción de reportes {state} para admin {u.username}",
                  user_id=request.state.current_user.id)
    db.commit()
    return RedirectResponse("/admin/admins", status_code=302)


@router.post("/admins/{admin_id}/toggle-technician")
@require_admin
async def toggle_admin_technician(request: Request, admin_id: int):
    """Activa/desactiva el doble rol (admin que también es técnico)."""
    db = request.state.db
    u = db.query(User).filter(User.id == admin_id, User.role == "admin").first()
    if not u:
        return RedirectResponse("/admin/admins?error=notfound", status_code=302)
    u.also_technician = not u.also_technician
    if u.also_technician:
        # Al volver a ser técnico, que vea el tour de técnico la próxima vez
        u.onboarding_tech_done = False
        state = "activado"
    else:
        state = "desactivado"
    audit_mod.log(db, f"Doble rol (técnico) {state} para admin {u.username}",
                  user_id=request.state.current_user.id)
    db.commit()
    return RedirectResponse("/admin/admins?success=dual_toggled", status_code=302)


@router.post("/admins/{admin_id}/demote")
@require_admin
async def demote_admin_to_tech(request: Request, admin_id: int):
    """Degrada un administrador a técnico puro (role=technician)."""
    db = request.state.db
    me = request.state.current_user
    u = db.query(User).filter(User.id == admin_id, User.role == "admin").first()
    if not u:
        return RedirectResponse("/admin/admins?error=notfound", status_code=302)
    if u.id == me.id:
        return RedirectResponse("/admin/admins?error=self", status_code=302)
    # No dejar el sistema sin administradores activos
    total_admins = db.query(User).filter(User.role == "admin", User.is_active == True).count()
    if total_admins <= 1:
        return RedirectResponse("/admin/admins?error=last", status_code=302)
    u.role = "technician"
    u.also_technician = False
    u.receive_reports = False           # los técnicos no reciben reportes de admin
    u.onboarding_tech_done = False       # que vea el tour de técnico
    audit_mod.log(db, f"Admin {u.username} degradado a técnico por {me.username}",
                  user_id=me.id, level="warning")
    db.commit()
    return RedirectResponse("/admin/users?success=demoted", status_code=302)


@router.post("/admins/send-report-now")
@require_admin
async def send_report_now(request: Request):
    """Envía el reporte de estado inmediatamente a todos los admins con receive_reports=True."""
    from scheduler import job_report_send
    try:
        sent = job_report_send()
        return RedirectResponse(f"/admin/admins?success=report_sent&count={sent}", status_code=302)
    except Exception as e:
        return RedirectResponse(f"/admin/admins?error=report_error&msg={str(e)[:80]}", status_code=302)


@router.post("/admins/{admin_id}/toggle")
@require_admin
async def toggle_admin(request: Request, admin_id: int):
    db = request.state.db
    me = request.state.current_user
    u = db.query(User).filter(User.id == admin_id, User.role.in_(["admin", "auditor"])).first()
    if not u:
        return RedirectResponse("/admin/admins?error=notfound", status_code=302)
    if u.id == me.id:
        return RedirectResponse("/admin/admins?error=self", status_code=302)
    u.is_active = not u.is_active
    action = "habilitado" if u.is_active else "deshabilitado"
    audit_mod.log(db, f"Usuario {u.username} ({u.role}) {action} por {me.username}",
                  user_id=me.id, level="warning")
    db.commit()
    return RedirectResponse("/admin/admins", status_code=302)


@router.post("/admins/{admin_id}/delete")
@require_admin
async def delete_admin(request: Request, admin_id: int):
    db = request.state.db
    me = request.state.current_user
    u = db.query(User).filter(User.id == admin_id, User.role.in_(["admin", "auditor"])).first()
    if not u:
        return RedirectResponse("/admin/admins?error=notfound", status_code=302)
    if u.id == me.id:
        return RedirectResponse("/admin/admins?error=self", status_code=302)
    # La protección "último admin" solo cuenta administradores reales (no auditores)
    if u.role == "admin":
        total_admins = db.query(User).filter(User.role == "admin", User.is_active == True).count()
        if total_admins <= 1:
            return RedirectResponse("/admin/admins?error=last", status_code=302)
    db.delete(u)
    audit_mod.log(db, f"Usuario {u.username} ({u.role}) eliminado por {me.username}",
                  user_id=me.id, level="warning")
    db.commit()
    return RedirectResponse("/admin/admins", status_code=302)


# ── Usuarios / Técnicos ─────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
@require_viewer
async def list_users(request: Request):
    db = request.state.db
    technicians = db.query(User).filter(User.role == "technician").all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "user": request.state.current_user,
        "technicians": technicians,
        "welcome_default_on": (get_setting("welcome_default_on") or "off") == "on",
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/users/create")
@require_admin
async def create_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    must_change_password: str = Form(default="off"),
    send_welcome: str = Form(default="off"),
):
    db = request.state.db
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return RedirectResponse("/admin/users?error=duplicate", status_code=302)
    u = User(
        username=username,
        email=email,
        hashed_password=hash_password(password),
        role="technician",
        must_change_password=(must_change_password == "on"),
    )
    db.add(u)
    db.flush()
    audit_mod.log(db, f"Usuario creado: {username} (por admin)", user_id=request.state.current_user.id)
    db.commit()
    # Email de bienvenida con credenciales (contraseña aún en claro aquí)
    welcomed = False
    if send_welcome == "on" and email:
        try:
            from scheduler import send_welcome_email
            welcomed = send_welcome_email(username, password, email)
        except Exception:
            welcomed = False
    flag = "created_welcome" if (send_welcome == "on" and welcomed) else ("created_welcome_fail" if send_welcome == "on" else "created")
    return RedirectResponse(f"/admin/users?success={flag}", status_code=302)


@router.post("/users/{user_id}/reset-password")
@require_admin
async def reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    force_change: str = Form(default="on"),
):
    db = request.state.db
    if len(new_password) < 6:
        return RedirectResponse(f"/admin/technician/{user_id}?error=password_short", status_code=302)
    u = db.query(User).filter(User.id == user_id).first()
    if u and u.role != "admin":
        u.hashed_password = hash_password(new_password)
        u.must_change_password = (force_change == "on")
        # Invalida sesiones activas del usuario tras el reseteo
        u.tokens_valid_from = datetime.utcnow()
        audit_mod.log(db, f"Contraseña reseteada para {u.username} por admin", user_id=request.state.current_user.id, level="warning")
        db.commit()
    return RedirectResponse(f"/admin/technician/{user_id}?success=password_reset", status_code=302)


@router.post("/users/{user_id}/toggle")
@require_admin
async def toggle_user(request: Request, user_id: int):
    db = request.state.db
    u = db.query(User).filter(User.id == user_id).first()
    if u and u.role != "admin":
        u.is_active = not u.is_active
        action = "habilitado" if u.is_active else "deshabilitado"
        audit_mod.log(db, f"Usuario {u.username} {action} por admin", user_id=request.state.current_user.id, level="warning")
        db.commit()
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/users/{user_id}/delete")
@require_admin
async def delete_user(request: Request, user_id: int):
    db = request.state.db
    u = db.query(User).filter(User.id == user_id).first()
    if u and u.role != "admin":
        import trash
        trash.archive_technician(db, u, request.state.current_user.username)
        audit_mod.log(db, f"Técnico archivado a papelera: {u.username}", user_id=request.state.current_user.id, level="warning")
        db.commit()
    return RedirectResponse("/admin/users?success=archived", status_code=302)


@router.post("/users/{user_id}/promote")
@require_admin
async def promote_tech_to_admin(request: Request, user_id: int):
    """Promueve un técnico a administrador, manteniendo sus funciones de técnico
    (role=admin + also_technician=True). Conserva email, contraseña y empresas."""
    db = request.state.db
    u = db.query(User).filter(User.id == user_id, User.role == "technician").first()
    if not u:
        return RedirectResponse("/admin/users?error=notfound", status_code=302)
    u.role = "admin"
    u.also_technician = True
    u.onboarding_admin_done = False      # que vea el tour de admin la próxima vez
    audit_mod.log(db, f"Técnico {u.username} promovido a administrador (también técnico) por {request.state.current_user.username}",
                  user_id=request.state.current_user.id, level="warning")
    db.commit()
    return RedirectResponse("/admin/admins?success=promoted", status_code=302)


# ── Empresas ────────────────────────────────────────────────────────────────

@router.get("/companies", response_class=HTMLResponse)
@require_viewer
async def list_companies(request: Request):
    db = request.state.db
    companies = db.query(Company).all()
    technicians = db.query(User).filter(_assignable_tech_filter(), User.is_active == True).all()
    return templates.TemplateResponse("admin_companies.html", {
        "request": request,
        "user": request.state.current_user,
        "companies": sorted(companies, key=lambda c: c.days_remaining),
        "technicians": technicians,
    })


@router.post("/companies/create")
@require_admin
async def create_company(
    request: Request,
    name: str = Form(...),
    vpn_url: str = Form(""),
    doc_url: str = Form(""),
    expiry_days: int = Form(30),
    notes: str = Form(""),
    last_changed: str = Form(""),
    technician_ids: list[int] = Form(default=[]),
):
    db = request.state.db
    existing = db.query(Company).filter(Company.name == name).first()
    if existing:
        return RedirectResponse("/admin/companies?error=duplicate", status_code=302)
    start_date, ok = _parse_date_flexible(last_changed)
    if not ok:
        return RedirectResponse("/admin/companies?error=bad_date", status_code=302)
    c = Company(
        name=name,
        vpn_url=vpn_url or None,
        doc_url=doc_url or None,
        expiry_days=expiry_days,
        notes=notes or None,
        last_changed=start_date or date.today(),   # vacío → hoy
    )
    db.add(c)
    db.flush()
    for tech_id in technician_ids:
        tech = db.query(User).filter(User.id == tech_id, _assignable_tech_filter()).first()
        if tech:
            # El técnico arranca desde la fecha de inicio de la empresa (snapshot)
            db.add(TechnicianCompany(technician_id=tech_id, company_id=c.id,
                                     last_changed=c.last_changed or date.today()))
    audit_mod.log(db, f"Empresa creada: {name} (inicio {c.last_changed})", user_id=request.state.current_user.id, company_id=c.id)
    db.commit()
    return RedirectResponse("/admin/companies", status_code=302)


@router.post("/companies/{company_id}/delete")
@require_admin
async def delete_company(request: Request, company_id: int):
    db = request.state.db
    c = db.query(Company).filter(Company.id == company_id).first()
    if c:
        import trash
        name = c.name
        trash.archive_company(db, c, request.state.current_user.username)
        audit_mod.log(db, f"Empresa archivada a papelera: {name}", user_id=request.state.current_user.id, level="warning")
        db.commit()
    return RedirectResponse("/admin/companies?success=archived", status_code=302)


# ── Ficha Empresa ────────────────────────────────────────────────────────────

@router.get("/company/{company_id}", response_class=HTMLResponse)
@require_viewer
async def company_detail(request: Request, company_id: int):
    db = request.state.db
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        return RedirectResponse("/admin/companies", status_code=302)
    all_techs = db.query(User).filter(_assignable_tech_filter(), User.is_active == True).all()
    assigned_ids = {t.id for t in company.technicians}

    # Estado de cada técnico EN ESTA EMPRESA (usando su propio last_changed)
    tech_performance = []
    for tc in company.tc_assocs:
        tech = tc.technician
        tech_performance.append({
            "tech":          tech,
            "tc":            tc,
            "days_remaining": tc.days_remaining,
            "status":        tc.status,
            "status_color":  tc.status_color,
            "last_changed":  tc._effective_last_changed,
            "progress_pct":  tc.progress_pct,
        })
    tech_performance.sort(key=lambda x: x["days_remaining"])

    # ── Histórico de rotaciones de esta empresa ──────────────────────────────
    from models import RotationHistory
    history = (db.query(RotationHistory)
               .filter(RotationHistory.company_id == company_id)
               .order_by(RotationHistory.confirmed_at.desc())
               .limit(100).all())
    h_total = db.query(RotationHistory).filter(RotationHistory.company_id == company_id).count()
    h_late = sum(1 for r in history if (r.days_late or 0) > 0)
    h_ontime = len(history) - h_late
    h_pct = round(h_ontime / len(history) * 100) if history else 100
    h_techs = len({r.technician_name for r in history})
    h_first = history[-1].confirmed_at if history else None
    h_last = history[0].confirmed_at if history else None

    return templates.TemplateResponse("admin_company.html", {
        "request": request,
        "user": request.state.current_user,
        "company": company,
        "all_techs": all_techs,
        "assigned_ids": assigned_ids,
        "tech_performance": tech_performance,
        "history": history,
        "h_stats": {
            "total": h_total, "shown": len(history), "late": h_late, "ontime": h_ontime,
            "pct": h_pct, "techs": h_techs, "first": h_first, "last": h_last,
        },
    })


@router.post("/company/{company_id}/update")
@require_admin
async def update_company(
    request: Request,
    company_id: int,
    name: str = Form(...),
    vpn_url: str = Form(""),
    doc_url: str = Form(""),
    expiry_days: int = Form(30),
    notes: str = Form(""),
    last_changed: str = Form(""),
    apply_to_techs: str = Form("off"),
):
    db = request.state.db
    start_date, ok = _parse_date_flexible(last_changed)
    if not ok:
        return RedirectResponse(f"/admin/company/{company_id}?error=bad_date", status_code=302)
    c = db.query(Company).filter(Company.id == company_id).first()
    if c:
        c.name = name
        c.vpn_url = vpn_url or None
        c.doc_url = doc_url or None
        c.expiry_days = expiry_days
        c.notes = notes or None
        if start_date:
            c.last_changed = start_date
        # Propagar a técnicos asignados que NO han confirmado (sin RotationHistory)
        if apply_to_techs == "on":
            from models import RotationHistory
            for tc in c.tc_assocs:
                confirmed = db.query(RotationHistory).filter(
                    RotationHistory.technician_id == tc.technician_id,
                    RotationHistory.company_id == company_id,
                ).first()
                if not confirmed:
                    tc.last_changed = c.last_changed   # alinea con la nueva fecha de la empresa
        audit_mod.log(db, f"Empresa actualizada: {name} (inicio {c.last_changed})", user_id=request.state.current_user.id, company_id=c.id)
        db.commit()
    return RedirectResponse(f"/admin/company/{company_id}?success=1", status_code=302)


# ── Ficha Técnico ────────────────────────────────────────────────────────────

@router.get("/technician/{tech_id}", response_class=HTMLResponse)
@require_viewer
async def technician_detail(request: Request, tech_id: int):
    db = request.state.db
    tech = db.query(User).filter(User.id == tech_id, User.role == "technician").first()
    if not tech:
        return RedirectResponse("/admin/users", status_code=302)
    all_companies = db.query(Company).all()
    # Estado POR TÉCNICO (su propio TechnicianCompany), no el global de la empresa
    tc_map = {tc.company_id: tc for tc in tech.tc_assocs}
    assigned_ids = set(tc_map.keys())
    assigned = list(tc_map.values())
    tstats = {
        "total":    len(assigned),
        "ok":       sum(1 for tc in assigned if tc.status == "ok"),
        "warning":  sum(1 for tc in assigned if tc.status == "warning"),
        "critical": sum(1 for tc in assigned if tc.status == "critical"),
    }
    # Datos serializables por empresa para la plantilla (días/estado del técnico)
    tc_state = {
        cid: {"days": tc.days_remaining, "status": tc.status, "color": tc.status_color}
        for cid, tc in tc_map.items()
    }
    return templates.TemplateResponse("admin_technician.html", {
        "request": request,
        "user": request.state.current_user,
        "tech": tech,
        "all_companies": sorted(all_companies, key=lambda c: c.name),
        "assigned_ids": assigned_ids,
        "tc_state": tc_state,
        "tstats": tstats,
        "success": request.query_params.get("success"),
    })


# ── Asignación Many-to-Many ──────────────────────────────────────────────────

@router.post("/assign")
@require_admin
async def toggle_assign(
    request: Request,
    tech_id: int = Form(...),
    company_id: int = Form(...),
    redirect_to: str = Form("company"),
):
    db = request.state.db
    existing = db.query(TechnicianCompany).filter_by(
        technician_id=tech_id, company_id=company_id
    ).first()
    tech = db.query(User).filter(User.id == tech_id).first()
    company = db.query(Company).filter(Company.id == company_id).first()
    if existing:
        db.delete(existing)
        audit_mod.log(db, f"Desasignado {tech.username if tech else tech_id} ← {company.name if company else company_id}", user_id=request.state.current_user.id)
    else:
        # El técnico arranca desde la fecha de inicio de la empresa (snapshot)
        anchor = (company.last_changed if company else None) or date.today()
        db.add(TechnicianCompany(technician_id=tech_id, company_id=company_id, last_changed=anchor))
        audit_mod.log(db, f"Asignado {tech.username if tech else tech_id} → {company.name if company else company_id}", user_id=request.state.current_user.id)
    db.commit()

    if redirect_to == "technician":
        return RedirectResponse(f"/admin/technician/{tech_id}", status_code=302)
    return RedirectResponse(f"/admin/company/{company_id}", status_code=302)


# ── Bulk Import ──────────────────────────────────────────────────────────────

@router.get("/bulk-import", response_class=HTMLResponse)
@require_admin
async def bulk_import_form(request: Request):
    mode = request.query_params.get("mode", "companies")
    return templates.TemplateResponse("admin_bulk_import.html", {
        "request": request,
        "user": request.state.current_user,
        "preview": None,
        "mode": mode,
        "welcome_default_on": (get_setting("welcome_default_on") or "off") == "on",
    })


@router.get("/bulk-import/sample/{mode}/{fmt}")
@require_admin
async def download_sample(request: Request, mode: str, fmt: str):
    if mode == "companies":
        content = "name,vpn_url,doc_url,expiry_days,notes,last_changed\n"
        content += "Empresa Ejemplo 1,https://vpn.empresa1.com,https://docs.empresa1.com,30,Cliente principal,\n"
        content += "Empresa Ejemplo 2,https://vpn.empresa2.com,,60,Renovación trimestral,2026-06-17\n"
        content += "Empresa Ejemplo 3,,,90,Sin acceso VPN directo,17/06/2026\n"
        filename = f"muestra_empresas.{fmt}"
    elif mode == "users":
        content = "username,email,password,must_change_password\n"
        content += "tecnico01,tecnico01@empresa.com,Pass1234!,si\n"
        content += "tecnico02,tecnico02@empresa.com,Pass1234!,si\n"
        content += "tecnico03,tecnico03@empresa.com,,si\n"
        filename = f"muestra_usuarios.{fmt}"
    elif mode == "assignments":
        content = "tecnico,empresa,last_changed\n"
        content += "tecnico01,Empresa Ejemplo 1,2026-06-17\n"
        content += "tecnico01,Empresa Ejemplo 2,\n"
        content += "tecnico02,Empresa Ejemplo 1,17/06/2026\n"
        filename = f"muestra_asignaciones.{fmt}"
    else:
        return Response("Modo no válido", status_code=400)

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bulk-import/preview", response_class=HTMLResponse)
@require_admin
async def bulk_import_preview(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default="companies"),
):
    db = request.state.db
    content = await file.read()
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    rows = []
    seen_in_file = set()

    if mode == "companies":
        existing_names = {c.name.lower() for c in db.query(Company).all()}
        required = {"name", "expiry_days"}

        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            row_errors = []
            if not required.issubset(row.keys()):
                row_errors.append("Faltan columnas: name, expiry_days")
            else:
                if not row.get("name"):
                    row_errors.append("name vacío")
                elif row["name"].lower() in existing_names:
                    row_errors.append("Ya existe en BD")
                elif row["name"].lower() in seen_in_file:
                    row_errors.append("Duplicado en archivo")
                try:
                    int(row.get("expiry_days", ""))
                except ValueError:
                    row_errors.append("expiry_days debe ser número")
                # Fecha de inicio opcional: vacía = hoy; con valor debe ser válida
                _d, _ok = _parse_date_flexible(row.get("last_changed", ""))
                if not _ok:
                    row_errors.append("last_changed inválida (usa AAAA-MM-DD o DD/MM/AAAA)")
            seen_in_file.add(row.get("name", "").lower())
            rows.append({
                "line": i,
                "col1": row.get("name", ""),
                "col2": row.get("vpn_url", ""),
                "col3": row.get("doc_url", ""),
                "col4": row.get("expiry_days", "30"),
                "col5": row.get("notes", ""),
                "col6": row.get("last_changed", ""),
                "errors": row_errors,
                "valid": len(row_errors) == 0,
            })

    elif mode == "users":
        existing_usernames = {u.username.lower() for u in db.query(User).all()}
        existing_emails = {u.email.lower() for u in db.query(User).all()}
        required = {"username", "email"}

        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            row_errors = []
            if not required.issubset(row.keys()):
                row_errors.append("Faltan columnas: username, email")
            else:
                if not row.get("username"):
                    row_errors.append("username vacío")
                elif row["username"].lower() in existing_usernames:
                    row_errors.append("Username ya existe")
                elif row["username"].lower() in seen_in_file:
                    row_errors.append("Duplicado en archivo")
                if not row.get("email"):
                    row_errors.append("email vacío")
                elif row["email"].lower() in existing_emails:
                    row_errors.append("Email ya existe")
                # Contraseña: vacía = se generará una temporal (migración).
                # Si se indica una, debe tener al menos 6 caracteres.
                pwd = row.get("password", "")
                if pwd and len(pwd) < 6:
                    row_errors.append("Contraseña muy corta (mín. 6 chars)")
            seen_in_file.add(row.get("username", "").lower())
            rows.append({
                "line": i,
                "col1": row.get("username", ""),
                "col2": row.get("email", ""),
                "col3": row.get("password", ""),
                "col4": row.get("must_change_password", "si"),
                "col5": "",
                "errors": row_errors,
                "valid": len(row_errors) == 0,
            })

    elif mode == "assignments":
        techs_by_name = {u.username.lower(): u for u in db.query(User).filter(_assignable_tech_filter()).all()}
        comps_by_name = {c.name.lower(): c for c in db.query(Company).all()}
        from models import TechnicianCompany
        existing_pairs = {(tc.technician_id, tc.company_id) for tc in db.query(TechnicianCompany).all()}
        required = {"tecnico", "empresa"}

        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            row_errors = []
            if not required.issubset(row.keys()):
                row_errors.append("Faltan columnas: tecnico, empresa")
            else:
                tname = row.get("tecnico", "")
                cname = row.get("empresa", "")
                tech = techs_by_name.get(tname.lower())
                comp = comps_by_name.get(cname.lower())
                if not tname:
                    row_errors.append("tecnico vacío")
                elif not tech:
                    row_errors.append("Técnico no existe")
                if not cname:
                    row_errors.append("empresa vacía")
                elif not comp:
                    row_errors.append("Empresa no existe")
                pair_key = (tname.lower(), cname.lower())
                if tech and comp and (tech.id, comp.id) in existing_pairs:
                    row_errors.append("Ya asignada")
                elif pair_key in seen_in_file:
                    row_errors.append("Duplicado en archivo")
                _d, _ok = _parse_date_flexible(row.get("last_changed", ""))
                if not _ok:
                    row_errors.append("last_changed inválida (AAAA-MM-DD o DD/MM/AAAA)")
                seen_in_file.add(pair_key)
            rows.append({
                "line": i,
                "col1": row.get("tecnico", ""),
                "col2": row.get("empresa", ""),
                "col3": row.get("last_changed", ""),
                "col4": "",
                "col5": "",
                "errors": row_errors,
                "valid": len(row_errors) == 0,
            })

    # Guardar SOLO las filas válidas en un almacén temporal en servidor.
    # Así las contraseñas no se incrustan en el HTML ni viajan de vuelta por el navegador.
    valid_rows = [
        {"col1": r["col1"], "col2": r["col2"], "col3": r["col3"],
         "col4": r["col4"], "col5": r["col5"], "col6": r.get("col6", "")}
        for r in rows if r["valid"]
    ]
    import_token = _store_import(valid_rows, mode) if valid_rows else ""

    return templates.TemplateResponse("admin_bulk_import.html", {
        "request": request,
        "user": request.state.current_user,
        "preview": rows,
        "mode": mode,
        "import_token": import_token,
        "valid_count": sum(1 for r in rows if r["valid"]),
        "error_count": sum(1 for r in rows if not r["valid"]),
        "welcome_default_on": (get_setting("welcome_default_on") or "off") == "on",
    })


@router.post("/bulk-import/confirm")
@require_admin
async def bulk_import_confirm(request: Request):
    db = request.state.db
    form = await request.form()
    import_token = form.get("import_token", "")

    data = _load_import(import_token)
    if not data:
        return RedirectResponse("/admin/bulk-import?error=expired", status_code=302)

    mode = data.get("mode", "companies")
    rows = data.get("rows", [])
    inserted = 0

    if mode == "companies":
        for r in rows:
            name = r["col1"]
            if not db.query(Company).filter(Company.name == name).first():
                _start, _ok = _parse_date_flexible(r.get("col6", ""))
                db.add(Company(
                    name=name, vpn_url=r["col2"] or None, doc_url=r["col3"] or None,
                    expiry_days=int(r["col4"]) if r["col4"] else 30,
                    notes=r["col5"] or None,
                    last_changed=(_start if _ok else None) or date.today(),
                ))
                inserted += 1
        db.commit()
        _delete_import(import_token)
        audit_mod.log(db, f"Importación masiva: {inserted} empresas importadas", user_id=request.state.current_user.id)
        return RedirectResponse(f"/admin/companies?imported={inserted}", status_code=302)
    elif mode == "assignments":
        from models import TechnicianCompany
        techs_by_name = {u.username.lower(): u for u in db.query(User).filter(_assignable_tech_filter()).all()}
        comps_by_name = {c.name.lower(): c for c in db.query(Company).all()}
        for r in rows:
            tech = techs_by_name.get((r["col1"] or "").lower())
            comp = comps_by_name.get((r["col2"] or "").lower())
            if not tech or not comp:
                continue
            if db.query(TechnicianCompany).filter_by(technician_id=tech.id, company_id=comp.id).first():
                continue
            _d, _ok = _parse_date_flexible(r.get("col3", ""))
            tc = TechnicianCompany(technician_id=tech.id, company_id=comp.id)
            # last_changed tiene default=hoy; solo lo fijamos si viene una fecha válida
            if _ok and _d:
                tc.last_changed = _d
            db.add(tc)
            inserted += 1
        db.commit()
        _delete_import(import_token)
        audit_mod.log(db, f"Importación masiva: {inserted} asignaciones importadas", user_id=request.state.current_user.id)
        return RedirectResponse(f"/admin/matrix?imported={inserted}", status_code=302)

    else:
        send_welcome = (form.get("send_welcome", "") == "on")
        new_users = []   # (username, email, password) para la bienvenida
        for r in rows:
            username, email, password, must_change = r["col1"], r["col2"], r["col3"], r["col4"]
            if not db.query(User).filter(User.username == username).first():
                # Contraseña vacía (migración) → generar temporal y forzar cambio
                generated = not password
                if generated:
                    password = _secrets.token_urlsafe(9)
                must_flag = generated or (str(must_change).lower() in ("si", "sí", "yes", "1", "true"))
                db.add(User(
                    username=username, email=email,
                    hashed_password=hash_password(password),
                    role="technician",
                    must_change_password=must_flag,
                ))
                inserted += 1
                new_users.append((username, email, password))
        db.commit()
        _delete_import(import_token)
        audit_mod.log(db, f"Importación masiva: {inserted} usuarios importados", user_id=request.state.current_user.id)
        welcomed = 0
        if send_welcome and new_users:
            from scheduler import send_welcome_email
            for uname, mail, pwd in new_users:
                if mail and send_welcome_email(uname, pwd, mail):
                    welcomed += 1
        suffix = f"&welcomed={welcomed}" if send_welcome else ""
        return RedirectResponse(f"/admin/users?imported={inserted}{suffix}", status_code=302)


# ── Settings — claves ────────────────────────────────────────────────────────

SMTP_KEYS   = ["smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_security"]
ALERT_KEYS  = ["alert_start_days", "alert_interval_hours", "email_subject", "email_body",
               "alert_escalation_enabled", "alert_escalation_count",
               "alert_escalation_admin_ids", "alert_escalation_extra_emails"]
REPORT_KEYS = ["report_subject", "report_body", "report_day", "report_hour"]
WELCOME_KEYS = ["welcome_subject", "welcome_body", "welcome_default_on"]
GEN_KEYS    = ["timezone", "app_base_url", "confirm_token_hours"]


# ── SMTP — servidor ──────────────────────────────────────────────────────────

def _save_smtp_settings(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_security):
    """Persiste la configuración SMTP. La contraseña solo se re-cifra si llega
    una nueva (campo vacío = conservar la existente)."""
    set_setting("smtp_host", smtp_host.strip())
    set_setting("smtp_port", smtp_port.strip() or "587")
    set_setting("smtp_user", smtp_user.strip())
    if smtp_pass:
        set_setting("smtp_pass", encrypt_secret(smtp_pass))
    set_setting("smtp_from", smtp_from.strip() or smtp_user.strip())
    set_setting("smtp_security", smtp_security if smtp_security in ("auto", "starttls", "ssl", "none") else "auto")


@router.get("/settings/smtp", response_class=HTMLResponse)
@require_viewer
async def smtp_settings_get(request: Request):
    cfg = {k: get_setting(k) for k in SMTP_KEYS}
    cfg.setdefault("smtp_security", "auto")
    test = {
        "status": get_setting("smtp_test_status"),
        "detail": get_setting("smtp_test_detail"),
        "at": None,
    }
    raw_at = get_setting("smtp_test_at")
    if raw_at:
        try:
            from datetime import datetime as _dt
            test["at"] = _dt.fromisoformat(raw_at).strftime("%d/%m/%Y %H:%M")
        except Exception:
            test["at"] = raw_at
    return templates.TemplateResponse("admin_smtp.html", {
        "request": request,
        "user": request.state.current_user,
        "cfg": cfg,
        "test": test,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/settings/smtp")
@require_admin
async def smtp_settings_post(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    smtp_from: str = Form(""),
    smtp_security: str = Form("auto"),
):
    db = request.state.db
    _save_smtp_settings(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_security)
    audit_mod.log(db, "Configuración SMTP actualizada", user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/smtp?success=1", status_code=302)


@router.post("/settings/smtp/check")
@require_admin
async def smtp_check_post(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    smtp_from: str = Form(""),
    smtp_security: str = Form("auto"),
):
    """Guarda la configuración del formulario y prueba la conexión SMTP sin
    enviar email."""
    from scheduler import smtp_diagnose
    _save_smtp_settings(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_security)
    res = smtp_diagnose()
    if res["ok"]:
        return RedirectResponse("/admin/settings/smtp?success=check_ok", status_code=302)
    return RedirectResponse("/admin/settings/smtp?error=check_fail", status_code=302)


@router.post("/settings/smtp/test")
@require_admin
async def smtp_test_post(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    smtp_from: str = Form(""),
    smtp_security: str = Form("auto"),
):
    """Guarda la configuración del formulario y envía un email de prueba a TU
    propio correo (el del admin logueado). El From sigue siendo el remitente."""
    from scheduler import _smtp_send, _log_email, _record_smtp_test
    _save_smtp_settings(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_security)
    me = request.state.current_user
    if not me.email:
        _record_smtp_test(False, "Tu usuario no tiene email configurado. Añádelo en tu perfil para recibir la prueba.")
        return RedirectResponse("/admin/settings/smtp?error=no_my_email", status_code=302)
    to_addr = me.email
    subject = "✅ Test SMTP — Gestión de Accesos"
    body = ("<b>Email de prueba desde Gestión de Accesos ✓</b>"
            "<p>Si lees esto, el envío SMTP funciona correctamente.</p>")
    ok, err = _smtp_send(to_addr, subject, body)
    _log_email(to_addr, subject, body, "test", ok, err)
    _record_smtp_test(ok, (f"Email de prueba enviado a {to_addr} (tu correo)" if ok else (err or "Error desconocido")))
    if ok:
        return RedirectResponse("/admin/settings/smtp?success=test_ok", status_code=302)
    return RedirectResponse("/admin/settings/smtp?error=test_fail", status_code=302)


# ── Alertas a técnicos ───────────────────────────────────────────────────────

def _pick_alert_example(db):
    """Elige el par (técnico, empresa) más relevante para la vista previa:
    el más crítico (menor días restantes) con técnico y empresa válidos.
    Devuelve (tc, mapping) o (None, fallback_placeholders)."""
    from models import TechnicianCompany
    from scheduler import alert_mapping
    tcs = [tc for tc in db.query(TechnicianCompany).all() if tc.technician and tc.company]
    if tcs:
        example = min(tcs, key=lambda tc: tc.days_remaining)
        return example, alert_mapping(example.technician.username, example.company, example)
    # Sin datos: placeholders neutros
    from datetime import date, timedelta
    return None, {
        "{technician}": "(técnico de ejemplo)",
        "{company}": "(empresa de ejemplo)",
        "{days_remaining}": "1",
        "{vpn_url}": "https://vpn.empresa.com",
        "{doc_url}": "https://docs.empresa.com",
        "{last_changed}": str(date.today() - timedelta(days=29)),
        "{expiry_date}": (date.today() + timedelta(days=1)).strftime("%d/%m/%Y"),
        "{vpn_url_block}": '<p><b>Acceso VPN:</b> <a href="https://vpn.empresa.com">https://vpn.empresa.com</a></p>',
        "{confirm_url}": "#",
        "{console_url}": "#",
        "{urgency_color}": "#dc2626",
        "{cta_button}": ('<a href="#" style="background-color:#16a34a;border-radius:8px;color:#ffffff;display:inline-block;'
                         'font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;line-height:46px;text-align:center;'
                         'text-decoration:none;width:240px">&#10003; Confirmar rotación</a>'),
        "{access_buttons}": ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="display:inline-block;margin:4px 4px"><tr><td bgcolor="#eef2f7" align="center" style="background-color:#eef2f7;border:1px solid #d1d5db"><a href="#" style="display:inline-block;padding:10px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;color:#1f2937;text-decoration:none">&#128272; Acceso VPN</a></td></tr></table>'
                             '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="display:inline-block;margin:4px 4px"><tr><td bgcolor="#eef2f7" align="center" style="background-color:#eef2f7;border:1px solid #d1d5db"><a href="#" style="display:inline-block;padding:10px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;color:#1f2937;text-decoration:none">&#128196; Documentación</a></td></tr></table>'),
    }


@router.get("/settings/alerts", response_class=HTMLResponse)
@require_viewer
async def alerts_settings_get(request: Request):
    from scheduler import DEFAULT_SUBJECT, DEFAULT_BODY, EMAIL_VARS
    db = request.state.db
    cfg = {k: get_setting(k) for k in ALERT_KEYS}
    if not cfg.get("email_subject"):
        cfg["email_subject"] = DEFAULT_SUBJECT
    if not cfg.get("email_body"):
        cfg["email_body"] = DEFAULT_BODY
    cfg.setdefault("alert_start_days", "7")
    cfg.setdefault("alert_interval_hours", "24")
    cfg.setdefault("alert_escalation_enabled", "off")
    cfg.setdefault("alert_escalation_count", "3")

    example, preview_vars = _pick_alert_example(db)

    # Admins disponibles como destinatarios del escalado + selección actual
    from models import User
    admins = db.query(User).filter(User.role == "admin").order_by(User.username).all()
    selected_admin_ids = {
        int(x) for x in (cfg.get("alert_escalation_admin_ids") or "").replace(";", ",").split(",")
        if x.strip().isdigit()
    }

    return templates.TemplateResponse("admin_alerts.html", {
        "request": request,
        "user": request.state.current_user,
        "cfg": cfg,
        "email_vars": EMAIL_VARS,
        "preview_vars": preview_vars,
        "example_label": (f"{example.technician.username} → {example.company.name}"
                          if example else None),
        "admins": admins,
        "selected_admin_ids": selected_admin_ids,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/settings/alerts")
@require_admin
async def alerts_settings_post(
    request: Request,
    alert_start_days: str = Form("7"),
    alert_interval_hours: str = Form("24"),
    email_subject: str = Form(""),
    email_body: str = Form(""),
    alert_escalation_enabled: str = Form("off"),
    alert_escalation_count: str = Form("3"),
    alert_escalation_admin_ids: list[str] = Form(default=[]),
    alert_escalation_extra_emails: str = Form(""),
):
    db = request.state.db
    set_setting("alert_start_days", alert_start_days)
    set_setting("alert_interval_hours", alert_interval_hours)
    set_setting("email_subject", email_subject)
    set_setting("email_body", email_body)
    set_setting("alert_escalation_enabled", "on" if alert_escalation_enabled == "on" else "off")
    set_setting("alert_escalation_count", alert_escalation_count)
    # Destinatarios del escalado: IDs de admins marcados + emails externos
    clean_ids = ",".join(x for x in alert_escalation_admin_ids if x.strip().isdigit())
    clean_emails = ",".join(
        e.strip() for e in alert_escalation_extra_emails.replace(";", ",").replace("\n", ",").split(",")
        if e.strip() and "@" in e
    )
    set_setting("alert_escalation_admin_ids", clean_ids)
    set_setting("alert_escalation_extra_emails", clean_emails)
    audit_mod.log(db, "Configuración de alertas a técnicos actualizada", user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/alerts?success=1", status_code=302)


@router.post("/settings/alerts/restore-default")
@require_admin
async def alerts_restore_default(request: Request):
    """Restaura el asunto y el cuerpo de la plantilla de alerta a los valores
    por defecto (el diseño nuevo). El admin puede seguir editándolo después."""
    from scheduler import DEFAULT_SUBJECT, DEFAULT_BODY
    set_setting("email_subject", DEFAULT_SUBJECT)
    set_setting("email_body", DEFAULT_BODY)
    audit_mod.log(request.state.db, "Plantilla de alerta restaurada a la de por defecto",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/alerts?success=restored", status_code=302)


@router.post("/settings/alerts/test")
@require_admin
async def alerts_test_send(
    request: Request,
    email_subject: str = Form(None),
    email_body: str = Form(None),
):
    """Envía la plantilla de alerta a técnico (con un caso REAL) al email del admin.
    Usa la plantilla ENVIADA desde el editor (incluye ediciones sin guardar);
    si no llega, cae a la guardada."""
    from scheduler import alert_mapping, _send_email, DEFAULT_SUBJECT, DEFAULT_BODY
    db = request.state.db
    me = request.state.current_user
    if not me.email:
        return RedirectResponse(
            "/admin/settings/alerts?error=Tu+usuario+no+tiene+email+configurado",
            status_code=302,
        )
    example, _ = _pick_alert_example(db)
    if not example:
        return RedirectResponse(
            "/admin/settings/alerts?error=No+hay+técnicos+con+empresas+para+generar+el+ejemplo",
            status_code=302,
        )
    subj = email_subject if email_subject is not None else (get_setting("email_subject") or DEFAULT_SUBJECT)
    body = email_body if email_body is not None else (get_setting("email_body") or DEFAULT_BODY)
    for k, v in alert_mapping(example.technician.username, example.company, example).items():
        subj = subj.replace(k, v)
        body = body.replace(k, v)
    ok = _send_email(me.email, "[PRUEBA] " + subj, body, kind="test")
    if ok:
        audit_mod.log(db, f"Alerta de prueba enviada a {me.email}", user_id=me.id)
        return RedirectResponse("/admin/settings/alerts?success=test", status_code=302)
    return RedirectResponse(
        "/admin/settings/alerts?error=No+se+pudo+enviar+(revisa+config+SMTP)",
        status_code=302,
    )


# ── Reportes a administradores ───────────────────────────────────────────────

@router.get("/settings/reports-config", response_class=HTMLResponse)
@require_viewer
async def reports_config_get(request: Request):
    from scheduler import DEFAULT_REPORT_SUBJECT, DEFAULT_REPORT_BODY, REPORT_VARS, report_mapping
    db = request.state.db
    cfg = {k: get_setting(k) for k in REPORT_KEYS}
    if not cfg.get("report_subject"):
        cfg["report_subject"] = DEFAULT_REPORT_SUBJECT
    if not cfg.get("report_body"):
        cfg["report_body"] = DEFAULT_REPORT_BODY
    cfg.setdefault("report_day", "mon")
    cfg.setdefault("report_hour", "9")

    # Datos REALES para la vista previa (los mismos que se enviarían)
    companies   = db.query(Company).all()
    technicians = db.query(User).filter(User.role == "technician", User.is_active == True).all()
    preview_vars = report_mapping(companies, technicians)

    return templates.TemplateResponse("admin_reports_config.html", {
        "request": request,
        "user": request.state.current_user,
        "cfg": cfg,
        "report_vars": REPORT_VARS,
        "preview_vars": preview_vars,
        "has_data": len(companies) > 0,
        "success": request.query_params.get("success"),
    })


@router.post("/settings/reports-config")
@require_admin
async def reports_config_post(
    request: Request,
    report_subject: str = Form(""),
    report_body: str = Form(""),
    report_day: str = Form("mon"),
    report_hour: str = Form("9"),
):
    db = request.state.db
    set_setting("report_subject", report_subject)
    set_setting("report_body", report_body)
    set_setting("report_day", report_day)
    set_setting("report_hour", report_hour)
    audit_mod.log(db, f"Configuración de reportes a admins actualizada (día={report_day} hora={report_hour}h)",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/reports-config?success=1", status_code=302)


@router.post("/settings/reports-config/restore-default")
@require_admin
async def reports_restore_default(request: Request):
    """Restaura asunto y cuerpo del reporte a los valores por defecto (diseño nuevo)."""
    from scheduler import DEFAULT_REPORT_SUBJECT, DEFAULT_REPORT_BODY
    set_setting("report_subject", DEFAULT_REPORT_SUBJECT)
    set_setting("report_body", DEFAULT_REPORT_BODY)
    audit_mod.log(request.state.db, "Plantilla de reporte restaurada a la de por defecto",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/reports-config?success=restored", status_code=302)


# ── Bienvenida — alta de técnicos ────────────────────────────────────────────

@router.get("/settings/welcome", response_class=HTMLResponse)
@require_viewer
async def welcome_settings_get(request: Request):
    from scheduler import DEFAULT_WELCOME_SUBJECT, DEFAULT_WELCOME_BODY, WELCOME_VARS, welcome_mapping
    cfg = {k: get_setting(k) for k in WELCOME_KEYS}
    if not cfg.get("welcome_subject"):
        cfg["welcome_subject"] = DEFAULT_WELCOME_SUBJECT
    if not cfg.get("welcome_body"):
        cfg["welcome_body"] = DEFAULT_WELCOME_BODY
    cfg.setdefault("welcome_default_on", "off")
    preview_vars = welcome_mapping("tecnico_ejemplo", "Temp-1234 (ejemplo)", "tecnico@empresa.com")
    return templates.TemplateResponse("admin_welcome.html", {
        "request": request,
        "user": request.state.current_user,
        "cfg": cfg,
        "welcome_vars": WELCOME_VARS,
        "preview_vars": preview_vars,
        "base_url_set": bool(get_setting("app_base_url")),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/settings/welcome")
@require_admin
async def welcome_settings_post(
    request: Request,
    welcome_subject: str = Form(""),
    welcome_body: str = Form(""),
    welcome_default_on: str = Form("off"),
):
    set_setting("welcome_subject", welcome_subject)
    set_setting("welcome_body", welcome_body)
    set_setting("welcome_default_on", "on" if welcome_default_on == "on" else "off")
    audit_mod.log(request.state.db, "Plantilla de email de bienvenida actualizada",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/welcome?success=1", status_code=302)


@router.post("/settings/welcome/restore-default")
@require_admin
async def welcome_restore_default(request: Request):
    from scheduler import DEFAULT_WELCOME_SUBJECT, DEFAULT_WELCOME_BODY
    set_setting("welcome_subject", DEFAULT_WELCOME_SUBJECT)
    set_setting("welcome_body", DEFAULT_WELCOME_BODY)
    audit_mod.log(request.state.db, "Plantilla de bienvenida restaurada a la de por defecto",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/welcome?success=restored", status_code=302)


@router.post("/settings/welcome/test")
@require_admin
async def welcome_test_send(
    request: Request,
    welcome_subject: str = Form(None),
    welcome_body: str = Form(None),
):
    """Envía la plantilla de bienvenida (con datos de ejemplo) al email del admin."""
    from scheduler import welcome_mapping, _send_email, DEFAULT_WELCOME_SUBJECT, DEFAULT_WELCOME_BODY
    me = request.state.current_user
    if not me.email:
        return RedirectResponse("/admin/settings/welcome?error=no_email", status_code=302)
    subj = welcome_subject if welcome_subject is not None else (get_setting("welcome_subject") or DEFAULT_WELCOME_SUBJECT)
    body = welcome_body if welcome_body is not None else (get_setting("welcome_body") or DEFAULT_WELCOME_BODY)
    for k, v in welcome_mapping(me.username, "Temp-1234 (ejemplo)", me.email).items():
        subj = subj.replace(k, v)
        body = body.replace(k, v)
    ok = _send_email(me.email, "[PRUEBA] " + subj, body, kind="welcome")
    return RedirectResponse(
        "/admin/settings/welcome?success=test" if ok else "/admin/settings/welcome?error=test_fail",
        status_code=302)


@router.get("/settings/general", response_class=HTMLResponse)
@require_viewer
async def general_settings_get(request: Request):
    cfg = {k: get_setting(k) for k in GEN_KEYS}
    cfg.setdefault("timezone", "Europe/Madrid")
    if not cfg.get("confirm_token_hours"):
        cfg["confirm_token_hours"] = "3"
    return templates.TemplateResponse("admin_settings_general.html", {
        "request": request,
        "user": request.state.current_user,
        "cfg": cfg,
        "success": request.query_params.get("success"),
    })


@router.post("/settings/general")
@require_admin
async def general_settings_post(
    request: Request,
    timezone: str = Form("Europe/Madrid"),
    app_base_url: str = Form(""),
    confirm_token_hours: str = Form("3"),
):
    db = request.state.db
    set_setting("timezone", timezone or "Europe/Madrid")
    set_setting("app_base_url", app_base_url.strip().rstrip("/"))
    try:
        set_setting("confirm_token_hours", str(max(1, int(confirm_token_hours))))
    except (ValueError, TypeError):
        set_setting("confirm_token_hours", "3")
    audit_mod.log(db, "Configuración general actualizada", user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/general?success=1", status_code=302)


# ── Base de datos — monitoreo y mantenimiento ────────────────────────────────

@router.get("/settings/database", response_class=HTMLResponse)
@require_viewer
async def database_settings_get(request: Request):
    import db_admin, remote_backup
    status = db_admin.full_status()
    rcfg = {k: (get_setting(k) or v) for k, v in remote_backup.REMOTE_DEFAULTS.items()}
    rcfg["has_password"] = bool(get_setting("remote_backup_pass"))
    rcfg["last_status"] = get_setting("remote_backup_last_status")
    rcfg["last_detail"] = get_setting("remote_backup_last_detail")
    rcfg["test_status"] = get_setting("remote_backup_test_status")
    rcfg["test_detail"] = get_setting("remote_backup_test_detail")
    from datetime import datetime as _dt
    for raw_key, dst_key in (("remote_backup_last_at", "last_at"),
                             ("remote_backup_test_at", "test_at")):
        raw = get_setting(raw_key)
        if raw:
            try:
                rcfg[dst_key] = _dt.fromisoformat(raw).strftime("%d/%m/%Y %H:%M")
            except Exception:
                rcfg[dst_key] = raw
    return templates.TemplateResponse("admin_database.html", {
        "request": request,
        "user": request.state.current_user,
        "st": status,
        "remote": rcfg,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
        "info": request.query_params.get("info"),
    })


@router.post("/settings/database")
@require_admin
async def database_settings_post(
    request: Request,
    db_max_size_mb: str = Form("500"),
    db_alert_threshold_pct: str = Form("80"),
    db_alert_enabled: str = Form("off"),
    db_purge_audit_days: str = Form("90"),
    db_auto_backup: str = Form("off"),
    db_backup_keep: str = Form("7"),
    rotation_purge_enabled: str = Form("off"),
    rotation_purge_days: str = Form("365"),
):
    db = request.state.db
    set_setting("db_max_size_mb", db_max_size_mb)
    set_setting("db_alert_threshold_pct", db_alert_threshold_pct)
    set_setting("db_alert_enabled", "on" if db_alert_enabled == "on" else "off")
    set_setting("db_purge_audit_days", db_purge_audit_days)
    set_setting("db_auto_backup", "on" if db_auto_backup == "on" else "off")
    set_setting("db_backup_keep", db_backup_keep)
    set_setting("rotation_purge_enabled", "on" if rotation_purge_enabled == "on" else "off")
    try:
        set_setting("rotation_purge_days", str(max(1, int(rotation_purge_days))))
    except (ValueError, TypeError):
        set_setting("rotation_purge_days", "365")
    # Aplicar la política de retención de inmediato (coherencia al bajar el límite)
    import db_admin
    db_admin.prune_backups_now()
    audit_mod.log(db, "Configuración de base de datos actualizada", user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/database?success=1", status_code=302)


def _save_remote_settings(enabled, protocol, host, port, user, pwd, path, tls, keep):
    """Persiste la configuración de copia externa. La contraseña solo se re-cifra
    si llega una nueva (campo vacío = conservar la existente)."""
    set_setting("remote_backup_enabled", "on" if enabled == "on" else "off")
    set_setting("remote_backup_protocol", "ftp" if protocol == "ftp" else "sftp")
    set_setting("remote_backup_host", host.strip())
    set_setting("remote_backup_port", port.strip())
    set_setting("remote_backup_user", user.strip())
    if pwd:
        set_setting("remote_backup_pass", encrypt_secret(pwd))
    set_setting("remote_backup_path", path.strip())
    set_setting("remote_backup_tls", "on" if tls == "on" else "off")
    try:
        keep_n = max(1, int(keep))
    except (ValueError, TypeError):
        keep_n = 14
    set_setting("remote_backup_keep", str(keep_n))


@router.post("/settings/database/remote")
@require_admin
async def database_remote_save(
    request: Request,
    remote_backup_enabled: str = Form("off"),
    remote_backup_protocol: str = Form("sftp"),
    remote_backup_host: str = Form(""),
    remote_backup_port: str = Form(""),
    remote_backup_user: str = Form(""),
    remote_backup_pass: str = Form(""),
    remote_backup_path: str = Form(""),
    remote_backup_tls: str = Form("off"),
    remote_backup_keep: str = Form("14"),
):
    _save_remote_settings(remote_backup_enabled, remote_backup_protocol, remote_backup_host,
                          remote_backup_port, remote_backup_user, remote_backup_pass,
                          remote_backup_path, remote_backup_tls, remote_backup_keep)
    audit_mod.log(request.state.db, "Configuración de copia externa (SFTP/FTP) actualizada",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/settings/database?success=remote", status_code=302)


@router.post("/settings/database/remote-test")
@require_admin
async def database_remote_test(
    request: Request,
    remote_backup_enabled: str = Form("off"),
    remote_backup_protocol: str = Form("sftp"),
    remote_backup_host: str = Form(""),
    remote_backup_port: str = Form(""),
    remote_backup_user: str = Form(""),
    remote_backup_pass: str = Form(""),
    remote_backup_path: str = Form(""),
    remote_backup_tls: str = Form("off"),
    remote_backup_keep: str = Form("14"),
):
    """Guarda la configuración del formulario y luego prueba la conexión."""
    import remote_backup
    _save_remote_settings(remote_backup_enabled, remote_backup_protocol, remote_backup_host,
                          remote_backup_port, remote_backup_user, remote_backup_pass,
                          remote_backup_path, remote_backup_tls, remote_backup_keep)
    ok, _msg = remote_backup.test_connection()
    flag = "success=remote_test_ok" if ok else "error=remote_test_fail"
    return RedirectResponse(f"/admin/settings/database?{flag}", status_code=302)


@router.get("/settings/database/metrics.json")
@require_viewer
async def database_metrics_json(request: Request):
    """Datos de monitoreo serializados para el auto-refresco del panel."""
    import db_admin
    return JSONResponse(db_admin.status_json())


@router.post("/settings/database/restore")
@require_admin
async def database_restore(request: Request, filename: str = Form(...)):
    import db_admin
    res = db_admin.restore_backup(filename)
    if res["ok"]:
        _log_after_restore(
            request,
            f"BD restaurada desde backup {res.get('restored_from', filename)} "
            f"(seguridad: {res.get('safety_backup')})",
        )
        return RedirectResponse(
            f"/admin/settings/database?info=Base+de+datos+restaurada+desde+{res.get('restored_from', filename)}",
            status_code=302,
        )
    return RedirectResponse(f"/admin/settings/database?error={res['error'][:120]}", status_code=302)


@router.post("/settings/database/import")
@require_admin
async def database_import(request: Request, db_file: UploadFile = File(...)):
    import db_admin
    # Guardar el fichero subido en un temporal para validarlo antes de tocar la BD
    tmp_path = _os.path.join("data", f"_import_{_secrets.token_hex(8)}.db")
    try:
        content = await db_file.read()
        if not content:
            return RedirectResponse("/admin/settings/database?error=El+fichero+está+vacío", status_code=302)
        with open(tmp_path, "wb") as f:
            f.write(content)
        res = db_admin.import_db_file(tmp_path)
    finally:
        try:
            _os.remove(tmp_path)
        except Exception:
            pass

    if res["ok"]:
        _log_after_restore(
            request,
            f"BD importada desde fichero externo '{db_file.filename}' "
            f"(seguridad: {res.get('safety_backup')})",
        )
        return RedirectResponse(
            "/admin/settings/database?info=Base+de+datos+importada+correctamente",
            status_code=302,
        )
    return RedirectResponse(f"/admin/settings/database?error={res['error'][:120]}", status_code=302)


def _log_after_restore(request: Request, message: str):
    """
    Registra auditoría tras una restauración usando una sesión NUEVA.
    La sesión del decorador puede haber quedado obsoleta tras engine.dispose(),
    y queremos que el log quede en la BD ya restaurada.
    """
    try:
        ndb = SessionLocal()
        try:
            uid = getattr(request.state.current_user, "id", None)
            # El usuario podría no existir en la BD restaurada → log sin user_id si falla
            try:
                audit_mod.log(ndb, message, user_id=uid, level="warning")
            except Exception:
                ndb.rollback()
                audit_mod.log(ndb, message, user_id=None, level="warning")
        finally:
            ndb.close()
    except Exception:
        pass


@router.post("/settings/database/vacuum")
@require_admin
async def database_vacuum(request: Request):
    import db_admin
    try:
        res = db_admin.run_vacuum()
        audit_mod.log(
            request.state.db,
            f"VACUUM ejecutado — recuperados {res['reclaimed_human']}",
            user_id=request.state.current_user.id,
        )
        return RedirectResponse(
            f"/admin/settings/database?info=Espacio+recuperado:+{res['reclaimed_human'].replace(' ', '+')}",
            status_code=302,
        )
    except Exception as e:
        return RedirectResponse(f"/admin/settings/database?error={str(e)[:120]}", status_code=302)


@router.post("/settings/database/backup")
@require_admin
async def database_backup(request: Request):
    import db_admin
    try:
        res = db_admin.create_backup()
        audit_mod.log(
            request.state.db,
            f"Backup de BD creado — {res['filename']} ({res['size_human']})",
            user_id=request.state.current_user.id,
        )
        return RedirectResponse(
            f"/admin/settings/database?info=Backup+creado:+{res['filename']}",
            status_code=302,
        )
    except Exception as e:
        return RedirectResponse(f"/admin/settings/database?error={str(e)[:120]}", status_code=302)


@router.get("/settings/database/backup/download/{filename}")
@require_admin
async def database_backup_download(request: Request, filename: str):
    """Descarga un backup .db al equipo del admin. Solo administradores."""
    import db_admin
    path = db_admin.backup_file_path(filename)
    if not path:
        return RedirectResponse("/admin/settings/database?error=Backup+no+encontrado", status_code=302)
    fname = _os.path.basename(path)
    audit_mod.log(request.state.db, f"Backup descargado: {fname}",
                  user_id=request.state.current_user.id, level="warning")
    return FileResponse(path, filename=fname, media_type="application/octet-stream")


@router.post("/settings/database/purge")
@require_admin
async def database_purge(request: Request, purge_days: str = Form("90")):
    import db_admin
    try:
        days = int(purge_days or "90")
    except ValueError:
        days = 90
    res = db_admin.purge_audit_logs(days=days)
    if res["ok"]:
        audit_mod.log(
            request.state.db,
            f"Purga de auditoría — {res['deleted']} registros (>{days} días)",
            user_id=request.state.current_user.id,
            level="warning",
        )
        return RedirectResponse(
            f"/admin/settings/database?info={res['deleted']}+registros+purgados",
            status_code=302,
        )
    return RedirectResponse(f"/admin/settings/database?error={res.get('error', 'purga')[:120]}", status_code=302)


# ── Exportación CSV ──────────────────────────────────────────────────────────

def _delimited_response(rows: list, header: list, filename: str,
                        delimiter: str = ";", media_type: str = "text/csv") -> Response:
    buf = io.StringIO()
    buf.write("﻿")  # BOM para que Excel reconozca UTF-8 y los acentos
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_response(rows: list, header: list, filename: str) -> Response:
    """CSV para informes (delimitado por ';', ideal para abrir en Excel)."""
    return _delimited_response(rows, header, filename, delimiter=";")


def _import_csv_response(rows: list, header: list, filename: str) -> Response:
    """CSV pensado para volver a importar: delimitado por ',' como espera el
    importador masivo (csv.DictReader usa coma por defecto)."""
    return _delimited_response(rows, header, filename, delimiter=",")


@router.get("/export/companies.csv")
@require_admin
async def export_companies_csv(request: Request):
    db = request.state.db
    rows = []
    for c in db.query(Company).order_by(Company.name).all():
        rows.append([
            c.id, c.name, c.vpn_url or "", c.doc_url or "", c.expiry_days,
            c.last_changed.strftime("%d/%m/%Y") if c.last_changed else "",
            c.status, c.days_remaining, len(c.technicians),
        ])
    audit_mod.log(db, "Exportación CSV de empresas", user_id=request.state.current_user.id)
    return _csv_response(
        rows,
        ["ID", "Nombre", "VPN", "Documentación", "Días expiración",
         "Último cambio", "Estado", "Días restantes", "Nº técnicos"],
        f"empresas_{date.today().isoformat()}.csv",
    )


@router.get("/export/technicians.csv")
@require_admin
async def export_technicians_csv(request: Request):
    db = request.state.db
    rows = []
    techs = db.query(User).filter(User.role == "technician").order_by(User.username).all()
    for t in techs:
        n_crit = sum(1 for tc in t.tc_assocs if tc.status == "critical")
        n_warn = sum(1 for tc in t.tc_assocs if tc.status == "warning")
        rows.append([
            t.id, t.username, t.email or "", "Sí" if t.is_active else "No",
            len(t.tc_assocs), n_crit, n_warn,
            t.created_at.strftime("%d/%m/%Y") if t.created_at else "",
        ])
    audit_mod.log(db, "Exportación CSV de técnicos", user_id=request.state.current_user.id)
    return _csv_response(
        rows,
        ["ID", "Usuario", "Email", "Activo", "Nº empresas", "Críticas", "En aviso", "Creado"],
        f"tecnicos_{date.today().isoformat()}.csv",
    )


@router.get("/export/audit.csv")
@require_admin
async def export_audit_csv(request: Request):
    db = request.state.db
    rows = []
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(5000).all()
    for l in logs:
        rows.append([
            l.timestamp.strftime("%d/%m/%Y %H:%M:%S") if l.timestamp else "",
            l.level, l.action, l.user.username if l.user else "", l.ip_address or "",
        ])
    audit_mod.log(db, "Exportación CSV de auditoría", user_id=request.state.current_user.id)
    return _csv_response(
        rows,
        ["Fecha", "Nivel", "Acción", "Usuario", "IP"],
        f"auditoria_{date.today().isoformat()}.csv",
    )


# ── Exportación para migración (round-trip con importación masiva) ───────────

@router.get("/export/companies-import.csv")
@require_admin
async def export_companies_import(request: Request):
    """Exporta empresas en el MISMO formato que acepta la importación masiva,
    para migrar o reconstruir rápidamente la configuración."""
    db = request.state.db
    rows = []
    for c in db.query(Company).order_by(Company.name).all():
        rows.append([
            c.name,
            c.vpn_url or "",
            c.doc_url or "",
            c.expiry_days,
            (c.notes or "").replace("\r", " ").replace("\n", " "),
            c.last_changed.isoformat() if c.last_changed else "",
        ])
    audit_mod.log(db, f"Exportación para migración: {len(rows)} empresas", user_id=request.state.current_user.id)
    return _import_csv_response(
        rows,
        ["name", "vpn_url", "doc_url", "expiry_days", "notes", "last_changed"],
        f"empresas_import_{date.today().isoformat()}.csv",
    )


@router.get("/export/technicians-import.csv")
@require_admin
async def export_technicians_import(request: Request):
    """Exporta técnicos en el formato de importación masiva. La contraseña va
    VACÍA (no es recuperable): al reimportar se generará una temporal y se
    forzará el cambio en el primer inicio de sesión."""
    db = request.state.db
    rows = []
    techs = db.query(User).filter(User.role == "technician").order_by(User.username).all()
    for t in techs:
        rows.append([
            t.username,
            t.email or "",
            "",  # password vacío → el importador genera una temporal
            "si" if t.must_change_password else "no",
        ])
    audit_mod.log(db, f"Exportación para migración: {len(rows)} técnicos", user_id=request.state.current_user.id)
    return _import_csv_response(
        rows,
        ["username", "email", "password", "must_change_password"],
        f"tecnicos_import_{date.today().isoformat()}.csv",
    )


@router.get("/export/assignments.csv")
@require_admin
async def export_assignments(request: Request):
    """Exporta las asignaciones técnico↔empresa (con su fecha de último cambio)
    en el formato que acepta la importación masiva de asignaciones."""
    db = request.state.db
    from models import TechnicianCompany
    rows = []
    for tc in db.query(TechnicianCompany).all():
        tech = tc.technician
        comp = tc.company
        if not tech or not comp:
            continue
        rows.append([
            tech.username,
            comp.name,
            tc.last_changed.isoformat() if tc.last_changed else "",
        ])
    rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    audit_mod.log(db, f"Exportación para migración: {len(rows)} asignaciones", user_id=request.state.current_user.id)
    return _import_csv_response(
        rows,
        ["tecnico", "empresa", "last_changed"],
        f"asignaciones_{date.today().isoformat()}.csv",
    )


# ── Vista matriz técnicos × empresas ─────────────────────────────────────────

@router.get("/matrix", response_class=HTMLResponse)
@require_viewer
async def access_matrix(request: Request):
    db = request.state.db
    from models import TechnicianCompany
    technicians = db.query(User).filter(_assignable_tech_filter()).order_by(User.username).all()
    companies   = db.query(Company).order_by(Company.name).all()
    assoc = {(tc.technician_id, tc.company_id): tc.status
             for tc in db.query(TechnicianCompany).all()}
    return templates.TemplateResponse("admin_matrix.html", {
        "request": request,
        "user": request.state.current_user,
        "technicians": technicians,
        "companies": companies,
        "assoc": assoc,
    })


# ── Historial de rotaciones ──────────────────────────────────────────────────

def _parse_rotations_filters(params) -> dict:
    """Normaliza los filtros del historial desde los query params."""
    preset = (params.get("preset") or "").strip()
    desde_raw = (params.get("desde") or "").strip()
    hasta_raw = (params.get("hasta") or "").strip()
    desde = hasta = None
    if preset in ("7", "30", "90"):
        desde = date.today() - timedelta(days=int(preset))
        desde_raw = hasta_raw = ""  # el preset manda; no mostramos fechas manuales
    else:
        preset = preset if preset == "todo" else ""
        d, ok = _parse_date_flexible(desde_raw)
        if ok and d:
            desde = d
        h, ok2 = _parse_date_flexible(hasta_raw)
        if ok2 and h:
            hasta = h
    try:
        tech_id = int(params.get("tech_id")) if params.get("tech_id") else None
    except (ValueError, TypeError):
        tech_id = None
    try:
        company_id = int(params.get("company_id")) if params.get("company_id") else None
    except (ValueError, TypeError):
        company_id = None
    return {
        "preset": preset, "desde": desde, "hasta": hasta,
        "desde_raw": desde_raw, "hasta_raw": hasta_raw,
        "tech_id": tech_id, "company_id": company_id,
    }


def _rotations_query(db, f: dict):
    from models import RotationHistory
    q = db.query(RotationHistory)
    if f["desde"]:
        q = q.filter(RotationHistory.confirmed_at >= datetime.combine(f["desde"], datetime.min.time()))
    if f["hasta"]:
        q = q.filter(RotationHistory.confirmed_at < datetime.combine(f["hasta"] + timedelta(days=1), datetime.min.time()))
    if f["tech_id"]:
        q = q.filter(RotationHistory.technician_id == f["tech_id"])
    if f["company_id"]:
        q = q.filter(RotationHistory.company_id == f["company_id"])
    return q.order_by(RotationHistory.confirmed_at.desc())


def _rotations_export_qs(f: dict) -> str:
    """Reconstruye el query string de filtros activos para los enlaces de export."""
    p = {}
    if f["preset"]:
        p["preset"] = f["preset"]
    if f["desde_raw"]:
        p["desde"] = f["desde_raw"]
    if f["hasta_raw"]:
        p["hasta"] = f["hasta_raw"]
    if f["tech_id"]:
        p["tech_id"] = f["tech_id"]
    if f["company_id"]:
        p["company_id"] = f["company_id"]
    return urlencode(p)


@router.get("/rotations", response_class=HTMLResponse)
@require_viewer
async def rotations_history(request: Request):
    db = request.state.db
    from models import RotationHistory
    f = _parse_rotations_filters(request.query_params)
    q = _rotations_query(db, f)
    matched = q.count()
    rows = q.limit(500).all()
    late = sum(1 for r in rows if (r.days_late or 0) > 0)
    grand_total = db.query(RotationHistory).count()
    # Opciones de filtro: pares (id, nombre) presentes en el historial
    tech_opts = sorted(
        {(t[0], t[1]) for t in db.query(RotationHistory.technician_id, RotationHistory.technician_name)
         .filter(RotationHistory.technician_id.isnot(None)).distinct().all()},
        key=lambda x: (x[1] or "").lower())
    comp_opts = sorted(
        {(c[0], c[1]) for c in db.query(RotationHistory.company_id, RotationHistory.company_name)
         .filter(RotationHistory.company_id.isnot(None)).distinct().all()},
        key=lambda x: (x[1] or "").lower())
    return templates.TemplateResponse("admin_rotations.html", {
        "request": request,
        "user": request.state.current_user,
        "rows": rows,
        "total": grand_total,
        "matched": matched,
        "late": late,
        "filters": f,
        "tech_options": tech_opts,
        "comp_options": comp_opts,
        "export_qs": _rotations_export_qs(f),
    })


@router.get("/rotations/export")
@require_admin
async def rotations_export(request: Request):
    db = request.state.db
    fmt = (request.query_params.get("fmt") or "csv").lower()
    f = _parse_rotations_filters(request.query_params)
    rows = _rotations_query(db, f).limit(20000).all()
    data = [[
        r.confirmed_at.strftime("%d/%m/%Y %H:%M") if r.confirmed_at else "",
        r.technician_name or (r.technician.username if r.technician else ""),
        r.company_name or (r.company.name if r.company else ""),
        r.rotated_on.strftime("%d/%m/%Y") if r.rotated_on else "",
        r.days_late or 0,
        "A tiempo" if (r.days_late or 0) <= 0 else f"{r.days_late}d tarde",
    ] for r in rows]
    header = ["Fecha confirmación", "Técnico", "Empresa", "Rotada el", "Días de retraso", "Puntualidad"]
    audit_mod.log(db, f"Exportación de historial de rotaciones ({len(data)} filas)", user_id=request.state.current_user.id)
    if fmt == "txt":
        return _delimited_response(
            data, header, f"rotaciones_{date.today().isoformat()}.txt",
            delimiter="\t", media_type="text/plain")
    return _csv_response(data, header, f"rotaciones_{date.today().isoformat()}.csv")


# ── Reporte de prueba con datos reales ───────────────────────────────────────

@router.post("/settings/reports-config/test")
@require_admin
async def reports_test_send(
    request: Request,
    report_subject: str = Form(None),
    report_body: str = Form(None),
):
    """Envía el reporte (con datos REALES) al email del admin. Usa la plantilla
    ENVIADA desde el editor (incluye ediciones sin guardar); si no llega, la guardada."""
    from scheduler import report_mapping, _send_email, DEFAULT_REPORT_SUBJECT, DEFAULT_REPORT_BODY
    db = request.state.db
    me = request.state.current_user
    if not me.email:
        return RedirectResponse(
            "/admin/settings/reports-config?error=Tu+usuario+no+tiene+email+configurado",
            status_code=302,
        )
    companies   = db.query(Company).all()
    technicians = db.query(User).filter(User.role == "technician", User.is_active == True).all()
    subj = report_subject if report_subject is not None else (get_setting("report_subject") or DEFAULT_REPORT_SUBJECT)
    body = report_body if report_body is not None else (get_setting("report_body") or DEFAULT_REPORT_BODY)
    for k, v in report_mapping(companies, technicians).items():
        subj = subj.replace(k, v)
        body = body.replace(k, v)
    ok = _send_email(me.email, "[PRUEBA] " + subj, body, kind="test")
    if ok:
        audit_mod.log(db, f"Reporte de prueba enviado a {me.email}", user_id=me.id)
        return RedirectResponse("/admin/settings/reports-config?success=test", status_code=302)
    return RedirectResponse(
        "/admin/settings/reports-config?error=No+se+pudo+enviar+(revisa+config+SMTP)",
        status_code=302,
    )


# ── Centro de notificaciones ─────────────────────────────────────────────────

@router.get("/notifications.json")
@require_viewer
async def notifications_json(request: Request):
    db = request.state.db
    from models import NotificationRead
    me = request.state.current_user
    nr = db.query(NotificationRead).filter(NotificationRead.user_id == me.id).first()
    last_read = nr.last_read_at if nr else None

    # La campana solo muestra eventos relevantes (warning/error): críticos,
    # escalados, logins fallidos, cuentas bloqueadas… Lo informativo (logins
    # correctos, navegación, cambios rutinarios) queda solo en Auditoría.
    RELEVANT_LEVELS = ["warning", "error"]
    base_q = db.query(AuditLog).filter(AuditLog.level.in_(RELEVANT_LEVELS))

    recent = base_q.order_by(AuditLog.timestamp.desc()).limit(15).all()

    if last_read:
        unread = base_q.filter(AuditLog.timestamp > last_read).count()
    else:
        unread = base_q.count()

    def _ago(ts):
        if not ts:
            return ""
        delta = datetime.utcnow() - ts
        s = int(delta.total_seconds())
        if s < 60:   return "hace un momento"
        if s < 3600: return f"hace {s // 60} min"
        if s < 86400: return f"hace {s // 3600} h"
        return f"hace {s // 86400} d"

    items = [{
        "action": l.action,
        "level": l.level or "info",
        "ago": _ago(l.timestamp),
        "user": l.user.username if l.user else None,
    } for l in recent]

    return JSONResponse({"unread": min(unread, 99), "items": items})


@router.post("/notifications/read")
@require_viewer
async def notifications_mark_read(request: Request):
    db = request.state.db
    from models import NotificationRead
    me = request.state.current_user
    nr = db.query(NotificationRead).filter(NotificationRead.user_id == me.id).first()
    if nr:
        nr.last_read_at = datetime.utcnow()
    else:
        db.add(NotificationRead(user_id=me.id, last_read_at=datetime.utcnow()))
    db.commit()
    return JSONResponse({"ok": True})


# ── Registro de emails ───────────────────────────────────────────────────────

EMAIL_KIND_LABELS = {
    "alert": "Alerta técnico", "report": "Reporte", "escalation": "Escalado",
    "db_alert": "Alerta BD", "test": "Prueba", "welcome": "Bienvenida", "other": "Otro",
}


@router.get("/emails", response_class=HTMLResponse)
@require_viewer
async def emails_log(request: Request):
    from models import EmailLog
    db = request.state.db
    status = request.query_params.get("status", "")
    kind = request.query_params.get("kind", "")

    q = db.query(EmailLog)
    if status:
        q = q.filter(EmailLog.status == status)
    if kind:
        q = q.filter(EmailLog.kind == kind)
    rows = q.order_by(EmailLog.created_at.desc()).limit(300).all()

    today_start = datetime.combine(date.today(), datetime.min.time())
    sent_today = db.query(EmailLog).filter(
        EmailLog.status == "sent", EmailLog.created_at >= today_start).count()
    failed_total = db.query(EmailLog).filter(EmailLog.status == "failed").count()
    total = db.query(EmailLog).count()

    return templates.TemplateResponse("admin_emails.html", {
        "request": request,
        "user": request.state.current_user,
        "rows": rows,
        "kind_labels": EMAIL_KIND_LABELS,
        "f_status": status,
        "f_kind": kind,
        "sent_today": sent_today,
        "failed_total": failed_total,
        "total": total,
        "max_attempts": 5,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/emails/{email_id}/retry")
@require_admin
async def email_retry(request: Request, email_id: int):
    from scheduler import _smtp_send
    from models import EmailLog
    db = request.state.db
    row = db.query(EmailLog).filter(EmailLog.id == email_id).first()
    if not row:
        return RedirectResponse("/admin/emails?error=notfound", status_code=302)
    ok, err = _smtp_send(row.to_address, row.subject or "", row.body or "")
    row.attempts = (row.attempts or 0) + 1
    row.last_attempt_at = datetime.utcnow()
    if ok:
        row.status = "sent"; row.sent_at = datetime.utcnow(); row.body = None; row.error = None
    else:
        row.status = "failed"; row.error = err
    db.commit()
    return RedirectResponse(
        "/admin/emails?success=retry" if ok else "/admin/emails?error=retry_failed",
        status_code=302)


@router.post("/emails/retry-failed")
@require_admin
async def emails_retry_all(request: Request):
    from scheduler import job_retry_failed_emails
    job_retry_failed_emails()
    return RedirectResponse("/admin/emails?success=retry_all", status_code=302)


# ── Papelera (archivado lógico) ──────────────────────────────────────────────

@router.get("/trash", response_class=HTMLResponse)
@require_viewer
async def trash_index(request: Request):
    import trash as trash_svc
    db = request.state.db
    items = trash_svc.list_trash(db)
    auto_enabled = (get_setting("trash_auto_purge_enabled") or "off") == "on"
    purge_days = get_setting("trash_purge_days") or "30"
    return templates.TemplateResponse("admin_trash.html", {
        "request": request,
        "user": request.state.current_user,
        "items": items,
        "auto_enabled": auto_enabled,
        "purge_days": purge_days,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/trash/{item_id}/restore")
@require_admin
async def trash_restore(request: Request, item_id: int):
    import trash as trash_svc
    from models import TrashItem
    db = request.state.db
    item = db.query(TrashItem).filter(TrashItem.id == item_id).first()
    if not item:
        return RedirectResponse("/admin/trash?error=notfound", status_code=302)
    name, etype = item.name, item.entity_type
    res = trash_svc.restore_item(db, item)
    if res["ok"]:
        audit_mod.log(db, f"Restaurado de papelera: {name} ({etype})",
                      user_id=request.state.current_user.id, level="warning")
        extra = f"&skipped={res['skipped']}" if res.get("skipped") else ""
        return RedirectResponse(f"/admin/trash?success=restored{extra}", status_code=302)
    return RedirectResponse(
        f"/admin/trash?error={res['error'].replace(' ', '+')}", status_code=302)


@router.post("/trash/{item_id}/purge")
@require_admin
async def trash_purge(request: Request, item_id: int):
    import trash as trash_svc
    from models import TrashItem
    db = request.state.db
    item = db.query(TrashItem).filter(TrashItem.id == item_id).first()
    if not item:
        return RedirectResponse("/admin/trash?error=notfound", status_code=302)
    name, etype = item.name, item.entity_type
    trash_svc.purge_item(db, item)
    audit_mod.log(db, f"Eliminado definitivamente de papelera: {name} ({etype})",
                  user_id=request.state.current_user.id, level="warning")
    return RedirectResponse("/admin/trash?success=purged", status_code=302)


@router.post("/trash/config")
@require_admin
async def trash_config(request: Request,
                       trash_auto_purge_enabled: str = Form("off"),
                       trash_purge_days: str = Form("30")):
    set_setting("trash_auto_purge_enabled", "on" if trash_auto_purge_enabled == "on" else "off")
    try:
        days = max(1, int(trash_purge_days))
    except (ValueError, TypeError):
        days = 30
    set_setting("trash_purge_days", str(days))
    audit_mod.log(request.state.db, "Configuración de retención de papelera actualizada",
                  user_id=request.state.current_user.id)
    return RedirectResponse("/admin/trash?success=config", status_code=302)


# ── Audit Logs ───────────────────────────────────────────────────────────────

@router.get("/audit-logs", response_class=HTMLResponse)
@require_viewer
async def audit_logs(request: Request):
    db = request.state.db
    level_filter = request.query_params.get("level", "")
    search = request.query_params.get("q", "")
    page = int(request.query_params.get("page", 1))
    per_page = 50

    query = db.query(AuditLog)
    if level_filter:
        query = query.filter(AuditLog.level == level_filter)
    if search:
        query = query.filter(AuditLog.action.ilike(f"%{search}%"))

    total = query.count()
    logs = query.order_by(AuditLog.timestamp.desc()).offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse("admin_audit_logs.html", {
        "request": request,
        "user": request.state.current_user,
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "level_filter": level_filter,
        "search": search,
    })
