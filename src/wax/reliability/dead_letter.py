"""Dead-letter handling — terminal failures stored for inspection.

When an operation exhausts retries and the circuit is open, the failed
payload is recorded in a dead-letter table. A future worker can inspect
these for re-processing (after fixing the underlying issue) or audit.

INVARIANT: Dead-letter entries are append-only. They record what failed,
why, and when — never contain secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from ulid import ULID

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class DeadLetterEntry(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A dead-letter entry — records a terminal failure.

    Append-only. The application layer enforces this; a future architecture
    test will verify no UPDATE/DELETE targets this table.
    """

    __tablename__ = "dead_letter_entries"
    __table_args__ = (
        Index("ix_dead_letter_kind", "kind"),
        Index("ix_dead_letter_principal", "principal_id"),
        Index("ix_dead_letter_created", "created_at"),
    )

    # What kind of operation failed (e.g. "llm_complete", "capability_invoke",
    # "whatsapp_send")
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Optional: principal whose operation failed
    principal_id: Mapped[str | None] = mapped_column(String(26), nullable=True, index=True)

    # Optional: execution that failed
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The error type + message
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)

    # Number of retry attempts before giving up
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)

    # Opaque payload for reprocessing (e.g. request body, capability name)
    # NEVER contains secrets — caller responsibility to scrub.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # When the failure happened (UTC)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Whether this entry has been reprocessed. RESERVED: no reprocessing
    # worker exists yet (the class docstring says the same). Nothing in
    # the runtime sets this to True — do not mistake it for a guarantee.
    reprocessed: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    reprocessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeadLetterRepository:
    """Data access for dead-letter entries. Append-only."""

    def __init__(self, session) -> None:
        self._session = session

    async def record(
        self,
        *,
        kind: str,
        error_type: str,
        error_message: str,
        attempts: int = 0,
        principal_id: str | None = None,
        execution_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DeadLetterEntry:
        """Record a terminal failure. Always appends; never updates."""
        entry = DeadLetterEntry(
            id=str(ULID()),
            kind=kind,
            principal_id=principal_id,
            execution_id=execution_id,
            error_type=error_type,
            error_message=error_message[:10000],  # truncate
            attempts=attempts,
            payload=payload,
            failed_at=datetime.now(UTC),
            reprocessed=False,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_recent(
        self, *, limit: int = 50, kind: str | None = None
    ) -> list[DeadLetterEntry]:
        from sqlalchemy import select

        stmt = (
            select(DeadLetterEntry)
            .where(DeadLetterEntry.reprocessed == False)  # noqa: E712
            .order_by(DeadLetterEntry.created_at.desc())
            .limit(limit)
        )
        if kind:
            stmt = stmt.where(DeadLetterEntry.kind == kind)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_reprocessed(self, entry_id: str) -> bool:
        from datetime import datetime

        from sqlalchemy import select

        result = await self._session.execute(
            select(DeadLetterEntry).where(DeadLetterEntry.id == entry_id)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            return False
        entry.reprocessed = True
        entry.reprocessed_at = datetime.now(UTC)
        await self._session.flush()
        return True
