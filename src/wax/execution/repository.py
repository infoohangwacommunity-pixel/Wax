"""Execution repository — data access for durable execution.

Repository handles:
- create: start a new execution
- get: retrieve by ID
- list_for_principal: paginated retrieval
- update_status: transition status (with lifecycle validation)
- record_step: append a step to an execution (for resumability)
- list_steps: retrieve all steps for an execution
- get_latest_checkpoint: reconstruct state for resume

Resumability pattern:
- Each step is recorded with inputs/outputs.
- On crash, the engine queries steps where status="succeeded".
- The latest succeeded step's outputs become the starting checkpoint
  for a resumed execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.execution.contracts import ExecutionKind, ExecutionStatus, StepStatus
from wax.runtime.logging import get_logger
from wax.state.execution_models import ExecutionRecord, ExecutionStepRecord

log = get_logger(__name__)


def _new_ulid() -> str:
    return str(ULID())


# Allowed status transitions
_VALID_TRANSITIONS: dict[str, set[str]] = {
    ExecutionStatus.PENDING.value: {
        ExecutionStatus.RUNNING.value,
        ExecutionStatus.CANCELLED.value,
    },
    ExecutionStatus.RUNNING.value: {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
    },
    ExecutionStatus.SUCCEEDED.value: set(),  # terminal
    ExecutionStatus.FAILED.value: {ExecutionStatus.RUNNING.value},  # retry allowed
    ExecutionStatus.CANCELLED.value: set(),  # terminal
}


class ExecutionRepository:
    """Data access for executions and their steps."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        principal_id: str,
        kind: ExecutionKind | str,
        objective: str,
    ) -> ExecutionRecord:
        """Create a new pending execution."""
        kind_value = kind.value if isinstance(kind, ExecutionKind) else kind
        execution = ExecutionRecord(
            id=_new_ulid(),
            principal_id=principal_id,
            kind=kind_value,
            status=ExecutionStatus.PENDING.value,
            objective=objective,
        )
        self._session.add(execution)
        await self._session.flush()
        log.info(
            "execution.created",
            execution_id=execution.id,
            principal_id=principal_id,
            kind=kind_value,
        )
        return execution

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        return await self._session.get(ExecutionRecord, execution_id)

    async def list_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        stmt = (
            select(ExecutionRecord)
            .where(ExecutionRecord.principal_id == principal_id)
            .order_by(ExecutionRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(ExecutionRecord.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def start(self, execution_id: str) -> bool:
        """Transition an execution from pending to running."""
        execution = await self.get(execution_id)
        if execution is None:
            return False

        if execution.status not in _VALID_TRANSITIONS:
            return False

        allowed = _VALID_TRANSITIONS[execution.status]
        if ExecutionStatus.RUNNING.value not in allowed:
            return False

        execution.status = ExecutionStatus.RUNNING.value
        execution.started_at = datetime.now(UTC)
        await self._session.flush()
        log.info("execution.started", execution_id=execution_id)
        return True

    async def complete(self, execution_id: str, checkpoint: dict[str, Any] | None = None) -> bool:
        """Mark an execution as successfully completed."""
        execution = await self.get(execution_id)
        if execution is None:
            return False
        allowed = _VALID_TRANSITIONS.get(execution.status, set())
        if ExecutionStatus.SUCCEEDED.value not in allowed:
            return False
        execution.status = ExecutionStatus.SUCCEEDED.value
        execution.ended_at = datetime.now(UTC)
        if checkpoint is not None:
            execution.checkpoint = checkpoint
        await self._session.flush()
        log.info("execution.completed", execution_id=execution_id)
        return True

    async def fail(self, execution_id: str, error: str) -> bool:
        """Mark an execution as failed with an error message."""
        execution = await self.get(execution_id)
        if execution is None:
            return False
        allowed = _VALID_TRANSITIONS.get(execution.status, set())
        if ExecutionStatus.FAILED.value not in allowed:
            return False
        execution.status = ExecutionStatus.FAILED.value
        execution.ended_at = datetime.now(UTC)
        execution.error = error
        await self._session.flush()
        log.warning("execution.failed", execution_id=execution_id, error=error)
        return True

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an execution."""
        execution = await self.get(execution_id)
        if execution is None:
            return False
        allowed = _VALID_TRANSITIONS.get(execution.status, set())
        if ExecutionStatus.CANCELLED.value not in allowed:
            return False
        execution.status = ExecutionStatus.CANCELLED.value
        execution.ended_at = datetime.now(UTC)
        await self._session.flush()
        log.info("execution.cancelled", execution_id=execution_id)
        return True

    async def update_checkpoint(
        self, execution_id: str, checkpoint: dict[str, Any]
    ) -> bool:
        """Update the checkpoint state without changing status."""
        execution = await self.get(execution_id)
        if execution is None:
            return False
        execution.checkpoint = checkpoint
        await self._session.flush()
        return True

    async def record_step(
        self,
        execution_id: str,
        *,
        kind: str,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        status: StepStatus = StepStatus.SUCCEEDED,
        capability_name: str | None = None,
        error: str | None = None,
    ) -> ExecutionStepRecord:
        """Append a step to an execution.

        Step numbers are assigned sequentially. This is the core of
        resumability: each successful step is a checkpoint the engine
        can resume from.
        """
        # Get the next step number
        existing = await self._session.execute(
            select(ExecutionStepRecord)
            .where(ExecutionStepRecord.execution_id == execution_id)
            .order_by(ExecutionStepRecord.step_number.desc())
            .limit(1)
        )
        latest = existing.scalar_one_or_none()
        next_step_number = (latest.step_number + 1) if latest else 1

        step = ExecutionStepRecord(
            id=_new_ulid(),
            execution_id=execution_id,
            step_number=next_step_number,
            kind=kind,
            inputs=inputs,
            outputs=outputs,
            status=status.value if isinstance(status, StepStatus) else status,
            capability_name=capability_name,
            error=error,
        )
        self._session.add(step)
        await self._session.flush()
        log.info(
            "execution.step.recorded",
            execution_id=execution_id,
            step_number=next_step_number,
            kind=kind,
            status=step.status,
        )
        return step

    async def list_steps(self, execution_id: str) -> list[ExecutionStepRecord]:
        """Return all steps for an execution, in order."""
        result = await self._session.execute(
            select(ExecutionStepRecord)
            .where(ExecutionStepRecord.execution_id == execution_id)
            .order_by(ExecutionStepRecord.step_number.asc())
        )
        return list(result.scalars().all())

    async def get_latest_succeeded_step(
        self, execution_id: str
    ) -> ExecutionStepRecord | None:
        """Return the latest succeeded step — the resume point."""
        result = await self._session.execute(
            select(ExecutionStepRecord)
            .where(
                ExecutionStepRecord.execution_id == execution_id,
                ExecutionStepRecord.status == StepStatus.SUCCEEDED.value,
            )
            .order_by(ExecutionStepRecord.step_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
