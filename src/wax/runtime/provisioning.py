"""Dynamic provisioning (Phase S) — temporary resources with lifecycles.

The runtime can provision temporary resources (today: scratch directories;
tomorrow: execution sandboxes, temp memory, disposable artifacts). Every
resource has:

- owner (principal) and origin (execution)
- a TTL — nothing lives forever unless intentionally promoted
- limits (declared + recorded; enforced where the kind allows)
- cleanup (TTL reaper + explicit release)
- audit (provisioned / released / expired / promoted events)

Path containment: resources live under settings.provisioning_root with
ULID names. Release/expire verify containment BEFORE deleting, so a
tampered DB row can never point the reaper at arbitrary paths.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.observability.audit import record_audit_event
from wax.runtime.logging import get_logger
from wax.state.provisioning_models import ProvisionedResourceRecord

log = get_logger(__name__)

MAX_TTL = timedelta(hours=24)


class ProvisioningError(Exception):
    """Raised when a provisioning request violates runtime limits."""


class ProvisioningService:
    """Allocates and retires ephemeral runtime resources. All methods take
    the caller's session; transactions belong to the caller."""

    def __init__(self, settings: Any) -> None:
        self._root = Path(settings.provisioning_root)
        self._max_active_per_principal = settings.provisioning_max_active_per_principal

    # --- Provision ---------------------------------------------------------

    async def provision_scratch_dir(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        ttl_seconds: int,
        execution_id: str | None = None,
        limits: dict[str, Any] | None = None,
    ) -> ProvisionedResourceRecord:
        ttl = timedelta(seconds=ttl_seconds)
        if ttl <= timedelta(0):
            raise ProvisioningError("ttl_seconds must be positive")
        if ttl > MAX_TTL:
            raise ProvisioningError("ttl_seconds exceeds the 24-hour provisioning maximum")

        # Limit: max active resources per principal (open-world budget).
        active = await session.execute(
            select(ProvisionedResourceRecord).where(
                ProvisionedResourceRecord.principal_id == principal_id,
                ProvisionedResourceRecord.status == "active",
            )
        )
        if len(active.all()) >= self._max_active_per_principal:
            raise ProvisioningError(
                f"Principal already holds {self._max_active_per_principal} "
                "active resources; release one before provisioning another"
            )

        resource_id = str(ULID())
        rel_path = Path("scratch_dir") / resource_id
        abs_path = self._root / rel_path
        abs_path.mkdir(parents=True, exist_ok=False)

        record = ProvisionedResourceRecord(
            id=resource_id,
            kind="scratch_dir",
            status="active",
            principal_id=principal_id,
            execution_id=execution_id,
            uri=str(abs_path),
            expires_at=datetime.now(UTC) + ttl,
            limits=limits or {},
        )
        session.add(record)
        await session.flush()
        await record_audit_event(
            session,
            actor_principal_id=principal_id,
            actor_kind="ai",
            event_kind="provisioning.allocated",
            outcome="success",
            payload={
                "resource_id": resource_id,
                "kind": "scratch_dir",
                "expires_at": record.expires_at.isoformat(),
            },
        )
        log.info(
            "provisioning.allocated",
            resource_id=resource_id,
            principal_id=principal_id,
            ttl_s=ttl_seconds,
        )
        return record

    # --- Release / expire ---------------------------------------------------

    async def release(
        self,
        session: AsyncSession,
        *,
        resource_id: str,
        principal_id: str,
        reason: str = "explicit_release",
    ) -> bool:
        """Release a resource the caller owns. Returns True if released."""
        record = await self._get_active(session, resource_id)
        if record is None:
            return False
        if record.principal_id != principal_id:
            raise ProvisioningError("resource belongs to a different principal")
        await self._destroy(record, session, status="released", reason=reason)
        return True

    async def expire_due(self, session: AsyncSession) -> int:
        """TTL reaper: destroy active resources past their expiry.
        Returns how many were expired."""
        now = datetime.now(UTC)
        result = await session.execute(
            select(ProvisionedResourceRecord).where(
                ProvisionedResourceRecord.status == "active",
                ProvisionedResourceRecord.expires_at.is_not(None),
                ProvisionedResourceRecord.expires_at <= now,
            )
        )
        expired = 0
        for record in result.scalars():
            await self._destroy(record, session, status="expired", reason="ttl")
            expired += 1
        return expired

    async def promote(
        self,
        session: AsyncSession,
        *,
        resource_id: str,
        principal_id: str,
    ) -> bool:
        """Intentionally remove the TTL: the only path to permanence."""
        record = await self._get_active(session, resource_id)
        if record is None:
            return False
        if record.principal_id != principal_id:
            raise ProvisioningError("resource belongs to a different principal")
        record.expires_at = None
        await session.flush()
        await record_audit_event(
            session,
            actor_principal_id=principal_id,
            actor_kind="ai",
            event_kind="provisioning.promoted",
            outcome="success",
            payload={"resource_id": resource_id, "kind": record.kind},
        )
        log.info("provisioning.promoted", resource_id=resource_id)
        return True

    # --- Internals ------------------------------------------------------------

    async def _get_active(
        self, session: AsyncSession, resource_id: str
    ) -> ProvisionedResourceRecord | None:
        result = await session.execute(
            select(ProvisionedResourceRecord).where(
                ProvisionedResourceRecord.id == resource_id,
                ProvisionedResourceRecord.status == "active",
            )
        )
        return result.scalar_one_or_none()

    async def _destroy(
        self,
        record: ProvisionedResourceRecord,
        session: AsyncSession,
        *,
        status: str,
        reason: str,
    ) -> None:
        """Delete the backing resource (with containment check) and mark
        the row. The ROW survives for audit."""
        uri = record.uri or ""
        root = str(self._root.resolve())
        try:
            path = Path(uri).resolve()
            if str(path).startswith(root) and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                # Contained file (future kinds) — still under root.
                if str(path).startswith(root):
                    path.unlink(missing_ok=True)
                else:
                    log.error(
                        "provisioning.path_outside_root_refused",
                        resource_id=record.id,
                        uri=uri,
                    )
        except Exception as e:
            log.error(
                "provisioning.destroy_failed",
                resource_id=record.id,
                error=str(e),
            )
        record.status = status
        await session.flush()
        await record_audit_event(
            session,
            actor_principal_id=record.principal_id,
            actor_kind="system" if reason == "ttl" else "ai",
            event_kind=f"provisioning.{status}",
            outcome="success",
            payload={"resource_id": record.id, "reason": reason},
        )
        self._metric().resource_released(record.kind, reason)

    def _metric(self):  # type: ignore[no-untyped-def]
        from wax.observability.runtime_metrics import get_runtime_metrics

        return get_runtime_metrics()


