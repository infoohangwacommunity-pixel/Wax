"""Context model — durable threads of meaning (Phase B, Part 6/9).

A Context is a persistent thread of meaning and state. It is NOT a
"project" — that word is too domain-specific. A context could be:
- a software project
- university admission
- a job application
- a person
- a relationship
- a business
- a research effort
- a legal matter
- a recurring responsibility
- a creative work
- a personal goal

The runtime stores the context; the intelligence defines the meaning.

Each context has:
- stable context ID
- human-readable label/title
- status (active, paused, completed, archived)
- last activity timestamp
- generic living state (JSON — the intelligence fills this)
- context summary
- workspace path (persistent per-context filesystem)
- associated memories (via memory_records.context_id)
- associated work items (via work_items.context_id)
- associated executions (via executions — linked through context_id)

The context index is compact — the runtime stores thousands of contexts
but only surfaces the 3-4 relevant ones per turn via context resolution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ContextRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A durable thread of meaning — a long-lived context.

    This is the world-state layer that makes the ten-project scenario
    work. The runtime stores thousands of these; the context intelligence
    surfaces only the relevant ones per turn.
    """

    __tablename__ = "contexts"
    __table_args__ = (
        Index("ix_contexts_principal_status", "principal_id", "status"),
        Index("ix_contexts_principal_updated", "principal_id", "updated_at"),
    )

    # Whose context this is
    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Human-readable label (set by the intelligence)
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # Status: active, paused, completed, archived
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    # Generic living state (JSON) — the intelligence fills this.
    # Examples: current phase, known facts, decisions, blockers, pending
    # external actions, next likely continuation. NOT hardcoded schema.
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Context summary — a compact natural-language description of where
    # this context stands. Updated by the intelligence after meaningful
    # interactions.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Workspace path — persistent per-context filesystem directory.
    # The AI can store files here that belong to this context.
    workspace_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Last activity timestamp (updated on any interaction in this context)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Optional: aliases the intelligence can use to refer to this context
    # (e.g. "android app", "the portfolio thing", "scholarship")
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Optional: parent context (for sub-contexts — e.g. "database setup"
    # as a child of "Android app")
    parent_context_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("contexts.id", ondelete="SET NULL"),
        nullable=True,
    )
