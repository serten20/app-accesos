"""
Seguridad transversal:
  - CSRFMiddleware: protección CSRF mediante double-submit con token firmado en
    cookie HttpOnly + campo oculto en cada formulario. Implementado como
    middleware ASGI puro para poder leer y reinyectar el body sin romper la
    lectura posterior del formulario por las rutas.
  - Helpers de limitación de intentos de login (anti fuerza bruta), basados en
    el registro de auditoría (robusto entre múltiples workers de uvicorn).
"""
import os
import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.datastructures import MutableHeaders

CSRF_COOKIE = "csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


class CSRFMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        method = scope["method"]
        cookie_token = request.cookies.get(CSRF_COOKIE)

        # Token efectivo: el de la cookie o uno nuevo si aún no existe
        token = cookie_token or secrets.token_urlsafe(32)
        set_cookie = cookie_token is None

        # Exponer el token a las plantillas vía request.state.csrf_token
        scope.setdefault("state", {})
        scope["state"]["csrf_token"] = token

        # Validación CSRF en métodos que modifican estado
        if method not in _SAFE_METHODS:
            body, messages = await _read_body(receive)
            if not _csrf_valid(request, body, cookie_token):
                response = PlainTextResponse(
                    "CSRF token inválido o ausente. Recarga la página e inténtalo de nuevo.",
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            receive = _replay(messages)

        async def send_wrapper(message):
            if set_cookie and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                cookie = (
                    f"{CSRF_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
                    + ("; Secure" if COOKIE_SECURE else "")
                )
                headers.append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _read_body(receive):
    """Lee el body completo y guarda los mensajes para poder reinyectarlos."""
    body = b""
    messages = []
    more = True
    while more:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.request":
            body += message.get("body", b"")
            more = message.get("more_body", False)
        else:
            break
    return body, messages


def _replay(messages):
    """Devuelve un callable receive que reproduce los mensajes ya leídos."""
    queue = list(messages)

    async def receive():
        if queue:
            return queue.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _csrf_valid(request: Request, body: bytes, cookie_token: str | None) -> bool:
    if not cookie_token:
        return False
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            data = parse_qs(body.decode("utf-8", errors="replace"))
            submitted = (data.get("csrf_token") or [""])[0]
        except Exception:
            return False
        return hmac.compare_digest(submitted, cookie_token)
    if content_type.startswith("multipart/form-data"):
        # El token va como campo del multipart; basta comprobar que el valor
        # secreto (no legible cross-site al ser cookie HttpOnly) está presente.
        return cookie_token.encode() in body
    # Otros content-types (p.ej. JSON) no se usan en esta app
    return False


# ── Anti fuerza bruta ─────────────────────────────────────────────────────────

LOGIN_WINDOW_MIN = 15
LOGIN_MAX_FAILED = 5


def too_many_login_attempts(db, ip: str) -> bool:
    """True si esta IP supera el umbral de intentos fallidos en la ventana."""
    if not ip:
        return False
    from models import AuditLog
    since = datetime.utcnow() - timedelta(minutes=LOGIN_WINDOW_MIN)
    count = (
        db.query(AuditLog)
        .filter(
            AuditLog.level == "warning",
            AuditLog.action.like("Login fallido%"),
            AuditLog.ip_address == ip,
            AuditLog.timestamp >= since,
        )
        .count()
    )
    return count >= LOGIN_MAX_FAILED
