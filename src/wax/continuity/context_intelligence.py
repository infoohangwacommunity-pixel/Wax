"""Context Intelligence — the inside-out resolution layer (Phase C/D).

The Context Intelligence is a translator between human language and
persistent world state. It asks: "What is actually going on?"

It is NOT the actor. It inspects, retrieves, reasons, verifies. The
main intelligence remains the actor — it asks "What should I do?"

The flow:
1. User sends a message
2. Context Intelligence retrieves candidate contexts (keyword + recency)
3. It uses a small LLM pass to resolve which context(s) the user means
4. It produces a structured context packet:
   - resolved context (or "unresolved" if ambiguous)
   - evidence (why this context was chosen)
   - confidence
   - current state
   - recent conversation
   - relevant memories
   - active work items
   - waiting states
5. The main intelligence receives the compact packet + the user message

The Context Intelligence can say "I don't know" — that is a successful
result. The main intelligence then asks the human for clarification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.continuity.context_repository import ContextRepository
from wax.intelligence.contracts import LLMMessage, LLMRequest, MessageRole
from wax.intelligence.service import IntelligenceService
from wax.memory.repository import MemoryRepository
from wax.runtime.logging import get_logger
from wax.state.context_models import ContextRecord
from wax.state.memory_models import MemoryRecord
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)


CONTEXT_RESOLUTION_SYSTEM_PROMPT = """\
You are the Context Intelligence inside WAX. Your job is to determine
which persistent context(s) the user is referring to.

You receive:
- The user's message
- A list of candidate contexts (with labels, summaries, last-active time)
- Recent conversation context

You must decide:
1. Is the user referring to a specific existing context?
2. Are they starting something new?
3. Is it ambiguous (multiple contexts could match)?
4. Is it a general/personal message (no specific context)?

Output a JSON object:
{
  "resolution": "specific|new|ambiguous|general",
  "context_id": "<id if specific, null otherwise>",
  "confidence": 0.0-1.0,
  "evidence": "<why you chose this>",
  "alternative_context_ids": ["<id>", ...],
  "ambiguity_note": "<if ambiguous, what's unclear>"
}

