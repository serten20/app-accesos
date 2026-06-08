"""
Servicio de administración y monitoreo de la base de datos.

Centraliza toda la lógica de:
  · Monitoreo de salud  (latencia, integridad, responsividad, RAM/CPU del proceso)
  · Métricas de uso     (tablas, nº de registros, tamaño en disco/por tabla)
  · Mantenimiento       (VACUUM, backup, purga de logs antiguos)
  · Estado de tamaño    (umbral configurable para alertas)

Diseñado para SQLite. Usa `sqlalchemy.inspect` para la metadata
(information_schema no existe en SQLite) y la SQLite Online Backup API
para copias de seguridad seguras con la aplicación en marcha.

Las rutas HTTP (en routers/admin.py) están protegidas con @require_admin.
"""
import os
import time
import shutil
import sqlite3
import logging
from datetime import datetime, timedelta

from sqlalchemy import inspect, text

from database import engine, SessionLocal, get_setting

logger = logging.getLogger(__name__)

# psutil es opcional — si no está instalado se omiten métricas de RAM/CPU
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:        # pragma: no cover
    _HAS_PSUTIL = False


# ── Claves de configuración (defaults) ───────────────────────────────────────
DB_DEFAULTS = {
    "db_max_size_mb":        "500",   # tamaño máximo esperado de la BD (MB)
    "db_alert_threshold_pct": "80",   # % a partir del cual alertar
    "db_alert_enabled":      "off",   # enviar email de alerta automático
    "db_purge_audit_days":   "90",    # antigüedad de logs de auditoría a purgar
    "db_auto_backup":        "off",   # backup automático diario
    "db_backup_keep":        "7",     # nº de backups a conservar
    "rotation_purge_enabled": "off",  # purga automática del histórico de rotaciones
    "rotation_purge_days":   "365",   # antigüedad a partir de la cual purgar
}

BACKUP_DIR = os.path.join("data", "backups")


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades internas
# ─────────────────────────────────────────────────────────────────────────────
def _db_path() -> str:
    """Ruta absoluta al fichero .db a partir de la URL del engine."""
    path = engine.url.database or "data/accesos.db"
    return os.path.abspath(path)


