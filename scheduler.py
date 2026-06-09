"""
Scheduler de alertas de expiración.

Lógica de envío:
  - días_restantes <= alert_start_days (ej. 7):  1 aviso inicial (una sola vez por ciclo)
  - días_restantes <= 2 o expirado (crítico):     repetir cada alert_interval_hours
    hasta que el técnico confirme el cambio (last_changed se actualiza).
"""
import os
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# ── Variables para email de alertas a técnicos ──────────────────────────────
EMAIL_VARS = {
    "{technician}": "Nombre del técnico",
    "{company}": "Nombre de la empresa",
    "{days_remaining}": "Días restantes",
    "{vpn_url}": "URL de acceso VPN",
    "{doc_url}": "URL de documentación",
    "{last_changed}": "Fecha del último cambio",
    "{expiry_date}": "Fecha de expiración estimada",
    "{confirm_url}": "Enlace de confirmación rápida (1 clic)",
    "{console_url}": "Enlace a la consola (login)",
    "{cta_button}": "Botón 'Confirmar rotación' (compatible Outlook)",
    "{access_buttons}": "Botones de VPN y documentación",
    "{urgency_color}": "Color según urgencia",
}

# ── Variables para email de reporte a administradores ────────────────────────
REPORT_VARS = {
    "{fecha}": "Fecha de generación del reporte",
    "{compliance_pct}": "Porcentaje de cumplimiento global",
    "{total_empresas}": "Número total de empresas",
    "{criticas}": "Empresas en estado crítico",
    "{en_aviso}": "Empresas en estado de atención",
    "{ok}": "Empresas en estado OK",
    "{tecnicos_ko}": "Técnicos con empresas críticas",
    "{tabla_empresas}": "Tabla HTML completa de todas las empresas",
}

# ── Variables para email de bienvenida (alta de técnico) ──────────────────────
WELCOME_VARS = {
    "{username}": "Nombre de usuario",
    "{password}": "Contraseña temporal",
    "{login_url}": "Enlace a la consola (login)",
    "{cta_button}": "Botón 'Acceder a la consola'",
    "{email}": "Email del técnico",
    "{app_name}": "Nombre de la aplicación",
}

DEFAULT_REPORT_SUBJECT = "📊 Reporte de Accesos — {fecha} — Cumplimiento {compliance_pct}%"
# Plantilla "a prueba de Outlook Desktop" (motor Word): tablas + bgcolor en todas
# las celdas, anchos por atributo, ghost table MSO. Tema claro.
DEFAULT_REPORT_BODY = """\
<!--[if mso]><table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f5f9" style="background-color:#f1f5f9">
<tr><td align="center" style="padding:24px 10px">
  <table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width:680px;max-width:680px;background-color:#ffffff;border:1px solid #e5e7eb">
    <tr><td bgcolor="#1e1b4b" style="background-color:#1e1b4b;padding:22px 30px;font-family:Arial,Helvetica,sans-serif">
      <span style="font-size:20px;font-weight:bold;color:#93c5fd;letter-spacing:2px">ACCESOS</span><br/>
      <span style="font-size:12px;color:#c7d2fe">Security Access Manager &middot; Reporte periódico — {fecha}</span>
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:24px 30px 6px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#374151;line-height:1.6">
      Resumen del estado de todas las empresas y técnicos a fecha de <b style="color:#111827">{fecha}</b>.
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:10px 30px 6px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="25%" bgcolor="#f9fafb" align="center" style="background-color:#f9fafb;border:1px solid #e5e7eb;padding:14px;font-family:Arial,Helvetica,sans-serif">
            <div style="font-size:24px;font-weight:bold;color:#16a34a">{ok}</div>
            <div style="font-size:10px;color:#6b7280;letter-spacing:1px;margin-top:4px">OK</div>
          </td>
          <td width="25%" bgcolor="#f9fafb" align="center" style="background-color:#f9fafb;border:1px solid #e5e7eb;padding:14px;font-family:Arial,Helvetica,sans-serif">
            <div style="font-size:24px;font-weight:bold;color:#d97706">{en_aviso}</div>
            <div style="font-size:10px;color:#6b7280;letter-spacing:1px;margin-top:4px">ATENCIÓN</div>
          </td>
          <td width="25%" bgcolor="#f9fafb" align="center" style="background-color:#f9fafb;border:1px solid #e5e7eb;padding:14px;font-family:Arial,Helvetica,sans-serif">
            <div style="font-size:24px;font-weight:bold;color:#dc2626">{criticas}</div>
            <div style="font-size:10px;color:#6b7280;letter-spacing:1px;margin-top:4px">CRÍTICAS</div>
          </td>
          <td width="25%" bgcolor="#eef2ff" align="center" style="background-color:#eef2ff;border:1px solid #c7d2fe;padding:14px;font-family:Arial,Helvetica,sans-serif">
            <div style="font-size:24px;font-weight:bold;color:#4338ca">{compliance_pct}%</div>
            <div style="font-size:10px;color:#6b7280;letter-spacing:1px;margin-top:4px">CUMPLIM.</div>
          </td>
        </tr>
      </table>
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:18px 30px 4px;font-family:Arial,Helvetica,sans-serif">
      <div style="font-size:11px;color:#6b7280;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px">Estado por empresa</div>
      {tabla_empresas}
    </td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:16px 30px 22px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6b7280">
      Técnicos con empresas críticas: <b style="color:#dc2626">{tecnicos_ko}</b> &middot; Total empresas: <b style="color:#111827">{total_empresas}</b>
    </td></tr>
    <tr><td bgcolor="#f1f5f9" align="center" style="background-color:#f1f5f9;padding:14px 30px;border-top:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#9ca3af">
      Security Access Manager &middot; Uso interno &middot; Generado automáticamente
    </td></tr>
  </table>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->"""

