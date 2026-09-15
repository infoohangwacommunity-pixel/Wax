"""Objective repository — persistence for objectives."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.objective.contracts import ObjectiveCreate, ObjectiveKind, ObjectiveStatus
from wax.runtime.logging import get_logger
from wax.state.objective_models import ObjectiveExecutionRecord, ObjectiveRecord

log = get_logger(__name__)


# Lifecycle transition map (ADR-0020). waiting/awaiting_human are
# runtime-synced EVIDENCE states: the runtime moves an objective into
# them when durable work or a pending approval exists, and back to
# in_progress when that condition resolves. cancelled is an active
# decision, distinct from abandoned (drift) and failed (error).
_VALID_TRANSITIONS: dict[str, set[str]] = {
    ObjectiveStatus.PENDING.value: {
        ObjectiveStatus.IN_PROGRESS.value,
        ObjectiveStatus.WAITING.value,
        ObjectiveStatus.AWAITING_HUMAN.value,
        ObjectiveStatus.CANCELLED.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.IN_PROGRESS.value: {
        ObjectiveStatus.WAITING.value,
        ObjectiveStatus.AWAITING_HUMAN.value,
        ObjectiveStatus.SUCCEEDED.value,
        ObjectiveStatus.FAILED.value,
        ObjectiveStatus.CANCELLED.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.WAITING.value: {
        ObjectiveStatus.IN_PROGRESS.value,
        # Wait expiry / work death with nothing pending is honest failure.
        ObjectiveStatus.FAILED.value,
        ObjectiveStatus.CANCELLED.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.AWAITING_HUMAN.value: {
        ObjectiveStatus.IN_PROGRESS.value,
        # The interaction itself may complete while an approval stays
        # independently pending (e.g. a summary was delivered and the
        # risky step waits); approval expiry with nothing pending fails.
        ObjectiveStatus.SUCCEEDED.value,
        ObjectiveStatus.FAILED.value,
        ObjectiveStatus.CANCELLED.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.SUCCEEDED.value: set(),
    ObjectiveStatus.FAILED.value: {ObjectiveStatus.IN_PROGRESS.value},  # retry
    ObjectiveStatus.CANCELLED.value: set(),
    ObjectiveStatus.ABANDONED.value: set(),
}

#: States from which a waiting-sync is legal (the objective is not past caring).
_WAITABLE_FROM = {
    ObjectiveStatus.PENDING.value,
    ObjectiveStatus.IN_PROGRESS.value,
}

#: States from which an awaiting-human-sync is legal.
_AWAITABLE_FROM = {
    ObjectiveStatus.PENDING.value,
    ObjectiveStatus.IN_PROGRESS.value,
    ObjectiveStatus.WAITING.value,
}

#: States from which a back-to-active sync is legal.
_REACTIVATABLE_FROM = {
    ObjectiveStatus.WAITING.value,
    ObjectiveStatus.AWAITING_HUMAN.value,
}


class ObjectiveRepository:
    """Data access for objectives."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: ObjectiveCreate) -> ObjectiveRecord:
        kind_value = payload.kind.value if isinstance(payload.kind, ObjectiveKind) else payload.kind
        record = ObjectiveRecord(
            id=str(ULID()),
            principal_id=payload.principal_id,
            description=payload.description,
            kind=kind_value,
            status=ObjectiveStatus.PENDING.value,
            success_criteria=payload.success_criteria,
            context=payload.context,
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "objective.created",
            objective_id=record.id,
            principal_id=payload.principal_id,
            kind=kind_value,
        )
        return record

    async def get(self, objective_id: str) -> ObjectiveRecord | None:
        return await self._session.get(ObjectiveRecord, objective_id)

    async def list_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ObjectiveRecord]:
        stmt = (
            select(ObjectiveRecord)
            .where(ObjectiveRecord.principal_id == principal_id)
            .order_by(ObjectiveRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(ObjectiveRecord.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def transition(
        self,
        objective_id: str,
        new_status: ObjectiveStatus | str,
    ) -> bool:
        """Transition an objective to a new status.

        Returns True if transition was allowed and applied, False otherwise.
        The CURRENT status is re-read from the database: capability
        invocations run in their own session and may have evidence-synced
        this objective (waiting/awaiting_human) after this session loaded
        it — deciding on a stale identity-map copy would let an
        interaction close an objective whose work is still outstanding.
        """
        record = await self.get(objective_id)
        if record is None:
            return False
        await self._session.refresh(record, attribute_names=["status"])

        new_value = new_status.value if isinstance(new_status, ObjectiveStatus) else new_status
        allowed = _VALID_TRANSITIONS.get(record.status, set())
        if new_value not in allowed:
            log.warning(
                "objective.invalid_transition",
                objective_id=objective_id,
                from_status=record.status,
                to_status=new_value,
            )
            return False

        record.status = new_value
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        log.info(
            "objective.transitioned",
            objective_id=objective_id,
            status=new_value,
        )
        return True

    async def attach_execution(self, objective_id: str, execution_id: str) -> bool:
        """Link an objective to the execution that is working on it."""
        record = await self.get(objective_id)
        if record is None:
            return False
        record.execution_id = execution_id
        await self._session.flush()
        return True

    async def detach_execution(self, objective_id: str) -> bool:
        record = await self.get(objective_id)
        if record is None:
            return False
        record.execution_id = None
        await self._session.flush()
        return True

    # --- execution history (ADR-0020: objective ≠ execution) --------------

    async def record_execution_start(
        self,
        objective_id: str,
        execution_id: str,
        kind: str,
        *,
        started_at: datetime | None = None,
    ) -> ObjectiveExecutionRecord | None:
        """Append an execution-participation row and set the current pointer.

        kind is "bridge" (a live interaction) or "work" (a durable run).
        Returns None if the objective does not exist.
        """
        record = await self.get(objective_id)
        if record is None:
            return None
        row = ObjectiveExecutionRecord(
            id=str(ULID()),
            objective_id=objective_id,
            execution_id=execution_id,
            kind=kind,
            started_at=started_at or datetime.now(UTC),
        )
        self._session.add(row)
        record.execution_id = execution_id
        await self._session.flush()
        return row

    async def record_execution_end(
        self,
        objective_id: str,
        execution_id: str,
        outcome: str,
    ) -> int:
        """Close the open participation rows for this (objective, execution).

        Returns how many rows were closed (0 is legitimate — e.g. a
        failure path that raced creation). Never fabricates a row that
        was never started.
        """
        result = await self._session.execute(
            select(ObjectiveExecutionRecord)
            .where(ObjectiveExecutionRecord.objective_id == objective_id)
            .where(ObjectiveExecutionRecord.execution_id == execution_id)
            .where(ObjectiveExecutionRecord.ended_at.is_(None))
        )
        rows = list(result.scalars().all())
        now = datetime.now(UTC)
        for row in rows:
            row.ended_at = now
            row.outcome = outcome
        if rows:
            await self._session.flush()
        return len(rows)

    async def list_executions(
        self, objective_id: str, *, limit: int = 50
    ) -> list[ObjectiveExecutionRecord]:
        """The objective's execution history, oldest first."""
        result = await self._session.execute(
            select(ObjectiveExecutionRecord)
            .where(ObjectiveExecutionRecord.objective_id == objective_id)
            .order_by(ObjectiveExecutionRecord.started_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_open_executions(self, objective_id: str) -> int:
        result = await self._session.execute(
            select(ObjectiveExecutionRecord.id)
            .where(ObjectiveExecutionRecord.objective_id == objective_id)
            .where(ObjectiveExecutionRecord.ended_at.is_(None))
            .limit(1)
        )
        return 1 if result.first() is not None else 0

    # --- evidence-state syncs (ADR-0020: runtime-owned, tolerant) ---------

    async def sync_waiting(self, objective_id: str) -> bool:
        """Move an active objective to `waiting` (durable work exists)."""
        record = await self.get(objective_id)
        if record is None:
            return False
        await self._session.refresh(record, attribute_names=["status"])
        if record.status not in _WAITABLE_FROM:
            return False
        record.status = ObjectiveStatus.WAITING.value
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def sync_awaiting_human(self, objective_id: str) -> bool:
        """Move an active objective to `awaiting_human` (approval pending)."""
        record = await self.get(objective_id)
        if record is None:
            return False
        await self._session.refresh(record, attribute_names=["status"])
        if record.status not in _AWAITABLE_FROM:
            return False
        record.status = ObjectiveStatus.AWAITING_HUMAN.value
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def sync_reactivated(self, objective_id: str) -> bool:
        """Return a waiting/awaiting_human objective to `in_progress`."""
        record = await self.get(objective_id)
        if record is None:
            return False
        await self._session.refresh(record, attribute_names=["status"])
        if record.status not in _REACTIVATABLE_FROM:
            return False
        record.status = ObjectiveStatus.IN_PROGRESS.value
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return True
