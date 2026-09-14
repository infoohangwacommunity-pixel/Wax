"""Alembic migrations environment for WAX.

Reads the database URL from the WAX_DATABASE_URL environment variable, so
the same alembic config works across dev / staging / production.

Run migrations:
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "description"
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make wax.* importable when running `alembic` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import wax.reliability.dead_letter
import wax.state.approval_models
import wax.state.artifact_models
import wax.state.audit_models
import wax.state.authority_models
import wax.state.bridge_models
import wax.state.capability_models
import wax.state.continuity_models
import wax.state.credential_models
import wax.state.delivery_models
import wax.state.environment_models
import wax.state.execution_models

# Import all model modules here so they register with Base.metadata.
# As new models are added in wax.state.* subpackages, import them here too.
import wax.state.identity_models
import wax.state.memory_models
import wax.state.objective_models
import wax.state.provisioning_models
import wax.state.terminal_models
import wax.state.work_models  # noqa: F401  (Phase R/V durable work)
from wax.core.config import WaxSettings
from wax.state.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the database URL from environment / WaxSettings.
# Priority: alembic.ini [default] < WAX_DATABASE_URL env var < WaxSettings.
settings = WaxSettings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout.

    Useful for reviewing what migrations would do without applying them:
        alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """Run migrations in 'online' async mode against the configured DB."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
