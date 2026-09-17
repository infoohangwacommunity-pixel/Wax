"""Runtime maintenance loop — lifecycle hygiene.

Simplified for the open-world architecture. Removed:
- approval expiry (approvals are gone)
- blob-store GC (blob store is gone)
- leadership / multi-instance election (single-instance)

Kept:
- signal-ledger retention
- delivery retries
- conversation lifecycle (active → idle → archived)
- audit-ledger retention (opt-in)
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


async def run_maintenance_pass(settings: Any, services: Any = None) -> dict[str, Any]:
    """One maintenance pass. Returns counters for logging/tests."""
    from wax.continuity.repository import ConversationRepository
    from wax.runtime.delivery_queue import DeliveryQueue
    from wax.runtime.work.signals import SignalRepository
    from wax.state.engine import db_session

    results: dict[str, Any] = {
        "signals_pruned": 0,
        "delivery_retries": {},
        "conversations_archived": 0,
        "audit_events_pruned": 0,
    }

    # 1. Signal-ledger retention
    async with db_session() as session:
        stats = await SignalRepository(session).prune(
            retention_seconds=float(settings.signal_retention_seconds),
            max_rows=int(settings.signal_max_ledger_rows),
        )
        await session.commit()
    pruned = int(stats.get("retention_deleted", 0)) + int(stats.get("bound_deleted", 0))
    results["signals_pruned"] = pruned
    results["signal_prune_stats"] = stats

    # 2. Delivery retries
    if services is not None:
        async with db_session() as session:
            queue = DeliveryQueue(
                session,
                services,
                retry_backoff_seconds=float(settings.delivery_retry_backoff_seconds),
                max_age_seconds=float(settings.delivery_max_age_seconds),
            )
            retry_stats = await queue.retry_due()
            await session.commit()
        results["delivery_retries"] = retry_stats
        if retry_stats["due"]:
            log.info("runtime.delivery_retry_pass", **retry_stats)

    # 3. Conversation lifecycle (active → idle → archived)
    async with db_session() as session:
        archived = await ConversationRepository(session).archive_stale()
        await session.commit()
    results["conversations_archived"] = archived
    if archived:
        log.info("runtime.conversation_lifecycle_pass", count=archived)

    # 4. Audit-ledger retention (opt-in)
    retention_days = int(getattr(settings, "audit_retention_days", 0) or 0)
    max_rows = int(getattr(settings, "audit_max_rows", 0) or 0)
    if retention_days > 0:
        from sqlalchemy import delete

        from wax.state.audit_models import AuditEvent

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with db_session() as session:
            deleted = await session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < cutoff)
            )
            await session.commit()
        results["audit_events_pruned"] = int(deleted.rowcount or 0)
    if max_rows > 0:
        from sqlalchemy import delete, func, select

        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            total = (
                await session.execute(select(func.count()).select_from(AuditEvent))
            ).scalar_one()
            overflow = max(0, int(total) - max_rows)
            if overflow:
                oldest_ids = (
                    (
                        await session.execute(
                            select(AuditEvent.id)
                            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                            .limit(overflow)
                        )
                    )
                    .scalars()
                    .all()
                )
                deleted = await session.execute(
                    delete(AuditEvent).where(AuditEvent.id.in_(list(oldest_ids)))
                )
                await session.commit()
                results["audit_events_overflow_pruned"] = int(deleted.rowcount or 0)

    if (
        results["signals_pruned"]
        or results["delivery_retries"].get("due", 0)
        or results["conversations_archived"]
        or results["audit_events_pruned"]
    ):
        log.info("runtime.maintenance_pass", **results)
    return results


async def maintenance_loop(
    settings: Any, interval_seconds: float = 300.0, services: Any = None
) -> None:
    """Periodic maintenance sweep. Runs as a lifespan task."""
    import asyncio

    log.info("runtime.maintenance_started", interval_s=interval_seconds)
    while True:
        try:
            await run_maintenance_pass(settings, services=services)
        except Exception as e:
            log.error(
                "runtime.maintenance_error",
                error=str(e),
                error_type=type(e).__name__,
            )
        await asyncio.sleep(interval_seconds)


async def stop_maintenance(task: object | None) -> None:
    """Cancel the maintenance task (lifespan shutdown hook)."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
