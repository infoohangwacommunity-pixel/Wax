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

    async def attach_objective(
        self, conversation_id: str, objective_id: str
    ) -> bool:
        """Link the conversation to the objective its current interaction
        is pursuing. This link is what the priority-0 OBJECTIVE evidence
        section reads (ADR-0012/ADR-0023); leaving it unwritten made the
        section dead in every live flow (constitutional audit fix)."""
        return await self._repo.attach_objective(conversation_id, objective_id)

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

        ADR-0037 (Phase 4: Context Becomes Environment): the context now
        also carries:
        - available_capabilities: the runtime's registered capability
          names (the AI can compose what's available)
        - current_time: the runtime's clock (the AI reasons about time)
        - recent_signals: the last few runtime signals the principal's
          work has been waiting on (so the AI sees what woke up)
        - waiting_work: durable work items in 'waiting' status,
          distinct from active_work which carries all non-terminal work
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
                    # New conversations still see outstanding work and
                    # artifacts: "keep working on this while I am away"
                    # must survive a conversation boundary (mission §17).
                    active_work=await self._fetch_active_work(principal_id),
                    recent_artifacts=await self._fetch_recent_artifacts(
                        principal_id
                    ),
                    environment=await self._build_environment(
                        principal_id, interface_kind
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

        # Fetch active work + artifact evidence (ADR-0023, mission §99:
        # a resumed objective reconstructs its pending actions and
        # artifacts — metadata only, never payload bytes).
        active_work = await self._fetch_active_work(principal_id)
        recent_artifacts = await self._fetch_recent_artifacts(principal_id)
        environment = await self._build_environment(
            principal_id, conversation.interface_kind
        )
        environment["active_work_count"] = len(active_work)

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
                active_work=active_work,
                recent_artifacts=recent_artifacts,
                environment=environment,
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

    async def _fetch_active_work(self, principal_id: str, *, limit: int = 5) -> list[dict]:
        """The principal's outstanding durable work — ACTIVE_WORK evidence.

        Metadata only: kind/status/wake facts, never payload contents
        (payloads may carry arbitrary inputs; the work's own execution
        trace is the place for those). This is what a resumed objective
        needs to see: what is still pending, and when it wakes (mission
        §99, §115: 'What remains? Why did WAX stop?').
        """
        from sqlalchemy import select

        from wax.state.work_models import WorkItemRecord

        result = await self._session.execute(
            select(WorkItemRecord)
            .where(
                WorkItemRecord.principal_id == principal_id,
                WorkItemRecord.status.in_(("pending", "leased", "running", "failed")),
            )
            .order_by(WorkItemRecord.wake_at.asc())
            .limit(limit)
        )
        items = list(result.scalars().all())
        entries = []
        for item in items:
            wake = item.wake_at.isoformat() if item.wake_at else None
            entries.append(
                {
                    "work_id": item.id,
                    "status": item.status,
                    "wake_kind": item.wake_kind,
                    "wake_at": wake,
                    "attempts": item.attempts,
                    "capability": (item.payload or {}).get("capability_name"),
                }
            )
        return entries

    async def _fetch_recent_artifacts(self, principal_id: str, *, limit: int = 5) -> list[dict]:
        """The principal's newest artifacts — ARTIFACTS evidence.

        Filename + integrity prefix + size only; never file bytes (the
        model sees text, never bytes — the media invariant).
        """
        from sqlalchemy import select

        from wax.state.artifact_models import ArtifactRecord

        result = await self._session.execute(
            select(ArtifactRecord)
            .where(ArtifactRecord.principal_id == principal_id)
            .order_by(ArtifactRecord.created_at.desc())
            .limit(limit)
        )
        artifacts = list(result.scalars().all())
        return [
            {
                "artifact_id": a.id,
                "filename": a.filename,
                "sha256": a.sha256[:12],
                "bytes": a.size_bytes,
                "source": a.source,
            }
            for a in artifacts
        ]

    async def _build_environment(
        self, principal_id: str, interface_kind: str
    ) -> dict[str, Any]:
        """ADR-0037 (Phase 4): compose the environment facts the AI wakes into.

        The environment is NOT chat history — it is the runtime reality
        the intelligence composes against:

        - interface: which interface this interaction is on
        - current_time: the runtime's clock (the AI reasons about time)
        - available_capabilities: the registered capability names
          (the AI can compose what's available; new platforms require
          no architectural rewrite)
        - recent_signals: the last few runtime signals emitted for this
          principal (so the AI sees what woke up — work.succeeded,
          approval.granted, etc.)
        - waiting_work_count: how many durable work items are waiting
          (subset of active_work; status=waiting specifically)

        Degrades gracefully: any subsystem that cannot be queried
        contributes nothing to the dict, never raises.
        """
        env: dict[str, Any] = {
            "interface": interface_kind,
            "current_time": datetime.now(UTC).isoformat(),
        }

        # Available capabilities — try to fetch from the runtime services
        # container attached to the session. Tolerant: a session without
        # the container (e.g. unit tests) contributes nothing.
        try:
            services = getattr(self._session, "wax_services", None)
            if services is not None and hasattr(services, "capability_registry"):
                capabilities = []
                for descriptor in services.capability_registry.list_capabilities():
                    capabilities.append(
                        {
                            "name": descriptor.name,
                            "description": descriptor.description[:200],
                            "is_destructive": descriptor.is_destructive,
                        }
                    )
                env["available_capabilities"] = capabilities
        except Exception:
            pass

        # Recent signals — the last few runtime signals emitted for this
        # principal's work (so the AI sees what woke up).
        try:
            from sqlalchemy import select

            from wax.state.work_models import RuntimeSignalRecord

            result = await self._session.execute(
                select(RuntimeSignalRecord)
                .where(RuntimeSignalRecord.name.like(f"%{principal_id}%"))
                .order_by(RuntimeSignalRecord.emitted_at.desc())
                .limit(5)
            )
            signals = list(result.scalars().all())
            env["recent_signals"] = [
                {
                    "name": s.name,
                    "emitted_at": (
                        s.emitted_at.isoformat() if s.emitted_at else None
                    ),
                    "emitted_by": s.emitted_by,
                }
                for s in signals
            ]
        except Exception:
            pass

        # Waiting work count — items in 'waiting' status specifically
        # (distinct from active_work which carries all non-terminal items)
        try:
            from sqlalchemy import func, select

            from wax.state.work_models import WorkItemRecord

            result = await self._session.execute(
                select(func.count(WorkItemRecord.id))
                .where(WorkItemRecord.principal_id == principal_id)
                .where(WorkItemRecord.status == "waiting")
            )
            env["waiting_work_count"] = int(result.scalar() or 0)
        except Exception:
            pass

        return env
