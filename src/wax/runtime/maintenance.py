"""Runtime maintenance loop — lifecycle hygiene the runtime owns.

Three deterministic sweeps, one loop, in the lifespan alongside the other
reapers:

1. Approval expiry — pending approvals past their deadline become
   `expired` (the authority boundary never leaves a stale YES available).
2. Signal-ledger retention — old and excess ledger rows are pruned with
   the waiter-safety rule (a signal a pending event-wake can still be
   woken by is never deleted).
3. Observability — each sweep emits structured logs and metrics so the
   operator sees what the runtime cleaned up.

Nothing here is AI-driven and nothing here is domain-specific: it is
pure lifecycle infrastructure.
"""

from __future__ import annotations

import contextlib
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


def _metric():  # type: ignore[no-untyped-def]
    from wax.observability.runtime_metrics import get_runtime_metrics

    return get_runtime_metrics()


async def run_maintenance_pass(settings: Any) -> dict[str, Any]:
    """One maintenance pass. Returns counters for logging/tests."""
    from wax.authority.approvals import ApprovalService
    from wax.runtime.work.signals import SignalRepository
    from wax.state.engine import db_session

    results: dict[str, Any] = {"expired_approvals": 0, "signals_pruned": 0}

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
    pruned = int(stats.get("retention_deleted", 0)) + int(
        stats.get("bound_deleted", 0)
    )
    results["signals_pruned"] = pruned
    results["signal_prune_stats"] = stats
    if pruned:
        _metric().signals_pruned(float(pruned))

    if results["expired_approvals"] or pruned:
        log.info("runtime.maintenance_pass", **results)
    return results


async def maintenance_loop(settings: Any, interval_seconds: float = 300.0) -> None:
    """Periodic maintenance sweep. Runs as a lifespan task."""
    import asyncio

    from wax.state.engine import db_session

    log.info("runtime.maintenance_started", interval_s=interval_seconds)
    while True:
        try:
            await run_maintenance_pass(settings)
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
