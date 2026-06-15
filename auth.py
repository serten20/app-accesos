import os
from functools import wraps
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from models import User
from crypto import SECRET_KEY

SESSION_COOKIE = "session_token"
MAX_AGE = 60 * 60 * 8  # 8 horas

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY)
# Serializer dedicado para los enlaces de confirmación rápida desde el email
confirm_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="confirm-rotation")
# Serializer dedicado para los enlaces de reseteo de contraseña (salt distinto)
reset_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="password-reset")


def password_fingerprint(hashed_password: str) -> str:
    """Huella corta del hash actual de la contraseña. Se incrusta en el token de
    reseteo para que el enlace sea de UN SOLO USO: al cambiar la contraseña la
    huella cambia y los enlaces antiguos quedan inválidos."""
    import hashlib
    return hashlib.sha256((hashed_password or "").encode()).hexdigest()[:16]


def create_reset_token(user_id: int, hashed_password: str) -> str:
    """Token firmado y con caducidad para el enlace de reseteo de contraseña."""
    return reset_serializer.dumps({"u": user_id, "h": password_fingerprint(hashed_password)})


def decode_reset_token(token: str, max_age_seconds: int):
    """Devuelve (user_id, fingerprint, reason). reason: None si válido,
    'expired' si caducó, 'invalid' si la firma no es válida."""
    try:
        data = reset_serializer.loads(token, max_age=max_age_seconds)
        return data.get("u"), data.get("h"), None
    except SignatureExpired:
        return None, None, "expired"
    except (BadSignature, Exception):
        return None, None, "invalid"


def create_confirm_token(technician_id: int, company_id: int) -> str:
    """Token firmado que codifica el par (técnico, empresa) para el enlace de
    confirmación rápida desde el email."""
    return confirm_serializer.dumps({"t": technician_id, "c": company_id})


def decode_confirm_token(token: str, max_age_seconds: int):
    """Devuelve (technician_id, company_id, reason). reason: None si válido,
    'expired' si caducó, 'invalid' si la firma no es válida."""
    try:
        data = confirm_serializer.loads(token, max_age=max_age_seconds)
        return data.get("t"), data.get("c"), None
    except SignatureExpired:
        return None, None, "expired"
    except (BadSignature, Exception):
        return None, None, "invalid"


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_session_token(user_id: int) -> str:
    return serializer.dumps(user_id)


def decode_session_token(token: str):
    """Devuelve (user_id, issued_at_utc_naive) o (None, None) si el token no es válido."""
    try:
        user_id, issued_at = serializer.loads(token, max_age=MAX_AGE, return_timestamp=True)
        # itsdangerous 2.x devuelve datetime tz-aware (UTC); normalizar a naive UTC
        if issued_at is not None and issued_at.tzinfo is not None:
            issued_at = issued_at.replace(tzinfo=None)
        return user_id, issued_at
    except (BadSignature, SignatureExpired):
        return None, None


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id, issued_at = decode_session_token(token)
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        return None
    # Invalidación de sesión: tokens emitidos antes de tokens_valid_from son inválidos
    if user.tokens_valid_from and issued_at and issued_at < user.tokens_valid_from:
        return None
    return user


def require_login(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        from database import SessionLocal
        db = SessionLocal()
        try:
            user = get_current_user(request, db)
            if not user:
                return RedirectResponse("/login", status_code=302)
            request.state.current_user = user
            request.state.db = db
            return await func(request, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def require_admin(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        from database import SessionLocal
        db = SessionLocal()
        try:
            user = get_current_user(request, db)
            if not user:
                return RedirectResponse("/login", status_code=302)
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="Acceso denegado")
            request.state.current_user = user
            request.state.db = db
            return await func(request, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def require_viewer(func):
    """Permite acceso a administradores Y auditores (solo lectura).
    Se usa en las rutas GET de visualización. Las rutas de escritura y de
    descarga siguen protegidas con @require_admin (el auditor recibe 403)."""
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        from database import SessionLocal
        db = SessionLocal()
        try:
            user = get_current_user(request, db)
            if not user:
                return RedirectResponse("/login", status_code=302)
            if user.role not in ("admin", "auditor"):
                raise HTTPException(status_code=403, detail="Acceso denegado")
            request.state.current_user = user
            request.state.db = db
            return await func(request, *args, **kwargs)
        finally:
            db.close()
    return wrapper


def seed_admin(db: Session):
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    email = os.getenv("ADMIN_EMAIL", "admin@local.com")
    existing = db.query(User).filter(User.role == "admin").first()
    if not existing:
        admin = User(
            username=username,
            email=email,
            hashed_password=hash_password(password),
            role="admin",
            must_change_password=True,   # fuerza el cambio en el primer login
        )
        db.add(admin)
        db.commit()
