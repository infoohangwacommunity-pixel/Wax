"""Conversation repository — persistence for conversation records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.continuity.contracts import (
    DEFAULT_IDLE_TIMEOUT,
    ConversationStatus,
)
from wax.runtime.logging import get_logger
from wax.state.continuity_models import ConversationRecord

log = get_logger(__name__)


class ConversationRepository:
    """Data access for conversation records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        principal_id: str,
        interface_kind: str,
    ) -> ConversationRecord:
        now = datetime.now(UTC)
        record = ConversationRecord(
            id=str(ULID()),
            principal_id=principal_id,
            status=ConversationStatus.ACTIVE.value,
            started_at=now,
            last_message_at=now,
            message_count=0,
            interface_kind=interface_kind,
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "conversation.created",
            conversation_id=record.id,
            principal_id=principal_id,
            interface=interface_kind,
        )
        return record

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        return await self._session.get(ConversationRecord, conversation_id)

    async def get_active_for_principal(self, principal_id: str) -> ConversationRecord | None:
        """Find an active or idle conversation for the principal.

        Returns the most-recently-active conversation that is not archived
        or closed. If found, the conversation may be resumed.
        """
        result = await self._session.execute(
            select(ConversationRecord)
            .where(
                ConversationRecord.principal_id == principal_id,
                ConversationRecord.status.in_(
                    [
                        ConversationStatus.ACTIVE.value,
                        ConversationStatus.IDLE.value,
                    ]
                ),
            )
            .order_by(ConversationRecord.last_message_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def touch(
        self,
        conversation_id: str,
        *,
        message_count_delta: int = 1,
        execution_id: str | None = None,
    ) -> bool:
        """Update last_message_at + message_count for a conversation."""
        now = datetime.now(UTC)
        record = await self.get(conversation_id)
        if record is None:
            return False
        # Use naive datetime for SQLite compatibility, but consistently
        record.last_message_at = (
            now.replace(tzinfo=None)
            if record.last_message_at and record.last_message_at.tzinfo is None
            else now
        )
        record.message_count += message_count_delta
        if record.status == ConversationStatus.IDLE.value:
            record.status = ConversationStatus.ACTIVE.value
        if execution_id:
            record.last_execution_id = execution_id
        await self._session.flush()
        return True

    async def attach_objective(self, conversation_id: str, objective_id: str) -> bool:
        """Point the conversation at the objective its current interaction
        pursues. The OBJECTIVE evidence section is sourced from this link;
        the bridge writes it when both ends of the link exist."""
        record = await self.get(conversation_id)
        if record is None:
            return False
        record.objective_id = objective_id
        await self._session.flush()
        return True

    async def archive_stale(
        self,
        *,
        idle_timeout: timedelta = DEFAULT_IDLE_TIMEOUT,
        archive_after_idle: timedelta = timedelta(days=7),
    ) -> int:
        """Archive conversations that have been idle too long.

        Returns the count of archived conversations. Called by a periodic
        background task; safe to call repeatedly.
        """
        now = datetime.now(UTC)
        idle_cutoff = now - idle_timeout
        archive_cutoff = now - archive_after_idle

        # Mark active conversations as idle if no message in idle_timeout
        result = await self._session.execute(
            update(ConversationRecord)
            .where(
                ConversationRecord.status == ConversationStatus.ACTIVE.value,
                ConversationRecord.last_message_at < idle_cutoff,
            )
            .values(status=ConversationStatus.IDLE.value)
        )
        idle_count = result.rowcount or 0

        # Archive idle conversations older than archive_after_idle
        result = await self._session.execute(
            update(ConversationRecord)
            .where(
                ConversationRecord.status == ConversationStatus.IDLE.value,
                ConversationRecord.last_message_at < archive_cutoff,
            )
            .values(status=ConversationStatus.ARCHIVED.value)
        )
        archive_count = result.rowcount or 0

        if idle_count or archive_count:
            log.info(
                "conversation.archived_stale",
                idle_count=idle_count,
                archive_count=archive_count,
            )
        return idle_count + archive_count

    async def close(self, conversation_id: str) -> bool:
        """Explicitly close a conversation."""
        record = await self.get(conversation_id)
        if record is None:
            return False
        record.status = ConversationStatus.CLOSED.value
        await self._session.flush()
        return True

    async def set_summary(self, conversation_id: str, summary: str) -> bool:
        record = await self.get(conversation_id)
        if record is None:
            return False
        record.summary = summary
        await self._session.flush()
        return True
