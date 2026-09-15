"""wax.state.execution_models — persistence for durable execution.

Tables:
- executions: a unit of work (objective, principal, status, checkpoint state)
- execution_steps: individual steps within an execution (for resumability)

An execution is intentionally generic. It does NOT contain "agent_loop_step"
or "tutor_session_step" — those would be domain contamination. It contains
universal execution concepts: status, checkpoint, resumable state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ExecutionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A single durable execution.

    Lifecycle: pending → running → succeeded | failed | cancelled

    An execution may produce many steps (ExecutionStepRecord) — each
    represents a checkpoint the runtime can resume from.
    """

    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_principal", "principal_id"),
        Index("ix_executions_status", "status"),
    )

    # Whose execution this is
    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # What kind of execution this is. Examples:
    # "agent_loop", "single_turn", "long_running_task", "background_workflow"
    # NOTE: these are NOT domain types. They are execution patterns.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Status: pending, running, succeeded, failed, cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # The objective description (what the human asked for).
    # Stored as a string for now; future versions may use structured form.
    objective: Mapped[str] = mapped_column(Text, nullable=False)

    # Opaque checkpoint state — JSON. The execution engine stores whatever
    # it needs to resume: last model message, completed steps, planning state.
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # When the execution started (UTC). NULL until started.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When the execution ended (UTC). NULL if still running.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Error message if status == failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExecutionStepRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A checkpointed step within an execution.

    Each step records:
    - the execution it belongs to
    - the step number (1-indexed)
    - the step's inputs (what was the state at the start)
    - the step's outputs (what was produced)
    - the step's status

    On resume, the engine queries steps where status="succeeded" to
    know what not to redo, and looks at the latest step's outputs to
    reconstruct state.
    """

    __tablename__ = "execution_steps"
    __table_args__ = (Index("ix_execution_steps_exec", "execution_id", "step_number"),)

    execution_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1-indexed step number within the execution
    step_number: Mapped[int] = mapped_column(nullable=False)

    # What kind of step this was. Examples:
    # "model_call", "capability_invoke", "user_message", "system_event"
    # NOTE: universal — not domain-specific.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Inputs to the step (JSON)
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Outputs from the step (JSON)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Status: pending, running, succeeded, failed, skipped
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # Error message if status == failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional: which capability was invoked (if kind == "capability_invoke")
    capability_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
