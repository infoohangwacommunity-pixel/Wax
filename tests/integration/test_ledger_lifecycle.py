"""Signal-ledger lifecycle tests: deterministic, waiter-safe retention.

The event ledger is append-only by design — which means it grows forever
unless the runtime prunes it. Pruning must NEVER change wait semantics:

- a signal that a pending event-wake item can still be woken by is never
  deleted (the waiter's watermark predates the signal);
- a signal whose waiters are all gone (terminal/expired) is prunable;
- retention (age) and bounded storage (row count) are explicit policy;
- pruning is deterministic and logged (audit requirement).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from wax.authority.seed import seed_builtin_roles
from wax.runtime.services import RuntimeServices
from wax.runtime.work.repository import WorkRepository
from wax.runtime.work.signals import SignalRepository
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import RuntimeSignalRecord, WAKE_KIND_EVENT


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield
    await dispose_engine()


async def _emit(name: str, *, age_seconds: float = 0.0) -> str:
    async with db_session() as session:
        record = await SignalRepository(session).emit(
            name,
            payload={"test": True},
            emitted_by="test",
            at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
        await session.commit()
        return record.id


async def _emit_many(name: str, count: int, *, age_seconds: float = 0.0) -> None:
    async with db_session() as session:
        for i in range(count):
            await SignalRepository(session).emit(
                name,
                payload={"n": i},
                emitted_by="test",
                at=datetime.now(UTC)
                - timedelta(seconds=age_seconds)
                + timedelta(microseconds=i),
            )
        await session.commit()


async def _ledger_count() -> int:
    async with db_session() as session:
        result = await session.execute(
            select(func.count()).select_from(RuntimeSignalRecord)
        )
        return int(result.scalar_one())


async def _add_waiter(name: str, *, watermark_age_seconds: float) -> str:
    """A pending event-wake item waiting on `name` with an older watermark."""
    async with db_session() as session:
        item = await WorkRepository(session).schedule(
            kind="capability",
            payload={"capability_name": "echo", "inputs": {}},
            wake_at=datetime.now(UTC),
            principal_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            wake_kind=WAKE_KIND_EVENT,
            wake_event=name,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # The watermark was captured at schedule time (now); move it back
        # to simulate a waiter created BEFORE the signal was emitted.
        item.wake_watermark = datetime.now(UTC) - timedelta(
            seconds=watermark_age_seconds
        )
        await session.commit()
        return item.id


class TestRetentionPruning:
    async def test_old_signal_without_waiters_is_pruned(self, fresh_db) -> None:
        await _emit("external.old-event", age_seconds=40 * 86400)
        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()
        assert stats["retention_deleted"] == 1
        assert await _ledger_count() == 0

    async def test_recent_signal_is_kept(self, fresh_db) -> None:
        await _emit("external.new-event", age_seconds=60)
        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()
        assert stats["retention_deleted"] == 0
        assert await _ledger_count() == 1

    async def test_signal_needed_by_pending_waiter_is_kept(self, fresh_db) -> None:
        """The core safety property: pruning must never break a live wait."""
        name = "external.payment"
        # A waiter whose watermark PREDATES the signal — the signal can
        # still wake it, so it must survive pruning.
        await _add_waiter(name, watermark_age_seconds=40 * 86400)
        signal_id = await _emit(name, age_seconds=30 * 86400 + 3600)

        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()
        assert stats["retention_deleted"] == 0
        assert await _ledger_count() == 1

        # And the wait still fires end-to-end.
        from wax.core.config import settings_for_testing
        from wax.runtime.work.handlers import capability_handler
        from wax.runtime.work.runner import WorkRunner

        services = RuntimeServices.build(settings_for_testing())
        runner = WorkRunner(services, poll_interval_seconds=0.05)
        runner.register_handler("capability", capability_handler)
        ran = await runner.run_once()
        assert ran == 1

    async def test_signal_with_terminal_waiters_is_pruned(self, fresh_db) -> None:
        """A waiter that already finished no longer protects old signals."""
        name = "external.done-event"
        waiter_id = await _add_waiter(name, watermark_age_seconds=40 * 86400)
        await _emit(name, age_seconds=35 * 86400)
        # Terminate the waiter.
        async with db_session() as session:
            item = await WorkRepository(session).get(waiter_id)
            item.status = "succeeded"
            await session.commit()

        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()
        assert stats["retention_deleted"] == 1

    async def test_expired_waiter_releases_its_signals(self, fresh_db) -> None:
        """When the wait expires, the signals it protected become prunable."""
        name = "external.exp-event"
        waiter_id = await _add_waiter(name, watermark_age_seconds=40 * 86400)
        await _emit(name, age_seconds=35 * 86400)
        async with db_session() as session:
            item = await WorkRepository(session).get(waiter_id)
            item.status = "dead"  # expired wait died honestly
            await session.commit()

        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()
        assert stats["retention_deleted"] == 1

    async def test_waiter_created_after_prune_cannot_see_old_signals(
        self, fresh_db
    ) -> None:
        """Replay semantics after pruning: a NEW waiter (watermark=now)
        could never have been woken by pruned rows anyway — semantics
        unchanged."""
        await _emit("external.once", age_seconds=40 * 86400)
        async with db_session() as session:
            await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0, max_rows=100_000
            )
            await session.commit()

        waiter_id = await _add_waiter("external.once", watermark_age_seconds=0)
        async with db_session() as session:
            item = await WorkRepository(session).get(waiter_id)
            batch = await WorkRepository(session).claim_due(
                worker_id="w1", lease_seconds=5.0, limit=10
            )
            await session.commit()
        assert len(batch.claimed) == 0, "no retroactive wake from pruned signal"


class TestBoundedStorage:
    async def test_ledger_is_bounded_to_max_rows(self, fresh_db) -> None:
        await _emit_many("external.bulk", 50)
        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=86400.0, max_rows=30
            )
            await session.commit()
        assert stats["bound_deleted"] == 20
        assert await _ledger_count() == 30

    async def test_bound_pruning_respects_waiters(self, fresh_db) -> None:
        """Even beyond the bound, waiter-needed signals survive."""
        name = "external.needed"
        await _emit_many("external.bulk", 50)
        await _add_waiter(name, watermark_age_seconds=10)
        await _emit(name, age_seconds=5)
        async with db_session() as session:
            await SignalRepository(session).prune(
                retention_seconds=86400.0, max_rows=30
            )
            await session.commit()
        count = await _ledger_count()
        assert count == 30, "bounded to max_rows"
        async with db_session() as session:
            result = await session.execute(
                select(RuntimeSignalRecord).where(
                    RuntimeSignalRecord.name == name
                )
            )
            assert len(result.scalars().all()) == 1, (
                "the protected signal must survive the bound"
            )

    async def test_prune_is_idempotent(self, fresh_db) -> None:
        await _emit_many("external.bulk", 40)
        for _ in range(2):
            async with db_session() as session:
                await SignalRepository(session).prune(
                    retention_seconds=86400.0, max_rows=30
                )
                await session.commit()
        assert await _ledger_count() == 30


class TestMaintenanceLoop:
    async def test_maintenance_pass_expires_approvals_and_prunes(
        self, fresh_db, test_settings
    ) -> None:
        """One pass: approval expiry + ledger retention, both honest."""
        from wax.authority.approvals import ApprovalService
        from wax.runtime.maintenance import run_maintenance_pass

        async with db_session() as session:
            await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
                expires_in_seconds=-0.01,
            )
            await session.commit()
        await _emit("external.stale", age_seconds=400 * 86400)

        results = await run_maintenance_pass(test_settings)
        assert results["expired_approvals"] == 1
        assert results["signals_pruned"] >= 1
        assert await _ledger_count() == 0


class TestPruneStatsAccounting:
    async def test_ledger_total_after_is_never_negative(self, fresh_db):
        """Regression: the live probe surfaced ledger_total_after=-5.
        `total` is measured AFTER retention deletes; subtracting the
        retention count again double-counted and reported a negative
        ledger."""
        from datetime import datetime, timedelta, timezone as tz

        for i in range(7):
            await _emit(f"acc.test.{i}", age_seconds=40 * 86400)
        await _emit("acc.fresh", age_seconds=0)

        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=30 * 86400.0,
                max_rows=10_000,
            )
            await session.commit()
        assert stats["retention_deleted"] == 7
        assert stats["ledger_total_after"] == 1
        assert stats["ledger_total_after"] >= 0
