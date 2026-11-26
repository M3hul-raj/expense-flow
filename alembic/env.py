from logging.config import fileConfig
import os
from pathlib import Path

from sqlalchemy import create_engine, pool

from alembic import context

from app import normalize_database_uri
from models import db

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = db.metadata
_DEFAULT_SQLITE_PATH = (
    Path(__file__).resolve().parent.parent / "instance" / "expenses.db"
).as_posix()


def _resolve_database_url() -> str:
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    raw_url = (
        configured
        or os.environ.get("EXPENSEFLOW_DATABASE_URI", "").strip()
        or f"sqlite:///{_DEFAULT_SQLITE_PATH}"
    )
    return normalize_database_uri(raw_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = _resolve_database_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=(connection.dialect.name == "sqlite"),
                compare_type=True,
                compare_server_default=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
