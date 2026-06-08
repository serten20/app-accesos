"""Entorno de Alembic.

Se integra con la app:
  · Toma la URL de la BD de DATABASE_URL (igual que database.py).
  · target_metadata = Base.metadata (de models.py) para autogenerar.
  · render_as_batch=True → imprescindible en SQLite para soportar ALTER
    (SQLite no permite ALTER COLUMN/DROP COLUMN nativo; Alembic lo emula
    recreando la tabla en modo "batch").
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Asegurar que la raíz del proyecto está en el path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Importar metadata de la app
from database import DATABASE_URL  # noqa: E402
from models import Base  # noqa: E402
import models  # noqa: F401,E402  (asegura el registro de todos los modelos)

config = context.config

# URL real de la BD (prioriza DATABASE_URL del entorno)
config.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL", DATABASE_URL))

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Migraciones en modo 'offline' (genera SQL sin conexión)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Migraciones en modo 'online' (con conexión a la BD)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly ALTER
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
