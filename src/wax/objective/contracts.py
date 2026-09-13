"""Pydantic contracts + enums for the objective system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ObjectiveStatus(StrEnum):
    """Lifecycle states for an objective.

    pending → in_progress → succeeded | failed | abandoned, plus the
    evidence-driven states the runtime syncs WITHOUT intelligence in the
    loop (ADR-0020):

    - waiting: durable work linked to this objective exists and is not
      yet claimed (time or event wake) — the objective is not stalled,
      it is WAITING on a real condition the runtime tracks.
    - awaiting_human: a pending human approval exists whose provenance
      traces to this objective's execution.
    - cancelled: actively cancelled (by the principal or by the
      intelligence under the principal's authority) — distinct from
      abandoned (drifted / no longer pursued) and from failed (honest
      failure with error evidence).

    Transitions are enforced by ObjectiveRepository._VALID_TRANSITIONS.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    AWAITING_HUMAN = "awaiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


#: States an objective can be resumed into active work from.
ACTIVE_ELIGIBLE_STATES = (
    ObjectiveStatus.PENDING.value,
    ObjectiveStatus.WAITING.value,
    ObjectiveStatus.AWAITING_HUMAN.value,
    ObjectiveStatus.FAILED.value,
    ObjectiveStatus.IN_PROGRESS.value,
)

#: Terminal states — no further transitions (evidence stands).
TERMINAL_OBJECTIVE_STATUSES = (
    ObjectiveStatus.SUCCEEDED.value,
    ObjectiveStatus.FAILED.value,
    ObjectiveStatus.CANCELLED.value,
    ObjectiveStatus.ABANDONED.value,
)


class ObjectiveKind(StrEnum):
    """Universal execution patterns for objectives.

    NOT domain-specific. No 'tutoring', 'business_planning', 'coding'
    kinds — those are OBJECTIVES (free-text descriptions) that the
    intelligence interprets, not architectural categories.
    """

    SINGLE_TURN = "single_turn"  # one request, one response
    MULTI_TURN = "multi_turn"  # back-and-forth within a session
    LONG_RUNNING = "long_running"  # persists across sessions


class ObjectiveCreate(BaseModel):
    """Payload to create an objective."""

    principal_id: str
    description: str = Field(..., min_length=1, max_length=10000)
    kind: ObjectiveKind = ObjectiveKind.MULTI_TURN
    success_criteria: str | None = Field(default=None, max_length=5000)
    context: dict[str, Any] = Field(default_factory=dict)


class Objective(BaseModel):
    """An objective as returned from the API."""

    id: str
    principal_id: str
    description: str
    kind: str
    status: str
    success_criteria: str | None
    context: dict[str, Any]
    execution_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm(cls, o: object) -> Objective:
        return cls(
            id=o.id,  # type: ignore[attr-defined]
            principal_id=o.principal_id,  # type: ignore[attr-defined]
            description=o.description,  # type: ignore[attr-defined]
            kind=o.kind,  # type: ignore[attr-defined]
            status=o.status,  # type: ignore[attr-defined]
            success_criteria=o.success_criteria,  # type: ignore[attr-defined]
            context=o.context,  # type: ignore[attr-defined]
            execution_id=o.execution_id,  # type: ignore[attr-defined]
            created_at=o.created_at,  # type: ignore[attr-defined]
            updated_at=o.updated_at,  # type: ignore[attr-defined]
        )
