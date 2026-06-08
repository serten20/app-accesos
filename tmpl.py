"""
Instancia única de Jinja2Templates compartida por todos los routers.
Centraliza los filtros personalizados (fmt_date, fmt_datetime con zona horaria).
"""
import pytz
from fastapi.templating import Jinja2Templates


def _get_app_timezone():
    try:
        from database import get_setting
        tz_name = get_setting("timezone") or "Europe/Madrid"
        return pytz.timezone(tz_name)
    except Exception:
        return pytz.timezone("Europe/Madrid")


def _fmt_date(value):
    """Fecha → DD/MM/YYYY"""
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _fmt_datetime(value, fmt="%d/%m/%Y %H:%M:%S"):
    """Datetime UTC → zona horaria configurada → DD/MM/YYYY HH:MM:SS"""
    if value is None:
        return "—"
    if not hasattr(value, "strftime"):
        return str(value)
    try:
        tz = _get_app_timezone()
        if value.tzinfo is None:
            value = pytz.utc.localize(value)
        return value.astimezone(tz).strftime(fmt)
    except Exception:
        return value.strftime(fmt)


templates = Jinja2Templates(directory="templates")
templates.env.filters["fmt_date"]     = _fmt_date
templates.env.filters["fmt_datetime"] = _fmt_datetime
