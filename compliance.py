"""
Cálculo de cumplimiento de accesos.

Fórmula compuesta:
  - Unidades totales   = total_empresas + técnicos_activos_con_asignaciones
  - Unidades fallidas  = empresas_críticas + técnicos_con_alguna_empresa_crítica
  - Cumplimiento (%)   = (total - fallidas) / total × 100

Solo el estado CRÍTICO penaliza. ATENCIÓN (warning) = aún cumple.
"""
from datetime import date
from models import Company, User


def take_compliance_snapshot(db):
    """Calcula los KPIs actuales y los guarda como foto del día (upsert).
    Idempotente: si ya existe snapshot para hoy, lo actualiza.
    Devuelve el objeto ComplianceSnapshot.
    """
    from models import ComplianceSnapshot

    k = compute_compliance(db)
    today = date.today()
    snap = db.query(ComplianceSnapshot).filter(
        ComplianceSnapshot.snapshot_date == today
    ).first()
    if not snap:
        snap = ComplianceSnapshot(snapshot_date=today)
        db.add(snap)

    snap.compliance_pct     = k["compliance_pct"]
    snap.total_companies    = k["total_companies"]
    snap.critical_companies = k["critical_companies"]
    snap.warning_companies  = k["warning_companies"]
    snap.ok_companies       = k["ok_companies"]
    snap.total_techs        = k["total_techs"]
    snap.techs_critical     = k["techs_critical"]
    db.commit()
    return snap


def compute_compliance(db):
    """
    Devuelve un dict con todos los KPIs de cumplimiento.
    """
    companies = db.query(Company).all()
    technicians = db.query(User).filter(
        User.role == "technician",
        User.is_active == True,
    ).all()

    # ── Empresas ────────────────────────────────────────────────────────────
    total_companies = len(companies)
    critical_companies = [c for c in companies if c.status == "critical"]
    warning_companies  = [c for c in companies if c.status == "warning"]
    ok_companies       = [c for c in companies if c.status == "ok"]

    # ── Técnicos ────────────────────────────────────────────────────────────
    techs_with_assignments = [t for t in technicians if len(t.tc_assocs) > 0]
    total_techs = len(techs_with_assignments)

    # Estado basado en los TC del técnico (last_changed individual)
    techs_critical = [
        t for t in techs_with_assignments
        if any(tc.status == "critical" for tc in t.tc_assocs)
    ]
    techs_ok = [
        t for t in techs_with_assignments
        if not any(tc.status == "critical" for tc in t.tc_assocs)
    ]

    # ── Fórmula ─────────────────────────────────────────────────────────────
    total_units  = total_companies + total_techs
    failed_units = len(critical_companies) + len(techs_critical)

    if total_units > 0:
        compliance_pct = round((total_units - failed_units) / total_units * 100)
    else:
        compliance_pct = 100

    # Nivel semáforo del indicador
    if compliance_pct >= 90:
        compliance_color = "success"
        compliance_label = "Excelente"
    elif compliance_pct >= 70:
        compliance_color = "warning"
        compliance_label = "Aceptable"
    else:
        compliance_color = "error"
        compliance_label = "Crítico"

    return {
        # Global
        "compliance_pct":    compliance_pct,
        "compliance_color":  compliance_color,
        "compliance_label":  compliance_label,
        "total_units":       total_units,
        "failed_units":      failed_units,
        # Empresas
        "total_companies":   total_companies,
        "critical_companies": len(critical_companies),
        "warning_companies":  len(warning_companies),
        "ok_companies":       len(ok_companies),
        "companies":          companies,
        # Técnicos
        "total_techs":       total_techs,
        "techs_critical":    len(techs_critical),
        "techs_ok":          len(techs_ok),
        "technicians":       technicians,
    }
