"""Work repository — claiming durable work safely across time and crashes.

Claim semantics (the heart of Phase R):
- A pending item is claimable when available_at <= now.
- A leased/running item whose lease EXPIRED is also claimable — its worker
  died, and the work must not be lost. Reclaiming counts as a new attempt
  (the dead worker's effect state is unknown; capability work is treated as
  at-least-once with attempts bounded).
- Claiming sets a lease (owner + expiry) so concurrent workers cannot
  double-run the same item.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)

VALID_WORK_STATUSES = frozenset(
    {"pending", "leased", "running", "succeeded", "failed", "dead", "cancelled"}
)
TERMINAL_WORK_STATUSES = frozenset({"succeeded", "dead", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkRepository:
    """Data access for work_items. Caller owns transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def schedule(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None,
        wake_at: datetime,
        principal_id: str | None = None,
        execution_id: str | None = None,
        max_attempts: int = 3,
    ) -> WorkItemRecord:
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=UTC)
        item = WorkItemRecord(
            id=str(ULID()),
            kind=kind,
            status="pending",
            principal_id=principal_id,
            execution_id=execution_id,
            payload=payload,
            wake_at=wake_at,
            available_at=wake_at,
            attempts=0,
            max_attempts=max(1, max_attempts),
        )
        self._session.add(item)
        await self._session.flush()
        log.info(
            "work.scheduled",
            work_id=item.id,
            kind=kind,
            wake_at=wake_at.isoformat(),
            principal_id=principal_id,
        )
        return item

    async def get(self, work_id: str) -> WorkItemRecord | None:
        return await self._session.get(WorkItemRecord, work_id)

    async def list_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[WorkItemRecord]:
        stmt = (
            select(WorkItemRecord)
            .where(WorkItemRecord.principal_id == principal_id)
            .order_by(WorkItemRecord.wake_at.asc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(WorkItemRecord.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def claim_due(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        limit: int = 10,
    ) -> list[WorkItemRecord]:
        """Claim up to `limit` runnable items for this worker.

        Claimable = pending and due, OR leased/running with an expired
        lease (the previous worker died). Each claim (or reclaim) takes one
        attempt; a reclaim that would exhaust max attempts goes dead
        instead of being re-leased.
        """
        now = _utcnow()
        lease_expiry = now + timedelta(seconds=lease_seconds)

        stmt = (
            select(WorkItemRecord)
            .where(
                or_(
                    (WorkItemRecord.status == "pending") & (WorkItemRecord.available_at <= now),
                    WorkItemRecord.status.in_(["leased", "running"])
                    & (WorkItemRecord.lease_expires_at < now),
                )
            )
            .order_by(WorkItemRecord.available_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        claimed: list[WorkItemRecord] = []
        for item in result.scalars():
            if item.status in ("leased", "running") and item.attempts >= item.max_attempts:
                # No attempts left — the item dies instead of being re-leased.
                item.status = "dead"
                item.lease_owner = None
                item.lease_expires_at = None
                item.last_error = f"lease expired after {item.attempts} attempts; giving up"
                continue
            reclaimed = item.status in ("leased", "running")
            item.status = "leased"
            item.lease_owner = worker_id
            item.lease_expires_at = lease_expiry
            item.attempts += 1
            if reclaimed:
                item.last_error = f"reclaimed after lease expiry (attempt {item.attempts})"
            claimed.append(item)
        if claimed:
            await self._session.flush()
        return claimed

    async def mark_running(self, work_id: str) -> bool:
        item = await self.get(work_id)
        if item is None or item.status != "leased":
            return False
        item.status = "running"
        await self._session.flush()
        return True

    async def mark_succeeded(self, work_id: str, result: dict[str, Any] | None) -> bool:
        item = await self.get(work_id)
        if item is None or item.status not in ("leased", "running"):
            return False
        item.status = "succeeded"
        item.result = result
        item.lease_owner = None
        item.lease_expires_at = None
        await self._session.flush()
        log.info("work.succeeded", work_id=work_id, kind=item.kind)
        return True

    async def mark_failed(
        self,
        work_id: str,
        error: str,
        *,
        backoff_seconds: float = 30.0,
    ) -> str:
        """Record a failed attempt. Returns the new status:
        "pending" (will retry after backoff) or "dead" (attempts exhausted).
        """
        item = await self.get(work_id)
        if item is None or item.status not in ("leased", "running"):
            return "unknown"
        item.last_error = error[:5000]
        if item.attempts >= item.max_attempts:
            item.status = "dead"
            item.lease_owner = None
            item.lease_expires_at = None
            log.warning("work.dead", work_id=work_id, kind=item.kind, error=error[:200])
        else:
            item.status = "pending"
            item.available_at = _utcnow() + timedelta(seconds=backoff_seconds)
            item.lease_owner = None
            item.lease_expires_at = None
            log.info(
                "work.retry_scheduled",
                work_id=work_id,
                attempt=item.attempts,
                backoff_s=backoff_seconds,
            )
        await self._session.flush()
        return item.status

    async def cancel(self, work_id: str) -> str | None:
        """Cancel non-terminal work. Returns the new status, or the current
        terminal status if already finished, or None if not found."""
        item = await self.get(work_id)
        if item is None:
            return None
        if item.status in TERMINAL_WORK_STATUSES:
            return item.status
        item.status = "cancelled"
        item.lease_owner = None
        item.lease_expires_at = None
        await self._session.flush()
        log.info("work.cancelled", work_id=work_id, kind=item.kind)
        return "cancelled"

    async def count_inflight(self) -> int:
        result = await self._session.execute(
            select(WorkItemRecord.id).where(
                WorkItemRecord.status.in_(["pending", "leased", "running"])
            )
        )
        return len(result.all())
