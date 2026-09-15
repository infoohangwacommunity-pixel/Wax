"""Persistence for terminal sessions (ADR-0039, Phase 6).

A terminal session is a persistent, governed shell bound to an
environment lease. The session's working_dir is workspace-relative
(never an absolute host path). env_vars are bounded + validated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class TerminalSessionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A persistent terminal session bound to an environment lease."""

    __tablename__ = "terminal_sessions"
    __table_args__ = (
        Index("ix_terminal_principal", "principal_id"),
        Index("ix_terminal_env", "environment_id"),
        Index("ix_terminal_status_expires", "status", "expires_at"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    environment_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("environment_leases.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Lifecycle: active / closed / expired / failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    # Workspace-relative path (never absolute host path).
    working_dir: Mapped[str] = mapped_column(
        String(512), nullable=False, default=".", server_default="."
    )

    # Bounded env vars the intelligence set (validated: no secrets).
    env_vars: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )

    # Session TTL.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last command execution time (for idle-session reaping).
    last_command_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last command's exit code (for observability).
    last_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional error message if status == "failed".
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