def _human_size(num_bytes: float) -> str:
    """Formatea bytes a una cadena legible (KB, MB, GB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key) or default)
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 1 · MONITOREO DE SALUD
# ─────────────────────────────────────────────────────────────────────────────
def get_db_file_info() -> dict:
    """Estado del fichero .db: ubicación, tamaño en disco, última modificación."""
    path = _db_path()
    exists = os.path.isfile(path)
    size = os.path.getsize(path) if exists else 0
    mtime = os.path.getmtime(path) if exists else None
    return {
        "path": path,
        "exists": exists,
        "size_bytes": size,
        "size_human": _human_size(size),
        "size_mb": round(size / (1024 * 1024), 2),
        "last_modified": datetime.fromtimestamp(mtime) if mtime else None,
        "engine_url": str(engine.url),
        "dialect": engine.dialect.name,
    }


def get_health_snapshot() -> dict:
    """
    Métricas de salud en tiempo real:
      · responsive       → la BD responde a un SELECT 1
      · query_latency_ms → tiempo de una consulta trivial
      · integrity_ok     → PRAGMA integrity_check
      · pool             → conexiones del pool de SQLAlchemy
      · process          → RAM/CPU del proceso (si psutil disponible)
    """
    snap = {
        "responsive": False,
        "query_latency_ms": None,
        "integrity_ok": None,
        "integrity_detail": None,
        "sqlite_version": None,
        "pool": {},
        "process": None,
        "checked_at": datetime.utcnow(),
    }

    # Latencia + responsividad
    db = SessionLocal()
    try:
        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        snap["query_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        snap["responsive"] = True

        ver = db.execute(text("SELECT sqlite_version()")).scalar()
        snap["sqlite_version"] = ver
    except Exception as e:
        logger.error("Health check: BD no responde — %s", e)
        snap["integrity_detail"] = str(e)
    finally:
        db.close()

    # Integridad (puede tardar en bases grandes → quick_check)
    if snap["responsive"]:
        db = SessionLocal()
        try:
            result = db.execute(text("PRAGMA quick_check")).scalar()
            snap["integrity_ok"] = (result == "ok")
            snap["integrity_detail"] = result
        except Exception as e:
            snap["integrity_ok"] = False
            snap["integrity_detail"] = str(e)
        finally:
            db.close()

    # Estado del pool de conexiones de SQLAlchemy
    try:
        pool = engine.pool
        snap["pool"] = {
            "size": getattr(pool, "size", lambda: "—")() if callable(getattr(pool, "size", None)) else "—",
            "checked_out": getattr(pool, "checkedout", lambda: "—")() if callable(getattr(pool, "checkedout", None)) else "—",
            "checked_in": getattr(pool, "checkedin", lambda: "—")() if callable(getattr(pool, "checkedin", None)) else "—",
            "overflow": getattr(pool, "overflow", lambda: "—")() if callable(getattr(pool, "overflow", None)) else "—",
        }
    except Exception:
        snap["pool"] = {}

    # RAM / CPU del proceso (psutil opcional)
    if _HAS_PSUTIL:
        try:
            p = psutil.Process(os.getpid())
            with p.oneshot():
                mem = p.memory_info().rss
                snap["process"] = {
                    "rss_bytes": mem,
                    "rss_human": _human_size(mem),
                    "cpu_pct": p.cpu_percent(interval=0.1),
                    "threads": p.num_threads(),
                    "system_mem_pct": psutil.virtual_memory().percent,
                }
        except Exception as e:
            logger.debug("psutil no disponible para este proceso: %s", e)

    return snap


# ─────────────────────────────────────────────────────────────────────────────
# 2 · MÉTRICAS DE USO
# ─────────────────────────────────────────────────────────────────────────────
def get_db_metrics() -> dict:
    """
    Métricas de uso de la BD usando sqlalchemy.inspect:
      · nº de tablas
      · nº de registros por tabla + total
      · espacio por tabla (vía dbstat si está disponible)
    """
    insp = inspect(engine)
    table_names = sorted(insp.get_table_names())

    # Tamaño por tabla vía dbstat (puede no estar compilado en SQLite)
    size_by_table = {}
    db = SessionLocal()
    try:
        try:
            rows = db.execute(text(
                "SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name"
            )).fetchall()
            size_by_table = {r[0]: r[1] for r in rows}
        except Exception:
            size_by_table = {}   # dbstat no disponible → solo conteos

        tables = []
        total_rows = 0
        for name in table_names:
            try:
                count = db.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0
            except Exception:
                count = 0
            total_rows += count
            sz = size_by_table.get(name)
            tables.append({
                "name": name,
                "rows": count,
                "size_bytes": sz,
                "size_human": _human_size(sz) if sz else None,
            })
    finally:
        db.close()

    # Ordenar por nº de registros (desc) para destacar las tablas grandes
    tables.sort(key=lambda t: t["rows"], reverse=True)

    return {
        "table_count": len(table_names),
        "total_rows": total_rows,
        "tables": tables,
        "dbstat_available": bool(size_by_table),
    }


def get_size_status() -> dict:
    """Estado del tamaño frente al umbral configurado (para alertas)."""
    info = get_db_file_info()
    max_mb = _cfg_int("db_max_size_mb", 500)
    threshold_pct = _cfg_int("db_alert_threshold_pct", 80)
    used_mb = info["size_mb"]
    pct = round((used_mb / max_mb) * 100, 1) if max_mb > 0 else 0
    return {
        "used_mb": used_mb,
        "max_mb": max_mb,
        "pct": pct,
        "threshold_pct": threshold_pct,
        "over_threshold": pct >= threshold_pct,
        "free_mb": round(max(0, max_mb - used_mb), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3 · MANTENIMIENTO
# ─────────────────────────────────────────────────────────────────────────────
def run_vacuum() -> dict:
    """
    Ejecuta VACUUM para recuperar espacio fragmentado.
    VACUUM no puede correr dentro de una transacción → usamos la conexión
    DBAPI cruda en modo autocommit.
    """
    size_before = get_db_file_info()["size_bytes"]
    raw = engine.raw_connection()
    try:
        old_iso = getattr(raw, "isolation_level", None)
        try:
            raw.isolation_level = None      # autocommit
        except Exception:
            pass
        cur = raw.cursor()
        cur.execute("VACUUM")
        cur.close()
        try:
            raw.isolation_level = old_iso
        except Exception:
            pass
    finally:
        raw.close()

    size_after = get_db_file_info()["size_bytes"]
    reclaimed = max(0, size_before - size_after)
    logger.info("VACUUM ejecutado — recuperados %s", _human_size(reclaimed))
    return {
        "ok": True,
        "size_before": size_before,
        "size_after": size_after,
        "size_before_human": _human_size(size_before),
        "size_after_human": _human_size(size_after),
        "reclaimed_bytes": reclaimed,
        "reclaimed_human": _human_size(reclaimed),
    }


def create_backup() -> dict:
    """
    Crea una copia de seguridad del .db usando la SQLite Online Backup API
    (segura aunque la app esté en uso). Guarda en data/backups/ y poda
    los backups antiguos según db_backup_keep.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    src_path = _db_path()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(BACKUP_DIR, f"accesos_{stamp}.db")

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)          # copia consistente
    finally:
        src.close()
        dst.close()

    size = os.path.getsize(dst_path)
    logger.info("Backup creado → %s (%s)", dst_path, _human_size(size))

    pruned = _prune_backups(_cfg_int("db_backup_keep", 7))

    # ── Copia externa (SFTP/FTP) si está activada ────────────────────────────
    remote = None
    try:
        import remote_backup
        if remote_backup.get_remote_cfg()["enabled"]:
            ok, msg = remote_backup.upload_file(dst_path)
            remote = {"ok": ok, "detail": msg}
    except Exception as e:
        logger.error("Copia externa falló: %s", e)
        remote = {"ok": False, "detail": str(e)[:160]}

    return {
        "ok": True,
        "path": dst_path,
        "filename": os.path.basename(dst_path),
        "size_bytes": size,
        "size_human": _human_size(size),
        "pruned": pruned,
        "remote": remote,
    }