async def maintenance_loop(settings: Any, interval_seconds: float = 60.0) -> None:
    """Periodic TTL reaper. Runs as a lifespan task.

    Multi-instance honesty (§61 audit fix): the reaper destroys real
    filesystem directories, so like every other destructive sweep it
    runs on the LEADER only (the same advisory-lock election the
    maintenance loop uses). Followers skip and say so.
    """
    import asyncio

    from wax.runtime.leadership import MaintenanceLeadership
    from wax.state.engine import db_session

    service = ProvisioningService(settings)
    log.info("provisioning.maintenance_started", interval_s=interval_seconds)
    while True:
        try:
            leadership = await MaintenanceLeadership.acquire(settings)
            try:
                if leadership.is_leader:
                    async with db_session() as session:
                        expired = await service.expire_due(session)
                        await session.commit()
                    if expired:
                        log.info("provisioning.expired_batch", count=expired)
                else:
                    log.debug(
                        "provisioning.maintenance.follower",
                        mode=leadership.mode,
                    )
            finally:
                await leadership.release()
        except Exception as e:
            log.error(
                "provisioning.maintenance_error",
                error=str(e),
                error_type=type(e).__name__,
            )
        await asyncio.sleep(interval_seconds)


async def stop_maintenance(task: object | None) -> None:
    """Cancel the reaper task (lifespan shutdown hook)."""
    import contextlib

    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
