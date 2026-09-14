"""Objective evidence sync — the runtime keeps objective state truthful.

The mission rule: an objective's status must reflect RUNTIME EVIDENCE,
not model claims and not silence. When durable work is scheduled for an
objective, the objective IS waiting; when a human approval is pending
for it, the objective IS awaiting the human; when that work wakes or the
approval is consumed, it IS active again; when its last pending work
dies with nothing else outstanding, it HAS failed.

These helpers are the single synchronization point (ADR-0020). They are
runtime-owned: no intelligence runs here, nothing here can be asked for
by a prompt. Every helper is TOLERANT — a sync must never break the
primary flow it observes (scheduling, running, approving); failures log
and return. Silent success-fabrication is impossible by construction:
these only transition to states the transition map already allows.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.state.objective_models import ObjectiveExecutionRecord, ObjectiveRecord
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)

# Work statuses that still represent pending progress (ADR-0013 set minus
# the terminal {succeeded, dead, cancelled}; "failed" is retryable).
_OUTSTANDING_WORK_STATUSES = ("pending", "leased", "running", "failed")


async def objective_for_execution(
    session: AsyncSession, execution_id: str | None
) -> ObjectiveRecord | None:
    """Resolve the objective an execution is working on (or None).

    Resolution order: an OPEN row in the execution history (the live
    participation — resumes redirect rows, so history beats the stale
    pointer), then the latest closed history row, then the objective's
    current-execution pointer (covers legacy links written before the
    history table existed).
    """
    if not execution_id:
        return None
    result = await session.execute(
        select(ObjectiveExecutionRecord)
        .where(ObjectiveExecutionRecord.execution_id == execution_id)
        .order_by(
            ObjectiveExecutionRecord.ended_at.is_not(None),
            ObjectiveExecutionRecord.started_at.desc(),
        )
        .limit(1)
    )
    history = result.scalars().first()
    if history is not None:
        return await session.get(ObjectiveRecord, history.objective_id)
    # Legacy/current-pointer fallback.
    result = await session.execute(
        select(ObjectiveRecord)
        .where(ObjectiveRecord.execution_id == execution_id)
        .order_by(ObjectiveRecord.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def sync_waiting_for_execution(
    session: AsyncSession, execution_id: str | None
) -> bool:
    """Durable work was scheduled under this execution → objective waits."""
    try:
        objective = await objective_for_execution(session, execution_id)
        if objective is None:
            return False
        changed = await _repo(session).sync_waiting(objective.id)
        if changed:
            log.info(
                "objective.evidence_waiting",
                objective_id=objective.id,
                execution_id=execution_id,
            )
        return changed
    except Exception as e:
        log.warning("objective.sync_failed", reason=str(e)[:300])
        return False


async def sync_awaiting_human_for_execution(
    session: AsyncSession, execution_id: str | None
) -> bool:
    """A pending approval was created under this execution → awaiting."""
    try:
        objective = await objective_for_execution(session, execution_id)
        if objective is None:
            return False
        changed = await _repo(session).sync_awaiting_human(objective.id)
        if changed:
            log.info(
                "objective.evidence_awaiting_human",
                objective_id=objective.id,
                execution_id=execution_id,
            )
        return changed
    except Exception as e:
        log.warning("objective.sync_failed", reason=str(e)[:300])
        return False


async def sync_active_for_execution(
    session: AsyncSession, execution_id: str | None
) -> bool:
    """The wait resolved (work claimed / approval consumed) → active."""
    try:
        objective = await objective_for_execution(session, execution_id)
        if objective is None:
            return False
        changed = await _repo(session).sync_reactivated(objective.id)
        if changed:
            log.info(
                "objective.evidence_reactivated",
                objective_id=objective.id,
                execution_id=execution_id,
            )
        return changed
    except Exception as e:
        log.warning("objective.sync_failed", reason=str(e)[:300])
        return False


async def objective_has_outstanding_work(
    session: AsyncSession, objective_id: str
) -> bool:
    """DB truth: does this objective still have pending durable work?

    Consulted before any terminal transition. `succeeded` with outstanding
    work would be a PERMANENT lie (terminal states are immutable), so the
    runtime checks the evidence itself instead of trusting the flow that
    asked for the transition.
    """
    result = await session.execute(
        select(ObjectiveExecutionRecord.execution_id).where(
            ObjectiveExecutionRecord.objective_id == objective_id
        )
    )
    execution_ids = [row[0] for row in result.all()]
    if not execution_ids:
        return False
    result = await session.execute(
        select(WorkItemRecord.id)
        .where(WorkItemRecord.execution_id.in_(execution_ids))
        .where(WorkItemRecord.status.in_(_OUTSTANDING_WORK_STATUSES))
        .limit(1)
    )
    return result.first() is not None


async def sync_failure_for_work(
    session: AsyncSession, work_item: WorkItemRecord
) -> bool:
    """A work item died (retries exhausted / wait expired).

    The objective fails ONLY when nothing else of it remains pending —
    one dead reminder must not kill an objective that still has live
    work. Evidence, not blanket pessimism.
    """
    if not work_item.execution_id:
        return False
    try:
        objective = await objective_for_execution(session, work_item.execution_id)
        if objective is None:
            return False
        if objective.status not in ("waiting", "awaiting_human", "in_progress"):
            return False
        outstanding = await session.execute(
            select(WorkItemRecord.id)
            .where(WorkItemRecord.execution_id == work_item.execution_id)
            .where(WorkItemRecord.id != work_item.id)
            .where(WorkItemRecord.status.in_(_OUTSTANDING_WORK_STATUSES))
            .limit(1)
        )
        if outstanding.first() is not None:
            return False
        repo = _repo(session)
        record = await repo.get(objective.id)
        if record is None or record.status not in ("waiting", "awaiting_human"):
            # in_progress objectives fail via the execution path, not here.
            return False
        record.status = "failed"
        await session.flush()
        log.info(
            "objective.evidence_failed",
            objective_id=objective.id,
            work_id=work_item.id,
            reason="last_pending_work_died",
        )
        return True
    except Exception as e:
        log.warning("objective.sync_failed", reason=str(e)[:300])
        return False


def _repo(session: AsyncSession):
    from wax.objective.repository import ObjectiveRepository

    return ObjectiveRepository(session)