def _prune_backups(keep: int) -> int:
    """Conserva solo los `keep` backups más recientes. Devuelve nº eliminados."""
    backups = list_backups()
    to_delete = backups[keep:] if keep > 0 else []
    deleted = 0
    for b in to_delete:
        try:
            os.remove(b["path"])
            deleted += 1
        except Exception:
            pass
    return deleted


def list_backups() -> list:
    """Lista los backups existentes, del más reciente al más antiguo."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for fname in os.listdir(BACKUP_DIR):
        if not fname.endswith(".db"):
            continue
        path = os.path.join(BACKUP_DIR, fname)
        try:
            st = os.stat(path)
            items.append({
                "filename": fname,
                "path": path,
                "size_bytes": st.st_size,
                "size_human": _human_size(st.st_size),
                "created": datetime.fromtimestamp(st.st_mtime),
            })
        except Exception:
            pass
    items.sort(key=lambda x: x["created"], reverse=True)
    return items


def prune_backups_now() -> int:
    """Aplica la política de retención inmediatamente (usado al guardar config)."""
    return _prune_backups(_cfg_int("db_backup_keep", 7))


# ── Validación y restauración ────────────────────────────────────────────────
SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite_file(path: str) -> bool:
    """Comprueba la cabecera mágica de un fichero SQLite."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except Exception:
        return False


def validate_sqlite(path: str) -> tuple[bool, str]:
    """Valida que `path` es una BD SQLite íntegra. Devuelve (ok, detalle)."""
    if not os.path.isfile(path):
        return False, "El fichero no existe."
    if not _is_sqlite_file(path):
        return False, "El fichero no es una base de datos SQLite válida."
    con = None
    try:
        con = sqlite3.connect(path)
        row = con.execute("PRAGMA integrity_check").fetchone()
        if row and row[0] == "ok":
            return True, "ok"
        return False, f"Fallo de integridad: {row[0] if row else 'desconocido'}"
    except Exception as e:
        return False, str(e)
    finally:
        if con:
            con.close()


