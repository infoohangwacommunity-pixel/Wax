"""Scheduled blob GC in the runtime maintenance pass (opt-in).

Deletion must never happen by default: `WAX_BLOB_GC_ENABLED` gates the
sweep. These tests pin the gate (disabled → nothing is ever removed)
and the sweep itself (enabled → only blobs unreachable from live
snapshots are collected, metrics recorded).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from ulid import ULID

from wax.core.config import settings_for_testing
from wax.runtime.blob_store import ContentAddressedBlobStore
from wax.runtime.maintenance import run_maintenance_pass
from wax.state.audit_models import AuditEvent
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.workspace_models import WorkspaceSnapshotRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def db():
    from wax.authority.seed import seed_builtin_roles

    settings = settings_for_testing()
    init_engine(settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield settings
    await dispose_engine()


def _services_with_store(store: ContentAddressedBlobStore):
    from wax.runtime.services import RuntimeServices

    services = RuntimeServices.build(settings_for_testing())
    services.blob_store = store
    return services


async def _seed_snapshot(principal_id: str, live_digest: str) -> None:
    async with db_session() as s:
        s.add(
            WorkspaceSnapshotRecord(
                id=str(ULID()),
                principal_id=principal_id,
                workspace_resource_id=principal_id,
                content_hash="0" * 64,
                files_json=[{"path": "live.bin", "sha256": live_digest, "size": 19}],
                file_count=1,
                total_bytes=19,
                captured_at=datetime.now(UTC),
            )
        )
        await s.commit()


async def _new_principal(display_name: str) -> str:
    from wax.identity.repository import PrincipalRepository

    async with db_session() as s:
        principal = await PrincipalRepository(s).create_principal(display_name=display_name)
        await s.commit()
        return principal.id


class TestScheduledBlobGC:
    async def test_disabled_by_default_never_deletes(self, db, tmp_path: Path):
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        services = _services_with_store(store)
        stale = store.put_bytes(b"stale bytes")
        live = store.put_bytes(b"live bytes")
        pid = await _new_principal("op")
        await _seed_snapshot(pid, live)

        results = await run_maintenance_pass(db, services=services)
        assert results["blob_gc"] == {"skipped": "disabled"}
        assert store.has(stale)  # gate off → nothing is ever deleted
        assert store.has(live)

    async def test_enabled_sweep_removes_only_unreachable(self, db, tmp_path: Path):
        settings = settings_for_testing(blob_gc_enabled=True)
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        services = _services_with_store(store)
        stale = store.put_bytes(b"stale bytes")
        live = store.put_bytes(b"live bytes")
        pid = await _new_principal("op")
        await _seed_snapshot(pid, live)

        # The metrics registry is a process-global singleton — other tests
        # in this process may already have incremented these counters, so
        # assert on the DELTA, never on an absolute value.
        before = services.metrics.snapshot_counters()
        results = await run_maintenance_pass(settings, services=services)
        after = services.metrics.snapshot_counters()

        assert results["blob_gc"] == {"removed": 1, "reclaimed_bytes": len(b"stale bytes")}
        assert store.has(live)
        assert not store.has(stale)
        assert (
            after.get("control_blob_gc_runs_total", 0.0)
            - before.get("control_blob_gc_runs_total", 0.0)
            == 1.0
        )
        assert (
            after.get("control_blob_gc_removed_total", 0.0)
            - before.get("control_blob_gc_removed_total", 0.0)
            == 1.0
        )


class TestScheduledGcAuditTrail:
    """Deletion is the security-relevant fact: when the scheduled sweep
    actually removes blobs, a system-actor audit row must exist (INV-06:
    every deletion attributable — here to 'the runtime itself'). No-op
    passes are deliberately NOT audited (they would flood the append-only
    ledger with non-facts)."""

    async def _audit_rows(self) -> list:
        async with db_session() as s:
            return (
                (await s.execute(select(AuditEvent).order_by(AuditEvent.created_at.asc())))
                .scalars()
                .all()
            )

    async def test_removal_writes_a_system_audit_event(self, db, tmp_path: Path):
        settings = settings_for_testing(blob_gc_enabled=True)
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        services = _services_with_store(store)
        stale = store.put_bytes(b"stale bytes")
        live = store.put_bytes(b"live bytes")
        pid = await _new_principal("op")
        await _seed_snapshot(pid, live)

        await run_maintenance_pass(settings, services=services)
        assert not store.has(stale)  # the removal actually happened

        events = await self._audit_rows()
        gc_events = [e for e in events if e.event_kind == "system.blob_gc"]
        assert len(gc_events) == 1
        assert gc_events[0].actor_kind == "system"
        assert gc_events[0].outcome == "success"
        assert gc_events[0].payload["removed"] == 1
        assert gc_events[0].payload["reclaimed_bytes"] == len(b"stale bytes")
        assert gc_events[0].actor_principal_id is None

    async def test_disabled_gate_writes_no_audit_event(self, db, tmp_path: Path):
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        services = _services_with_store(store)
        store.put_bytes(b"stale bytes")

        await run_maintenance_pass(db, services=services)  # gate off
        assert await self._audit_rows() == []

    async def test_noop_pass_writes_no_audit_event(self, db, tmp_path: Path):
        settings = settings_for_testing(blob_gc_enabled=True)
        store = ContentAddressedBlobStore(tmp_path / "blobs")
        services = _services_with_store(store)
        live = store.put_bytes(b"live bytes")
        pid = await _new_principal("op")
        await _seed_snapshot(pid, live)  # nothing orphaned

        results = await run_maintenance_pass(settings, services=services)
        assert results["blob_gc"] == {"removed": 0, "reclaimed_bytes": 0}
        assert await self._audit_rows() == []
