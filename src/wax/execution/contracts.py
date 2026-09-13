"""Pydantic contracts + enums for the execution system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionKind(StrEnum):
    """Universal execution patterns. NOT domain-specific.

    A future 'agent_loop' execution might be used for an education objective
    OR a research objective OR a software-development objective — the
    execution kind does not encode the domain.
    """

    SINGLE_TURN = "single_turn"
    AGENT_LOOP = "agent_loop"
    LONG_RUNNING_TASK = "long_running_task"
    BACKGROUND_WORKFLOW = "background_workflow"


class Execution(BaseModel):
    """An execution as returned from the API."""

    id: str
    principal_id: str
    kind: str
    status: str
    objective: str
    checkpoint: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm(cls, e: object) -> Execution:
        return cls(
            id=e.id,  # type: ignore[attr-defined]
            principal_id=e.principal_id,  # type: ignore[attr-defined]
            kind=e.kind,  # type: ignore[attr-defined]
            status=e.status,  # type: ignore[attr-defined]
            objective=e.objective,  # type: ignore[attr-defined]
            checkpoint=e.checkpoint,  # type: ignore[attr-defined]
            started_at=e.started_at,  # type: ignore[attr-defined]
            ended_at=e.ended_at,  # type: ignore[attr-defined]
            error=e.error,  # type: ignore[attr-defined]
            created_at=e.created_at,  # type: ignore[attr-defined]
            updated_at=e.updated_at,  # type: ignore[attr-defined]
        )


class ExecutionStep(BaseModel):
    """A step within an execution."""

    id: str
    execution_id: str
    step_number: int
    kind: str
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    status: str
    error: str | None
    capability_name: str | None
    created_at: datetime
    updated_at: datetime
