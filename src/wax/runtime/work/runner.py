"""WorkRunner — the background runtime that makes work survive time.

Phase R (durable work) + Phase V (background runtime): the runtime — not
the AI — owns time. The runner is an in-process asyncio worker started by
the app lifespan. The lease design (atomic claims with SKIP LOCKED row
locks, fencing on terminal writes, lease heartbeats, expired-lease
reclaim) admits MANY runners concurrently — additional processes or
replicas just construct another WorkRunner against the same database.

Loop:
1. claim_due(): take leased ownership of due or condition-satisfied items
   (crash-safe; event-wake items are correlated against the signal ledger;
   concurrent workers can never claim the same row). The batch also carries
   the pass's DEATHS — expired waits and reclaim-exhausted items — which
   are announced here, in the same transaction that persisted them.
2. dispatch each claimed item to its registered handler, with a HEARTBEAT:
   while the handler runs, the worker renews its lease (a healthy slow
   worker is never mistaken for a dead one).
3. success → mark_succeeded (FENCED: a zombie worker whose lease was
   reclaimed cannot write its result); failure → mark_failed with backoff;
   exhausted attempts → dead + dead-letter row.
4. Terminal states are ANNOUNCED: the runner appends a runtime signal
   (work.succeeded:<id> / work.dead:<id> / work.expired:<id>) to the event
   ledger, so other work can wait on "this work finished" or honestly
   react to "this work died" — dependency composition without any
   workflow engine.

Recovery scan (on startup): executions left "running" by a previous process
and processed_messages left "pending" are reconciled to failed/retryable so
Meta redeliveries can retry — closing the crash hole the audit found
(Section 8: durable-state machinery existed but nothing ever resumed).

Graceful shutdown is part of the contract: stop() stops claiming and lets
in-flight items finish (bounded by a grace period) before the process
exits — a shutdown never orphans a running item.

The runner knows NOTHING about use cases. Handlers are registered
mechanisms; today there is exactly one: run a capability.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ulid import ULID

from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.runtime.work.repository import WorkRepository
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)

# A handler receives (services, item) and returns a JSON-safe result dict.
# Raise to trigger retry semantics.
WorkHandler = Callable[[RuntimeServices, WorkItemRecord], Awaitable[dict[str, Any]]]


class WorkExecutionError(Exception):
    """Raised by handlers to signal the attempt failed (retry semantics)."""


class WorkRunner:
    """In-process background worker for durable work (multi-worker safe)."""

    def __init__(
        self,
        services: RuntimeServices,
        *,
        poll_interval_seconds: float = 2.0,
        lease_seconds: float = 120.0,
        batch_size: int = 10,
        retry_backoff_seconds: float = 30.0,
        stale_execution_seconds: float = 900.0,
        max_concurrency: int = 1,
    ) -> None:
        self._services = services
        self._poll_interval = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._retry_backoff = retry_backoff_seconds
        self._stale_execution_seconds = stale_execution_seconds
        self._max_concurrency = max(1, max_concurrency)
        self._worker_id = f"worker-{str(ULID())[:8]}"
        self._handlers: dict[str, WorkHandler] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._heartbeats: set[asyncio.Task] = set()

    # --- Handler registration --------------------------------------------

    def register_handler(self, kind: str, handler: WorkHandler) -> None:
        self._handlers[kind] = handler
        log.info("work.handler_registered", kind=kind)

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="wax-work-runner")
        log.info(
            "work.runner_started",
            worker_id=self._worker_id,
            poll_interval_s=self._poll_interval,
        )

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        """Graceful shutdown: stop claiming, let in-flight items finish.

        Bounded by `grace_seconds`; if work is still in-flight past the
        grace period the task is cancelled and the items' leases expire —
        another worker reclaims them (the standard recovery path). No item
        is ever lost to a shutdown, and a finished shutdown never leaves a
        half-written state.
        """
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
            self._task = None
        for hb in list(self._heartbeats):
            hb.cancel()
        self._heartbeats.clear()
        log.info("work.runner_stopped", worker_id=self._worker_id)

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as e:
                # The loop MUST survive handler/DB failures.
                log.error(
                    "work.loop_error",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            if self._stopping.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

    # --- Core --------------------------------------------------------------

    async def run_once(self) -> int:
        """Claim and process one batch. Returns the number of items run."""
        from wax.reliability.dead_letter import DeadLetterRepository
        from wax.runtime.work.signals import SignalRepository
        from wax.state.engine import db_session

        async with db_session() as session:
            repo = WorkRepository(session)
            batch = await repo.claim_due(
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                limit=self._batch_size,
            )
            # Announce every death this pass produced, in the SAME
            # transaction that persisted it — no silent deaths.
            for item in batch.expired:
                await SignalRepository(session).emit(
                    f"work.expired:{item.id}",
                    payload={"work_id": item.id, "kind": item.kind},
                    emitted_by="work_runner",
                )
            for item in batch.reclaim_dead:
                await DeadLetterRepository(session).record(
                    kind=f"work.{item.kind}",
                    principal_id=item.principal_id,
                    execution_id=item.execution_id,
                    error_type="LeaseExhausted",
                    error_message=item.last_error or "lease expired; attempts exhausted",
                    attempts=item.attempts,
                    payload=item.payload,
                )
                await SignalRepository(session).emit(
                    f"work.dead:{item.id}",
                    payload={"work_id": item.id, "kind": item.kind, "error": (item.last_error or "")[:500]},
                    emitted_by="work_runner",
                )
            claimed = list(batch.claimed)
            if batch.reclaimed:
                self._services.metrics.work_reclaimed()
            await session.commit()

        if not claimed:
            self._services.metrics.work_inflight(float(await self._inflight()))
            return 0

        if self._max_concurrency == 1:
            for item in claimed:
                await self._process_item(item)
        else:
            sem = asyncio.Semaphore(self._max_concurrency)

            async def _guarded(rec: WorkItemRecord) -> None:
                async with sem:
                    await self._process_item(rec)

            await asyncio.gather(*(_guarded(item) for item in claimed))

        self._services.metrics.work_inflight(float(await self._inflight()))
        return len(claimed)

    def _spawn_heartbeat(self, item_id: str) -> asyncio.Task:
        """Renew this item's lease while its handler runs.

        Interval is lease/3: two failed renewals still leave a live lease,
        so a healthy slow worker is never reclaimed by mistake. The task
        ends itself when ownership is lost or the item finishes.
        """

        async def _beat() -> None:
            from wax.state.engine import db_session

            interval = max(self._lease_seconds / 3.0, 0.05)
            while True:
                await asyncio.sleep(interval)
                try:
                    async with db_session() as session:
                        renewed = await WorkRepository(session).renew_lease(
                            item_id,
                            worker_id=self._worker_id,
                            lease_seconds=self._lease_seconds,
                        )
                        await session.commit()
                    if not renewed:
                        return
                except Exception as e:
                    log.warning(
                        "work.heartbeat_error",
                        work_id=item_id,
                        error=str(e),
                    )

        task = asyncio.create_task(_beat(), name=f"wax-heartbeat-{item_id}")
        self._heartbeats.add(task)
        task.add_done_callback(self._heartbeats.discard)
        return task

    async def _process_item(self, item: WorkItemRecord) -> None:
        from wax.runtime.work.signals import SignalRepository
        from wax.state.engine import db_session

        handler = self._handlers.get(item.kind)
        kind = item.kind
        heartbeat: asyncio.Task | None = None
        try:
            if handler is None:
                raise WorkExecutionError(f"No handler registered for kind={kind!r}")
            async with db_session() as session:
                outcome = await WorkRepository(session).mark_running(
                    item.id, expected_owner=self._worker_id
                )
                await session.commit()
            if outcome != "running":
                # Fenced or already terminal: another attempt owns this item.
                self._services.metrics.work_fenced()
                return

            heartbeat = self._spawn_heartbeat(item.id)
            result = await handler(self._services, item)

            async with db_session() as session:
                status = await WorkRepository(session).mark_succeeded(
                    item.id, result, expected_owner=self._worker_id
                )
                if status == "succeeded":
                    # Announce the terminal state on the event ledger: other
                    # work may be waiting for THIS work to finish.
                    await SignalRepository(session).emit(
                        f"work.succeeded:{item.id}",
                        payload={"work_id": item.id, "kind": kind},
                        emitted_by="work_runner",
                    )
                else:
                    self._services.metrics.work_fenced()
                await session.commit()
            self._services.metrics.work_woken("succeeded", kind)
        except Exception as e:
            self._services.metrics.work_woken("failed", kind)
            await self._fail_item(item, e)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()

    async def _fail_item(self, item: WorkItemRecord, error: Exception) -> None:
        from wax.reliability.dead_letter import DeadLetterRepository
        from wax.runtime.work.signals import SignalRepository
        from wax.state.engine import db_session

        try:
            async with db_session() as session:
                repo = WorkRepository(session)
                status = await repo.mark_failed(
                    item.id,
                    f"{type(error).__name__}: {error}",
                    backoff_seconds=self._retry_backoff,
                    expected_owner=self._worker_id,
                )
                if status == "fenced":
                    # A zombie worker's failure report is discarded.
                    self._services.metrics.work_fenced()
                    await session.commit()
                    return
                if status == "dead":
                    await DeadLetterRepository(session).record(
                        kind=f"work.{item.kind}",
                        principal_id=item.principal_id,
                        execution_id=item.execution_id,
                        error_type=type(error).__name__,
                        error_message=str(error)[:5000],
                        attempts=item.attempts,
                        payload=item.payload,
                    )
                    # Dead is terminal too — announce it so dependents can
                    # react honestly (retry, compensate, notify the human).
                    await SignalRepository(session).emit(
                        f"work.dead:{item.id}",
                        payload={
                            "work_id": item.id,
                            "kind": item.kind,
                            "error": str(error)[:500],
                        },
                        emitted_by="work_runner",
                    )
                await session.commit()
        except Exception as finalize_error:
            log.critical(
                "work.finalize_failed",
                work_id=item.id,
                error=str(finalize_error),
            )

    async def _inflight(self) -> int:
        from wax.state.engine import db_session

        try:
            async with db_session() as session:
                return await WorkRepository(session).count_inflight()
        except Exception:
            return 0

    # --- Recovery ----------------------------------------------------------

    async def recover_orphans(self) -> dict[str, int]:
        """Reconcile state a dead process left behind.

        - executions stuck in "running" since before the stale threshold →
          failed ("runtime restart"), so the record is honest.
        - processed_messages stuck in "pending" (work was accepted, the
          process died mid-LLM) → "failed", which the bridge treats as
          retryable on Meta redelivery.

        Returns counters for logging/observability.
        """
        from sqlalchemy import select

        from wax.state.bridge_models import ProcessedMessageRecord
        from wax.state.engine import db_session
        from wax.state.execution_models import ExecutionRecord

        cutoff = datetime.now(UTC) - timedelta(seconds=self._stale_execution_seconds)
        stale_ids: list[str] = []
        retriable_messages = 0
        async with db_session() as session:
            result = await session.execute(
                select(ExecutionRecord).where(ExecutionRecord.status == "running")
            )
            for execution in result.scalars():
                started = execution.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if started is not None and started < cutoff:
                    execution.status = "failed"
                    execution.ended_at = datetime.now(UTC)
                    execution.error = "runtime restart or crash (recovered by work runner)"
                    stale_ids.append(execution.id)
            if stale_ids:
                # Their idempotency locks become retryable: a redelivery of
                # the same message ID will re-run the work.
                message_result = await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.outcome == "pending",
                        ProcessedMessageRecord.execution_id.in_(stale_ids),
                    )
                )
                for record in message_result.scalars():
                    record.outcome = "failed"
                    retriable_messages += 1
                await session.commit()
        failed_executions = len(stale_ids)

        if failed_executions or retriable_messages:
            log.warning(
                "work.recovered_orphans",
                failed_executions=failed_executions,
                retriable_messages=retriable_messages,
            )
        return {
            "failed_executions": failed_executions,
            "retriable_messages": retriable_messages,
        }
