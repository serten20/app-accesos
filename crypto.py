"""
Gestión del SECRET_KEY y cifrado simétrico de secretos en reposo.

- El SECRET_KEY se toma de la variable de entorno si es válido (>=32 chars y no
  es un placeholder). Si no, se genera uno aleatorio fuerte y se persiste en
  data/secret_key (que sobrevive a reinicios al estar en el volumen Docker).
- Los secretos sensibles (p.ej. contraseña SMTP) se cifran con Fernet usando una
  clave derivada del SECRET_KEY, de modo que el fichero de BD por sí solo no
  revela las credenciales.
"""
import os
import base64
import hashlib
import secrets

_PLACEHOLDERS = {
    "",
    "dev-secret-key-change-me",
    "cambia_esto_por_una_clave_secreta_muy_larga_y_aleatoria",
    "change-me",
}

_SECRET_PATH = os.path.join("data", "secret_key")


def load_secret_key() -> str:
    """Devuelve un SECRET_KEY robusto, generándolo y persistiéndolo si hace falta."""
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key and env_key not in _PLACEHOLDERS and len(env_key) >= 32:
        return env_key

    # No hay clave válida en el entorno → usar/crear una persistida en disco
    try:
        if os.path.exists(_SECRET_PATH):
            with open(_SECRET_PATH, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    return k
        os.makedirs("data", exist_ok=True)
        k = secrets.token_urlsafe(64)
        with open(_SECRET_PATH, "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(_SECRET_PATH, 0o600)
        except Exception:
            pass
        return k
    except Exception:
        # Último recurso: clave efímera (las sesiones no sobrevivirán al reinicio)
        return env_key or secrets.token_urlsafe(64)


SECRET_KEY = load_secret_key()


def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())
    return Fernet(key)


_ENC_PREFIX = "enc:"


def encrypt_secret(plain: str) -> str:
    """Cifra un valor para almacenarlo. Cadena vacía → cadena vacía."""
    if not plain:
        return ""
    try:
        return _ENC_PREFIX + _fernet().encrypt(plain.encode()).decode()
    except Exception:
        # Si el cifrado falla, no almacenar en claro: devolver vacío
        return ""


def decrypt_secret(stored: str) -> str:
    """Descifra un valor almacenado. Soporta valores antiguos en claro (legacy)."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored  # valor antiguo guardado en texto plano
    try:
        from cryptography.fernet import InvalidToken
        try:
            return _fernet().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
        except InvalidToken:
            return ""
    except Exception:
        return ""
