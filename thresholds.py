"""Umbrales configurables del semáforo de estado (atención / crítico).

Hoy el estado se decide por días restantes:
    crítico  →  quedan <= threshold_critical  (por defecto 2)
    atención →  quedan <= threshold_warning   (por defecto 7)
    ok       →  resto

Estos valores son configurables en Configuración → General. Se cachean en
memoria porque `status` se calcula muchísimas veces por página y `get_setting`
abre una sesión de BD en cada llamada. La caché se invalida explícitamente al
guardar la configuración (ver `reset_cache`).
"""
from database import get_setting

DEFAULT_CRITICAL = 2
DEFAULT_WARNING = 7

_cache = None  # (critical, warning) | None


def _read_int(key: str, default: int) -> int:
    try:
        v = int(get_setting(key) or default)
        return v if v >= 0 else default
    except (ValueError, TypeError):
        return default


def get_thresholds() -> tuple[int, int]:
    """Devuelve (critical_days, warning_days) con los valores actuales (cacheado)."""
    global _cache
    if _cache is None:
        crit = _read_int("threshold_critical", DEFAULT_CRITICAL)
        warn = _read_int("threshold_warning", DEFAULT_WARNING)
        # Coherencia: crítico nunca puede superar a atención
        if crit > warn:
            crit, warn = DEFAULT_CRITICAL, DEFAULT_WARNING
        _cache = (crit, warn)
    return _cache


def classify(days_remaining: int) -> str:
    """Clasifica los días restantes en 'critical' | 'warning' | 'ok'."""
    crit, warn = get_thresholds()
    if days_remaining <= crit:
        return "critical"
    if days_remaining <= warn:
        return "warning"
    return "ok"


def reset_cache():
    """Invalida la caché para que el próximo `get_thresholds` relea la BD."""
    global _cache
    _cache = None
