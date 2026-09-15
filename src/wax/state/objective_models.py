"""Persistence model for objectives.

The ObjectiveRecord is intentionally generic. The description is free-text;
the intelligence interprets it. The runtime does NOT switch on a 'kind'
field that lists tutoring / coding / business — that would be domain
contamination (INV-01, INV-08).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ObjectiveRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A human objective the intelligence is pursuing."""

    __tablename__ = "objectives"
    __table_args__ = (
        Index("ix_objectives_principal", "principal_id"),
        Index("ix_objectives_status", "status"),
    )

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Free-text description of what the human wants. The runtime does NOT
    # parse this for keywords like 'tutor' or 'plan a business'. The
    # intelligence interprets it.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Universal execution pattern (single_turn, multi_turn, long_running).
    # NOT a domain kind.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # Lifecycle status
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # Optional success criteria — the human's stated criteria for completion.
    success_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured context provided at creation time (e.g. referenced files,
    # prior conversation summary). The runtime passes this to the
    # intelligence; it does not interpret it.
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Optional: the execution that is working on this objective.
    # Set when an execution starts; cleared when it ends.
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)


class ObjectiveExecutionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """One execution's participation in an objective's life (ADR-0020).

    An objective is NOT one execution (mission §16: an objective may
    produce retries, resumptions, parallel work). The objective's
    ``execution_id`` column remains the CURRENT-execution pointer; this
    table is the append-only HISTORY — who worked on the objective,
    when it started/ended, and the honest outcome. Resumption (§99)
    reads this history to reconstruct what happened without starting
    from scratch.
    """

    __tablename__ = "objective_executions"
    __table_args__ = (
        Index("ix_objexec_objective", "objective_id"),
        Index("ix_objexec_execution", "execution_id"),
    )

    objective_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("objectives.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The execution (bridge interaction or durable-work run) that acted.
    execution_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # "bridge" (a live interaction) | "work" (a durable work run).
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Honest terminal outcome of THIS participation:
    # succeeded | failed | cancelled | superseded (resumed elsewhere).
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