DEFAULT_SUBJECT = "⚠️ Rotación de contraseña pendiente — {company}"
# Plantilla "a prueba de Outlook Desktop" (motor Word): bgcolor en TODAS las
# celdas, anchos por atributo, ghost table MSO y botón VML. Tema claro.
DEFAULT_BODY = """\
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f5f9" style="background-color:#f1f5f9">
<tr><td align="center" style="padding:24px 10px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width:600px;max-width:600px;background-color:#ffffff;border:1px solid #e5e7eb">
    <tr><td bgcolor="#1e1b4b" style="background-color:#1e1b4b;padding:22px 30px;font-family:Arial,Helvetica,sans-serif">
      <span style="font-size:18px;font-weight:bold;color:#fde68a">&#9888;&#65039; Rotación de contraseña pendiente</span><br/>
      <span style="font-size:12px;color:#c7d2fe">Security Access Manager &middot; Aviso automático</span>
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:26px 30px 6px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2937;line-height:1.6">
      Hola <b style="color:#111827">{technician}</b>,<br/><br/>
      La contraseña de acceso del cliente <b style="color:#111827">{company}</b> está próxima a expirar o ya ha expirado. Necesita que la rotes y lo confirmes.
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:10px 30px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3f4f6" style="background-color:#f3f4f6;border:1px solid #e5e7eb">
        <tr><td align="center" style="padding:16px;font-family:Arial,Helvetica,sans-serif">
          <span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#6b7280">Días restantes</span><br/>
          <span style="font-size:32px;font-weight:bold;color:{urgency_color}">{days_remaining}</span>
        </td></tr>
      </table>
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:14px 30px 4px;font-family:Arial,Helvetica,sans-serif;font-size:13px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="padding:8px 0;color:#6b7280;border-bottom:1px solid #eef0f3">Cliente</td><td align="right" style="padding:8px 0;color:#111827;font-weight:bold;border-bottom:1px solid #eef0f3">{company}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280;border-bottom:1px solid #eef0f3">Último cambio</td><td align="right" style="padding:8px 0;color:#111827;border-bottom:1px solid #eef0f3">{last_changed}</td></tr>
        <tr><td style="padding:8px 0;color:#6b7280">Fecha límite</td><td align="right" style="padding:8px 0;color:#111827">{expiry_date}</td></tr>
      </table>
    </td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:6px 30px">{access_buttons}</td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:20px 30px 6px">{cta_button}</td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:0 30px 22px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6b7280">
      o <a href="{console_url}" style="color:#2563eb;text-decoration:none">entra a la consola</a> para gestionarlo
    </td></tr>
    <tr><td bgcolor="#f1f5f9" align="center" style="background-color:#f1f5f9;padding:14px 30px;border-top:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#9ca3af">
      Este aviso se repetirá hasta que confirmes el cambio &middot; No respondas a este mensaje
    </td></tr>
  </table>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->"""


def _get_smtp_cfg():
    try:
        from database import get_setting
        from crypto import decrypt_secret
        host = get_setting("smtp_host") or os.getenv("SMTP_HOST", "")
        port = int(get_setting("smtp_port") or os.getenv("SMTP_PORT", "587"))
        user = get_setting("smtp_user") or os.getenv("SMTP_USER", "")
        pwd = decrypt_secret(get_setting("smtp_pass")) or os.getenv("SMTP_PASS", "")
        from_addr = get_setting("smtp_from") or os.getenv("SMTP_FROM", user)
        admin_email = get_setting("admin_email") or os.getenv("ADMIN_EMAIL", "")
    except Exception:
        host = os.getenv("SMTP_HOST", "")
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        pwd = os.getenv("SMTP_PASS", "")
        from_addr = os.getenv("SMTP_FROM", user)
        admin_email = os.getenv("ADMIN_EMAIL", "")
    return host, port, user, pwd, from_addr, admin_email


def _get_alert_cfg():
    """Retorna (alert_start_days, alert_interval_hours)."""
    try:
        from database import get_setting
        start_days = int(get_setting("alert_start_days") or "7")
        interval_h = float(get_setting("alert_interval_hours") or "24")
    except Exception:
        start_days, interval_h = 7, 24.0
    return start_days, interval_h


def _get_escalation_cfg():
    """Retorna (enabled, threshold, admin_ids, extra_emails).
    - admin_ids: IDs de usuarios elegidos explícitamente como destinatarios.
    - extra_emails: emails externos (personas sin cuenta en la plataforma).
    Si AMBAS listas están vacías, el destinatario por defecto son todos los
    admins activos con email (comportamiento retrocompatible)."""
    try:
        from database import get_setting
        enabled = (get_setting("alert_escalation_enabled") or "off") == "on"
        threshold = int(get_setting("alert_escalation_count") or "3")
        ids_raw = get_setting("alert_escalation_admin_ids") or ""
        emails_raw = get_setting("alert_escalation_extra_emails") or ""
        admin_ids = [int(x) for x in ids_raw.replace(";", ",").split(",") if x.strip().isdigit()]
        extra_emails = [
            e.strip() for e in emails_raw.replace(";", ",").replace("\n", ",").split(",")
            if e.strip() and "@" in e
        ]
    except Exception:
        enabled, threshold, admin_ids, extra_emails = False, 3, [], []
    return enabled, max(1, threshold), admin_ids, extra_emails


