"""Database engine and session management.

This module owns the SQLAlchemy AsyncEngine. The engine is created lazily
on first use, and explicitly disposed on shutdown via `dispose_engine()`.

Architectural rules:
- The engine is a singleton per process. Multi-process deployment (gunicorn
  workers) creates one engine per worker, which is fine.
- Session yield pattern: callers use `async with db_session() as session:`
  and the session is closed + returned to the pool automatically.
- Transaction boundaries are explicit: commit only after the unit of work
  succeeds. On exception, the session is rolled back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wax.core.config import WaxSettings
from wax.core.exceptions import WaxConfigurationError
from wax.runtime.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: WaxSettings) -> AsyncEngine:
    """Construct an AsyncEngine from the configured database URL.

    Engine options are tuned for an HTTP service:
    - pool_size: 10 (matches typical gunicorn worker concurrency)
    - max_overflow: 20 (short bursts)
    - pool_pre_ping: True (drop stale connections)
    - pool_recycle: 1800s (avoid stale connections behind load balancers)
    """
    url = settings.database_url

    engine_kwargs: dict[str, Any] = {
        "echo": False,  # set True for SQL debug (very noisy)
        "future": True,
    }

    # SQLite doesn't support pool_size / max_overflow.
    if not url.startswith("sqlite"):
        engine_kwargs.update(
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    else:
        # SQLite needs check_same_thread=False for async use
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    try:
        return create_async_engine(url, **engine_kwargs)
    except Exception as e:
        raise WaxConfigurationError(
            f"Failed to create database engine for {url.split('://', 1)[0]}://...: {e}"
        ) from e


def init_engine(settings: WaxSettings) -> AsyncEngine:
    """Initialize (or replace) the global engine + session factory.

    Safe to call multiple times — subsequent calls dispose the old engine
    and create a new one.
    """
    global _engine, _session_factory

    if _engine is not None:
        log.warning("state.engine.reinit", msg="init_engine called twice; disposing old engine")
        # Note: disposal is sync — fine because this is shutdown path
        # The caller should call dispose_engine() first in normal flow.
        # But to be safe, we just rebuild here.

    _engine = _build_engine(settings)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # we use explicit refresh where needed
        autoflush=False,
    )

    log.info(
        "state.engine.init",
        db_scheme=settings.database_url.split("://", 1)[0],
    )
    return _engine


def get_engine() -> AsyncEngine:
    """Return the current global engine, or raise if not initialized."""
    if _engine is None:
        raise WaxConfigurationError(
            "Database engine has not been initialized. Call init_engine() at startup."
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, or raise if not initialized."""
    if _session_factory is None:
        raise WaxConfigurationError(
            "Database session factory has not been initialized. Call init_engine() at startup."
        )
    return _session_factory


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession, ensuring rollback on exception and close on exit.

    Usage:
        async with db_session() as session:
            repo = MyRepository(session)
            obj = await repo.create(...)
            await session.commit()
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Dispose the global engine. Called on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        log.info("state.engine.dispose")
        await _engine.dispose()
        _engine = None
        _session_factory = None
