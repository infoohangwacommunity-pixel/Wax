"""wax.state.memory_models — persistence for the memory system.

Memory is NOT a vector database. Each memory record carries:
- principal_id: whose memory this is (memory is per-principal)
- kind: episodic / semantic / procedural / contextual / external
- content: the actual memory (structured, not just text)
- provenance: where this memory came from (model observation, user
  statement, external API, etc.)
- confidence: 0.0 to 1.0 — how confident we are this is accurate
- superseded_by: if a newer memory has replaced this one, its ID
- retention_policy: when this memory may be forgotten
- sensitivity: how private this memory is

This is deliberately richer than "embed text → store vector → search."
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class MemoryRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A single retained memory.

    Memory is per-principal — there is no global memory. A future
    improvement may add shared/organizational memory, but that requires
    explicit authorization design.

    Lifecycle:
    - active: currently relevant
    - superseded: replaced by a newer record (superseded_by set)
    - archived: kept for audit but not retrieved by default
    - forgotten: marked for deletion (retention policy fired)
    """

    __tablename__ = "memory_records"
    __table_args__ = (
        Index("ix_memory_principal_kind", "principal_id", "kind"),
        Index("ix_memory_principal_status", "principal_id", "status"),
        Index("ix_memory_superseded_by", "superseded_by"),
    )

    # Whose memory this is
    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Memory discriminator
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # One of: episodic, semantic, procedural, contextual, external

    # Status: active, superseded, archived, forgotten
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )

    # The memory itself — structured content
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Where this memory came from
    provenance: Mapped[str] = mapped_column(String(64), nullable=False)
    # Examples: "user_statement", "model_observation", "external_api:web_search",
    #           "capability:invoke:...", "system_event"

    # Optional reference to the execution / capability invocation that produced this
    source_execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Confidence: 0.0 (low) to 1.0 (high). Not all memories have confidence.
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    # If this memory was superseded by a newer one, the newer one's ID
    superseded_by: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # How much this memory matters, 0.0-1.0 (mission §6.3). NULL = the
    # intelligence has not expressed an importance; ranking treats it as
    # neutral (0.5). Importance weights the RANK, never the lifecycle.
    importance: Mapped[float | None] = mapped_column(nullable=True)

    # When the fact was OBSERVED (mission §6.3 observation time). Differs
    # from created_at when evidence enters the runtime late ("yesterday I
    # finished the exam"). NULL = observed at creation time. Temporal
    # reasoning reads this, not created_at, when present.
    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When this memory should be considered for forgetting (UTC)
    # NULL = retain indefinitely
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Sensitivity level: 0 (public) to 3 (highly sensitive).
    # CONSTITUTIONAL AUDIT NOTE: this column is currently RESERVED — no
    # runtime path reads it, and no retention/logging/access behavior is
    # keyed on it. The honest contract is the default 0 with this comment;
    # claiming enforcement that does not exist would be a false mechanism.
    # Wiring sensitivity into evidence assembly + retention is documented
    # in docs/constitutional-audit/memory-deep-audit.md as the planned
    # remediation.
    sensitivity: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )

    # Optional short human-readable summary (for fast retrieval / display)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemoryLinkRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A typed edge between two of the principal's memories (ADR-0022).

    Evidence relationships — supports / contradicts / derived_from /
    related_to — traversable by retrieval. Supersession stays a lifecycle
    mechanism (superseded_by column) and is deliberately NOT a link kind.
    Links are principal-scoped: both endpoints must belong to the owner.
    """

    __tablename__ = "memory_links"
    __table_args__ = (
        Index("ix_memlinks_from", "from_memory_id"),
        Index("ix_memlinks_to", "to_memory_id"),
        UniqueConstraint(
            "from_memory_id", "to_memory_id", "kind", name="uq_memlink_edge"
        ),
    )

    from_memory_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("memory_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_memory_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("memory_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)

    # Ownership: both endpoint memories already carry principal_id; this
    # column makes ownership checks index-friendly and asserts the link
    # itself is principal-scoped (privacy, mission §94).
    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Provenance of the LINK decision (which execution proposed it).
    created_by_execution_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True
    )
