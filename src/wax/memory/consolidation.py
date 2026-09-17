"""Memory consolidation — the reflection pass (Part 29).

Every night, or after enough conversations, the AI should reflect. Not to
answer users. To reorganize itself.

Example: "I've seen David struggle with chemistry six times." Instead of
six memories, it creates one stronger memory.

Humans do this during sleep. The runtime does it during idle periods.

The consolidation pass:
1. Finds clusters of related episodic memories for a principal
2. Uses the LLM to distill them into a single semantic memory
3. Supersedes the individual episodes (kept as historical evidence)
4. The consolidated memory becomes the efficient representation

This is NOT a capability. It's a maintenance mechanism — the runtime
keeping itself organized, like sleep.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.intelligence.contracts import LLMMessage, LLMRequest, MessageRole
from wax.intelligence.service import IntelligenceService
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.runtime.logging import get_logger
from wax.state.memory_models import MemoryRecord

log = get_logger(__name__)

CONSOLIDATION_SYSTEM_PROMPT = """\
You are a memory consolidation assistant inside WAX.

You receive a cluster of related episodic memories from the same person.
Your job: distill them into ONE durable semantic memory that captures
the pattern.

Rules:
1. Output a single JSON object with the consolidated memory.
2. The kind should be "fact", "preference", "skill", or "procedural" —
   whichever best fits the pattern.
3. The content should be a distilled insight, not a summary of episodes.
4. If the memories don't form a meaningful pattern, return {"skip": true}.

Output format:
{
  "kind": "fact|preference|skill|procedural",
  "content": {"text": "the distilled insight"},
  "importance": 0.0-1.0,
  "confidence": 0.0-1.0
}

Or: {"skip": true}
"""


async def consolidate_principal_memories(
    *,
    session: AsyncSession,
    intelligence: IntelligenceService,
    principal_id: str,
    min_cluster_size: int = 3,
    max_clusters: int = 3,
) -> int:
    """Run consolidation for one principal.

    Finds clusters of related episodic memories, distills each cluster
    into a single semantic memory, and supersedes the episodes.

    Returns the number of consolidated memories created.
    """
    repo = MemoryRepository(session)

    # Find recent episodic memories (last 7 days) that are still active
    cutoff = datetime.now(UTC) - timedelta(days=7)
    result = await session.execute(
        select(MemoryRecord)
        .where(
            MemoryRecord.principal_id == principal_id,
            MemoryRecord.kind == MemoryKind.EPISODIC.value,
            MemoryRecord.status == "active",
            MemoryRecord.created_at >= cutoff,
        )
        .order_by(MemoryRecord.created_at.desc())
        .limit(20)
    )
    episodes = list(result.scalars().all())

    if len(episodes) < min_cluster_size:
        return 0

    # Cluster episodes by simple keyword overlap (a real implementation
    # would use embeddings, but keyword clustering is good enough for MVP)
    clusters = _cluster_episodes(episodes)

    consolidated_count = 0
    for cluster in clusters[:max_clusters]:
        if len(cluster) < min_cluster_size:
            continue

        # Build the consolidation request
        episode_texts = []
        for ep in cluster:
            content = ep.content if isinstance(ep.content, dict) else {"text": str(ep.content)}
            text = content.get("text", str(content))
            episode_texts.append(f"- {text[:200]}")

        request = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content=CONSOLIDATION_SYSTEM_PROMPT),
                LLMMessage(
                    role=MessageRole.USER,
                    content=f"Episodes from this person:\n{chr(10).join(episode_texts)}",
                ),
            ],
            temperature=0.1,
        )

        try:
            response = await intelligence.complete(request)
            consolidated = _parse_consolidation_response(response.content)
        except Exception as e:
            log.warning("consolidation.failed", error=str(e)[:200])
            continue

        if not consolidated or consolidated.get("skip"):
            continue

        # Create the consolidated memory
        try:
            kind_str = consolidated.get("kind", "fact")
            try:
                kind_enum = MemoryKind(kind_str)
            except ValueError:
                kind_enum = MemoryKind.FACT

            content = consolidated.get("content", {})
            if isinstance(content, str):
                content = {"text": content}

            consolidated_record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=kind_enum,
                    content=content,
                    provenance="consolidation",
                    confidence=float(consolidated.get("confidence", 0.8)),
                    importance=float(consolidated.get("importance", 0.7)),
                    observed_at=datetime.now(UTC),
                )
            )

            # Supersede the individual episodes — they become historical evidence
            for ep in cluster:
                await repo.supersede(ep.id, consolidated_record.id)

            consolidated_count += 1
            log.info(
                "memory.consolidated",
                principal_id=principal_id,
                consolidated_id=consolidated_record.id,
                episode_count=len(cluster),
                kind=kind_enum.value,
            )
        except Exception as e:
            log.warning("consolidation.store_failed", error=str(e)[:200])

    return consolidated_count


def _cluster_episodes(
    episodes: list[MemoryRecord],
) -> list[list[MemoryRecord]]:
    """Simple keyword-based clustering of episodic memories.

    A real implementation would use embeddings + cosine similarity.
    For MVP, we group by shared significant words.
    """
    clusters: list[list[MemoryRecord]] = []
    used: set[str] = set()

    for ep in episodes:
        if ep.id in used:
            continue
        content = ep.content if isinstance(ep.content, dict) else {"text": str(ep.content)}
        text = content.get("text", "").lower()
        words = set(text.split()) - {
            "the",
            "a",
            "an",
            "is",
            "was",
            "to",
            "and",
            "of",
            "in",
            "for",
            "user",
            "asked",
            "said",
        }

        cluster = [ep]
        used.add(ep.id)

        for other in episodes:
            if other.id in used:
                continue
            other_content = (
                other.content if isinstance(other.content, dict) else {"text": str(other.content)}
            )
            other_text = other_content.get("text", "").lower()
            other_words = set(other_text.split()) - {
                "the",
                "a",
                "an",
                "is",
                "was",
                "to",
                "and",
                "of",
                "in",
                "for",
                "user",
                "asked",
                "said",
            }
            overlap = words & other_words
            if len(overlap) >= 2:  # at least 2 shared significant words
                cluster.append(other)
                used.add(other.id)

        clusters.append(cluster)

    return clusters


def _parse_consolidation_response(content: str) -> dict[str, Any] | None:
    """Parse the LLM's JSON response."""
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
        log.warning("consolidation.parse_failed", content_preview=text[:200])
        return None
