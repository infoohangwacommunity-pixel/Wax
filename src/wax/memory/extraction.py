"""Memory extraction — automatic, LLM-driven, structured.

After every interaction, the runtime runs a tiny intelligence pass that
asks: "What should future WAX genuinely remember?"

The output is structured memories (facts, preferences, skills, episodes,
projects, waiting states) — NOT transcript walls. The extractor uses
the same LLM provider as the main bridge, with a small focused prompt.

If the LLM is unavailable (mock provider), the extractor falls back to
a heuristic: store an episodic memory of the exchange only if it looks
meaningful (user message > 20 chars, response > 50 chars).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wax.intelligence.contracts import LLMMessage, LLMRequest, MessageRole
from wax.intelligence.service import IntelligenceService
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.runtime.logging import get_logger

log = get_logger(__name__)


EXTRACTION_SYSTEM_PROMPT = """\
You are a memory extraction assistant inside WAX, an AI runtime.

Your job: after a conversation between the user and the main AI, decide
what future WAX should genuinely remember. Output structured memories.

Memory kinds:
- fact: stable knowledge (name, exam date, goals, deadlines)
- preference: how the user likes things (short explanations, diagrams)
- skill: the user's strengths/weaknesses (strong at chemistry)
- episodic: meaningful events (NOT every conversation — only salient ones)
- project: living things being worked on (resume, WAX redesign, assignment)
- waiting: the AI is waiting for something ("I'll send the PDF tomorrow")
- procedural: how-to knowledge the AI learned

Rules:
1. Only extract memories that are genuinely worth retaining long-term.
2. Do NOT store conversation transcripts. Distill.
3. Do NOT store trivia ("user said okay").
4. If nothing important happened, return an empty list.
5. Each memory should be one focused piece of knowledge.

Output format: a JSON array of memory objects. Each object has:
{
  "kind": "fact|preference|skill|episodic|project|waiting|procedural",
  "content": {"text": "the distilled knowledge"},
  "importance": 0.0-1.0,
  "confidence": 0.0-1.0
}

Output ONLY the JSON array, no other text.
"""


# Words that are too common to be meaningful for similarity comparison.
_STOP_WORDS = frozenset(
    {
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
        "student",
        "said",
        "asked",
        "has",
        "have",
        "with",
        "that",
        "this",
        "from",
        "are",
        "be",
        "been",
        "will",
        "would",
        "could",
        "should",
    }
)


def _texts_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Check if two texts are saying the same thing via word overlap.

    "Student's name is David" and "The user's name is David" — same fact,
    different wording. Word overlap catches this: if 60%+ of the
    significant words in the shorter text appear in the longer text,
    they're the same fact.
    """
    words_a = {w.lower() for w in a.split() if w.lower() not in _STOP_WORDS and len(w) > 2}
    words_b = {w.lower() for w in b.split() if w.lower() not in _STOP_WORDS and len(w) > 2}
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    smaller = min(len(words_a), len(words_b))
    return (overlap / smaller) >= threshold


