"""Multi-worker / durable-runtime hardening tests.

The single-instance worker was an honest deployment simplification, but the
correctness mechanisms underneath it — atomic claiming, lease fencing,
heartbeats, reclaim recovery, graceful shutdown — are runtime guarantees,
not deployment details. These tests prove them under adverse conditions:

- a second runner cannot claim an item another live runner owns
- a zombie worker whose lease was reclaimed CANNOT write its result
- a healthy slow worker's heartbeat protects it from spurious reclaim
- a worker that dies mid-run has its work reclaimed and finished
- reclaim-exhausted items die LOUDLY (dead-letter row + work.dead signal)
- expired waits are announced (work.expired signal)
- graceful shutdown lets in-flight work finish

Concurrency note: SQLite (:memory: via StaticPool, or file) serializes
writers and has no row locks, so tests interleave claim passes explicitly
instead of racing two coroutines. On PostgreSQL the claim query's
FOR UPDATE SKIP LOCKED row locks give the same exclusion under true
concurrent polling — the claim SQL is identical in both cases.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.seed import ensure_principal_role, seed_builtin_roles
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRepository, WorkRunner, capability_handler
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import RuntimeSignalRecord, WorkItemRecord

TEST_PRINCIPAL = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await ensure_principal_role(session, TEST_PRINCIPAL)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


def _runner(services: RuntimeServices, **kw) -> WorkRunner:
    defaults = dict(
        poll_interval_seconds=0.05,
        lease_seconds=0.4,
        retry_backoff_seconds=0.0,
        stale_execution_seconds=900.0,
    )
    defaults.update(kw)
    wr = WorkRunner(services, **defaults)
    wr.register_handler("capability", capability_handler)
    return wr


async def _schedule(
    principal_id: str | None = TEST_PRINCIPAL,
    *,
    kind: str = "capability",
    capability: str = "echo",
    inputs: dict | None = None,
    max_attempts: int = 3,
    expires_in: float | None = None,
    delay: float = 0.0,
) -> str:
    """Insert a work item directly (repo level — the unit under test is the
    runner, not the capability surface)."""
    async with db_session() as session:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
        item = await WorkRepository(session).schedule(
            kind=kind,
            payload={"capability_name": capability, "inputs": inputs or {}},
            wake_at=datetime.now(UTC) + timedelta(seconds=delay),
            principal_id=principal_id,
            max_attempts=max_attempts,
            expires_at=expires_at,
        )
        await session.commit()
        return item.id


async def _work(work_id: str) -> WorkItemRecord | None:
    async with db_session() as session:
        item = await WorkRepository(session).get(work_id)
        if item is None:
            return None
        _ = (item.status, item.attempts, item.last_error, item.result, item.lease_owner)
        session.expunge(item)
        return item


async def _signal_count(name_prefix: str) -> int:
    async with db_session() as session:
        result = await session.execute(
            select(RuntimeSignalRecord).where(RuntimeSignalRecord.name.like(f"{name_prefix}%"))
        )
        return len(result.scalars().all())


class TestExclusiveClaiming:
    async def test_second_runner_cannot_claim_leased_item(self, fresh_db, services) -> None:
        """The multi-worker guarantee: an item claimed by a live runner is
        invisible to every other runner's claim pass."""
        work_id = await _schedule()

        runner_a = _runner(services)
        # A claims (and processes) the item.
        ran_a = await runner_a.run_once()
        assert ran_a == 1

        # B's claim pass finds nothing claimable — the item is terminal.
        runner_b = _runner(services)
        ran_b = await runner_b.run_once()
        assert ran_b == 0
        item = await _work(work_id)
        assert item.status == "succeeded"
        assert item.attempts == 1, "ran exactly once, never double-claimed"

    async def test_claimed_item_not_visible_while_leased(self, fresh_db, services) -> None:
        """Between claim and completion, no other runner can take the item."""
        work_id = await _schedule()
        runner_a = _runner(services, lease_seconds=5.0)

        # Manually lease the item as runner A (the claim step only).
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=5.0, limit=10
            )
            assert len(batch.claimed) == 1
            await session.commit()

        # Another worker's claim pass: nothing to claim (lease is live).
        runner_b = _runner(services)
        assert await runner_b.run_once() == 0
        item = await _work(work_id)
        assert item.status == "leased"
        assert item.lease_owner == runner_a._worker_id

    async def test_many_items_each_run_exactly_once(self, fresh_db, services) -> None:
        """10 items across alternating runner passes: no item runs twice."""
        ran: list[str] = []

        async def _spy(services, item) -> dict:
            ran.append(item.id)
            return {"ok": True}

        ids = [await _schedule(kind="spy") for _ in range(10)]
        runner_a = _runner(services)
        runner_b = _runner(services)
        runner_a._handlers["spy"] = _spy
        runner_b._handlers["spy"] = _spy

        for _ in range(4):
            await runner_a.run_once()
            await runner_b.run_once()
        assert sorted(ran) == sorted(ids)


