"""Persistence model for conversations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ConversationRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A conversation record (groups messages within a time window)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_principal_status", "principal_id", "status"),
        Index("ix_conversations_last_message", "last_message_at"),
    )

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    message_count: Mapped[int] = mapped_column(nullable=False, default=0)

    interface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    last_execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
