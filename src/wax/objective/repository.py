"""Objective repository — persistence for objectives."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.objective.contracts import ObjectiveCreate, ObjectiveKind, ObjectiveStatus
from wax.runtime.logging import get_logger
from wax.state.objective_models import ObjectiveRecord

log = get_logger(__name__)


_VALID_TRANSITIONS: dict[str, set[str]] = {
    ObjectiveStatus.PENDING.value: {
        ObjectiveStatus.IN_PROGRESS.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.IN_PROGRESS.value: {
        ObjectiveStatus.SUCCEEDED.value,
        ObjectiveStatus.FAILED.value,
        ObjectiveStatus.ABANDONED.value,
    },
    ObjectiveStatus.SUCCEEDED.value: set(),
    ObjectiveStatus.FAILED.value: {ObjectiveStatus.IN_PROGRESS.value},  # retry
    ObjectiveStatus.ABANDONED.value: set(),
}


class ObjectiveRepository:
    """Data access for objectives."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: ObjectiveCreate) -> ObjectiveRecord:
        kind_value = (
            payload.kind.value if isinstance(payload.kind, ObjectiveKind) else payload.kind
        )
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
        """
        record = await self.get(objective_id)
        if record is None:
            return False

        new_value = (
            new_status.value if isinstance(new_status, ObjectiveStatus) else new_status
        )
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
        record.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        log.info(
            "objective.transitioned",
            objective_id=objective_id,
            status=new_value,
        )
        return True

    async def attach_execution(
        self, objective_id: str, execution_id: str
    ) -> bool:
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
