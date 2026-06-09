from datetime import date, datetime
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from database import Base
from thresholds import classify


class TechnicianCompany(Base):
    """Relación muchos-a-muchos con estado POR técnico.
    Cada técnico tiene su propio last_changed y alert_last_sent para cada empresa.
    """
    __tablename__ = "technician_company"

    technician_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    company_id    = Column(Integer, ForeignKey("companies.id"), primary_key=True)
    last_changed  = Column(Date, default=date.today, nullable=True)
    alert_last_sent = Column(DateTime, nullable=True)
    # ── Escalado automático ──────────────────────────────────────────────────
    alert_count   = Column(Integer, default=0)        # avisos enviados sin confirmar
    escalated_at  = Column(DateTime, nullable=True)   # cuándo se escaló (None = sin escalar)

    technician = relationship("User", back_populates="tc_assocs")
    company    = relationship("Company", back_populates="tc_assocs")

    # ── Estado por técnico ───────────────────────────────────────────────────
    @property
    def _effective_last_changed(self):
        """last_changed del técnico, o el global de la empresa como fallback."""
        return self.last_changed or self.company.last_changed or date.today()

    @property
    def days_remaining(self):
        elapsed = (date.today() - self._effective_last_changed).days
        return self.company.expiry_days - elapsed

    @property
    def status(self):
        return classify(self.days_remaining)

    @property
    def status_color(self):
        return {"ok": "success", "warning": "warning", "critical": "error"}[self.status]

    @property
    def progress_pct(self):
        if self.company.expiry_days == 0:
            return 0
        elapsed = (date.today() - self._effective_last_changed).days
        return max(0, min(100, int((1 - elapsed / self.company.expiry_days) * 100)))


class User(Base):
    __tablename__ = "users"

    id               = Column(Integer, primary_key=True, index=True)
    username         = Column(String(64), unique=True, nullable=False)
    email            = Column(String(128), unique=True, nullable=True)   # opcional para admins
    hashed_password  = Column(String(256), nullable=False)
    role             = Column(String(16), default="technician")  # admin | technician
    also_technician  = Column(Boolean, default=False)  # admin que TAMBIÉN es técnico (tiene empresas y confirma)
    is_active        = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    receive_reports  = Column(Boolean, default=False)  # admin recibe reporte periódico
    onboarding_admin_done = Column(Boolean, default=False)  # ya vio el tour de admin
    onboarding_tech_done  = Column(Boolean, default=False)  # ya vio el tour de técnico
    created_at       = Column(DateTime, default=datetime.utcnow)
    tokens_valid_from = Column(DateTime, nullable=True)  # sesiones emitidas antes = inválidas

    # ── Helpers de rol ───────────────────────────────────────────────────────
    @property
    def can_be_technician(self) -> bool:
        """True si el usuario puede tener empresas asignadas y confirmar rotaciones:
        un técnico puro, o un admin marcado como 'también técnico'."""
        return self.role == "technician" or bool(self.also_technician)

    @property
    def is_dual_role(self) -> bool:
        """Admin que además actúa como técnico."""
        return self.role == "admin" and bool(self.also_technician)

    # Relación directa a la tabla intermedia (para obtener last_changed por técnico)
    tc_assocs = relationship(
        "TechnicianCompany",
        back_populates="technician",
        cascade="all, delete-orphan",
    )
    # Relación de conveniencia: lista de Company (sin estado por técnico)
    companies = relationship(
        "Company",
        secondary="technician_company",
        back_populates="technicians",
        viewonly=True,
    )
    audit_logs = relationship("AuditLog", back_populates="user")


class Company(Base):
    __tablename__ = "companies"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(128), unique=True, nullable=False)
    vpn_url      = Column(String(512), nullable=True)
    doc_url      = Column(String(512), nullable=True)
    expiry_days  = Column(Integer, default=30)
    last_changed = Column(Date, default=date.today)          # global (para admin/reports)
    notes        = Column(Text, nullable=True)
    alert_last_sent = Column(DateTime, nullable=True)        # global (obsoleto — se usa el de TC)
    created_at   = Column(DateTime, default=datetime.utcnow)

    # Relación directa a la tabla intermedia
    tc_assocs = relationship(
        "TechnicianCompany",
        back_populates="company",
        cascade="all, delete-orphan",
    )
    # Relación de conveniencia: lista de User
    technicians = relationship(
        "User",
        secondary="technician_company",
        back_populates="companies",
        viewonly=True,
    )
    audit_logs = relationship("AuditLog", back_populates="company")

    # ── Estado global (peor estado entre todos los técnicos) ─────────────────
    @property
    def days_remaining(self):
        """Días según el last_changed global (para admin/reports)."""
        elapsed = (date.today() - self.last_changed).days
        return self.expiry_days - elapsed

    @property
    def status(self):
        """Peor estado entre todos los técnicos asignados."""
        if self.tc_assocs:
            statuses = [tc.status for tc in self.tc_assocs]
            if "critical" in statuses:
                return "critical"
            if "warning" in statuses:
                return "warning"
            return "ok"
        # Sin técnicos → usar last_changed global
        return classify(self.days_remaining)

    @property
    def status_color(self):
        return {"ok": "success", "warning": "warning", "critical": "error"}[self.status]

    @property
    def progress_pct(self):
        if self.expiry_days == 0:
            return 0
        elapsed = (date.today() - self.last_changed).days
        return max(0, min(100, int((1 - elapsed / self.expiry_days) * 100)))


