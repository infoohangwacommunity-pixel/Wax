"""Conversation + Continuity services.

ConversationService: lifecycle of a single conversation (open, touch, close)
ContinuityService: top-level "what context should the AI see?" — composes
  conversation + memory + objective + execution into a ContinuityContext
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wax.continuity.contracts import (
    ContinuityContext,
)
from wax.continuity.repository import ConversationRepository
from wax.execution.repository import ExecutionRepository
from wax.memory.repository import MemoryRepository
from wax.objective.repository import ObjectiveRepository
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class ConversationService:
    """Lifecycle management for a single conversation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ConversationRepository(session)

    async def open_or_resume(
        self,
        principal_id: str,
        interface_kind: str,
    ):
        """Find an active conversation or open a new one.

        If the principal has an active/idle conversation, resume it.
        Otherwise, create a new one.
        """
        existing = await self._repo.get_active_for_principal(principal_id)
        if existing is not None:
            log.info(
                "conversation.resumed",
                conversation_id=existing.id,
                principal_id=principal_id,
                last_message_at=existing.last_message_at.isoformat(),
            )
            return existing
        return await self._repo.create(principal_id, interface_kind)

    async def touch(
        self,
        conversation_id: str,
        *,
        execution_id: str | None = None,
    ) -> bool:
        return await self._repo.touch(
            conversation_id, execution_id=execution_id
        )

    async def close(self, conversation_id: str) -> bool:
        return await self._repo.close(conversation_id)


class ContinuityService:
    """Top-level "what context should the AI see?" API.

    Called by the runtime bridge BEFORE calling the intelligence layer.
    Returns a ContinuityContext that includes:
    - Active conversation (if any)
    - Recent memory (Phase F)
    - Active objective (Phase L)
    - Last execution state (Phase H)

    The AI uses this to "remember" what happened before.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conv_repo = ConversationRepository(session)
        self._memory_repo = MemoryRepository(session)
        self._objective_repo = ObjectiveRepository(session)
        self._exec_repo = ExecutionRepository(session)

    async def build_context(
        self,
        principal_id: str,
        interface_kind: str,
        current_message: str | None = None,
    ) -> tuple[ContinuityContext, str | None]:
        """Build the continuity context for a principal.

        `current_message` (when provided) drives relevance-based memory
        retrieval: the AI sees what is RELEVANT, not merely what is recent.
        Returns (context, conversation_id). If conversation_id is None, a
        new conversation will need to be opened by the caller.
        """
        conversation = await self._conv_repo.get_active_for_principal(principal_id)

        now = datetime.now(UTC)

        if conversation is None:
            # Brand-new conversation
            return (
                ContinuityContext(
                    principal_id=principal_id,
                    is_new_conversation=True,
                    recent_memories=await self._fetch_context_memories(
                        principal_id, current_message
                    ),
                ),
                None,
            )

        # Resuming — compute days_since_last_message
        # SQLite may return naive datetimes; normalize to aware.
        last_msg = conversation.last_message_at
        if last_msg.tzinfo is None:
            last_msg = last_msg.replace(tzinfo=UTC)
        delta = now - last_msg
        days_since = delta.total_seconds() / 86400.0

        # Fetch memories: relevance pool (against the current message) merged
        # with the recency pool, deduplicated, capped.
        recent_memories = await self._fetch_context_memories(principal_id, current_message)

        # Fetch active objective (if any)
        active_objective = None
        if conversation.objective_id:
            active_objective = await self._objective_repo.get(conversation.objective_id)

        # Fetch last execution state (if any)
        last_execution = None
        if conversation.last_execution_id:
            last_execution = await self._exec_repo.get(conversation.last_execution_id)

        return (
            ContinuityContext(
                principal_id=principal_id,
                conversation_id=conversation.id,
                conversation_summary=conversation.summary,
                active_objective_id=conversation.objective_id,
                active_objective_description=(
                    active_objective.description if active_objective else None
                ),
                last_execution_id=conversation.last_execution_id,
                last_execution_status=(
                    last_execution.status if last_execution else None
                ),
                recent_memories=recent_memories,
                is_new_conversation=False,
                days_since_last_message=days_since,
            ),
            conversation.id,
        )

    async def _fetch_context_memories(
        self,
        principal_id: str,
        current_message: str | None = None,
        *,
        recency_limit: int = 5,
        relevance_limit: int = 5,
        max_total: int = 8,
    ) -> list[dict]:
        """Compose the memory context: relevance + recency, deduplicated.

        - Recency pool: the newest memories regardless of wording (the
          conversation's immediate past).
        - Relevance pool: what speaks to the CURRENT message, across all
          kinds and ages — the reason "what did I tell you about my
          exam?" can resurface a weeks-old exam memory.
        Each entry carries `reason` so the model can weigh the evidence;
        the runtime never silently rewrites what the AI sees.
        """
        recent = await self._memory_repo.list_active_for_principal(
            principal_id, limit=recency_limit
        )
        relevant: list[tuple[Any, float]] = []
        if current_message:
            relevant = await self._memory_repo.search_relevant(
                principal_id, current_message, limit=relevance_limit
            )

        merged: dict[str, dict] = {}
        for m in recent:
            merged.setdefault(
                m.id,
                {
                    "id": m.id,
                    "summary": m.summary or str(m.content)[:200],
                    "kind": m.kind,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "reason": "recent",
                },
            )
        for m, score in relevant:
            entry = merged.get(m.id)
            if entry is not None:
                entry["reason"] = "recent+relevant"
                entry["score"] = round(score, 4)
            else:
                merged[m.id] = {
                    "id": m.id,
                    "summary": m.summary or str(m.content)[:200],
                    "kind": m.kind,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "reason": "relevant",
                    "score": round(score, 4),
                }

        # Newest first within the merged pool (stable conversational order).
        entries = sorted(
            merged.values(),
            key=lambda e: e["created_at"] or "",
            reverse=True,
        )
        return entries[:max_total]
