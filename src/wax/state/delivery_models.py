"""Persistence for durable outbound deliveries (ADR-0021).

The delivery of a completed result is NOT the completion itself. When
the runtime finishes work but the interface send fails, the work stays
done and the delivery becomes RECOVERABLE STATE: a record with a real
lifecycle (pending → retrying → delivered | failed) that the runtime's
maintenance loop retries until it is delivered, exhausted, or past the
deliverability horizon.

This replaces the old dead-letter graveyard for outbound messages — a
row nobody ever re-drove was audit, not recovery (mission §55).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin

# Lifecycle:
#   pending   — not yet deliverable/attempted, or awaiting its next
#               retry (attempts > 0 + last_error = retrying)
#   delivered — the interface accepted it (provider ack observed)
#   failed    — terminal honest failure: attempts exhausted or the
#               deliverability horizon passed. The record remains for
#               audit and operator re-drive.
VALID_DELIVERY_STATUSES = ("pending", "delivered", "failed")

# Where the delivery came from — provenance, not a domain category.
DELIVERY_SOURCES = ("bridge_reply", "capability", "approval_notify", "work")


class DeliveryRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """One outbound delivery the runtime owes an interface."""

    __tablename__ = "delivery_records"
    __table_args__ = (
        Index("ix_delivery_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_delivery_principal", "principal_id"),
    )

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )

    interface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Earliest time the next retry may run (backoff); NULL = due now.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