class AppSettings(Base):
    """Configuración clave-valor de la aplicación (SMTP, etc.)."""
    __tablename__ = "app_settings"

    key   = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)


class RotationHistory(Base):
    """Registro inmutable de cada confirmación de rotación de contraseña.
    A diferencia de TechnicianCompany.last_changed (que se sobrescribe),
    aquí se conserva el historial completo para auditoría y analítica.
    """
    __tablename__ = "rotation_history"

    id            = Column(Integer, primary_key=True, index=True)
    technician_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    company_id    = Column(Integer, ForeignKey("companies.id"), nullable=True)
    technician_name = Column(String(64))   # desnormalizado: sobrevive a borrados
    company_name    = Column(String(128))  # desnormalizado: sobrevive a borrados
    rotated_on    = Column(Date, default=date.today)        # fecha que el técnico declara
    days_late     = Column(Integer, default=0)              # >0 si rotó tras expirar
    confirmed_at  = Column(DateTime, default=datetime.utcnow)  # cuándo lo registró

    technician = relationship("User")
    company    = relationship("Company")


class ComplianceSnapshot(Base):
    """Foto diaria de los KPIs de cumplimiento para gráficas de tendencia."""
    __tablename__ = "compliance_history"

    id                = Column(Integer, primary_key=True, index=True)
    snapshot_date     = Column(Date, default=date.today, unique=True, index=True)
    compliance_pct    = Column(Integer, default=100)
    total_companies   = Column(Integer, default=0)
    critical_companies = Column(Integer, default=0)
    warning_companies = Column(Integer, default=0)
    ok_companies      = Column(Integer, default=0)
    total_techs       = Column(Integer, default=0)
    techs_critical    = Column(Integer, default=0)
    created_at        = Column(DateTime, default=datetime.utcnow)


class NotificationRead(Base):
    """Marca de última lectura del centro de notificaciones por usuario."""
    __tablename__ = "notification_reads"

    user_id      = Column(Integer, ForeignKey("users.id"), primary_key=True)
    last_read_at = Column(DateTime, default=datetime.utcnow)


class TrashItem(Base):
    """Papelera de reciclaje: registros archivados (empresas/técnicos) movidos
    fuera de sus tablas reales. Guarda un snapshot JSON completo para poder
    restaurarlos. El resto de la app no ve estos elementos (están físicamente
    fuera de companies/users), evitando que sigan operando o enviando alertas."""
    __tablename__ = "trash_items"

    id          = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(24), index=True)   # company | technician
    original_id = Column(Integer)                   # id que tenía en su tabla
    name        = Column(String(256))               # nombre legible
    detail      = Column(String(256), nullable=True)  # info extra (email, nº asignaciones)
    payload     = Column(Text)                       # snapshot JSON (datos + relaciones)
    archived_at = Column(DateTime, default=datetime.utcnow, index=True)
    archived_by = Column(String(64), nullable=True)  # username que lo archivó


class EmailLog(Base):
    """Registro de cada email enviado por el sistema, con estado y reintentos.
    El cuerpo (body) se guarda solo mientras el envío no es definitivo (failed/
    pending) para poder reintentarlo; al enviarse OK se libera para no inflar la BD.
    """
    __tablename__ = "email_logs"

    id            = Column(Integer, primary_key=True, index=True)
    to_address    = Column(String(256), nullable=False)
    subject       = Column(String(512), nullable=True)
    kind          = Column(String(24), default="other")   # alert | report | escalation | db_alert | test | other
    status        = Column(String(16), default="pending", index=True)  # sent | failed | pending
    body          = Column(Text, nullable=True)            # se borra al enviarse OK
    error         = Column(Text, nullable=True)            # motivo del último fallo
    attempts      = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)
    last_attempt_at = Column(DateTime, nullable=True)
    sent_at       = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    action     = Column(String(512), nullable=False)
    level      = Column(String(16), default="info")   # info | warning | error
    ip_address = Column(String(64), nullable=True)
    timestamp  = Column(DateTime, default=datetime.utcnow)

    user    = relationship("User", back_populates="audit_logs")
    company = relationship("Company", back_populates="audit_logs")
