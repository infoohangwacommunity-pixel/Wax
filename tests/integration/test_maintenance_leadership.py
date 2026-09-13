"""Maintenance-loop leadership tests (ADR-0017).

Postgres advisory-lock election is exercised on Postgres deployments;
these tests prove the LOGIC that hosts the election: per-dialect mode
routing, leader execution, follower skip (visible, not silent), and
token release semantics.
"""

from __future__ import annotations

import pytest

from wax.authority.approvals import ApprovalService
from wax.authority.seed import seed_builtin_roles
from wax.runtime.leadership import (
    MAINTENANCE_ADVISORY_LOCK_KEY,
    MaintenanceLeadership,
    _mode_for,
)
from wax.runtime.maintenance import run_maintenance_pass
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


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


class TestModeRouting:
    def test_postgres_url_uses_advisory_lock(self):
        assert _mode_for("postgresql+asyncpg://u:p@db/wax") == "postgres_advisory_lock"
        assert _mode_for("postgres://u:p@db/wax") == "postgres_advisory_lock"

    def test_sqlite_url_is_single_writer(self):
        assert _mode_for("sqlite+aiosqlite:///:memory:") == "single_writer"
        assert _mode_for("sqlite:///wax.db") == "single_writer"

    def test_unknown_dialect_fails_open(self):
        assert _mode_for("mysql://x") == "fail_open"
        assert _mode_for("") == "fail_open"

    def test_lock_key_is_stable(self):
        assert MAINTENANCE_ADVISORY_LOCK_KEY == 0x5741_5852


class TestAcquireOnSingleWriter:
    async def test_sqlite_acquire_is_always_leader(self, test_settings):
        token = await MaintenanceLeadership.acquire(test_settings)
        try:
            assert token.is_leader is True
            assert token.mode == "single_writer"
        finally:
            await token.release()

    async def test_release_without_held_session_is_noop(self):
        token = MaintenanceLeadership(mode="single_writer", is_leader=True)
        await token.release()  # must not raise
        assert token._cm is None


class TestPassIntegration:
    async def test_leader_pass_runs_sweeps_and_reports_mode(self, fresh_db, test_settings):
        async with db_session() as session:
            dead, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
                expires_in_seconds=-0.01,
            )
            await session.commit()

        result = await run_maintenance_pass(test_settings)
        assert result["leadership_mode"] == "single_writer"
        assert result["expired_approvals"] == 1
        assert "skipped" not in result

    async def test_follower_pass_skips_visibly(
        self, fresh_db, test_settings, monkeypatch
    ):
        async with db_session() as session:
            dead, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
                expires_in_seconds=-0.01,
            )
            await session.commit()

        from wax.runtime import maintenance

        async def _fake_acquire(settings):
            return MaintenanceLeadership(
                mode="postgres_advisory_lock", is_leader=False
            )

        monkeypatch.setattr(
            maintenance.MaintenanceLeadership, "acquire", _fake_acquire
        )
        result = await run_maintenance_pass(test_settings)
        assert result["skipped"] == "not_leader"
        assert result["expired_approvals"] == 0

        # The sweep genuinely did NOT run: the approval is still pending.
        async with db_session() as session:
            record = await ApprovalService(session).get(dead.id)
            assert record.status == "pending"

        # The skip was METERED, not silent.
        from wax.observability.runtime_metrics import get_runtime_metrics

        counter = get_runtime_metrics()._registry.counter(
            "maintenance_leadership_total", role="follower"
        )
        assert counter.value >= 1


class TestMetrics:
    def test_leader_metric_counts(self):
        from wax.observability.runtime_metrics import get_runtime_metrics

        get_runtime_metrics().maintenance_led()
        counter = get_runtime_metrics()._registry.counter(
            "maintenance_leadership_total", role="leader"
        )
        assert counter.value >= 1
