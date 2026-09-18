"""Pydantic contracts + enums for the execution system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


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
    """Execution lifecycle kinds — NOT domain-specific.

    An execution is one active period of thinking/acting. A durable Work
    item can span many executions. The kind describes what triggered
    this execution, not what the AI is doing.

    CONVERSATION: triggered by a user message
    REENTRY: triggered by a scheduled wake / signal
    RECOVERY: triggered by process restart recovering interrupted work
    CONTINUATION: triggered by execution budget exhaustion (checkpoint +
    resume)
    """

    CONVERSATION = "conversation"
    REENTRY = "reentry"
    RECOVERY = "recovery"
    CONTINUATION = "continuation"


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
    kind: str  # model, terminal, terminal_observation, etc.
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    status: str
    error: str | None
    # Deprecated: capability_name is a fossil from the old capability
    # architecture. Retained for DB compatibility but no longer used
    # for dispatch. Will be removed in a future migration baseline.
    capability_name: str | None = None
    created_at: datetime
    updated_at: datetime
