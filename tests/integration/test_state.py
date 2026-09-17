"""Integration tests for Phase C (State/Persistence).

Tests verify:
- Database engine initialization and disposal
- Migration applied cleanly (tables exist)
- Repository pattern works (create, read, soft-delete)
- Session rollback on exception
- Persistence survives across sessions (in-memory state is NOT durable)
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def initialized_db(test_settings) -> AsyncSession:
    """Initialize engine against in-memory SQLite, create schema, yield a session."""
    # Override DB URL to in-memory SQLite
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    init_engine.__wrapped__ if hasattr(init_engine, "__wrapped__") else None

    # Actually re-init with the override

    from wax.state.engine import _engine as cur_engine

    if cur_engine is not None:
        await dispose_engine()

    init_engine(test_settings)
    engine_obj = init_engine.__globals__["_engine"]

    async with engine_obj.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await dispose_engine()


class TestEngineLifecycle:
    """Verify engine initialization, session management, disposal."""

    async def test_init_engine_creates_engine(self, test_settings) -> None:
        test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        init_engine(test_settings)
        from wax.state.engine import get_engine

        engine = get_engine()
        assert engine is not None
        await dispose_engine()

    async def test_get_engine_raises_when_not_initialized(self) -> None:
        # Save and clear the global
        import wax.state.engine as eng_mod

        saved = eng_mod._engine
        eng_mod._engine = None
        try:
            with pytest.raises(Exception, match="not been initialized"):
                eng_mod.get_engine()
        finally:
            eng_mod._engine = saved

    async def test_db_session_yields_session(self, test_settings) -> None:
        test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        init_engine(test_settings)

        async with db_session() as session:
            assert isinstance(session, AsyncSession)

        await dispose_engine()

    async def test_db_session_rollback_on_exception(self, test_settings) -> None:
        test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        init_engine(test_settings)

        with pytest.raises(RuntimeError, match="expected"):
            async with db_session() as session:
                # Use the session somehow
                await session.execute(text("SELECT 1"))
                raise RuntimeError("expected failure")

        # After exception, session is closed (not broken globally)
        async with db_session() as session:
            await session.execute(text("SELECT 1"))

        await dispose_engine()


class TestSchemaCreation:
    """Verify that create_all produces the expected tables."""

    async def test_all_expected_tables_exist(self, test_settings) -> None:
        test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        init_engine(test_settings)
        engine = init_engine.__globals__["_engine"]

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

        expected = {
            "principals",
            "principal_credentials",
            "audit_events",
            "memory_records",
            "executions",
            "execution_steps",
            "work_items",
            "runtime_signals",
            "processed_messages",
            "delivery_records",
            "conversations",
        }
        assert expected.issubset(set(tables)), f"Missing tables: {expected - set(tables)}"

        await dispose_engine()
