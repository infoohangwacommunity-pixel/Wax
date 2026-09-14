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
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"


class MemoryProvenance(StrEnum):
    """Where a memory came from."""

    USER_STATEMENT = "user_statement"
    MODEL_OBSERVATION = "model_observation"
    SYSTEM_EVENT = "system_event"
    EXTERNAL_API = "external_api"


class MemoryLinkKind(StrEnum):
    """Typed relationships between memories (ADR-0022, mission Phase 3).

    Knowledge relationships the retrieval engine can traverse. Deliberate
    split of responsibilities:

    - supersession is a LIFECYCLE mechanism (MemoryRecord.superseded_by +
      MemoryStatus.superseded) — it changes what is retrieved;
    - these link kinds are EVIDENCE relationships — they say how memories
      relate and are traversable, but never rewrite lifecycle state.

    Relational table, not a graph database: the abstraction is the typed
    edge; the storage is a detail (mission §8).
    """

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"
    RELATED_TO = "related_to"


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
        description="How much this memory matters (mission §6.3); "
        "NULL = neutral (0.5) in ranking",
    )
    observed_at: datetime | None = Field(
        default=None,
        description="When the fact was observed (may differ from write "
        "time); NULL = observed at creation",
    )
    expires_at: datetime | None = None
    sensitivity: int = Field(default=0, ge=0, le=3)
    summary: str | None = Field(default=None, max_length=2000)


class MemoryRead(BaseModel):
    """A memory record as returned from the API."""

    id: str
    principal_id: str
    kind: str
    status: str
    content: dict[str, Any]
    provenance: str
    source_execution_id: str | None
    confidence: float | None
    superseded_by: str | None
    expires_at: datetime | None
    sensitivity: int
    summary: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm(cls, m: object) -> MemoryRead:
        return cls(
            id=m.id,  # type: ignore[attr-defined]
            principal_id=m.principal_id,  # type: ignore[attr-defined]
            kind=m.kind,  # type: ignore[attr-defined]
            status=m.status,  # type: ignore[attr-defined]
            content=m.content,  # type: ignore[attr-defined]
            provenance=m.provenance,  # type: ignore[attr-defined]
            source_execution_id=m.source_execution_id,  # type: ignore[attr-defined]
            confidence=m.confidence,  # type: ignore[attr-defined]
            superseded_by=m.superseded_by,  # type: ignore[attr-defined]
            expires_at=m.expires_at,  # type: ignore[attr-defined]
            sensitivity=m.sensitivity,  # type: ignore[attr-defined]
            summary=m.summary,  # type: ignore[attr-defined]
            created_at=m.created_at,  # type: ignore[attr-defined]
            updated_at=m.updated_at,  # type: ignore[attr-defined]
        )
