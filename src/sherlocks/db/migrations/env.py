"""Alembic environment.

The database URL and schema come from Sherlocks settings, not from alembic.ini, so
there is exactly one place to configure a deployment.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from sherlocks.db.models import SCHEMA, Base
from sherlocks.settings import load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings()
config.set_main_option("sqlalchemy.url", settings.database.url)

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, _parent_names: dict) -> bool:
    """Confine autogenerate to our own schema.

    Without this, alembic sees cdr_report_app's ``report_jobs`` in the public schema
    of the same database and cheerfully proposes dropping it.
    """
    if type_ == "schema":
        return name == SCHEMA
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
