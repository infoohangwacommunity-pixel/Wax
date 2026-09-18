"""Durable process registry — DB-backed detached process lifecycle (spec §5).

Detached processes (servers, workers, tunnels) are tracked in the database
so they survive intelligence turns, process restarts, and container
replacements. The registry knows:

- which Work/principal owns the process
- the PID (valid only within the current container)
- the command that started it
- when it started
- its last-known status
- how to stop it

When a container restarts, the PID becomes stale. The maintenance sweep
detects stale entries and marks them as 'dead'. The associated Work can
then decide whether to restart the process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ProcessRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A durable record of a detached terminal process.

    The PID is only valid within the current container instance. When
    the container restarts, the maintenance sweep checks whether the
    PID still exists. If not, the process is marked 'dead'.
    """

    __tablename__ = "process_registry"
    __table_args__ = (
        Index("ix_processes_principal", "principal_id"),
        Index("ix_processes_work", "work_id"),
        Index("ix_processes_status", "status"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    work_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The PID within the current container. Stale after restart.
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The command that started the process
    command: Mapped[str] = mapped_column(Text, nullable=False)

    # Status: running, dead, stopped
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")

    # When the process was started
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # When the process was last confirmed alive
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional metadata (port, workspace path, etc.)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # How the process ended (if it did)
    termination_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