def _cta_button(url: str, label: str, color: str = "#16a34a", width: int = 240) -> str:
    """Botón 'bulletproof' con VML para Outlook Desktop + fallback HTML para el
    resto de clientes. El namespace VML se declara en el wrapper del email."""
    return (
        f'<!--[if mso]>'
        f'<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" '
        f'href="{url}" style="height:46px;v-text-anchor:middle;width:{width}px;" arcsize="16%" stroke="f" fillcolor="{color}">'
        f'<w:anchorlock/><center style="color:#ffffff;font-family:Arial,sans-serif;font-size:15px;font-weight:bold;">{label}</center>'
        f'</v:roundrect>'
        f'<![endif]-->'
        f'<!--[if !mso]><!-- -->'
        f'<a href="{url}" style="background-color:{color};border-radius:8px;color:#ffffff;display:inline-block;'
        f'font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;line-height:46px;text-align:center;'
        f'text-decoration:none;width:{width}px;-webkit-text-size-adjust:none;mso-hide:all;">{label}</a>'
        f'<!--<![endif]-->'
    )


def _wrap_html_email(inner: str) -> str:
    """Envuelve el cuerpo en un documento HTML completo con los namespaces VML
    y la meta color-scheme. Si ya es un documento completo, lo deja igual."""
    head = inner.lstrip().lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return inner
    return (
        '<!DOCTYPE html>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>'
        '<meta name="color-scheme" content="light dark"/>'
        '<meta name="supported-color-schemes" content="light dark"/>'
        '<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>'
        '</o:OfficeDocumentSettings></xml><![endif]-->'
        '<style>body{margin:0;padding:0;}table{border-collapse:collapse;}</style></head>'
        f'<body style="margin:0;padding:0;background-color:#f1f5f9;">{inner}</body></html>'
    )


def alert_mapping(tech_name: str, company, tc) -> dict:
    """Diccionario de sustitución de variables del email de alerta a técnico,
    con datos REALES del par (técnico, empresa). Reutilizado por el envío y la
    vista previa del panel."""
    from datetime import timedelta
    from database import get_setting
    from auth import create_confirm_token
    expiry_date = (tc._effective_last_changed + timedelta(days=company.expiry_days)).strftime("%d/%m/%Y")
    days = tc.days_remaining
    vpn_block = f'<p><b>Acceso VPN:</b> <a href="{company.vpn_url}">{company.vpn_url}</a></p>' if company.vpn_url else ""

    # Enlaces para el email
    base = (get_setting("app_base_url") or "").rstrip("/")
    token = create_confirm_token(tc.technician_id, tc.company_id)
    confirm_url = f"{base}/confirm/{token}"
    console_url = f"{base}/login"

    # Color según urgencia (tonos legibles sobre fondo claro)
    urgency = "#dc2626" if days <= 2 else ("#d97706" if days <= 7 else "#16a34a")

    # Botones de acceso (VPN/doc) — tabla + bgcolor (Word los respeta)
    def _btn(url, label):
        return (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="display:inline-block;margin:4px 4px">'
            f'<tr><td bgcolor="#eef2f7" align="center" style="background-color:#eef2f7;border:1px solid #d1d5db">'
            f'<a href="{url}" style="display:inline-block;padding:10px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;color:#1f2937;text-decoration:none">{label}</a>'
            '</td></tr></table>'
        )
    btns = ""
    if company.vpn_url:
        btns += _btn(company.vpn_url, "&#128272; Acceso VPN")
    if company.doc_url:
        btns += _btn(company.doc_url, "&#128196; Documentación")
    access_buttons = btns or ""

    # Botón CTA "bulletproof" con VML para Outlook Desktop
    cta_button = _cta_button(confirm_url, "&#10003; Confirmar rotación")

    return {
        "{technician}": tech_name,
        "{company}": company.name,
        "{days_remaining}": str(days) if days > 0 else "EXPIRADO",
        "{vpn_url}": company.vpn_url or "—",
        "{doc_url}": company.doc_url or "—",
        "{last_changed}": str(tc._effective_last_changed),
        "{expiry_date}": expiry_date,
        "{vpn_url_block}": vpn_block,
        "{confirm_url}": confirm_url,
        "{console_url}": console_url,
        "{cta_button}": cta_button,
        "{urgency_color}": urgency,
        "{access_buttons}": access_buttons,
    }


def _render_template(tech_name: str, company, tc) -> tuple[str, str]:
    """Renderiza subject + body usando el last_changed del técnico concreto (tc)."""
    try:
        from database import get_setting
        subject_tpl = get_setting("email_subject") or DEFAULT_SUBJECT
        body_tpl = get_setting("email_body") or DEFAULT_BODY
    except Exception:
        subject_tpl = DEFAULT_SUBJECT
        body_tpl = DEFAULT_BODY

    for k, v in alert_mapping(tech_name, company, tc).items():
        subject_tpl = subject_tpl.replace(k, v)
        body_tpl = body_tpl.replace(k, v)

    return subject_tpl, body_tpl