def restore_from_path(source_path: str) -> dict:
    """
    Restaura la BD activa a partir de `source_path` usando la Backup API.
    SIEMPRE crea antes un backup de seguridad del estado actual.
    No reemplaza el fichero (escribe sobre la BD existente) para evitar
    problemas con descriptores/locks abiertos.
    """
    db_path = _db_path()

    # 1 · Backup de seguridad del estado ACTUAL (por si hay que revertir)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safety_path = os.path.join(BACKUP_DIR, f"pre_restore_{stamp}.db")
    try:
        src0 = sqlite3.connect(db_path)
        dst0 = sqlite3.connect(safety_path)
        try:
            with dst0:
                src0.backup(dst0)
        finally:
            src0.close()
            dst0.close()
    except Exception as e:
        return {"ok": False, "error": f"No se pudo crear el backup de seguridad: {e}"}

    # 2 · Soltar el pool de conexiones para minimizar locks
    try:
        engine.dispose()
    except Exception:
        pass

    # 3 · Restaurar: source → BD activa
    try:
        src = sqlite3.connect(source_path)
        dst = sqlite3.connect(db_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
    except Exception as e:
        return {
            "ok": False,
            "error": f"Error al restaurar: {e}",
            "safety_backup": os.path.basename(safety_path),
        }

    # 4 · Nuevo pool limpio contra la BD restaurada
    try:
        engine.dispose()
    except Exception:
        pass

    logger.warning("BD restaurada desde %s (seguridad: %s)", source_path, safety_path)
    return {"ok": True, "safety_backup": os.path.basename(safety_path)}


def restore_backup(filename: str) -> dict:
    """Restaura desde un backup existente en data/backups/ (valida nombre)."""
    safe = os.path.basename(filename)          # evita path traversal
    path = os.path.join(BACKUP_DIR, safe)
    if not os.path.isfile(path):
        return {"ok": False, "error": "Backup no encontrado."}
    ok, detail = validate_sqlite(path)
    if not ok:
        return {"ok": False, "error": detail}
    res = restore_from_path(path)
    res["restored_from"] = safe
    return res


def import_db_file(tmp_path: str) -> dict:
    """Valida y restaura desde un fichero .db subido por el usuario."""
    ok, detail = validate_sqlite(tmp_path)
    if not ok:
        return {"ok": False, "error": detail}
    return restore_from_path(tmp_path)


def purge_audit_logs(days: int | None = None, keep_last: int | None = None) -> dict:
    """
    Purga registros antiguos de auditoría.
      · days      → elimina los anteriores a hoy-`days`
      · keep_last → conserva solo los `keep_last` más recientes
    Si se pasan ambos, se aplican los dos criterios.
    """
    from models import AuditLog

    db = SessionLocal()
    deleted = 0
    try:
        if days is not None and days > 0:
            cutoff = datetime.utcnow() - timedelta(days=days)
            deleted += db.query(AuditLog).filter(AuditLog.timestamp < cutoff).delete(
                synchronize_session=False
            )

        if keep_last is not None and keep_last > 0:
            # IDs a conservar (los más recientes)
            keep_ids = [
                row.id for row in
                db.query(AuditLog.id).order_by(AuditLog.timestamp.desc()).limit(keep_last).all()
            ]
            if keep_ids:
                deleted += db.query(AuditLog).filter(
                    ~AuditLog.id.in_(keep_ids)
                ).delete(synchronize_session=False)

        db.commit()
        logger.info("Purga de auditoría: %d registros eliminados", deleted)
    except Exception as e:
        db.rollback()
        logger.error("Error en purga de auditoría: %s", e)
        return {"ok": False, "deleted": 0, "error": str(e)}
    finally:
        db.close()

    return {"ok": True, "deleted": deleted}


def purge_rotation_history(days: int) -> dict:
    """Purga el histórico de rotaciones anterior a hoy-`days`. days<=0 → no hace nada."""
    if days <= 0:
        return {"ok": True, "deleted": 0}
    from models import RotationHistory
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = db.query(RotationHistory).filter(
            RotationHistory.confirmed_at < cutoff).delete(synchronize_session=False)
        db.commit()
        if deleted:
            logger.info("Purga de histórico de rotaciones: %d registros (>%dd)", deleted, days)
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        db.rollback()
        logger.error("Error en purga de histórico de rotaciones: %s", e)
        return {"ok": False, "deleted": 0, "error": str(e)}
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4 · ESTADO AGREGADO (para el dashboard) + ALERTAS
# ─────────────────────────────────────────────────────────────────────────────
def full_status() -> dict:
    """Reúne toda la información para pintar el panel de una sola vez."""
    return {
        "file": get_db_file_info(),
        "health": get_health_snapshot(),
        "metrics": get_db_metrics(),
        "size": get_size_status(),
        "backups": list_backups(),
        "config": {k: get_setting(k) or v for k, v in DB_DEFAULTS.items()},
        "has_psutil": _HAS_PSUTIL,
    }


def status_json() -> dict:
    """
    Versión serializable (sin objetos datetime) de los datos de monitoreo,
    pensada para el endpoint de auto-refresco del panel.
    """
    st = full_status()
    file = st["file"]
    health = st["health"]
    return {
        "file": {
            "size_human": file["size_human"],
            "size_mb": file["size_mb"],
            "last_modified": file["last_modified"].strftime("%d/%m/%Y %H:%M:%S") if file["last_modified"] else "—",
        },
        "health": {
            "responsive": health["responsive"],
            "query_latency_ms": health["query_latency_ms"],
            "integrity_ok": health["integrity_ok"],
            "sqlite_version": health["sqlite_version"],
            "pool": health["pool"],
            "process": health["process"],   # ya es serializable o None
        },
        "metrics": {
            "table_count": st["metrics"]["table_count"],
            "total_rows": st["metrics"]["total_rows"],
            "dbstat_available": st["metrics"]["dbstat_available"],
            "tables": st["metrics"]["tables"],   # dicts simples
        },
        "size": st["size"],
        "server_time": datetime.now().strftime("%H:%M:%S"),
    }


def evaluate_alerts() -> dict | None:
    """
    Evalúa condiciones de alerta. Devuelve un dict con el problema detectado
    o None si todo está correcto. Usado por el job del scheduler.
    """
    problems = []

    health = get_health_snapshot()
    if not health["responsive"]:
        problems.append("La base de datos NO responde a las consultas.")
    if health.get("integrity_ok") is False:
        problems.append(f"Fallo de integridad: {health.get('integrity_detail')}")

    size = get_size_status()
    if size["over_threshold"]:
        problems.append(
            f"Almacenamiento al {size['pct']}% del límite "
            f"({size['used_mb']} MB de {size['max_mb']} MB)."
        )

    if not problems:
        return None
    return {
        "problems": problems,
        "size": size,
        "health": health,
    }
