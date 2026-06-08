"""
Papelera de reciclaje (archivado lógico) para empresas y técnicos.

Al archivar, el registro se serializa a JSON (con sus relaciones) y se mueve a
la tabla `trash_items`, eliminándolo de su tabla real. Así el resto de la app
(dashboard, cumplimiento, alertas por email…) deja de verlo por completo, sin
riesgo de fugas. Restaurar reconstruye el registro y sus asignaciones.
"""
import json
import logging
from datetime import date, datetime, timedelta

from models import Company, User, TechnicianCompany, TrashItem

logger = logging.getLogger("accesos")


# ── Serialización de fechas ──────────────────────────────────────────────────
def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else None


def _date(v):
    return date.fromisoformat(v) if v else None


def _dt(v):
    return datetime.fromisoformat(v) if v else None


# ── Snapshots ────────────────────────────────────────────────────────────────
def _company_payload(c: Company) -> dict:
    return {
        "company": {
            "name": c.name, "vpn_url": c.vpn_url, "doc_url": c.doc_url,
            "expiry_days": c.expiry_days, "last_changed": _iso(c.last_changed),
            "notes": c.notes, "alert_last_sent": _iso(c.alert_last_sent),
            "created_at": _iso(c.created_at),
        },
        "assignments": [
            {"technician_id": tc.technician_id, "last_changed": _iso(tc.last_changed),
             "alert_last_sent": _iso(tc.alert_last_sent), "alert_count": tc.alert_count,
             "escalated_at": _iso(tc.escalated_at)}
            for tc in c.tc_assocs
        ],
    }


def _technician_payload(u: User) -> dict:
    return {
        "user": {
            "username": u.username, "email": u.email, "hashed_password": u.hashed_password,
            "role": u.role, "also_technician": u.also_technician, "is_active": u.is_active,
            "must_change_password": u.must_change_password, "receive_reports": u.receive_reports,
            "onboarding_admin_done": u.onboarding_admin_done,
            "onboarding_tech_done": u.onboarding_tech_done,
            "created_at": _iso(u.created_at), "tokens_valid_from": _iso(u.tokens_valid_from),
        },
        "assignments": [
            {"company_id": tc.company_id, "last_changed": _iso(tc.last_changed),
             "alert_last_sent": _iso(tc.alert_last_sent), "alert_count": tc.alert_count,
             "escalated_at": _iso(tc.escalated_at)}
            for tc in u.tc_assocs
        ],
    }


# ── Archivar ─────────────────────────────────────────────────────────────────
def archive_company(db, company: Company, by_username: str | None) -> TrashItem:
    payload = _company_payload(company)
    item = TrashItem(
        entity_type="company", original_id=company.id, name=company.name,
        detail=f"{len(payload['assignments'])} asignación(es)",
        payload=json.dumps(payload), archived_by=by_username,
    )
    db.add(item)
    db.delete(company)          # cascada borra sus TechnicianCompany
    db.commit()
    logger.info("Empresa archivada a papelera: %s (por %s)", company.name, by_username)
    return item


def archive_technician(db, user: User, by_username: str | None) -> TrashItem:
    payload = _technician_payload(user)
    item = TrashItem(
        entity_type="technician", original_id=user.id, name=user.username,
        detail=(user.email or "sin email"),
        payload=json.dumps(payload), archived_by=by_username,
    )
    db.add(item)
    db.delete(user)
    db.commit()
    logger.info("Técnico archivado a papelera: %s (por %s)", user.username, by_username)
    return item


# ── Restaurar ────────────────────────────────────────────────────────────────
def _id_free(db, model, original_id) -> bool:
    return original_id is not None and db.query(model).filter(model.id == original_id).first() is None


