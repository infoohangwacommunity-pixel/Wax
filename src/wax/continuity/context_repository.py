"""Context repository — data access for durable contexts (Phase B).

CRUD + resolution helpers for the context index. The context
intelligence layer (Phase C) uses this to find candidate contexts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.context_models import ContextRecord

log = get_logger(__name__)


def _new_ulid() -> str:
    return str(ULID())


class ContextRepository:
    """Data access for context records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        principal_id: str,
        label: str,
        state: dict[str, Any] | None = None,
        summary: str | None = None,
        workspace_path: str | None = None,
        aliases: list[str] | None = None,
        parent_context_id: str | None = None,
    ) -> ContextRecord:
        """Create a new context."""
        record = ContextRecord(
            id=_new_ulid(),
            principal_id=principal_id,
            label=label,
            status="active",
            state=state or {},
            summary=summary,
            workspace_path=workspace_path,
            last_active_at=datetime.now(UTC),
            aliases=aliases or [],
            parent_context_id=parent_context_id,
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "context.created",
            context_id=record.id,
            principal_id=principal_id,
            label=label,
        )
        return record

    async def get(self, context_id: str) -> ContextRecord | None:
        return await self._session.get(ContextRecord, context_id)

    async def list_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ContextRecord]:
        """List contexts for a principal, optionally filtered by status."""
        stmt = (
            select(ContextRecord)
            .where(ContextRecord.principal_id == principal_id)
            .order_by(ContextRecord.last_active_at.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(ContextRecord.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_active(self, principal_id: str, limit: int = 50) -> list[ContextRecord]:
        """List active contexts for a principal."""
        return await self.list_for_principal(principal_id, status="active", limit=limit)

    async def update_state(
        self,
        context_id: str,
        state: dict[str, Any],
        *,
        merge: bool = True,
    ) -> bool:
        """Update the living state of a context."""
        record = await self.get(context_id)
        if record is None:
            return False
        if merge:
            new_state = dict(record.state or {})
            new_state.update(state)
        else:
            new_state = state
        record.state = new_state
        record.last_active_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def update_summary(self, context_id: str, summary: str) -> bool:
        """Update the context summary."""
        record = await self.get(context_id)
        if record is None:
            return False
        record.summary = summary
        record.last_active_at = datetime.now(UTC)
        await self._session.flush()
        return True

    async def touch(self, context_id: str) -> bool:
        """Update last_active_at without changing state."""
        result = await self._session.execute(
            update(ContextRecord)
            .where(ContextRecord.id == context_id)
            .values(last_active_at=datetime.now(UTC))
        )
        return result.rowcount > 0

    async def set_status(self, context_id: str, status: str) -> bool:
        """Change the context status (active, paused, completed, archived)."""
        result = await self._session.execute(
            update(ContextRecord)
            .where(ContextRecord.id == context_id)
            .values(status=status, last_active_at=datetime.now(UTC))
        )
        return result.rowcount > 0

    async def add_alias(self, context_id: str, alias: str) -> bool:
        """Add an alias to a context."""
        record = await self.get(context_id)
        if record is None:
            return False
        aliases = list(record.aliases or [])
        if alias not in aliases:
            aliases.append(alias)
            record.aliases = aliases
            await self._session.flush()
        return True

    async def resolve_by_alias(self, principal_id: str, alias: str) -> ContextRecord | None:
        """Find a context by alias (case-insensitive)."""
        contexts = await self.list_active(principal_id)
        alias_lower = alias.lower().strip()
        for ctx in contexts:
            if ctx.label.lower() == alias_lower:
                return ctx
            for a in ctx.aliases or []:
                if a.lower() == alias_lower:
                    return ctx
        return None

    async def resolve_by_keyword(
        self, principal_id: str, text: str, limit: int = 5
    ) -> list[tuple[ContextRecord, float]]:
        """Find candidate contexts by keyword overlap with the text.

        Returns a list of (context, score) tuples sorted by score desc.
        The context intelligence layer uses this as one signal among many.
        """
        contexts = await self.list_active(principal_id)
        text_lower = text.lower()
        text_words = set(text_lower.split())
        results: list[tuple[ContextRecord, float]] = []

        for ctx in contexts:
            score = 0.0
            # Label match (strong signal)
            label_lower = ctx.label.lower()
            if label_lower in text_lower:
                score += 3.0
            # Label word overlap
            label_words = set(label_lower.split())
            overlap = text_words & label_words
            score += len(overlap) * 1.0
            # Alias match
            for alias in ctx.aliases or []:
                alias_lower = alias.lower()
                if alias_lower in text_lower:
                    score += 2.0
                alias_words = set(alias_lower.split())
                overlap = text_words & alias_words
                score += len(overlap) * 0.5
            # Summary word overlap
            if ctx.summary:
                summary_words = set(ctx.summary.lower().split())
                overlap = text_words & summary_words
                score += len(overlap) * 0.3
            if score > 0:
                results.append((ctx, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]