Output ONLY the JSON object, no other text.
"""


@dataclass
class ContextPacket:
    """The compact context representation for the main model.

    This is what the main intelligence receives instead of a giant dump
    of memories and history. It's the "what is actually going on" packet.
    """

    resolution: str  # specific, new, ambiguous, general
    context_id: str | None = None
    context_label: str | None = None
    confidence: float = 0.0
    evidence: str = ""
    current_state: dict[str, Any] | None = None
    context_summary: str | None = None
    recent_conversation: list[dict[str, str]] = field(default_factory=list)
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    active_work: list[dict[str, Any]] = field(default_factory=list)
    waiting_states: list[dict[str, Any]] = field(default_factory=list)
    workspace_path: str | None = None
    ambiguity_note: str | None = None

    def to_prompt_section(self) -> str:
        """Render the packet as a compact section for the system prompt."""
        parts: list[str] = ["\n--- CONTEXT ---"]

        if self.resolution == "specific" and self.context_label:
            parts.append(f"Active context: {self.context_label}")
            if self.confidence < 0.8:
                parts.append(f"  (confidence: {self.confidence:.1f})")
            if self.evidence:
                parts.append(f"  Evidence: {self.evidence}")
            if self.context_summary:
                parts.append(f"  State: {self.context_summary}")
            if self.current_state:
                for key, value in list(self.current_state.items())[:5]:
                    parts.append(f"  {key}: {str(value)[:200]}")
            if self.workspace_path:
                parts.append(f"  Workspace: {self.workspace_path}")
        elif self.resolution == "ambiguous":
            parts.append("Context: AMBIGUOUS — multiple contexts could match")
            if self.ambiguity_note:
                parts.append(f"  Note: {self.ambiguity_note}")
        elif self.resolution == "new":
            parts.append("Context: starting something new")
        else:
            parts.append("Context: general (no specific project)")

        if self.relevant_memories:
            parts.append("\n  Relevant memories:")
            for m in self.relevant_memories[:5]:
                parts.append(f"  - [{m.get('kind', '?')}] {m.get('text', '')[:200]}")

        if self.active_work:
            parts.append("\n  Active work:")
            for w in self.active_work[:3]:
                parts.append(f"  - {w.get('prompt', '')[:150]}")

        if self.waiting_states:
            parts.append("\n  Waiting for:")
            for w in self.waiting_states[:3]:
                parts.append(f"  - {w.get('text', '')[:150]}")

        return "\n".join(parts)


async def resolve_context(
    *,
    session: AsyncSession,
    intelligence: IntelligenceService,
    principal_id: str,
    user_message: str,
) -> ContextPacket:
    """Resolve which context the user is referring to.

    This is the main entrypoint for the Context Intelligence. It:
    1. Retrieves candidate contexts via keyword + recency
    2. Uses a small LLM pass to resolve
    3. Gathers the context packet (memories, work, waiting states)
    """
    ctx_repo = ContextRepository(session)

    # Fix 8: Skip context resolution for trivial messages — no LLM call needed.
    # Short messages like "ok", "thanks", "yes", "send it" don't need context
    # resolution. This saves one LLM call for every trivial interaction.
    if len(user_message.strip()) < 15:
        packet = ContextPacket(
            resolution="general",
            confidence=1.0,
            evidence="message too short for context resolution",
        )
        packet.relevant_memories = await _fetch_relevant_memories(
            session, principal_id, user_message, context_id=None
        )
        return packet

    # 1. Retrieve candidate contexts
    candidates = await ctx_repo.resolve_by_keyword(principal_id, user_message, limit=5)
    active_contexts = await ctx_repo.list_active(principal_id, limit=10)

    # 2. If no contexts exist at all, this is a "new" or "general" resolution
    if not active_contexts:
        packet = ContextPacket(
            resolution="general",
            confidence=1.0,
            evidence="no existing contexts for this principal",
        )
        # Still gather global memories
        packet.relevant_memories = await _fetch_relevant_memories(
            session, principal_id, user_message, context_id=None
        )
        return packet

    # 3. Use the LLM to resolve
    candidate_descriptions = []
    for ctx, score in candidates:
        last_active = ctx.last_active_at
        if last_active and last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=UTC)
        days_ago = (datetime.now(UTC) - last_active).days if last_active else 0
        candidate_descriptions.append(
            f"- ID: {ctx.id}\n"
            f"  Label: {ctx.label}\n"
            f"  Summary: {ctx.summary or '(no summary)'}\n"
            f"  Last active: {days_ago} days ago\n"
            f"  Keyword score: {score:.1f}\n"
            f"  Aliases: {', '.join(ctx.aliases or [])}"
        )

    # If only one candidate with high score, resolve directly without LLM
    if len(candidates) == 1 and candidates[0][1] >= 3.0:
        ctx = candidates[0][0]
        return await _build_packet_for_context(
            session, principal_id, ctx, user_message, "direct keyword match"
        )

    # Multiple candidates — use the LLM to resolve
    try:
        request = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content=CONTEXT_RESOLUTION_SYSTEM_PROMPT),
                LLMMessage(
                    role=MessageRole.USER,
                    content=(
                        f"User message: {user_message[:500]}\n\n"
                        f"Candidate contexts:\n{chr(10).join(candidate_descriptions)}"
                    ),
                ),
            ],
            temperature=0.1,
        )
        response = await intelligence.complete(request)
        resolution = _parse_resolution(response.content)
    except Exception as e:
        log.warning("context.intelligence_failed", error=str(e)[:200])
        resolution = {
            "resolution": "general",
            "confidence": 0.0,
            "evidence": f"resolution failed: {type(e).__name__}",
        }

    # 4. Build the packet based on resolution
    resolution_type = resolution.get("resolution", "general")
    context_id = resolution.get("context_id")

    if resolution_type == "specific" and context_id:
        ctx = await ctx_repo.get(context_id)
        if ctx:
            return await _build_packet_for_context(
                session,
                principal_id,
                ctx,
                user_message,
                resolution.get("evidence", ""),
                resolution.get("confidence", 0.8),
            )

    if resolution_type == "ambiguous":
        packet = ContextPacket(
            resolution="ambiguous",
            confidence=resolution.get("confidence", 0.3),
            evidence=resolution.get("evidence", ""),
            ambiguity_note=resolution.get("ambiguity_note"),
        )
        packet.relevant_memories = await _fetch_relevant_memories(
            session, principal_id, user_message, context_id=None
        )
        return packet

    # "new" or "general"
    packet = ContextPacket(
        resolution=resolution_type if resolution_type in ("new", "general") else "general",
        confidence=resolution.get("confidence", 0.5),
        evidence=resolution.get("evidence", ""),
    )
    packet.relevant_memories = await _fetch_relevant_memories(
        session, principal_id, user_message, context_id=None
    )
    return packet


async def _build_packet_for_context(
    session: AsyncSession,
    principal_id: str,
    ctx: ContextRecord,
    user_message: str,
    evidence: str,
    confidence: float = 0.9,
) -> ContextPacket:
    """Build a full context packet for a resolved context."""
    packet = ContextPacket(
        resolution="specific",
        context_id=ctx.id,
        context_label=ctx.label,
        confidence=confidence,
        evidence=evidence,
        current_state=ctx.state if isinstance(ctx.state, dict) else {},
        context_summary=ctx.summary,
        workspace_path=ctx.workspace_path,
    )

    # Gather context-bound memories
    packet.relevant_memories = await _fetch_relevant_memories(
        session, principal_id, user_message, context_id=ctx.id
    )

    # Gather active work for this context
    packet.active_work = await _fetch_active_work(session, principal_id, ctx.id)

    # Gather waiting states for this context
    packet.waiting_states = await _fetch_waiting_states(session, principal_id, ctx.id)

    return packet


async def _fetch_relevant_memories(
    session: AsyncSession,
    principal_id: str,
    query: str,
    context_id: str | None,
) -> list[dict[str, Any]]:
    """Fetch relevant memories, optionally filtered by context."""
    memory_repo = MemoryRepository(session)
    memories = await memory_repo.search_relevant(
        principal_id=principal_id,
        query=query,
        limit=10,
    )
    result = []
    for m in memories:
        # If context_id is specified, prioritize memories bound to it
        mem_context = getattr(m, "context_id", None)
        if context_id and mem_context and mem_context != context_id:
            continue  # skip memories bound to a different context
        content = m.content if isinstance(m.content, dict) else {"text": str(m.content)}
        result.append(
            {
                "id": m.id,
                "kind": m.kind,
                "text": content.get("text", str(content)),
                "confidence": m.confidence,
                "importance": m.importance,
            }
        )
    return result[:5]


async def _fetch_active_work(
    session: AsyncSession,
    principal_id: str,
    context_id: str,
) -> list[dict[str, Any]]:
    """Fetch active work items for a context."""
    result = await session.execute(
        select(WorkItemRecord)
        .where(
            WorkItemRecord.principal_id == principal_id,
            WorkItemRecord.status.in_(["pending", "running"]),
        )
        .order_by(WorkItemRecord.wake_at.desc())
        .limit(5)
    )
    items = result.scalars().all()
    return [
        {
            "id": item.id,
            "prompt": (item.payload or {}).get("prompt", "")[:200],
            "wake_at": item.wake_at.isoformat() if item.wake_at else None,
        }
        for item in items
    ][:3]


async def _fetch_waiting_states(
    session: AsyncSession,
    principal_id: str,
    context_id: str,
) -> list[dict[str, Any]]:
    """Fetch waiting-kind memories for a context."""
    result = await session.execute(
        select(MemoryRecord)
        .where(
            MemoryRecord.principal_id == principal_id,
            MemoryRecord.kind == "waiting",
            MemoryRecord.status == "active",
        )
        .order_by(MemoryRecord.updated_at.desc())
        .limit(5)
    )
    items = result.scalars().all()
    return [
        {
            "id": m.id,
            "text": (m.content if isinstance(m.content, dict) else {"text": str(m.content)}).get(
                "text", ""
            ),
        }
        for m in items
    ][:3]


def _parse_resolution(content: str) -> dict[str, Any]:
    """Parse the LLM's JSON resolution response."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("context.resolution_parse_failed", content_preview=text[:200])
        return {"resolution": "general", "confidence": 0.0}