def restore_item(db, item: TrashItem) -> dict:
    """Reconstruye el registro archivado. Devuelve {ok, error}."""
    try:
        data = json.loads(item.payload)
        if item.entity_type == "company":
            return _restore_company(db, item, data)
        elif item.entity_type == "technician":
            return _restore_technician(db, item, data)
        return {"ok": False, "error": "Tipo desconocido"}
    except Exception as e:
        db.rollback()
        logger.error("Error al restaurar item %s: %s", item.id, e)
        return {"ok": False, "error": str(e)[:160]}


def _restore_company(db, item, data) -> dict:
    cd = data["company"]
    if db.query(Company).filter(Company.name == cd["name"]).first():
        return {"ok": False, "error": f"Ya existe una empresa llamada '{cd['name']}'."}
    c = Company(
        name=cd["name"], vpn_url=cd["vpn_url"], doc_url=cd["doc_url"],
        expiry_days=cd["expiry_days"], last_changed=_date(cd["last_changed"]) or date.today(),
        notes=cd["notes"], alert_last_sent=_dt(cd["alert_last_sent"]),
        created_at=_dt(cd["created_at"]) or datetime.utcnow(),
    )
    if _id_free(db, Company, item.original_id):
        c.id = item.original_id          # conserva el id → reconecta historial
    db.add(c)
    db.flush()
    skipped = 0
    for a in data.get("assignments", []):
        if db.query(User).filter(User.id == a["technician_id"]).first() is None:
            skipped += 1
            continue
        db.add(TechnicianCompany(
            technician_id=a["technician_id"], company_id=c.id,
            last_changed=_date(a["last_changed"]), alert_last_sent=_dt(a["alert_last_sent"]),
            alert_count=a.get("alert_count") or 0, escalated_at=_dt(a["escalated_at"]),
        ))
    db.delete(item)
    db.commit()
    return {"ok": True, "skipped": skipped, "name": c.name}


def _restore_technician(db, item, data) -> dict:
    ud = data["user"]
    if db.query(User).filter(User.username == ud["username"]).first():
        return {"ok": False, "error": f"Ya existe un usuario '{ud['username']}'."}
    if ud.get("email") and db.query(User).filter(User.email == ud["email"]).first():
        return {"ok": False, "error": f"El email '{ud['email']}' ya está en uso."}
    u = User(
        username=ud["username"], email=ud["email"], hashed_password=ud["hashed_password"],
        role=ud["role"], also_technician=ud.get("also_technician", False),
        is_active=ud.get("is_active", True), must_change_password=ud.get("must_change_password", False),
        receive_reports=ud.get("receive_reports", False),
        onboarding_admin_done=ud.get("onboarding_admin_done", False),
        onboarding_tech_done=ud.get("onboarding_tech_done", False),
        created_at=_dt(ud["created_at"]) or datetime.utcnow(),
        tokens_valid_from=_dt(ud.get("tokens_valid_from")),
    )
    if _id_free(db, User, item.original_id):
        u.id = item.original_id
    db.add(u)
    db.flush()
    skipped = 0
    for a in data.get("assignments", []):
        if db.query(Company).filter(Company.id == a["company_id"]).first() is None:
            skipped += 1
            continue
        db.add(TechnicianCompany(
            technician_id=u.id, company_id=a["company_id"],
            last_changed=_date(a["last_changed"]), alert_last_sent=_dt(a["alert_last_sent"]),
            alert_count=a.get("alert_count") or 0, escalated_at=_dt(a["escalated_at"]),
        ))
    db.delete(item)
    db.commit()
    return {"ok": True, "skipped": skipped, "name": u.username}


# ── Purga ────────────────────────────────────────────────────────────────────
def purge_item(db, item: TrashItem) -> None:
    db.delete(item)
    db.commit()


def list_trash(db) -> list:
    return db.query(TrashItem).order_by(TrashItem.archived_at.desc()).all()


def purge_old(db, days: int) -> int:
    """Borra definitivamente lo archivado hace más de `days` días. Devuelve nº."""
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    n = db.query(TrashItem).filter(TrashItem.archived_at < cutoff).delete(
        synchronize_session=False)
    db.commit()
    return n
