"""Runtime maintenance loop — lifecycle hygiene the runtime owns.

Four deterministic sweeps, one loop, in the lifespan alongside the other
reapers:

1. Approval expiry — pending approvals past their deadline become
   `expired` (the authority boundary never leaves a stale YES available).
2. Signal-ledger retention — old and excess ledger rows are pruned with
   the waiter-safety rule (a signal a pending event-wake can still be
   woken by is never deleted).
3. Delivery retries — outbound messages that could not be delivered are
   retried with backoff until delivered, exhausted, or past the
   deliverability horizon (ADR-0021, mission §55).
4. Observability — each sweep emits structured logs and metrics so the
   operator sees what the runtime cleaned up.

Nothing here is AI-driven and nothing here is domain-specific: it is
pure lifecycle infrastructure.
"""

from __future__ import annotations

import contextlib
from typing import Any

from wax.runtime.leadership import MaintenanceLeadership
from wax.runtime.logging import get_logger

log = get_logger(__name__)


def _metric():  # type: ignore[no-untyped-def]
    from wax.observability.runtime_metrics import get_runtime_metrics

    return get_runtime_metrics()


async def run_maintenance_pass(settings: Any, services: Any = None) -> dict[str, Any]:
    """One maintenance pass. Returns counters for logging/tests.

    ``services`` is the live RuntimeServices container; when omitted
    (legacy tests / follower skip paths) the delivery-retry sweep is
    skipped — retrying against an empty router would burn attempts.

    Multi-instance correctness: the pass runs on the LEADER only
    (Postgres advisory-lock election). Followers skip and say so —
    the skip is visible in the return value, the log, and metrics.
    """
    from wax.authority.approvals import ApprovalService
    from wax.continuity.repository import ConversationRepository
    from wax.runtime.work.signals import SignalRepository
    from wax.state.engine import db_session

    leadership = await MaintenanceLeadership.acquire(settings)
    try:
        if not leadership.is_leader:
            _metric().maintenance_followed()
            return {
                "skipped": "not_leader",
                "leadership_mode": leadership.mode,
                "expired_approvals": 0,
                "signals_pruned": 0,
                "delivery_retries": {},
            }

        results: dict[str, Any] = {
            "expired_approvals": 0,
            "signals_pruned": 0,
            "delivery_retries": {},
            "leadership_mode": leadership.mode,
        }

        async with db_session() as session:
            expired = await ApprovalService(session).expire_due()
            results["expired_approvals"] = len(expired)
            if expired:
                _metric().approval_expired()
            await session.commit()

        async with db_session() as session:
            stats = await SignalRepository(session).prune(
                retention_seconds=float(settings.signal_retention_seconds),
                max_rows=int(settings.signal_max_ledger_rows),
            )
            await session.commit()
        pruned = int(stats.get("retention_deleted", 0)) + int(stats.get("bound_deleted", 0))
        results["signals_pruned"] = pruned
        results["signal_prune_stats"] = stats
        if pruned:
            _metric().signals_pruned(float(pruned))

        # 3. Delivery retries (ADR-0021): every due pending outbound
        # message gets one attempt through the delivery router. Requires
        # the LIVE services container (the interface senders are
        # registered on it); without one the sweep is skipped honestly —
        # an empty router would burn attempts on "no sender attached".
        if services is not None:
            from wax.runtime.delivery_queue import DeliveryQueue

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

        # 4. Conversation lifecycle (audit fix: the mechanism existed and
        # advertised itself as a background task, but NO task ever called
        # it — false mechanism). Wired here: active → idle after the idle
        # timeout, idle → archived past the archive horizon. State
        # marking only — no row is deleted, so this is lifecycle hygiene,
        # not retention policy.
        async with db_session() as session:
            archived = await ConversationRepository(session).archive_stale()
            await session.commit()
        results["conversations_archived"] = archived
        if archived:
            log.info("runtime.conversation_lifecycle_pass", count=archived)

        _metric().maintenance_led()
        if results["expired_approvals"] or pruned or results["delivery_retries"].get("due", 0):
            log.info("runtime.maintenance_pass", **results)
        return results
    finally:
        await leadership.release()


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
