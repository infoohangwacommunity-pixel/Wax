"""Persistence for the bridge's idempotency store.

`ProcessedMessageRecord` records that we've already processed a given
(interface_kind, interface_message_id) pair. If the same message arrives
twice (Meta retries, network glitch, etc.), we return DUPLICATE instead
of re-executing.

This table is the foundation of "one user message → one runtime execution".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
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
