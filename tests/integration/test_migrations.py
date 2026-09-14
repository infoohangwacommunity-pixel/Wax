"""Migration discipline tests: from zero, and from the current production
schema. A migration that only works on a green field is a liability —
these tests run the real Alembic chain over real SQLite files.

- fresh: `alembic upgrade head` on an empty database creates the full
  schema; the ORM can then use it (smoke: insert + query a work item).
- upgrade: a database at the PREVIOUS revision (e5c2a9f47b61 — the schema
  real deployments run today) upgrades to head, the new table appears,
  and pre-existing rows survive untouched.
- rollback: head → back down one step → forward again, without error.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.work_models import WorkItemRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
PREV_REVISION = "e5c2a9f47b61"  # what production ran before the approval primitive


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **{k: v for k, v in __import__("os").environ.items() if k != "WAX_DATABASE_URL"},
        "WAX_DATABASE_URL": database_url,
        "WAX_SECRET_KEY": "test-secret-key-not-for-production-use-xxxxxxxxxxxxxxxx",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    venv_bin = Path(sys.executable).parent
    result = subprocess.run(
        [str(venv_bin / "alembic"), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _sqlite_file_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


class TestFreshDatabase:
    async def test_upgrade_head_from_zero_creates_full_schema(self, tmp_path) -> None:
        db_file = tmp_path / "fresh.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")

        # The approval primitive's table exists with its indexes.
        init_engine(__import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(database_url=url))
        try:
            async with db_session() as session:
                rows = (
                    await session.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).scalars().all()
                tables = set(rows)
                assert "pending_approvals" in tables
                assert "capability_invocations" in tables
                assert "runtime_signals" in tables
                assert "work_items" in tables
                assert "principals" in tables

                # The at-most-one-claim property is a DATABASE property.
                indexes = (
                    await session.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='index' "
                            "AND tbl_name='capability_invocations'"
                        )
                    )
                ).scalars().all()
                assert (
                    "uq_capability_invocations_principal_capability_key"
                    in set(indexes)
                )
        finally:
            await dispose_engine()

    async def test_fresh_schema_is_usable_by_the_orm(self, tmp_path) -> None:
        """Migrations produce a schema the live code actually works against
        (catches drift between models and migrations)."""
        db_file = tmp_path / "usable.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")

        settings = __import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            from datetime import UTC, datetime, timedelta

            async with db_session() as session:
                item = WorkItemRecord(
                    id="01TESTWORKITEM0000000000",
                    kind="capability",
                    status="pending",
                    principal_id="01TESTPRINCIPAL000000000",
                    payload={"capability_name": "echo", "inputs": {}},
                    wake_at=datetime.now(UTC),
                    available_at=datetime.now(UTC),
                    wake_kind="time",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    attempts=0,
                    max_attempts=3,
                )
                session.add(item)
                await session.commit()
            async with db_session() as session:
                fetched = await session.get(WorkItemRecord, "01TESTWORKITEM0000000000")
                assert fetched is not None
                assert fetched.wake_kind == "time"
        finally:
            await dispose_engine()


class TestUpgradeFromProductionSchema:
    async def test_upgrade_from_previous_revision_preserves_rows(self, tmp_path) -> None:
        """The upgrade path real deployments will take: data written under
        the previous schema survives, and the new table appears."""
        db_file = tmp_path / "upgrade.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", PREV_REVISION)

        # Write production-shaped rows at the old revision.
        settings = __import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            from datetime import UTC, datetime

            async with db_session() as session:
                await session.execute(
                    text(
                        "INSERT INTO work_items (id, kind, status, principal_id, "
                        "wake_at, available_at, wake_kind, attempts, max_attempts, "
                        "created_at, updated_at) VALUES "
                        "(:id, 'capability', 'pending', :pid, :ts, :ts, 'time', 0, 3, :ts, :ts)"
                    ),
                    {
                        "id": "01PRODWORKITEM0000000000000",
                        "pid": "01PRODPRINCIPAL0000000000",
                        "ts": datetime.now(UTC).isoformat(),
                    },
                )
                await session.commit()
        finally:
            await dispose_engine()

        # Upgrade to head.
        _alembic(url, "upgrade", "head")

        init_engine(settings)
        try:
            async with db_session() as session:
                # Pre-existing row survived (server_default 'time' applied).
                row = (
                    await session.execute(
                        text("SELECT wake_kind, status FROM work_items WHERE id=:i"),
                        {"i": "01PRODWORKITEM0000000000000"},
                    )
                ).first()
                assert row is not None
                assert row.wake_kind == "time"
                assert row.status == "pending"

                # The new approval table is present and empty-but-real.
                count = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM pending_approvals")
                    )
                ).scalar_one()
                assert count == 0

                # The idempotency ledger is present and empty-but-real.
                ledger = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM capability_invocations")
                    )
                ).scalar_one()
                assert ledger == 0
        finally:
            await dispose_engine()

    async def test_downgrade_then_upgrade_roundtrip(self, tmp_path) -> None:
        db_file = tmp_path / "roundtrip.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")
        _alembic(url, "downgrade", "-1")  # drop capability_invocations
        _alembic(url, "upgrade", "head")  # bring it back
        settings = __import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            async with db_session() as session:
                ledger = (
                    await session.execute(
                        text("SELECT COUNT(*) FROM capability_invocations")
                    )
                ).scalar_one()
                assert ledger == 0
        finally:
            await dispose_engine()

    async def test_downgrade_drops_idempotency_ledger(self, tmp_path) -> None:
        """head → -5 removes Phase 3 (memory) + Phase 5 (environment)
        + Phase 6 (terminal) + Phase 7 (vault) columns/tables AND the
        idempotency ledger table.

        As more migrations are added after the idempotency ledger
        migration, this downgrade step count increases. The test's
        intent is to verify the downgrade boundary matches the upgrade.
        """
        db_file = tmp_path / "ledgerdown.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")
        _alembic(url, "downgrade", "-5")

        settings = __import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            async with db_session() as session:
                tables = (
                    await session.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).scalars().all()
                assert "capability_invocations" not in set(tables)
                assert "pending_approvals" in set(tables), (
                    "only the ledger table is dropped by the -4 step"
                )
        finally:
            await dispose_engine()
