"""Runtime maintenance loop — lifecycle hygiene the runtime owns.

Deterministic sweeps, one loop, in the lifespan alongside the other
reapers:

1. Approval expiry — pending approvals past their deadline become
   `expired` (the authority boundary never leaves a stale YES available).
2. Signal-ledger retention — old and excess ledger rows are pruned with
   the waiter-safety rule (a signal a pending event-wake can still be
   woken by is never deleted).
3. Delivery retries — outbound messages that could not be delivered are
   retried with backoff until delivered, exhausted, or past the
   deliverability horizon (ADR-0021, mission §55).
4. Conversation lifecycle — active → idle → archived marking.
5. Blob-store garbage collection (opt-in, WAX_BLOB_GC_ENABLED).
6. Audit-ledger retention (opt-in: WAX_AUDIT_RETENTION_DAYS age window,
   WAX_AUDIT_MAX_ROWS row bound — each an explicit operator decision).
7. Observability — each sweep emits structured logs and metrics so the
   operator sees what the runtime cleaned up.

Nothing here is AI-driven and nothing here is domain-specific: it is
pure lifecycle infrastructure.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


def _metric():  # type: ignore[no-untyped-def]
    from wax.observability.runtime_metrics import get_runtime_metrics

    return get_runtime_metrics()


async def run_maintenance_pass(settings: Any, services: Any = None) -> dict[str, Any]:
    """One maintenance pass. Returns counters for logging/tests.

    ``services`` is the live RuntimeServices container; when omitted
    the delivery-retry sweep is skipped — retrying against an empty
    router would burn attempts.
    """
    from wax.authority.approvals import ApprovalService
    from wax.continuity.repository import ConversationRepository
    from wax.runtime.work.signals import SignalRepository
    from wax.state.engine import db_session

    results: dict[str, Any] = {
        "expired_approvals": 0,
        "signals_pruned": 0,
        "delivery_retries": {},
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

    # 5. Blob-store garbage collection (OPT-IN via WAX_BLOB_GC_ENABLED).
    # Deletion is an explicit operator decision, never a silent
    # default: when the gate is off this sweep does nothing at all.
    # When enabled, blobs no longer referenced by ANY live workspace
    # snapshot are unreachable by construction and their disk space
    # is reclaimed (the blob store refuses everything else).
    results["blob_gc"] = {"skipped": "disabled"}
    if getattr(settings, "blob_gc_enabled", False) and services is not None:
        from sqlalchemy import select

        from wax.state.workspace_models import WorkspaceSnapshotRecord

        blob_store = getattr(services, "blob_store", None)
        if blob_store is not None:
            async with db_session() as session:
                rows = await session.execute(select(WorkspaceSnapshotRecord.files_json))
                await session.commit()
            live: set[str] = set()
            for files in rows.scalars():
                if isinstance(files, list):
                    for entry in files:
                        if isinstance(entry, dict):
                            digest = entry.get("sha256")
                            if isinstance(digest, str) and digest:
                                live.add(digest)
            gc = blob_store.collect_garbage(live)
            results["blob_gc"] = gc
            _metric().control_blob_gc_run(
                removed=gc["removed"], reclaimed_bytes=gc["reclaimed_bytes"]
            )
            if gc["removed"]:
                log.info("runtime.blob_gc_pass", **gc)
                # Deletion is the security-relevant fact — when the
                # scheduled sweep actually removes blobs, append a
                # system-actor audit row so the operator ledger shows
                # WHO deleted (here: nobody human). No-op passes
                # (removed == 0) are deliberately NOT audited: they
                # would flood the append-only ledger (~288 rows/day)
                # with non-facts; the metric + this log line already
                # record that the pass ran.
                try:
                    from wax.observability.audit import record_audit_event

                    async with db_session() as audit_session:
                        await record_audit_event(
                            audit_session,
                            actor_principal_id=None,
                            actor_kind="system",
                            event_kind="system.blob_gc",
                            outcome="success",
                            payload={
                                "removed": gc["removed"],
                                "reclaimed_bytes": gc["reclaimed_bytes"],
                            },
                        )
                        await audit_session.commit()
                except Exception as e:  # audit write must never break the pass
                    log.warning(
                        "runtime.blob_gc_audit_failed",
                        error=str(e)[:200],
                        error_type=type(e).__name__,
                    )
        else:
            results["blob_gc"] = {"skipped": "no_blob_store"}

    # 6. Audit-ledger retention (OPT-IN via WAX_AUDIT_RETENTION_DAYS
    # and WAX_AUDIT_MAX_ROWS). The audit ledger is append-only
    # (INV-06): the application never updates or silently deletes
    # history. When the operator configures an explicit retention
    # policy — an age window, a row bound, or both — the scheduled
    # pass removes what is past it, the same "deletion is a
    # decision, never a default" philosophy as blob GC. Both gates
    # off (default) → the sweep does nothing and the ledger grows
    # unbounded (the audit page shows a growth warning so the
    # operator always knows).
    results["audit_events_pruned"] = 0
    results["audit_events_overflow_pruned"] = 0
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
        pruned_audit = int(deleted.rowcount or 0)
        results["audit_events_pruned"] = pruned_audit
    if max_rows > 0:
        from sqlalchemy import delete, func, select

        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            total = (
                await session.execute(select(func.count()).select_from(AuditEvent))
            ).scalar_one()
            overflow = max(0, int(total) - max_rows)
            if overflow:
                # the OLDEST rows beyond the bound go first; ULIDs are
                # time-ordered so (created_at, id) ordering is stable
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
    if results["audit_events_pruned"] or results["audit_events_overflow_pruned"]:
        log.info(
            "runtime.audit_retention_pass",
            removed_by_age=results["audit_events_pruned"],
            removed_by_bound=results["audit_events_overflow_pruned"],
            retention_days=retention_days,
            max_rows=max_rows,
        )

    _metric().maintenance_led()
    if results["expired_approvals"] or pruned or results["delivery_retries"].get("due", 0):
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
