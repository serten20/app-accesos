import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/accesos.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_alembic(action: str, revision: str = "head"):
    """Ejecuta un comando de Alembic programáticamente (upgrade/stamp)."""
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    getattr(command, action)(cfg, revision)


def init_database():
    """Inicializa el esquema de forma segura, integrando Alembic sin romper
    instalaciones existentes:

      · BD nueva (sin tablas)         → alembic upgrade head (crea todo del baseline).
      · BD existente sin alembic      → create_all + run_migrations + alembic stamp head.
      · BD ya gestionada por alembic  → alembic upgrade head (aplica migraciones nuevas).

    Si Alembic fallara por cualquier motivo, cae al método clásico
    (create_all + run_migrations) para que la app siempre arranque.
    """
    import models  # noqa: F401  asegura el registro de todas las tablas en Base.metadata
    from sqlalchemy import inspect
    import logging
    log = logging.getLogger("accesos")

    try:
        insp = inspect(engine)
        has_core = insp.has_table("users")
        has_alembic = insp.has_table("alembic_version")

        if not has_core:
            # Instalación nueva: Alembic construye el esquema baseline…
            _run_alembic("upgrade", "head")
            # …create_all añade tablas posteriores al baseline (email_logs, etc.)…
            Base.metadata.create_all(bind=engine)
            # …y run_migrations añade las columnas posteriores al baseline
            # (also_technician, onboarding_*, etc.). Todo idempotente.
            run_migrations()
            log.info("BD nueva creada vía Alembic (upgrade head) + create_all + run_migrations")
        else:
            # Instalación existente: garantizar esquema con el método clásico…
            Base.metadata.create_all(bind=engine)
            run_migrations()
            # …y poner la BD bajo control de Alembic
            if not has_alembic:
                _run_alembic("stamp", "head")
                log.info("BD existente marcada como baseline de Alembic (stamp head)")
            else:
                _run_alembic("upgrade", "head")
                log.info("Migraciones de Alembic aplicadas (upgrade head)")
    except Exception as e:
        # Fallback robusto: nunca impedir el arranque por un problema de migración
        log.error("Init Alembic falló (%s). Fallback a create_all + run_migrations.", e)
        Base.metadata.create_all(bind=engine)
        run_migrations()


def run_migrations():
    """Aplica migraciones manuales para columnas/tablas nuevas en esquemas existentes."""
    migrations = [
        "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0 NOT NULL",
        "ALTER TABLE companies ADD COLUMN doc_url VARCHAR(512)",
        "ALTER TABLE companies ADD COLUMN alert_last_sent DATETIME",
        "ALTER TABLE audit_logs ADD COLUMN level VARCHAR(16) DEFAULT 'info'",
        "ALTER TABLE audit_logs ADD COLUMN ip_address VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN tokens_valid_from DATETIME",
        # Confirmación por técnico: cada par (técnico, empresa) tiene su propio estado
        "ALTER TABLE technician_company ADD COLUMN last_changed DATE",
        "ALTER TABLE technician_company ADD COLUMN alert_last_sent DATETIME",
        "UPDATE technician_company SET last_changed = (SELECT last_changed FROM companies WHERE companies.id = technician_company.company_id) WHERE last_changed IS NULL",
        # Escalado automático de alertas
        "ALTER TABLE technician_company ADD COLUMN alert_count INTEGER DEFAULT 0",
        "ALTER TABLE technician_company ADD COLUMN escalated_at DATETIME",
        # Doble rol: admin que también es técnico
        "ALTER TABLE users ADD COLUMN also_technician BOOLEAN DEFAULT 0",
        # Wizard de bienvenida (onboarding) por usuario
        "ALTER TABLE users ADD COLUMN onboarding_admin_done BOOLEAN DEFAULT 0",
        "ALTER TABLE users ADD COLUMN onboarding_tech_done BOOLEAN DEFAULT 0",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # columna/tabla ya existe

        # ── Migración especial: users v2 ─────────────────────────────────────
        # Hace email nullable y añade receive_reports.
        # SQLite no permite ALTER COLUMN, hay que recrear la tabla.
        _migrate_users_v2(conn)


def _migrate_users_v2(conn):
    """Recrea la tabla users para: email nullable + columna receive_reports."""
    # Detectar si ya está migrada comprobando si receive_reports existe
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
    if "receive_reports" in cols:
        return  # ya migrada

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("""
        CREATE TABLE users_v2 (
            id                   INTEGER NOT NULL PRIMARY KEY,
            username             VARCHAR(64)  NOT NULL UNIQUE,
            email                VARCHAR(128) UNIQUE,
            hashed_password      VARCHAR(256) NOT NULL,
            role                 VARCHAR(16),
            is_active            BOOLEAN,
            must_change_password BOOLEAN NOT NULL DEFAULT 0,
            receive_reports      BOOLEAN NOT NULL DEFAULT 0,
            created_at           DATETIME,
            tokens_valid_from    DATETIME
        )
    """))
    conn.execute(text("""
        INSERT INTO users_v2
            (id, username, email, hashed_password, role, is_active,
             must_change_password, receive_reports, created_at, tokens_valid_from)
        SELECT id, username, email, hashed_password, role, is_active,
               must_change_password, 0, created_at, tokens_valid_from
        FROM users
    """))
    conn.execute(text("DROP TABLE users"))
    conn.execute(text("ALTER TABLE users_v2 RENAME TO users"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.commit()


def get_setting(key: str, default: str = "") -> str:
    """Lee un valor de configuración de la BD."""
    db = SessionLocal()
    try:
        from models import AppSettings
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        return row.value if row and row.value is not None else default
    finally:
        db.close()


def set_setting(key: str, value: str):
    """Guarda un valor de configuración en la BD."""
    db = SessionLocal()
    try:
        from models import AppSettings
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSettings(key=key, value=value))
        db.commit()
    finally:
        db.close()
