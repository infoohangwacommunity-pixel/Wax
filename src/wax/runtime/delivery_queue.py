"""Delivery queue — outbound messages as recoverable runtime state.

Mission §55: "If WAX successfully completes work but cannot deliver the
result: That should become recoverable state." and "Keep execution
result separate from delivery result."

The queue turns an outbound send into a durable record with an honest
lifecycle:

    pending (attempt 0, due now)
      → immediate attempt
          → delivered   (provider ack observed)
          → pending (retrying: attempts+1, backoff until next_attempt_at)
              → ... maintenance retries ...
                  → delivered
                  → failed (attempts exhausted or deliverability
                            horizon passed — terminal, loud, queryable)

The queue is interface-agnostic: it sends through the DeliveryRouter,
so whichever interface adapters are attached is whichever interface gets
served. It is runtime infrastructure — no AI in the loop, no domain
concepts, no fabricated success: a delivery is "delivered" only when
the interface sender returned without raising.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.delivery_models import DeliveryRecord

log = get_logger(__name__)


class DeliveryQueue:
    """Enqueue, attempt, and retry outbound deliveries as durable state."""

    def __init__(
        self,
        session: AsyncSession,
        services: Any,
        *,
        retry_backoff_seconds: float = 60.0,
        backoff_factor: float = 2.0,
        max_age_seconds: float = 86400.0,
    ) -> None:
        self._session = session
        self._services = services
        self._retry_backoff_seconds = retry_backoff_seconds
        self._backoff_factor = backoff_factor
        self._max_age_seconds = max_age_seconds

    # --- enqueue ----------------------------------------------------------

    async def enqueue(
        self,
        *,
        principal_id: str,
        interface_kind: str,
        recipient_id: str,
        text: str,
        source: str,
        execution_id: str | None = None,
        max_attempts: int = 5,
    ) -> DeliveryRecord:
        """Record a delivery the runtime OWES. Never sends by itself."""
        record = DeliveryRecord(
            id=str(ULID()),
            principal_id=principal_id,
            interface_kind=interface_kind,
            recipient_id=recipient_id,
            text=text,
            status="pending",
            attempts=0,
            max_attempts=max_attempts,
            source=source,
            execution_id=execution_id,
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "delivery.enqueued",
            delivery_id=record.id,
            interface=interface_kind,
            source=source,
            principal_id=principal_id,
        )
        return record

    # --- attempts ---------------------------------------------------------

    async def attempt(self, record: DeliveryRecord) -> bool:
        """One delivery attempt against the interface router.

        Returns True when delivered. Exhaustion and horizon expiry are
        terminal honest failures — the record says exactly what happened.
        """
        if record.status != "pending":
            return record.status == "delivered"

        now = datetime.now(UTC)

        # Deliverability horizon: a message that could not be delivered
        # within the configured window is honestly failed, not retried
        # forever. (For WhatsApp this matches the 24h customer-service
        # window; the setting itself is interface-agnostic.)
        created = record.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created is not None and (now - created) > timedelta(seconds=self._max_age_seconds):
            record.status = "failed"
            record.last_error = (
                f"deliverability horizon passed "
                f"({int(self._max_age_seconds)}s); not retried further"
            )
            await self._session.flush()
            log.warning(
                "delivery.expired",
                delivery_id=record.id,
                interface=record.interface_kind,
                age_s=self._max_age_seconds,
            )
            return False

        sender_attached = self._services.delivery.has(record.interface_kind)
        if not sender_attached:
            # No interface attached is a real condition (worker boot,
            # adapter down). It is a retryable failure, not a success.
            error = f"no delivery interface attached for {record.interface_kind!r}"
        else:
            try:
                await self._services.delivery.send(
                    record.interface_kind, record.recipient_id, record.text
                )
            except Exception as e:
                error = f"{type(e).__name__}: {e}"[:2000]
            else:
                record.status = "delivered"
                record.delivered_at = datetime.now(UTC)
                record.last_error = None
                await self._session.flush()
                log.info(
                    "delivery.delivered",
                    delivery_id=record.id,
                    interface=record.interface_kind,
                    attempts=record.attempts + 1,
                )
                return True

        record.attempts += 1
        record.last_error = error
        if record.attempts >= record.max_attempts:
            record.status = "failed"
            await self._session.flush()
            log.error(
                "delivery.failed_terminal",
                delivery_id=record.id,
                interface=record.interface_kind,
                attempts=record.attempts,
                error=error[:300],
            )
            return False

        backoff = self._retry_backoff_seconds * (self._backoff_factor ** (record.attempts - 1))
        record.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
        await self._session.flush()
        log.warning(
            "delivery.retry_scheduled",
            delivery_id=record.id,
            interface=record.interface_kind,
            attempt=record.attempts,
            retry_in_s=int(backoff),
            error=error[:300],
        )
        return False

    # --- retry sweep (maintenance) -----------------------------------------

    async def retry_due(self, *, limit: int = 50) -> dict[str, int]:
        """Retry every due pending delivery. Returns sweep counters."""
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(DeliveryRecord)
            .where(DeliveryRecord.status == "pending")
            .where(
                (DeliveryRecord.next_attempt_at.is_(None)) | (DeliveryRecord.next_attempt_at <= now)
            )
            .order_by(DeliveryRecord.created_at.asc())
            .limit(limit)
        )
        due = list(result.scalars().all())
        delivered = 0
        for record in due:
            if await self.attempt(record):
                delivered += 1
        return {"due": len(due), "delivered": delivered, "retrying": len(due) - delivered}

    async def expire_stale(self) -> int:
        """Terminal-fail pending records past the deliverability horizon."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._max_age_seconds)
        result = await self._session.execute(
            select(DeliveryRecord)
            .where(DeliveryRecord.status == "pending")
            .where(DeliveryRecord.created_at < cutoff)
            .limit(200)
        )
        stale = list(result.scalars().all())
        for record in stale:
            record.status = "failed"
            record.last_error = (
                f"deliverability horizon passed "
                f"({int(self._max_age_seconds)}s); not retried further"
            )
        if stale:
            await self._session.flush()
            log.warning("delivery.expired_stale", count=len(stale))
        return len(stale)
