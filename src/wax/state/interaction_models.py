"""Interaction session model — DB-backed (replaces in-memory dict).

The in-memory dict couldn't cross process boundaries (the terminal
subprocess that calls serve_page() is a different process from the
FastAPI server that serves /i/{token}). Moving to DB fixes this
AND survives Railway restarts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class InteractionSessionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A temporary interaction session between the AI and the human."""

    __tablename__ = "interaction_sessions"
    __table_args__ = (
        Index("ix_interaction_sessions_token", "secret_token"),
        Index("ix_interaction_sessions_principal", "principal_id"),
    )

    secret_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    context_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    html_content: Mapped[str] = mapped_column(Text, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_submissions: Mapped[int] = mapped_column(nullable=False, default=1)
    submission_count: Mapped[int] = mapped_column(nullable=False, default=0)

    # pending, submitted, expired, cancelled
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # The result captured after submission (form data, file metadata, etc.)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # The event to emit when the session is submitted
    wake_event: Mapped[str | None] = mapped_column(String(255), nullable=True)