class TestFencing:
    async def test_zombie_worker_cannot_write_while_reclaimed(self, fresh_db, services) -> None:
        """Worker A claims, its lease expires, worker B reclaims; A's late
        result must be REFUSED (fenced) while B's attempt is authoritative."""
        work_id = await _schedule(kind="stuck", max_attempts=5)
        runner_a = _runner(services, lease_seconds=0.15)

        # A claims the item (claim step only, simulating a long handler).
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=0.15, limit=10
            )
            assert len(batch.claimed) == 1
            await WorkRepository(session).mark_running(work_id, expected_owner=runner_a._worker_id)
            await session.commit()

        # A's lease expires. B reclaims (and is now the authoritative owner).
        await asyncio.sleep(0.2)
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id="worker-ZZZ", lease_seconds=5.0, limit=10
            )
            assert len(batch.claimed) == 1
            await session.commit()

        # Zombie A finally finishes — its write must be fenced mid-run.
        async with db_session() as session:
            status = await WorkRepository(session).mark_succeeded(
                work_id,
                {"from": "zombie"},
                expected_owner=runner_a._worker_id,
            )
            await session.commit()
        assert status == "fenced"
        item = await _work(work_id)
        assert item.lease_owner == "worker-ZZZ", "authoritative attempt untouched"

        # B (the authoritative owner) completes — its result is the one that lands.
        async with db_session() as session:
            status = await WorkRepository(session).mark_succeeded(
                work_id, {"from": "fast-worker"}, expected_owner="worker-ZZZ"
            )
            await session.commit()
        assert status == "succeeded"
        item = await _work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"from": "fast-worker"}, "zombie result discarded"

    async def test_zombie_write_after_completion_is_refused(self, fresh_db, services) -> None:
        """Even after the item is terminal, a zombie write changes nothing."""
        work_id = await _schedule(kind="stuck", max_attempts=5)
        runner_a = _runner(services, lease_seconds=0.15)
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=0.15, limit=10
            )
            assert len(batch.claimed) == 1
            await session.commit()

        await asyncio.sleep(0.2)
        runner_b = _runner(services, lease_seconds=5.0)

        async def _fast(services, item) -> dict:
            return {"from": "fast-worker"}

        runner_b.register_handler("stuck", _fast)
        await runner_b.run_once()
        item = await _work(work_id)
        assert item.result == {"from": "fast-worker"}

        async with db_session() as session:
            status = await WorkRepository(session).mark_succeeded(
                work_id, {"from": "zombie"}, expected_owner=runner_a._worker_id
            )
            await session.commit()
        assert status in ("fenced", "stale")  # refused either way
        assert (await _work(work_id)).result == {"from": "fast-worker"}

    async def test_zombie_failure_is_discarded(self, fresh_db, services) -> None:
        """A zombie's failure report must not kill the authoritative attempt."""
        work_id = await _schedule(kind="boom", max_attempts=5)
        runner_a = _runner(services, lease_seconds=0.15)
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=0.15, limit=10
            )
            assert len(batch.claimed) == 1
            await WorkRepository(session).mark_running(work_id, expected_owner=runner_a._worker_id)
            await session.commit()

        await asyncio.sleep(0.2)  # lease expires

        # B reclaims (attempt 2) — still mid-run when A reports failure.
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id="worker-ZZZ", lease_seconds=5.0, limit=10
            )
            assert len(batch.claimed) == 1
            await session.commit()

        # Zombie reports failure — fenced, authoritative attempt untouched.
        async with db_session() as session:
            status = await WorkRepository(session).mark_failed(
                work_id,
                "zombie failure",
                expected_owner=runner_a._worker_id,
            )
            await session.commit()
        assert status == "fenced"
        item = await _work(work_id)
        assert item.status == "leased"
        assert item.lease_owner == "worker-ZZZ"
        assert "zombie failure" not in (item.last_error or ""), "zombie error not recorded"

    async def test_unowned_terminal_write_is_stale(self, fresh_db, services) -> None:
        """mark_succeeded on a terminal item is 'stale', never a re-write."""
        work_id = await _schedule()
        runner = _runner(services)
        await runner.run_once()
        async with db_session() as session:
            status = await WorkRepository(session).mark_succeeded(work_id, {"again": True})
            await session.commit()
        assert status == "stale"


class TestHeartbeat:
    async def test_heartbeat_protects_slow_worker_from_reclaim(self, fresh_db, services) -> None:
        """A healthy worker processing a long item keeps its lease alive."""
        release = asyncio.Event()

        async def _long(services, item) -> dict:
            await release.wait()
            return {"done": True}

        runner = _runner(services, lease_seconds=0.3)
        runner.register_handler("long", _long)
        work_id = await _schedule(kind="long")

        task = asyncio.create_task(runner.run_once())
        # Hold the handler far longer than the lease; heartbeats (lease/3)
        # must keep renewing it.
        try:
            for _ in range(4):
                await asyncio.sleep(0.2)
                item = await _work(work_id)
                assert item.status in ("leased", "running")
                assert item.lease_owner == runner._worker_id
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=5.0)
        item = await _work(work_id)
        assert item.status == "succeeded"
        assert item.attempts == 1, "never reclaimed"

    async def test_heartbeat_stops_when_ownership_lost(self, fresh_db, services) -> None:
        """renew_lease returns False once the lease has moved on."""
        work_id = await _schedule()
        async with db_session() as session:
            item = await WorkRepository(session).get(work_id)
            item.status = "running"
            item.lease_owner = "worker-AAA"
            item.lease_expires_at = datetime.now(UTC) + timedelta(seconds=0.1)
            await session.commit()

        async with db_session() as session:
            ok = await WorkRepository(session).renew_lease(
                work_id, worker_id="worker-BBB", lease_seconds=5.0
            )
            await session.commit()
        assert ok is False, "non-owner cannot renew"

        async with db_session() as session:
            ok = await WorkRepository(session).renew_lease(
                work_id, worker_id="worker-AAA", lease_seconds=5.0
            )
            await session.commit()
        assert ok is True


