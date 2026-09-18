"""Runtime maintenance loop — lifecycle hygiene.

Simplified for the open-world architecture. Removed:

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

    # Fix 7: Memory consolidation (daily, for principals with many episodic memories)
    results["memories_consolidated"] = await _run_consolidation(services)

    # Fix 10: Workspace cleanup — remove per-execution subdirectories older than 7 days
    results["workspaces_cleaned"] = _cleanup_old_workspaces(settings)

    # Spec §5: Stale process detection — mark dead processes
    results["processes_reaped"] = await _reap_stale_processes()

    return results


async def _reap_stale_processes() -> int:
    """Detect processes whose PID no longer exists (container restarted).

    Marks them as 'dead' in the process registry. The associated Work
    can then decide whether to restart.
    """
    try:
        import os

        from sqlalchemy import select

        from wax.state.engine import db_session
        from wax.state.process_models import ProcessRecord

        reaped = 0
        async with db_session() as session:
            result = await session.execute(
                select(ProcessRecord).where(ProcessRecord.status == "running")
            )
            for proc in result.scalars():
                if proc.pid is None:
                    continue
                try:
                    os.kill(proc.pid, 0)  # signal 0 = check if process exists
                except (ProcessLookupError, PermissionError):
                    # Process is dead — mark it
                    proc.status = "dead"
                    proc.termination_reason = "process disappeared (container restart or exit)"
                    proc.terminated_at = datetime.now(UTC)
                    reaped += 1
            if reaped:
                await session.commit()
                log.info("maintenance.processes_reaped", count=reaped)
        return reaped
    except Exception as e:
        log.warning("maintenance.process_reap_failed", error=str(e)[:200])
        return 0


async def _run_consolidation(services: Any) -> int:
    """Run memory consolidation for principals with many episodic memories.

    This is the 'reflection' pass — like sleep consolidation. Finds
    principals with 20+ active episodic memories and distills clusters
    into stronger semantic memories.
    """
    if services is None:
        return 0
    try:
        from sqlalchemy import select

        from wax.state.engine import db_session
        from wax.state.memory_models import MemoryRecord

        consolidated = 0
        async with db_session() as session:
            # Simple approach: get distinct principals with episodic memories
            principals = set()
            all_result = await session.execute(
                select(MemoryRecord.principal_id)
                .where(
                    MemoryRecord.kind == "episodic",
                    MemoryRecord.status == "active",
                )
                .distinct()
                .limit(10)
            )
            for row in all_result.scalars():
                principals.add(row)

        # Run consolidation for each principal
        from wax.memory.consolidation import consolidate_principal_memories

        for principal_id in principals:
            try:
                async with db_session() as session:
                    count = await consolidate_principal_memories(
                        session=session,
                        intelligence=services._intelligence
                        if hasattr(services, "_intelligence")
                        else None,
                        principal_id=principal_id,
                    )
                    await session.commit()
                    consolidated += count
            except Exception as e:
                log.warning(
                    "maintenance.consolidation_failed",
                    principal_id=principal_id,
                    error=str(e)[:200],
                )

        return consolidated
    except Exception as e:
        log.warning("maintenance.consolidation_error", error=str(e)[:200])
        return 0


def _cleanup_old_workspaces(settings: Any) -> int:
    """Remove per-execution workspace subdirectories older than 7 days.

    Keeps the per-principal root directory intact. Only removes the
    per-execution subdirectories under {root}/{principal_id}/executions/.
    """
    import shutil
    from pathlib import Path

    ws_root = getattr(settings, "terminal_working_dir_root", "./wax-workspaces")
    root_path = Path(ws_root)
    if not root_path.exists():
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=7)
    cleaned = 0

    try:
        for principal_dir in root_path.iterdir():
            if not principal_dir.is_dir():
                continue
            executions_dir = principal_dir / "executions"
            if not executions_dir.exists():
                continue
            for exec_dir in executions_dir.iterdir():
                if not exec_dir.is_dir():
                    continue
                # Check modification time
                mtime = datetime.fromtimestamp(exec_dir.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    shutil.rmtree(exec_dir, ignore_errors=True)
                    cleaned += 1
    except Exception as e:
        log.warning("maintenance.workspace_cleanup_failed", error=str(e)[:200])

    return cleaned


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