async def extract_memories(
    *,
    session: AsyncSession,
    intelligence: IntelligenceService,
    principal_id: str,
    execution_id: str,
    user_message: str,
    ai_response: str,
) -> list[str]:
    """Run the memory extraction pass and store the results.

    Returns the list of created memory IDs.
    """
    # If the user message is trivial, skip extraction entirely.
    if len(user_message.strip()) < 20 or len(ai_response.strip()) < 50:
        return []

    # Build the extraction request
    conversation = f"User said: {user_message[:2000]}\n\nAI responded: {ai_response[:2000]}"
    request = LLMRequest(
        messages=[
            LLMMessage(role=MessageRole.SYSTEM, content=EXTRACTION_SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=conversation),
        ],
        temperature=0.1,  # low temperature for deterministic extraction
    )

    memory_ids: list[str] = []
    try:
        response = await intelligence.complete(request)
        memories = _parse_extraction_response(response.content)
        # If the LLM returned non-JSON or empty, fall back to heuristic
        if not memories:
            log.info("memory.extraction_empty_fallback", principal_id=principal_id)
            memories = [
                {
                    "kind": "episodic",
                    "content": {
                        "text": f"User asked: {user_message[:200]}",
                        "response_preview": ai_response[:200],
                    },
                    "importance": 0.4,
                    "confidence": 0.8,
                }
            ]
    except Exception as e:
        log.warning("memory.extraction_failed", error=str(e)[:200], error_type=type(e).__name__)
        # Fallback: heuristic episodic memory
        memories = [
            {
                "kind": "episodic",
                "content": {
                    "text": f"User asked: {user_message[:200]}",
                    "response_preview": ai_response[:200],
                },
                "importance": 0.4,
                "confidence": 0.8,
            }
        ]

    repo = MemoryRepository(session)
    for mem in memories[:5]:  # cap at 5 memories per interaction
        try:
            kind_str = mem.get("kind", "episodic")
            try:
                kind_enum = MemoryKind(kind_str)
            except ValueError:
                kind_enum = MemoryKind.EPISODIC

            content = mem.get("content", {})
            if isinstance(content, str):
                content = {"text": content}

            # Part 8: Memory lifecycle — STRENGTHEN existing memories
            # Before creating a new memory, check if a similar one already
            # exists for this principal. If it does, strengthen it (increase
            # confidence + importance) instead of creating a duplicate.
            content_text = content.get("text", "") if isinstance(content, dict) else str(content)
            if content_text and kind_enum in (
                MemoryKind.FACT,
                MemoryKind.PREFERENCE,
                MemoryKind.SKILL,
            ):
                existing = await repo.search_relevant(
                    principal_id=principal_id,
                    query=content_text[:200],
                    limit=3,
                )
                for ex in existing:
                    if ex.kind == kind_enum.value and ex.status == "active":
                        ex_content = (
                            ex.content
                            if isinstance(ex.content, dict)
                            else {"text": str(ex.content)}
                        )
                        ex_text = ex_content.get("text", "")
                        # Fix 9: Use word-overlap similarity instead of exact first-50-chars.
                        # "Student's name is David" and "The user's name is David" are the
                        # same fact but have different first 50 chars. Word overlap catches
                        # this: if 60%+ of significant words match, it's the same fact.
                        if _texts_similar(ex_text, content_text):
                            await repo.strengthen(ex.id)
                            log.info(
                                "memory.strengthened",
                                memory_id=ex.id,
                                principal_id=principal_id,
                                kind=kind_enum.value,
                            )
                            memory_ids.append(ex.id)
                            break
                else:
                    # No similar memory found — create a new one
                    record = await repo.create(
                        MemoryCreate(
                            principal_id=principal_id,
                            kind=kind_enum,
                            content=content,
                            provenance="model_observation",
                            source_execution_id=execution_id,
                            confidence=float(mem.get("confidence", 0.7)),
                            importance=float(mem.get("importance", 0.5)),
                            observed_at=datetime.now(UTC),
                        )
                    )
                    memory_ids.append(record.id)
                    continue
            else:
                record = await repo.create(
                    MemoryCreate(
                        principal_id=principal_id,
                        kind=kind_enum,
                        content=content,
                        provenance="model_observation",
                        source_execution_id=execution_id,
                        confidence=float(mem.get("confidence", 0.7)),
                        importance=float(mem.get("importance", 0.5)),
                        observed_at=datetime.now(UTC),
                    )
                )
                memory_ids.append(record.id)
        except Exception as e:
            log.warning("memory.store_failed", error=str(e)[:200])

    if memory_ids:
        log.info(
            "memory.extracted",
            count=len(memory_ids),
            principal_id=principal_id,
            execution_id=execution_id,
        )

    return memory_ids


def _parse_extraction_response(content: str) -> list[dict[str, Any]]:
    """Parse the LLM's JSON array response, tolerating markdown fences."""
    text = content.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last line (fences)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        if isinstance(parsed, dict) and "memories" in parsed:
            return [m for m in parsed["memories"] if isinstance(m, dict)]
    except json.JSONDecodeError:
        # The LLM didn't produce valid JSON — fall back to empty (the
        # caller will use the heuristic fallback).
        log.warning("memory.extraction_parse_failed", content_preview=text[:200])
        return []

    return []
