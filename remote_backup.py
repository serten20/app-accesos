"""
Copia externa de backups a un destino remoto (SFTP o FTP/FTPS).

Tras crear un backup local (db_admin.create_backup), si la copia externa está
activada, se sube el fichero al destino remoto y se aplica retención remota.
La contraseña se guarda cifrada (módulo crypto), igual que el SMTP.
"""
import os
import ftplib
import logging
from datetime import datetime

from database import get_setting, set_setting
from crypto import decrypt_secret

logger = logging.getLogger("accesos")

REMOTE_DEFAULTS = {
    "remote_backup_enabled":  "off",
    "remote_backup_protocol": "sftp",   # sftp | ftp
    "remote_backup_host":     "",
    "remote_backup_port":     "",        # vacío → 22 (sftp) / 21 (ftp)
    "remote_backup_user":     "",
    "remote_backup_path":     "",        # carpeta remota destino
    "remote_backup_tls":      "off",     # FTP con TLS (FTPS)
    "remote_backup_keep":     "14",      # nº de backups a conservar en remoto
}

BACKUP_PREFIX = "accesos_"


def get_remote_cfg() -> dict:
    cfg = {k: (get_setting(k) or v) for k, v in REMOTE_DEFAULTS.items()}
    cfg["enabled"] = cfg["remote_backup_enabled"] == "on"
    cfg["tls"] = cfg["remote_backup_tls"] == "on"
    proto = cfg["remote_backup_protocol"]
    try:
        cfg["port"] = int(cfg["remote_backup_port"]) if cfg["remote_backup_port"] else (22 if proto == "sftp" else 21)
    except (ValueError, TypeError):
        cfg["port"] = 22 if proto == "sftp" else 21
    try:
        cfg["keep"] = max(1, int(cfg["remote_backup_keep"]))
    except (ValueError, TypeError):
        cfg["keep"] = 14
    cfg["password"] = decrypt_secret(get_setting("remote_backup_pass") or "")
    return cfg


def _record_status(ok: bool, msg: str):
    set_setting("remote_backup_last_status", "ok" if ok else "error")
    set_setting("remote_backup_last_detail", (msg or "")[:300])
    set_setting("remote_backup_last_at", datetime.utcnow().isoformat())


# ── SFTP (paramiko) ──────────────────────────────────────────────────────────
def _sftp_client(cfg):
    import paramiko
    transport = paramiko.Transport((cfg["remote_backup_host"], cfg["port"]))
    transport.connect(username=cfg["remote_backup_user"], password=cfg["password"])
    return paramiko.SFTPClient.from_transport(transport), transport


def _sftp_upload(cfg, local_path):
    sftp, transport = _sftp_client(cfg)
    try:
        remote_dir = cfg["remote_backup_path"] or "."
        fname = os.path.basename(local_path)
        remote_path = remote_dir.rstrip("/") + "/" + fname if remote_dir != "." else fname
        sftp.put(local_path, remote_path)
        # Retención remota: conservar los `keep` más recientes con nuestro prefijo
        try:
            files = [f for f in sftp.listdir(remote_dir) if f.startswith(BACKUP_PREFIX) and f.endswith(".db")]
            files.sort(reverse=True)   # nombres con timestamp → orden cronológico
            for old in files[cfg["keep"]:]:
                sftp.remove(remote_dir.rstrip("/") + "/" + old if remote_dir != "." else old)
        except Exception as e:
            logger.warning("Retención remota SFTP no aplicada: %s", e)
        return True, f"Subido a {remote_path} (SFTP)"
    finally:
        try: sftp.close()
        except Exception: pass
        try: transport.close()
        except Exception: pass


def _sftp_test(cfg):
    """Diagnóstico SFTP: conexión, servidor, listado de carpeta y prueba de
    escritura real. Devuelve (ok, detalle)."""
    sftp, transport = _sftp_client(cfg)
    steps = [f"Conectado a {cfg['remote_backup_host']}:{cfg['port']} (SFTP)"]
    try:
        try:
            ver = transport.remote_version
            if ver:
                steps.append(f"Servidor: {ver}")
        except Exception:
            pass
        remote_dir = cfg["remote_backup_path"] or "."
        entries = sftp.listdir(remote_dir)
        steps.append(f"Carpeta '{remote_dir}': {len(entries)} entrada(s)")
        marker = (remote_dir.rstrip("/") + "/.accesos_conn_test") if remote_dir != "." else ".accesos_conn_test"
        try:
            with sftp.open(marker, "w") as fh:
                fh.write("ok")
            sftp.remove(marker)
            steps.append("Permiso de escritura: OK")
            return True, " · ".join(steps)
        except Exception as e:
            steps.append(f"Permiso de escritura: FALLO ({type(e).__name__}: {e})")
            steps.append("⚠ Los backups NO podrían guardarse en esta carpeta")
            return False, " · ".join(steps)
    finally:
        try: sftp.close()
        except Exception: pass
        try: transport.close()
        except Exception: pass


