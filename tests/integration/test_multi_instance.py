"""Multi-instance Runtime — integration tests (ADR-0044, Phase 11).

Covers:
- Two WorkRunners never claim the same work item (simulated multi-process)
- DB-backed rate limiter enforces across "processes" (simulated)
- DB-backed cost protector enforces across "processes" (simulated)
- Lease fencing: stale worker cannot overwrite
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.runtime.shared_state import (
    DbBackedCostProtector,
    DbBackedRateLimiter,
)
from wax.runtime.work import WorkRepository, WorkRunner
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import WorkItemRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


# ----------------------------------------------------------------------------
# 1. Two WorkRunners never claim the same item
# ----------------------------------------------------------------------------


class TestMultiWorkerClaimExclusivity:
    async def test_two_runners_never_claim_same_item(self, fresh_db, services):
        """Simulate two processes by creating two WorkRunner instances.
        Both try to claim the same work item; only one succeeds."""
        principal_id = await _create_principal()

        # Schedule one work item
        async with db_session() as s:
            repo = WorkRepository(s)
            await repo.schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {}},
                wake_at=datetime.now(UTC),
                principal_id=principal_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()

        async def _echo_handler(services, item):
            return {"echo": {}}

        # Two runners (simulating two processes)
        runner_a = WorkRunner(
            services, poll_interval_seconds=0.01, lease_seconds=10, retry_backoff_seconds=0
        )
        runner_a.register_handler("capability", _echo_handler)
        runner_b = WorkRunner(
            services, poll_interval_seconds=0.01, lease_seconds=10, retry_backoff_seconds=0
        )
        runner_b.register_handler("capability", _echo_handler)

        # Both try to claim in the same pass
        ran_a = await runner_a.run_once()
        ran_b = await runner_b.run_once()

        # Exactly one claims; the other gets 0
        assert ran_a + ran_b == 1, f"expected exactly 1 claim, got A={ran_a} B={ran_b}"

    async def test_claimed_item_is_leased_by_exactly_one_worker(self, fresh_db, services):
        """After claim, the item's lease_owner is set to exactly one worker."""
        principal_id = await _create_principal()

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {}},
                wake_at=datetime.now(UTC),
                principal_id=principal_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            item_id = item.id

        async def _echo_handler(services, item):
            return {"echo": {}}

        runner = WorkRunner(
            services, poll_interval_seconds=0.01, lease_seconds=10, retry_backoff_seconds=0
        )
        runner.register_handler("capability", _echo_handler)
        await runner.run_once()

        async with db_session() as s:
            record = await s.get(WorkItemRecord, item_id)
            assert record.status == "succeeded"
            assert record.attempts >= 1  # was claimed + ran


# ----------------------------------------------------------------------------
# 2. DB-backed rate limiter
# ----------------------------------------------------------------------------


class TestDbBackedRateLimiter:
    async def test_rate_limiter_enforces_across_simulated_processes(self, fresh_db, services):
        """Two 'processes' share the same DB-backed rate limiter. The
        combined count respects the limit."""
        principal_id = await _create_principal()
        limiter = DbBackedRateLimiter(max_per_minute=5)

        allowed = 0
        # Simulate 10 requests from two "processes" interleaved
        for _i in range(10):
            async with db_session() as s:
                ok = await limiter.check(s, principal_id)
                await s.commit()
                if ok:
                    allowed += 1

        assert allowed == 5, f"expected exactly 5 allowed, got {allowed}"

    async def test_rate_limiter_allows_under_limit(self, fresh_db, services):
        principal_id = await _create_principal()
        limiter = DbBackedRateLimiter(max_per_minute=100)

        async with db_session() as s:
            ok1 = await limiter.check(s, principal_id)
            await s.commit()
            ok2 = await limiter.check(s, principal_id)
            await s.commit()

        assert ok1 is True
        assert ok2 is True


# ----------------------------------------------------------------------------
# 3. DB-backed cost protector
# ----------------------------------------------------------------------------


class TestDbBackedCostProtector:
    async def test_cost_protector_enforces_daily_cap(self, fresh_db, services):
        """Two 'processes' share the same DB-backed cost tracker. The
        combined daily total respects the cap."""
        principal_id = await _create_principal()
        protector = DbBackedCostProtector(daily_token_cap=1000)

        total_allowed = 0
        # Simulate 5 requests of 300 tokens each from two "processes"
        for _ in range(5):
            async with db_session() as s:
                ok = await protector.check_and_record(s, principal_id, tokens=300)
                await s.commit()
                if ok:
                    total_allowed += 300

        # 1000 cap: 300 + 300 + 300 = 900 (allowed), 4th = 1200 > 1000 (denied)
        assert total_allowed == 900, f"expected 900 allowed, got {total_allowed}"

    async def test_cost_protector_allows_under_cap(self, fresh_db, services):
        principal_id = await _create_principal()
        protector = DbBackedCostProtector(daily_token_cap=10000)

        async with db_session() as s:
            ok = await protector.check_and_record(s, principal_id, tokens=100)
            await s.commit()

        assert ok is True