# ── Email de bienvenida (alta de técnico) ────────────────────────────────────
DEFAULT_WELCOME_SUBJECT = "👋 Bienvenido a Access Manager — tus credenciales"
DEFAULT_WELCOME_BODY = """\
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f5f9" style="background-color:#f1f5f9">
<tr><td align="center" style="padding:24px 10px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width:600px;max-width:600px;background-color:#ffffff;border:1px solid #e5e7eb">
    <tr><td bgcolor="#1e1b4b" style="background-color:#1e1b4b;padding:22px 30px;font-family:Arial,Helvetica,sans-serif">
      <span style="font-size:18px;font-weight:bold;color:#fde68a">&#128075; Te damos la bienvenida</span><br/>
      <span style="font-size:12px;color:#c7d2fe">Access Manager &middot; Alta de usuario</span>
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:26px 30px 6px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2937;line-height:1.6">
      Hola <b style="color:#111827">{username}</b>,<br/><br/>
      Se te ha registrado en <b style="color:#111827">Access Manager</b>, la plataforma para dar seguimiento a la rotación de los accesos de los diferentes clientes. A partir de ahora recibirás un aviso cuando toque rotar la contraseña de alguno de tus clientes.
    </td></tr>
    <tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:12px 30px 0">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f9fafb" style="background-color:#f9fafb;border:1px solid #e5e7eb">
        <tr><td style="padding:10px 14px;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:13px">Usuario</td><td align="right" style="padding:10px 14px;color:#111827;font-weight:bold;font-family:Arial,Helvetica,sans-serif;font-size:13px">{username}</td></tr>
        <tr><td style="padding:10px 14px;color:#6b7280;border-top:1px solid #eef0f3;font-family:Arial,Helvetica,sans-serif;font-size:13px">Contraseña temporal</td><td align="right" style="padding:10px 14px;color:#111827;border-top:1px solid #eef0f3;font-family:'Courier New',monospace;font-weight:bold;font-size:13px">{password}</td></tr>
      </table>
      <p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6b7280;margin:8px 0 0">&#128274; Por seguridad, deberás cambiar la contraseña en tu primer acceso.</p>
    </td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:20px 30px 6px">{cta_button}</td></tr>
    <tr><td bgcolor="#ffffff" align="center" style="background-color:#ffffff;padding:0 30px 22px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#6b7280">
      o entra desde <a href="{login_url}" style="color:#2563eb;text-decoration:none">{login_url}</a>
    </td></tr>
    <tr><td bgcolor="#f1f5f9" align="center" style="background-color:#f1f5f9;padding:14px 30px;border-top:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#9ca3af">
      Si no esperabas este correo, avisa a tu administrador &middot; No respondas a este mensaje
    </td></tr>
  </table>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->"""


def welcome_mapping(username: str, password: str, email: str | None) -> dict:
    """Variables del email de bienvenida, con datos reales del alta."""
    from database import get_setting
    base = (get_setting("app_base_url") or "").rstrip("/")
    login_url = f"{base}/login" if base else "/login"
    return {
        "{username}": username,
        "{password}": password,
        "{email}": email or "—",
        "{login_url}": login_url,
        "{cta_button}": _cta_button(login_url, "&#128273; Acceder a la consola", color="#2563eb"),
        "{app_name}": "Access Manager",
    }


def send_welcome_email(username: str, password: str, email: str | None) -> bool:
    """Renderiza y envía el email de bienvenida al técnico. Requiere email."""
    if not email:
        return False
    try:
        from database import get_setting
        subj = get_setting("welcome_subject") or DEFAULT_WELCOME_SUBJECT
        body = get_setting("welcome_body") or DEFAULT_WELCOME_BODY
    except Exception:
        subj, body = DEFAULT_WELCOME_SUBJECT, DEFAULT_WELCOME_BODY
    for k, v in welcome_mapping(username, password, email).items():
        subj = subj.replace(k, v)
        body = body.replace(k, v)
    return _send_email(email, subj, body, kind="welcome")


def _smtp_security() -> str:
    """Modo de seguridad SMTP: auto | starttls | ssl | none."""
    try:
        from database import get_setting
        return (get_setting("smtp_security") or "auto").lower()
    except Exception:
        return "auto"


def _open_smtp(host, port, user, pwd, security, timeout=25):
    """Abre y prepara una conexión SMTP de forma adaptativa.
    - STARTTLS: forzado si security=='starttls'; automático si security=='auto'
      y el servidor lo anuncia; nunca si security=='none'.
    - AUTH (login): solo si hay credenciales Y el servidor anuncia AUTH.
      Si no hay credenciales o el servidor no soporta AUTH → modo relay.
    Devuelve (srv, steps). Lanza excepción si falla la conexión/TLS/login.
    """
    steps = []
    if security == "ssl":
        srv = smtplib.SMTP_SSL(host, port, timeout=timeout)
        srv.ehlo()
        steps.append(f"Conexión SSL/TLS implícita a {host}:{port}")
    else:
        srv = smtplib.SMTP(host, port, timeout=timeout)
        srv.ehlo()
        steps.append(f"Conexión a {host}:{port} + EHLO")
        starttls_ok = srv.has_extn("starttls")
        if security == "starttls" or (security == "auto" and starttls_ok):
            srv.starttls()
            srv.ehlo()
            steps.append("STARTTLS activado")
        else:
            steps.append("Sin STARTTLS" + ("" if security == "none" else " (el servidor no lo anuncia)"))

    if user and pwd:
        if srv.has_extn("auth"):
            srv.login(user, pwd)
            steps.append(f"Autenticado como {user}")
        else:
            steps.append("El servidor NO soporta AUTH → se envía sin autenticar (relay)")
    else:
        steps.append("Sin credenciales → modo relay (sin autenticar)")
    return srv, steps


def _smtp_send(to: str, subject: str, html_body: str) -> tuple[bool, str | None]:
    """Envío SMTP puro y adaptativo. Devuelve (ok, error). No registra nada —
    eso lo hace el llamador (_send_email o el job de reintentos)."""
    host, port, user, pwd, from_addr, _ = _get_smtp_cfg()
    if not host:
        return False, "SMTP no configurado (falta el host)"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.attach(MIMEText(_wrap_html_email(html_body), "html"))
        srv, _steps = _open_smtp(host, port, user, pwd, _smtp_security())
        try:
            srv.sendmail(from_addr, to, msg.as_string())
        finally:
            try: srv.quit()
            except Exception: pass
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:500]


def _record_smtp_test(ok: bool, detail: str):
    """Guarda el resultado del último test/diagnóstico SMTP para mostrarlo."""
    try:
        from database import set_setting
        set_setting("smtp_test_status", "ok" if ok else "error")
        set_setting("smtp_test_detail", (detail or "")[:600])
        set_setting("smtp_test_at", datetime.utcnow().isoformat())
    except Exception:
        pass


