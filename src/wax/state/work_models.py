"""Persistence for the durable work runtime (Phase R / V).

A WorkItem is the runtime's representation of UNFINISHED WORK that must
survive process restarts, crashes, user disappearance, and long waits.
It is deliberately schema-light:

- kind: which HANDLER should run this item (a runtime mechanism name,
  e.g. "capability") — not a domain category.
- payload: opaque JSON interpreted by the handler (evidence, not ontology).
- wake_at: when the work should run (the runtime owns time; the AI owns
  decisions).
- lease: claim/lease columns make claiming safe across workers and across
  restarts (a worker that dies releases its lease by expiry).

Nothing here knows about timers, reminders, study sessions, or any use
case. A reminder is one composition: work.schedule → capability handler →
message.send.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class WorkItemRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A unit of durable work with a wake condition and a lifecycle."""

    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_status_wake", "status", "wake_at"),
        Index("ix_work_items_principal", "principal_id"),
        Index("ix_work_items_execution", "execution_id"),
    )

    # Which handler runs this item. Runtime mechanism name — currently
    # "capability" (wake and invoke a registered capability). Free text so
    # new handler kinds absorb new work shapes without migration.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Lifecycle: pending → leased → running → succeeded | failed(→pending)
    # | dead | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    # The human this work belongs to (identity continuity across time).
    principal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The originating execution (objective continuity / traceability).
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Opaque work description for the handler. NEVER secrets.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # What the handler produced (if it succeeded).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # When the work should run (UTC). The runtime owns time.
    wake_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Earliest claim time (wake_at initially; now+backoff on retry).
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Claim lease (crash safety across workers/restarts).
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
