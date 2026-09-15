"""Pydantic contracts + enums for the memory system."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryKind(StrEnum):
    """Discriminator for memory types.

    Modeled on human memory systems (Tulving, 1972):
    - EPISODIC: events the system observed or participated in
    - SEMANTIC: facts and knowledge
    - PROCEDURAL: how-to knowledge (capability usage patterns)
    - CONTEXTUAL: current/recent context for active work
    - EXTERNAL: information retrieved from external sources (APIs, web)
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    CONTEXTUAL = "contextual"
    EXTERNAL = "external"


class MemoryStatus(StrEnum):
    """Lifecycle states a memory row actually takes.

    active → superseded (a newer memory replaced it, provenance kept)
    active → forgotten  (explicit forget or TTL expiry; row retained for
            audit, excluded from every retrieval path — soft delete by
            declared design)

    There is deliberately no "archived" state: nothing in the runtime
    sets one, and a lifecycle state no writer ever produces is a false
    mechanism. Memory retention (hard deletion) is a founder policy
    decision, not an engineering constant — see docs/omega docs.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class MemoryProvenance(StrEnum):
    """Where a memory came from."""

    USER_STATEMENT = "user_statement"
    MODEL_OBSERVATION = "model_observation"
    SYSTEM_EVENT = "system_event"
    EXTERNAL_API = "external_api"


class MemoryLinkKind(StrEnum):
    """Typed relationships between memories (ADR-0022, ADR-0036 Phase 3).

    Knowledge relationships the retrieval engine can traverse. Deliberate
    split of responsibilities:

    - supersession is a LIFECYCLE mechanism (MemoryRecord.superseded_by +
      MemoryStatus.superseded) — it changes what is retrieved;
    - these link kinds are EVIDENCE relationships — they say how memories
      relate and are traversable, but never rewrite lifecycle state.

    Relational table, not a graph database: the abstraction is the typed
    edge; the storage is a detail (mission §8).

    ADR-0036 (Phase 3) extends the set with:
    - DEPENDS_ON: A needs B to be true (dependency edge)
    - CONFLICTS_WITH: A and B are mutually exclusive (conflict graph)
    """

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"
    RELATED_TO = "related_to"
    DEPENDS_ON = "depends_on"
    CONFLICTS_WITH = "conflicts_with"


class MemoryLinkCreate(BaseModel):
    """One typed edge between two of the principal's memories."""

    to_memory_id: str
    kind: MemoryLinkKind


class MemoryLinkRead(BaseModel):
    """A typed edge as returned from the API."""

    id: str
    from_memory_id: str
    to_memory_id: str
    kind: str
    created_at: datetime


class MemoryCreate(BaseModel):
    """Payload to create a memory record."""

    principal_id: str
    kind: MemoryKind
    content: dict[str, Any] = Field(..., description="Structured memory content")
    provenance: str = Field(..., description="Where this memory came from")
    source_execution_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How much this memory matters (mission §6.3); NULL = neutral (0.5) in ranking",
    )
    observed_at: datetime | None = Field(
        default=None,
        description="When the fact was observed (may differ from write "
        "time); NULL = observed at creation",
    )
    expires_at: datetime | None = None
    sensitivity: int = Field(default=0, ge=0, le=3)
    summary: str | None = Field(default=None, max_length=2000)
