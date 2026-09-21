"""
Alembic environment script — async Postgres.
"""

import asyncio
import os
from logging.config import fileConfig
from dotenv import load_dotenv

load_dotenv()

from alembic import context  # type: ignore[attr-defined]
from sqlalchemy.ext.asyncio import create_async_engine

# Import all models so Alembic autogenerate picks up the full schema.
from app.database import Base
import app.models  # noqa: F401
from app.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # Prefer DATABASE_URL env var, fall back to config settings
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            url = get_settings().database_url
        except Exception:
            pass
    return url or "sqlite+aiosqlite:///talos_cloud.db"



def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
