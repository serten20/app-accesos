"""Helper para registrar eventos de auditoría de forma consistente."""
from datetime import datetime
from sqlalchemy.orm import Session
from models import AuditLog


def log(
    db: Session,
    action: str,
    user_id: int | None = None,
    company_id: int | None = None,
    level: str = "info",
    ip: str | None = None,
):
    entry = AuditLog(
        user_id=user_id,
        company_id=company_id,
        action=action,
        level=level,
        ip_address=ip,
        timestamp=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