# ── FTP / FTPS (ftplib) ──────────────────────────────────────────────────────
def _ftp_connect(cfg):
    if cfg["tls"]:
        ftp = ftplib.FTP_TLS()
        ftp.connect(cfg["remote_backup_host"], cfg["port"], timeout=20)
        ftp.login(cfg["remote_backup_user"], cfg["password"])
        ftp.prot_p()          # cifrar también el canal de datos
    else:
        ftp = ftplib.FTP()
        ftp.connect(cfg["remote_backup_host"], cfg["port"], timeout=20)
        ftp.login(cfg["remote_backup_user"], cfg["password"])
    if cfg["remote_backup_path"]:
        ftp.cwd(cfg["remote_backup_path"])
    return ftp


def _ftp_upload(cfg, local_path):
    ftp = _ftp_connect(cfg)
    try:
        fname = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {fname}", f)
        try:
            names = [n for n in ftp.nlst() if n.startswith(BACKUP_PREFIX) and n.endswith(".db")]
            names.sort(reverse=True)
            for old in names[cfg["keep"]:]:
                try: ftp.delete(old)
                except Exception: pass
        except Exception as e:
            logger.warning("Retención remota FTP no aplicada: %s", e)
        kind = "FTPS" if cfg["tls"] else "FTP"
        return True, f"Subido {fname} ({kind})"
    finally:
        try: ftp.quit()
        except Exception:
            try: ftp.close()
            except Exception: pass


def _ftp_test(cfg):
    """Diagnóstico FTP/FTPS: conexión, banner, directorio, listado y prueba de
    escritura real. Devuelve (ok, detalle)."""
    import io
    kind = "FTPS" if cfg["tls"] else "FTP"
    ftp = _ftp_connect(cfg)
    steps = [f"Conectado a {cfg['remote_backup_host']}:{cfg['port']} ({kind})"]
    try:
        try:
            welcome = (ftp.getwelcome() or "").strip().replace("\n", " ")
            if welcome:
                steps.append(f"Servidor: {welcome[:80]}")
        except Exception:
            pass
        try:
            steps.append(f"Directorio actual: {ftp.pwd()}")
        except Exception:
            pass
        try:
            steps.append(f"{len(ftp.nlst())} entrada(s) en la carpeta")
        except Exception:
            pass
        try:
            ftp.storbinary("STOR .accesos_conn_test", io.BytesIO(b"ok"))
            try: ftp.delete(".accesos_conn_test")
            except Exception: pass
            steps.append("Permiso de escritura: OK")
            return True, " · ".join(steps)
        except Exception as e:
            steps.append(f"Permiso de escritura: FALLO ({type(e).__name__}: {e})")
            steps.append("⚠ Los backups NO podrían guardarse en esta carpeta")
            return False, " · ".join(steps)
    finally:
        try: ftp.quit()
        except Exception:
            try: ftp.close()
            except Exception: pass


# ── API pública ──────────────────────────────────────────────────────────────
def _missing(cfg) -> str | None:
    if not cfg["remote_backup_host"]:
        return "Falta el host remoto."
    if not cfg["remote_backup_user"]:
        return "Falta el usuario remoto."
    if not cfg["password"]:
        return "Falta la contraseña remota."
    return None


def upload_file(local_path: str) -> tuple[bool, str]:
    """Sube un fichero al destino remoto configurado. Registra el estado."""
    cfg = get_remote_cfg()
    if not cfg["enabled"]:
        return False, "Copia externa desactivada"
    miss = _missing(cfg)
    if miss:
        _record_status(False, miss)
        return False, miss
    try:
        if cfg["remote_backup_protocol"] == "sftp":
            ok, msg = _sftp_upload(cfg, local_path)
        else:
            ok, msg = _ftp_upload(cfg, local_path)
        _record_status(ok, msg)
        if ok:
            logger.info("Backup replicado a remoto: %s", msg)
        return ok, msg
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _record_status(False, msg)
        logger.error("Error subiendo backup a remoto: %s", msg)
        return False, msg


def _record_remote_test(ok: bool, detail: str):
    """Guarda el resultado del último diagnóstico de la copia externa."""
    try:
        set_setting("remote_backup_test_status", "ok" if ok else "error")
        set_setting("remote_backup_test_detail", (detail or "")[:600])
        set_setting("remote_backup_test_at", datetime.utcnow().isoformat())
    except Exception:
        pass


def test_connection() -> tuple[bool, str]:
    """Prueba la conexión remota SIN subir un backup real (usa un fichero marcador
    diminuto que borra al instante). Registra el resultado para mostrarlo."""
    cfg = get_remote_cfg()
    miss = _missing(cfg)
    if miss:
        _record_remote_test(False, miss)
        return False, miss
    try:
        if cfg["remote_backup_protocol"] == "sftp":
            ok, detail = _sftp_test(cfg)
        else:
            ok, detail = _ftp_test(cfg)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _record_remote_test(ok, detail)
    return ok, detail