class TestCrashRecovery:
    async def test_worker_death_midrun_is_recovered(self, fresh_db, services) -> None:
        """A worker that vanishes mid-run: its lease expires, another worker
        reclaims, the work completes. No lost work."""
        work_id = await _schedule(kind="crash", max_attempts=3)
        runner_a = _runner(services, lease_seconds=0.15)

        # A claims + marks running, then "dies" (no finalize at all).
        async with db_session() as session:
            await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=0.15, limit=10
            )
            await WorkRepository(session).mark_running(work_id, expected_owner=runner_a._worker_id)
            await session.commit()
        assert (await _work(work_id)).status == "running"

        # Another worker picks it up after the lease expires.
        await asyncio.sleep(0.2)
        runner_b = _runner(services, lease_seconds=5.0)

        async def _ok(services, item) -> dict:
            return {"recovered": True}

        runner_b.register_handler("crash", _ok)
        await runner_b.run_once()
        item = await _work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"recovered": True}
        assert item.attempts == 2

    async def test_reclaim_exhaustion_dies_loudly(self, fresh_db, services) -> None:
        """A reclaim that exhausts attempts produces dead + dead-letter row +
        work.dead signal — the previously silent death path."""
        work_id = await _schedule(kind="die", max_attempts=2)

        # Attempt 1: claim + run, then "crash" (no finalize).
        runner_a = _runner(services, lease_seconds=0.15)
        async with db_session() as session:
            await WorkRepository(session).claim_due(
                worker_id=runner_a._worker_id, lease_seconds=0.15, limit=10
            )
            await WorkRepository(session).mark_running(work_id, expected_owner=runner_a._worker_id)
            await session.commit()
        await asyncio.sleep(0.2)  # lease expires

        # Attempt 2: reclaimed + "crash" again (still no finalize).
        async with db_session() as session:
            batch = await WorkRepository(session).claim_due(
                worker_id="worker-B", lease_seconds=0.15, limit=10
            )
            assert len(batch.claimed) == 1
            await WorkRepository(session).mark_running(work_id, expected_owner="worker-B")
            await session.commit()
        await asyncio.sleep(0.2)  # lease expires again

        # Attempt 3: the reclaim would exceed max_attempts — it must KILL
        # the item, and the runner must announce the death.
        runner_c = _runner(services, lease_seconds=5.0)
        await runner_c.run_once()

        item = await _work(work_id)
        assert item.status == "dead"
        assert "giving up" in (item.last_error or "")
        # Loud death: ledger announcement (directive §13: dead_letter
        # table removed; the signal ledger + audit log are the
        # surviving records of terminal failure).
        assert await _signal_count(f"work.dead:{work_id}") == 1

    async def test_expired_wait_is_announced(self, fresh_db, services) -> None:
        """An unmet wait dying at its deadline emits work.expired:<id> so
        dependents can react honestly instead of waiting forever."""
        work_id = await _schedule(kind="capability", expires_in=-0.01)
        await asyncio.sleep(0.02)
        runner = _runner(services)
        await runner.run_once()
        item = await _work(work_id)
        assert item.status == "dead"
        assert "wait expired" in (item.last_error or "")
        assert await _signal_count(f"work.expired:{work_id}") == 1


class TestGracefulShutdown:
    async def test_stop_lets_inflight_item_finish(self, fresh_db, services) -> None:
        """stop() during a running handler: the item finishes and its result
        is recorded — a shutdown never orphans in-flight work."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def _inflight(services, item) -> dict:
            started.set()
            await release.wait()
            return {"finished": True}

        runner = _runner(services, poll_interval_seconds=0.05, lease_seconds=5.0)
        runner.register_handler("inflight", _inflight)
        work_id = await _schedule(kind="inflight")
        runner.start()

        await asyncio.wait_for(started.wait(), timeout=2.0)
        stop_task = asyncio.create_task(runner.stop(grace_seconds=5.0))
        await asyncio.sleep(0.05)  # stop() is waiting for the batch
        release.set()
        await asyncio.wait_for(stop_task, timeout=5.0)

        item = await _work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"finished": True}

    async def test_stop_without_work_is_immediate(self, fresh_db, services) -> None:
        runner = _runner(services)
        runner.start()
        await asyncio.wait_for(runner.stop(grace_seconds=1.0), timeout=2.0)