def smtp_diagnose() -> dict:
    """Prueba la conexión SMTP SIN enviar email. Reporta capacidades del
    servidor (STARTTLS, AUTH y mecanismos) y los pasos realizados. Registra
    el resultado para la trazabilidad de la pestaña SMTP."""
    host, port, user, pwd, from_addr, _ = _get_smtp_cfg()
    res = {"ok": False, "host": host, "port": port, "steps": [], "features": [],
           "auth_mechs": None, "error": None}
    if not host:
        res["error"] = "SMTP no configurado (falta el host)"
        _record_smtp_test(False, res["error"])
        return res
    try:
        srv, steps = _open_smtp(host, port, user, pwd, _smtp_security())
        res["steps"] = steps
        try:
            res["features"] = sorted(srv.esmtp_features.keys())
            res["auth_mechs"] = srv.esmtp_features.get("auth")
        except Exception:
            pass
        try: srv.quit()
        except Exception: pass
        res["ok"] = True
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:400]

    detail = (" · ".join(res["steps"]) if res["ok"] else (res["error"] or ""))
    if res["features"]:
        detail += " | Capacidades: " + ", ".join(res["features"])
    _record_smtp_test(res["ok"], detail)
    return res


def _log_email(to: str, subject: str, html_body: str, kind: str,
               ok: bool, error: str | None):
    """Registra el resultado de un envío en email_logs (sesión propia, aislada
    del llamador). En éxito libera el body para no inflar la BD."""
    from database import SessionLocal
    from models import EmailLog
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        row = EmailLog(
            to_address=to or "—",
            subject=(subject or "")[:512],
            kind=kind,
            status="sent" if ok else "failed",
            body=None if ok else html_body,
            error=None if ok else error,
            attempts=1,
            created_at=now,
            last_attempt_at=now,
            sent_at=now if ok else None,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        logger.error("No se pudo registrar EmailLog: %s", e)
        db.rollback()
    finally:
        db.close()


def _send_email(to: str, subject: str, html_body: str, kind: str = "other"):
    """Envía un email y registra el resultado en email_logs. Mantiene la firma
    retrocompatible (devuelve True/False); `kind` es opcional."""
    ok, error = _smtp_send(to, subject, html_body)
    if ok:
        logger.info("Email enviado → %s | %s", to, subject)
    else:
        logger.error("Error SMTP → %s: %s", to, error)
    _log_email(to, subject, html_body, kind, ok, error)
    return ok


def _should_alert(tc, start_days: int, interval_hours: float) -> bool:
    """Decide si hay que enviar alerta para este par técnico+empresa.
    Usa el last_changed y alert_last_sent propios del técnico.
    """
    days = tc.days_remaining
    if days > start_days:
        return False                              # aún no es momento

    now = datetime.utcnow()

    if tc.alert_last_sent is None:
        return True                               # nunca se ha enviado a este técnico

    if days > 2:
        # Zona de aviso: solo UNA vez desde la última confirmación del técnico
        last_changed_dt = datetime.combine(tc._effective_last_changed, datetime.min.time())
        return tc.alert_last_sent < last_changed_dt
    else:
        # Zona crítica: repetir cada interval_hours hasta que ESTE técnico confirme
        elapsed = (now - tc.alert_last_sent).total_seconds() / 3600
        return elapsed >= interval_hours


def _send_escalation(tc, tech, company, recipient_emails) -> bool:
    """Envía un email de escalado a la lista de destinatarios (admins elegidos
    y/o emails externos) indicando que un técnico no ha confirmado tras varios
    avisos."""
    days = tc.days_remaining
    estado = "EXPIRADO" if days <= 0 else f"quedan {days} días"
    subject = f"⛔ Escalado — {company.name} sin rotar ({tech.username})"
    body = f"""\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#0f1117;color:#a6adbb;border-radius:12px;overflow:hidden">
  <div style="background:linear-gradient(135deg,#7c2d12,#1e1b4b);padding:24px 32px">
    <h1 style="margin:0;font-size:18px;color:#fdba74;letter-spacing:1px">⛔ ALERTA ESCALADA</h1>
    <p style="margin:4px 0 0;font-size:12px;color:#9ca3af">Security Access Manager · Escalado automático</p>
  </div>
  <div style="padding:24px 32px">
    <p style="font-size:14px;color:#e5e7eb;margin:0 0 16px">
      El técnico <b style="color:#fdba74">{tech.username}</b> ha recibido
      <b>{tc.alert_count}</b> avisos sobre el cliente <b style="color:#fdba74">{company.name}</b>
      y aún <b>no ha confirmado</b> la rotación.
    </p>
    <div style="background:#1a1f2e;border:1px solid #374151;border-radius:8px;padding:14px;font-size:13px">
      <p style="margin:0 0 6px">· Cliente: <b style="color:#e5e7eb">{company.name}</b></p>
      <p style="margin:0 0 6px">· Técnico: <b style="color:#e5e7eb">{tech.username}</b> ({tech.email})</p>
      <p style="margin:0 0 6px">· Estado: <b style="color:#f87171">{estado}</b></p>
      <p style="margin:0">· Avisos enviados sin respuesta: <b style="color:#f87171">{tc.alert_count}</b></p>
    </div>
    <p style="margin:18px 0 0;font-size:12px;color:#6b7280">Requiere intervención manual.</p>
  </div>
  <div style="background:#0a0d14;padding:14px 32px;text-align:center">
    <p style="margin:0;font-size:11px;color:#374151">Generado automáticamente · No respondas a este mensaje</p>
  </div>
</div>"""
    sent = False
    for addr in recipient_emails:
        if _send_email(addr, subject, body, kind="escalation"):
            sent = True
    return sent


def job_check_alerts():
    """Tarea principal que se ejecuta cada hora.
    Itera sobre pares (técnico, empresa) — cada uno tiene su propio estado.
    Solo se avisa a técnicos que NO han confirmado aún su cambio.
    Si el escalado está activo, tras N avisos sin confirmar avisa a los admins.
    """
    from database import SessionLocal
    from models import TechnicianCompany, User

    import audit as audit_mod

    start_days, interval_hours = _get_alert_cfg()
    esc_enabled, esc_threshold, esc_admin_ids, esc_extra_emails = _get_escalation_cfg()
    db = SessionLocal()
    try:
        # Destinatarios del escalado: admins elegidos ∪ emails externos.
        # Si no se ha configurado nada → todos los admins activos (retrocompat).
        escalation_recipients = []
        if esc_enabled:
            emails = set()
            if esc_admin_ids:
                chosen = db.query(User).filter(
                    User.id.in_(esc_admin_ids), User.is_active == True, User.email != None,
                ).all()
                for a in chosen:
                    if a.email:
                        emails.add(a.email.strip())
            for e in esc_extra_emails:
                emails.add(e)
            if not esc_admin_ids and not esc_extra_emails:
                fallback = db.query(User).filter(
                    User.role == "admin", User.is_active == True, User.email != None,
                ).all()
                for a in fallback:
                    if a.email:
                        emails.add(a.email.strip())
            escalation_recipients = sorted(emails)

        tc_list = db.query(TechnicianCompany).all()
        sent_total = 0
        escalated_total = 0

        for tc in tc_list:
            tech    = tc.technician
            company = tc.company

            if not tech.is_active or not tech.email:
                continue
            if not _should_alert(tc, start_days, interval_hours):
                continue

            subject, body = _render_template(tech.username, company, tc)
            if _send_email(tech.email, subject, body, kind="alert"):
                sent_total += 1
                tc.alert_last_sent = datetime.utcnow()
                tc.alert_count = (tc.alert_count or 0) + 1

                # Escalado: tras N avisos sin confirmar, avisar a destinatarios (una vez)
                if (esc_enabled and escalation_recipients
                        and tc.alert_count >= esc_threshold
                        and tc.escalated_at is None):
                    if _send_escalation(tc, tech, company, escalation_recipients):
                        tc.escalated_at = datetime.utcnow()
                        escalated_total += 1
                        # Queda registrado como warning → visible en la campana
                        audit_mod.log(
                            db,
                            f"⛔ Escalado: {company.name} sin rotar por {tech.username} "
                            f"({tc.alert_count} avisos sin confirmar)",
                            company_id=company.id,
                            level="warning",
                        )

        if sent_total:
            db.commit()
            logger.info("Alertas enviadas: %d (escaladas: %d)", sent_total, escalated_total)
    except Exception as e:
        logger.error("Error en job_check_alerts: %s", e)
    finally:
        db.close()


def _build_tabla_empresas(companies: list) -> str:
    """Genera la tabla HTML de empresas para el email de reporte. A prueba de
    Outlook Desktop: tablas + bgcolor, colores legibles sobre fondo claro."""
    color_map = {"critical": "#dc2626", "warning": "#d97706", "ok": "#16a34a"}
    bg_map    = {"critical": "#fef2f2", "warning": "#fffbeb", "ok": "#f0fdf4"}
    label_map = {"critical": "CRÍTICO", "warning": "ATENCIÓN", "ok": "OK"}
    th = ("padding:8px 12px;text-align:left;font-size:10px;color:#6b7280;letter-spacing:1px;"
          "text-transform:uppercase;border-bottom:1px solid #e5e7eb;font-family:Arial,Helvetica,sans-serif")
    rows = ""
    for c in sorted(companies, key=lambda x: x.days_remaining):
        color = color_map.get(c.status, "#6b7280")
        bg    = bg_map.get(c.status, "#f3f4f6")
        label = label_map.get(c.status, c.status.upper())
        dias = "EXP" if c.days_remaining <= 0 else f"{c.days_remaining}d"
        rows += (
            "<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eef0f3;color:#111827;font-family:Arial,Helvetica,sans-serif;font-size:13px'>{c.name}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eef0f3;font-family:Courier New,monospace;font-weight:bold;color:{color};font-size:13px'>{dias}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eef0f3'>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' border='0' style='display:inline-block'><tr>"
            f"<td bgcolor='{bg}' style='background-color:{bg};border:1px solid {color};padding:2px 9px;"
            f"font-family:Arial,Helvetica,sans-serif;font-size:10px;font-weight:bold;letter-spacing:1px;color:{color}'>{label}</td>"
            f"</tr></table></td>"
            "</tr>"
        )
    return (
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' border='0' bgcolor='#ffffff' "
        "style='background-color:#ffffff;border:1px solid #e5e7eb'>"
        f"<tr bgcolor='#f9fafb'>"
        f"<td bgcolor='#f9fafb' style='background-color:#f9fafb;{th}'>Empresa</td>"
        f"<td bgcolor='#f9fafb' style='background-color:#f9fafb;{th}'>Días</td>"
        f"<td bgcolor='#f9fafb' style='background-color:#f9fafb;{th}'>Estado</td>"
        f"</tr>{rows}</table>"
    )


def report_mapping(companies: list, technicians: list) -> dict:
    """Diccionario de sustitución de variables del reporte a admins, con datos
    REALES. El % de cumplimiento usa EXACTAMENTE la misma lógica que el
    dashboard (compliance.py): solo el estado CRÍTICO penaliza; las unidades
    son empresas + técnicos con asignaciones. ATENCIÓN no penaliza."""
    total = len(companies)
    n_ok       = sum(1 for c in companies if c.status == "ok")
    n_warning  = sum(1 for c in companies if c.status == "warning")
    n_critical = sum(1 for c in companies if c.status == "critical")

    # Mismo cálculo que compute_compliance(): unidades = empresas + técnicos
    # con asignaciones; fallos = empresas críticas + técnicos con alguna crítica.
    techs_with  = [t for t in technicians if len(t.tc_assocs) > 0]
    total_techs = len(techs_with)
    techs_ko    = sum(1 for t in techs_with if any(tc.status == "critical" for tc in t.tc_assocs))

    total_units  = total + total_techs
    failed_units = n_critical + techs_ko
    compliance   = round((total_units - failed_units) / total_units * 100) if total_units else 100

    return {
        "{fecha}":          date.today().strftime("%d/%m/%Y"),
        "{compliance_pct}": str(compliance),
        "{total_empresas}": str(total),
        "{criticas}":       str(n_critical),
        "{en_aviso}":       str(n_warning),
        "{ok}":             str(n_ok),
        "{tecnicos_ko}":    str(techs_ko),
        "{tabla_empresas}": _build_tabla_empresas(companies),
    }


def _render_report(companies: list, technicians: list) -> tuple[str, str]:
    """Renderiza subject + body del reporte usando la plantilla de BD."""
    try:
        from database import get_setting
        subject_tpl = get_setting("report_subject") or DEFAULT_REPORT_SUBJECT
        body_tpl = get_setting("report_body") or DEFAULT_REPORT_BODY
    except Exception:
        subject_tpl = DEFAULT_REPORT_SUBJECT
        body_tpl = DEFAULT_REPORT_BODY

    for k, v in report_mapping(companies, technicians).items():
        subject_tpl = subject_tpl.replace(k, v)
        body_tpl = body_tpl.replace(k, v)

    return subject_tpl, body_tpl


def job_report_send() -> int:
    """
    Genera y envía el reporte a todos los admins con receive_reports=True y email.
    Devuelve el número de emails enviados.
    """
    from database import SessionLocal
    from models import Company, User

    db = SessionLocal()
    try:
        admins = db.query(User).filter(
            User.role == "admin",
            User.is_active == True,
            User.receive_reports == True,
            User.email != None,
        ).all()

        if not admins:
            logger.info("Reporte: sin administradores con receive_reports=True y email")
            return 0

        companies    = db.query(Company).all()
        technicians  = db.query(User).filter(User.role == "technician", User.is_active == True).all()
        subject, body = _render_report(companies, technicians)

        sent = 0
        for admin in admins:
            if _send_email(admin.email, subject, body, kind="report"):
                sent += 1

        logger.info("Reporte enviado a %d administrador(es)", sent)
        return sent
    except Exception as e:
        logger.error("Error en job_report_send: %s", e)
        return 0
    finally:
        db.close()


def job_weekly_report():
    """Wrapper llamado por el scheduler — comprueba si toca enviar según la config."""
    try:
        from database import get_setting
        report_day  = get_setting("report_day")  or "mon"
        report_hour = int(get_setting("report_hour") or "9")
    except Exception:
        report_day, report_hour = "mon", 9

    now = datetime.utcnow()

    # "daily" = todos los días; cualquier otro valor = día de la semana abreviado
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if report_day == "daily":
        day_match = True
    else:
        current_day = day_names[now.weekday()]
        day_match = (current_day == report_day)

    hour_match = (now.hour == report_hour)

    if day_match and hour_match:
        job_report_send()


def job_db_health_check():
    """
    Comprueba la salud de la BD cada hora. Si las alertas están activadas y
    se detecta un problema (umbral de tamaño superado o BD sin respuesta),
    envía un email a los administradores con receive_reports=True.

    Además, si el backup automático está activado, crea una copia diaria
    a las 03:00 UTC.
    """
    try:
        import db_admin
        from database import get_setting

        # ── Backup automático diario (03:00 UTC) ─────────────────────────────
        now = datetime.utcnow()
        if (get_setting("db_auto_backup") or "off") == "on" and now.hour == 3:
            try:
                res = db_admin.create_backup()
                logger.info("Backup automático creado: %s", res.get("filename"))
            except Exception as e:
                logger.error("Error en backup automático: %s", e)

        # ── Auto-purga de la papelera (si está activada) ─────────────────────
        if (get_setting("trash_auto_purge_enabled") or "off") == "on":
            try:
                days = int(get_setting("trash_purge_days") or "30")
                import trash as trash_svc
                from database import SessionLocal as _SL
                tdb = _SL()
                try:
                    purged = trash_svc.purge_old(tdb, days)
                    if purged:
                        logger.info("Papelera: %d elemento(s) purgado(s) (>%dd)", purged, days)
                finally:
                    tdb.close()
            except Exception as e:
                logger.error("Error en auto-purga de papelera: %s", e)

        # ── Auto-purga del histórico de rotaciones (si está activada) ────────
        if (get_setting("rotation_purge_enabled") or "off") == "on":
            try:
                days = int(get_setting("rotation_purge_days") or "365")
                res = db_admin.purge_rotation_history(days)
                if res.get("deleted"):
                    logger.info("Histórico de rotaciones: %d purgado(s) (>%dd)", res["deleted"], days)
            except Exception as e:
                logger.error("Error en auto-purga de rotaciones: %s", e)

        # ── Alertas de salud ─────────────────────────────────────────────────
        if (get_setting("db_alert_enabled") or "off") != "on":
            return

        alert = db_admin.evaluate_alerts()
        if not alert:
            return

        from database import SessionLocal
        from models import User
        db = SessionLocal()
        try:
            admins = db.query(User).filter(
                User.role == "admin",
                User.is_active == True,
                User.receive_reports == True,
                User.email != None,
            ).all()
            recipients = [a.email for a in admins]
        finally:
            db.close()

        if not recipients:
            logger.warning("Alerta de BD detectada pero sin destinatarios con email")
            return

        problems_html = "".join(f"<li style='margin:6px 0'>{p}</li>" for p in alert["problems"])
        subject = "🚨 Alerta de Base de Datos — Gestión de Accesos"
        body = f"""\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;background:#0f1117;color:#a6adbb;border-radius:12px;overflow:hidden">
  <div style="background:linear-gradient(135deg,#7f1d1d,#1e1b4b);padding:24px 32px">
    <h1 style="margin:0;font-size:18px;color:#fca5a5;letter-spacing:1px">🚨 ALERTA — BASE DE DATOS</h1>
    <p style="margin:4px 0 0;font-size:12px;color:#9ca3af">Security Access Manager · Monitoreo automático</p>
  </div>
  <div style="padding:24px 32px">
    <p style="font-size:14px;color:#e5e7eb;margin:0 0 12px">Se han detectado los siguientes problemas:</p>
    <ul style="font-size:13px;color:#fca5a5;padding-left:20px;margin:0 0 20px">{problems_html}</ul>
    <div style="background:#1a1f2e;border:1px solid #374151;border-radius:8px;padding:14px">
      <p style="margin:0;font-size:12px;color:#9ca3af">Uso de almacenamiento:
        <b style="color:#f87171">{alert['size']['pct']}%</b>
        ({alert['size']['used_mb']} MB de {alert['size']['max_mb']} MB)</p>
    </div>
    <p style="margin:20px 0 0;font-size:12px;color:#6b7280">
      Revisa el panel <b>Configuración → Base de datos</b> para ejecutar mantenimiento
      (VACUUM, purga de logs o backup).
    </p>
  </div>
  <div style="background:#0a0d14;padding:14px 32px;text-align:center">
    <p style="margin:0;font-size:11px;color:#374151">Generado automáticamente · No respondas a este mensaje</p>
  </div>
</div>"""

        sent = 0
        for to in recipients:
            if _send_email(to, subject, body, kind="db_alert"):
                sent += 1
        logger.warning("Alerta de BD enviada a %d administrador(es): %s", sent, alert["problems"])
    except Exception as e:
        logger.error("Error en job_db_health_check: %s", e)


def job_daily_snapshot():
    """Guarda la foto de cumplimiento del día (upsert). Alimenta las gráficas
    de tendencia. Idempotente: una fila por día, siempre con el estado más reciente."""
    from database import SessionLocal
    from compliance import take_compliance_snapshot
    db = SessionLocal()
    try:
        snap = take_compliance_snapshot(db)
        logger.info("Snapshot de cumplimiento %s: %d%%", snap.snapshot_date, snap.compliance_pct)
    except Exception as e:
        logger.error("Error en job_daily_snapshot: %s", e)
    finally:
        db.close()


MAX_EMAIL_ATTEMPTS = 5
EMAIL_RETENTION_DAYS = 30


def job_retry_failed_emails():
    """Reintenta los emails con estado 'failed' (hasta MAX_EMAIL_ATTEMPTS) y purga
    los enviados correctamente con más de EMAIL_RETENTION_DAYS días de antigüedad."""
    from datetime import timedelta
    from database import SessionLocal
    from models import EmailLog
    db = SessionLocal()
    try:
        pending = db.query(EmailLog).filter(
            EmailLog.status == "failed",
            EmailLog.attempts < MAX_EMAIL_ATTEMPTS,
        ).all()
        retried = recovered = 0
        for row in pending:
            ok, error = _smtp_send(row.to_address, row.subject or "", row.body or "")
            row.attempts = (row.attempts or 0) + 1
            row.last_attempt_at = datetime.utcnow()
            retried += 1
            if ok:
                row.status = "sent"
                row.sent_at = datetime.utcnow()
                row.body = None
                row.error = None
                recovered += 1
            else:
                row.error = error

        cutoff = datetime.utcnow() - timedelta(days=EMAIL_RETENTION_DAYS)
        purged = db.query(EmailLog).filter(
            EmailLog.status == "sent",
            EmailLog.created_at < cutoff,
        ).delete(synchronize_session=False)

        db.commit()
        if retried or purged:
            logger.info("Reintento de emails: %d reintentados, %d recuperados, %d purgados",
                        retried, recovered, purged)
    except Exception as e:
        logger.error("Error en job_retry_failed_emails: %s", e)
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    from apscheduler.triggers.cron import CronTrigger
    scheduler = BackgroundScheduler()

    # Comprobar alertas cada hora
    scheduler.add_job(
        job_check_alerts,
        IntervalTrigger(hours=1),
        id="check_alerts",
        next_run_time=datetime.utcnow(),
    )

    # Reporte: comprobar cada hora si toca enviar (la config de día/hora se lee en runtime)
    scheduler.add_job(
        job_weekly_report,
        IntervalTrigger(hours=1),
        id="periodic_report",
    )

    # Salud de la BD: comprobar cada hora (alertas + backup automático)
    scheduler.add_job(
        job_db_health_check,
        IntervalTrigger(hours=1),
        id="db_health_check",
    )

    # Snapshot de cumplimiento: upsert horario (1 fila/día) + uno inicial inmediato
    scheduler.add_job(
        job_daily_snapshot,
        IntervalTrigger(hours=1),
        id="daily_snapshot",
        next_run_time=datetime.utcnow(),
    )

    # Reintento de emails fallidos + purga de enviados antiguos (cada hora)
    scheduler.add_job(
        job_retry_failed_emails,
        IntervalTrigger(hours=1),
        id="retry_emails",
    )

    scheduler.start()
    logger.info("Scheduler iniciado — alertas, reporte, salud de BD, snapshot y reintento de emails cada hora")
    return scheduler
