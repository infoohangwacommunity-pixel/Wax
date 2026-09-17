"""Persistence for the bridge's idempotency store.

`ProcessedMessageRecord` records that we've already processed a given
(interface_kind, interface_message_id) pair. If the same message arrives
twice (Meta retries, network glitch, etc.), we return DUPLICATE instead
of re-executing.

This table is the foundation of "one user message → one runtime execution".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ProcessedMessageRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """Records that an interface message has been processed.

    The unique constraint on (interface_kind, interface_message_id) is
    the idempotency guarantee: inserting a duplicate raises IntegrityError,
    which the bridge catches and returns as RuntimeResponseStatus.DUPLICATE.
    """

    __tablename__ = "processed_messages"
    __table_args__ = (
        UniqueConstraint(
            "interface_kind",
            "interface_message_id",
            name="uq_processed_messages_interface_id",
        ),
        Index("ix_processed_messages_principal", "principal_id"),
        Index("ix_processed_messages_execution", "execution_id"),
    )

    interface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    interface_message_id: Mapped[str] = mapped_column(String(256), nullable=False)

    # The principal we resolved the sender to
    principal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The execution we started (or NULL if we rejected before starting)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The objective we created
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Outcome: success | duplicate | principal_unauthorized | rate_limited | internal_error
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    # Original request text (truncated for storage)
    request_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Response text (truncated; null if execution was async)
    response_text: Mapped[str | None] = mapped_column(String(5000), nullable=True)

    # When the message was received by the interface (UTC)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When the bridge finished processing (UTC)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Opaque metadata for debugging (never contains secrets)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ConversationMessageRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """The conversation message ledger (Part 2/4 — the biggest gap).

    WAX was missing this entirely. Memory is NOT conversation. The bridge
    was trying to substitute memory for conversation history, which breaks
    for references like "the second option" or "continue" that only make
    sense in recent dialogue.

    This table stores the actual conversation stream:
    - who said it (principal_id + role: user/assistant)
    - when (timestamp)
    - what (content, verbatim)
    - which execution it belonged to
    - which context it was part of (optional)
    - interface metadata (message ID, interface kind)

    The bridge loads the last N messages as "conversation memory" —
    short-term working context — SEPARATE from long-term memory.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conv_messages_principal_time", "principal_id", "created_at"),
        Index("ix_conv_messages_conversation", "conversation_id"),
        Index("ix_conv_messages_context", "context_id"),
    )

    # Whose conversation this message is part of
    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Which conversation thread
    conversation_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Optional: which context this message belongs to
    context_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("contexts.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Role: user or assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # The message content (verbatim for recent messages; the bridge loads
    # the last ~20 messages as working context)
    content: Mapped[str] = mapped_column(String(10000), nullable=False)

    # Which execution produced this (for assistant messages) or triggered
    # this (for user messages)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Interface metadata
    interface_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    interface_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # When the message was sent/received (UTC)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
