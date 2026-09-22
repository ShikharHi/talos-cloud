"""
Talos Cloud — async database session factory.
Uses SQLAlchemy 2.0 async engine backed by asyncpg (Postgres).

Why Postgres and not SQLite:
  The relay's atomic pre-check-and-debit relies on Postgres's row-level
  locking semantics for:
    UPDATE accounts
    SET balance_credits = balance_credits - :worst_case
    WHERE account_id = :id AND balance_credits >= :worst_case
    RETURNING balance_credits;
  SQLite's WAL-mode write-locking under concurrent connections has different
  concurrency guarantees and would require different correctness reasoning.
  Postgres from the start avoids silent behavioural changes on "migrate later."
"""

from collections.abc import AsyncGenerator
from typing import Any
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base. Importable without needing DATABASE_URL."""
    pass


# Patch asyncpg do_terminate to gracefully terminate when client cancels request
try:
    import asyncio
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

    _orig_do_terminate = PGDialect_asyncpg.do_terminate

    def _safe_do_terminate(self, dbapi_connection):
        try:
            _orig_do_terminate(self, dbapi_connection)
        except (asyncio.CancelledError, GeneratorExit, Exception):
            try:
                if hasattr(dbapi_connection, "_connection") and dbapi_connection._connection:
                    dbapi_connection._connection.terminate()
            except Exception:
                pass

    PGDialect_asyncpg.do_terminate = _safe_do_terminate
except Exception:
    pass


# Lazy engine — only created when get_engine() is first called.
# This keeps model imports cheap at test collection time when DATABASE_URL
# may not be set in unit tests that don't touch the database.
_engine: AsyncEngine | None = None


def _normalize_database_url(database_url: str) -> str:
    """Translate hosted Postgres URL options into asyncpg-compatible settings."""
    url = make_url(database_url)
    if url.drivername != "postgresql+asyncpg":
        return database_url

    query = dict(url.query)
    query.pop("channel_binding", None)
    query.pop("sslmode", None)
    return url.set(query=query).render_as_string(hide_password=False)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        from app.config import get_settings
        settings = get_settings()
        url = _normalize_database_url(settings.database_url)
        database_url = make_url(settings.database_url)
        import os
        kwargs: dict = {
            "pool_pre_ping": True,
            "echo": os.environ.get("TALOS_DB_ECHO", "false").lower() == "true",
        }
        if url.startswith("postgresql"):
            if settings.talos_env in ("test", "testing"):
                from sqlalchemy.pool import NullPool
                kwargs["poolclass"] = NullPool
            else:
                kwargs["pool_pre_ping"] = True
                kwargs["pool_size"] = 10
                kwargs["max_overflow"] = 20
                kwargs["pool_timeout"] = 30
                kwargs["pool_recycle"] = 1800  # 30 minutes to recycle before Neon serverless drops idle connections
                kwargs["connect_args"] = {
                    "command_timeout": 60,
                    "server_settings": {"application_name": "talos_cloud"},
                }
                sslmode = database_url.query.get("sslmode")
                if sslmode in {"require", "verify-ca", "verify-full"}:
                    kwargs["connect_args"]["ssl"] = True
        elif url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(url, **kwargs)
    return _engine


async def check_db_health() -> dict[str, Any]:
    """Validates DB connectivity with a lightweight SELECT 1 query."""
    import time
    from sqlalchemy import text

    factory = get_session_factory()
    t0 = time.perf_counter()
    try:
        async with factory() as session:
            result = await session.execute(text("SELECT 1"))
            val = result.scalar()
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            if val == 1:
                return {"status": "healthy", "latency_ms": latency_ms}
            return {"status": "unhealthy", "error": f"Unexpected result: {val}"}
    except Exception as e:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"status": "unhealthy", "latency_ms": latency_ms, "error": str(e)}


TRANSIENT_DB_ERRORS = (
    "ConnectionDoesNotExistError",
    "CannotConnectNowError",
    "ConnectionResetError",
    "server closed the connection unexpectedly",
    "the database system is starting up",
    "terminating connection due to administrator command",
    "connection is closed",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """Checks if an exception is a transient connection drop from serverless Postgres."""
    err_str = f"{type(exc).__name__}: {str(exc)}".lower()
    return any(t.lower() in err_str for t in TRANSIENT_DB_ERRORS)




def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a database session per request."""
    import asyncio
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            if session.is_active:
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
        except (asyncio.CancelledError, GeneratorExit):
            try:
                if session.is_active:
                    await session.rollback()
            except Exception:
                pass
            raise
        except Exception:
            try:
                if session.is_active:
                    await session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                await session.close()
            except Exception:
                pass
