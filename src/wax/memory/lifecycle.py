"""Memory lifecycle worker — forgetting is a runtime mechanism, not an AI chore.

`MemoryRepository.expire_due()` existed from Phase F but nothing ever
called it: memories with an expires_at lived forever, which is both a
resource leak and a privacy failure (the Foundation PDF names retention
as a runtime concern; the audit's cleanup findings apply to memory too).

This worker runs in the lifespan alongside the provisioning reaper. Each
tick it finds expired active memories and FORGETS them (soft delete —
records retained for audit, excluded from retrieval), writing an audit
event and a metric per memory. The runtime decides expiry; no AI has to
remember to clean up, and no domain policy is embedded here: TTLs are
set per-memory at creation time by whoever created it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


def _metric():  # type: ignore[no-untyped-def]
    from wax.observability.runtime_metrics import get_runtime_metrics

    return get_runtime_metrics()


async def expire_due_memories(session: Any) -> int:
    """One sweep: forget every active memory whose expires_at has passed.

    Returns the number forgotten. Caller owns the commit.
    """
    from wax.memory.repository import MemoryRepository
    from wax.observability.audit import record_audit_event

    repo = MemoryRepository(session)
    due = await repo.expire_due()
    for memory in due:
        await repo.forget(memory.id)
        await record_audit_event(
            session,
            actor_principal_id=memory.principal_id,
            actor_kind="system",
            event_kind="memory.expired",
            outcome="success",
            payload={"memory_id": memory.id, "kind": memory.kind},
        )
        _metric().memory_expired(memory.kind)
    if due:
        log.info("memory.expired_batch", count=len(due))
    return len(due)


async def memory_maintenance_loop(settings: Any, interval_seconds: float = 300.0) -> None:
    """Periodic expiry sweep. Runs as a lifespan task.

    Multi-instance honesty (§61 audit fix): like every other lifecycle
    sweep, it runs on the LEADER only (the same advisory-lock election
    the maintenance loop uses). Followers skip and say so — the sweep is
    idempotent, but N replicas re-forgetting the same rows multiplies
    audit noise and contention for nothing.
    """
    import asyncio

    from wax.runtime.leadership import MaintenanceLeadership
    from wax.state.engine import db_session

    log.info("memory.maintenance_started", interval_s=interval_seconds)
    while True:
        try:
            leadership = await MaintenanceLeadership.acquire(settings)
            try:
                if leadership.is_leader:
                    async with db_session() as session:
                        await expire_due_memories(session)
                        await session.commit()
                else:
                    log.debug("memory.maintenance.follower", mode=leadership.mode)
            finally:
                await leadership.release()
        except Exception as e:
            log.error(
                "memory.maintenance_error",
                error=str(e),
                error_type=type(e).__name__,
            )
        await asyncio.sleep(interval_seconds)


async def stop_maintenance(task: object | None) -> None:
    """Cancel the memory worker task (lifespan shutdown hook)."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
