"""Migration discipline tests: from zero, and round-trip.

Verifies the Alembic migration chain works end-to-end on real SQLite files.
The open-world reset dropped many tables; these tests verify the surviving
schema is created correctly and the round-trip works.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.work_models import WorkItemRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        """Fresh install: upgrade head creates the surviving schema."""
        db_file = tmp_path / "fresh.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")

        init_engine(
            __import__("wax.core.config", fromlist=["settings_for_testing"]).settings_for_testing(
                database_url=url
            )
        )
        try:
            async with db_session() as session:
                rows = (
                    (
                        await session.execute(
                            text("SELECT name FROM sqlite_master WHERE type='table'")
                        )
                    )
                    .scalars()
                    .all()
                )
                tables = set(rows)
                # Surviving tables (post open-world reset)
                assert "principals" in tables
                assert "principal_credentials" in tables
                assert "memory_records" in tables
                assert "executions" in tables
                assert "execution_steps" in tables
                assert "work_items" in tables
                assert "runtime_signals" in tables
                assert "processed_messages" in tables
                assert "delivery_records" in tables
                assert "conversations" in tables
                assert "audit_events" in tables

                # Removed tables must NOT exist
                assert "pending_approvals" not in tables
                assert "capability_invocations" not in tables
                assert "roles" not in tables
                assert "objectives" not in tables
                assert "artifacts" not in tables
                assert "workspace_snapshots" not in tables
                assert "provisioned_resources" not in tables
        finally:
            await dispose_engine()

    async def test_fresh_schema_is_usable_by_the_orm(self, tmp_path) -> None:
        """Migrations produce a schema the live code works against."""
        db_file = tmp_path / "usable.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")

        settings = __import__(
            "wax.core.config", fromlist=["settings_for_testing"]
        ).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            from datetime import UTC, datetime, timedelta

            async with db_session() as session:
                item = WorkItemRecord(
                    id="01TESTWORKITEM0000000000",
                    kind="intelligence",
                    status="pending",
                    principal_id="01TESTPRINCIPAL000000000",
                    payload={"prompt": "test", "observation": {}},
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


class TestRoundTrip:
    async def test_downgrade_then_upgrade_roundtrip(self, tmp_path) -> None:
        """head → -1 → head should work cleanly."""
        db_file = tmp_path / "roundtrip.db"
        url = _sqlite_file_url(db_file)
        _alembic(url, "upgrade", "head")
        _alembic(url, "downgrade", "-1")
        _alembic(url, "upgrade", "head")

        settings = __import__(
            "wax.core.config", fromlist=["settings_for_testing"]
        ).settings_for_testing(database_url=url)
        init_engine(settings)
        try:
            async with db_session() as session:
                # The surviving tables should all be present after round-trip
                rows = (
                    (
                        await session.execute(
                            text("SELECT name FROM sqlite_master WHERE type='table'")
                        )
                    )
                    .scalars()
                    .all()
                )
                tables = set(rows)
                assert "work_items" in tables
                assert "memory_records" in tables
                assert "executions" in tables
        finally:
            await dispose_engine()
