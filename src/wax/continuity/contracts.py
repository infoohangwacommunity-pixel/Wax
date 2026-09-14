"""Contracts for the continuity system."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConversationStatus(StrEnum):
    """Lifecycle of a conversation.

    A conversation is "active" while the user is exchanging messages within
    a short time window. After the idle period (default 30 min) the
    maintenance loop marks it "idle"; past the archive horizon (default
    7 days of silence) it becomes "archived" and is no longer resumed —
    the next message opens a fresh conversation (memory and objectives
    carry the continuity, not the conversation row). These are state
    markings only: no conversation row is ever deleted here; retention
    (deletion) is a founder policy boundary.
    """

    ACTIVE = "active"
    IDLE = "idle"  # no messages for a while, but still resumable
    ARCHIVED = "archived"  # idle past the archive horizon; not resumed
    CLOSED = "closed"  # explicitly closed by user or system


# Default timeouts
DEFAULT_IDLE_TIMEOUT = timedelta(minutes=30)
DEFAULT_ARCHIVE_TIMEOUT = timedelta(days=7)


class ConversationRecord(BaseModel):
    """A conversation groups messages within a time window per principal.

    The runtime uses this to decide:
    - Whether to fetch recent context for an incoming message
    - Whether to start a new conversation (vs. resume an existing one)
    - When to forget stale context
    """

    id: str
    principal_id: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    started_at: datetime
    last_message_at: datetime
    message_count: int = 0
    # The interface the conversation started on (may change if user
    # switches interface mid-conversation)
    interface_kind: str
    # Optional link to the active objective (if the conversation has one)
    objective_id: str | None = None
    # Optional: the last execution_id in this conversation (for resume)
    last_execution_id: str | None = None
    # Optional summary (computed periodically via the intelligence layer)
    summary: str | None = None


class ContinuityContext(BaseModel):
    """The full context bundle the AI receives when continuing a conversation.

    Composed by ContinuityService from:
    - Active conversation (if any)
    - Recent memory (Phase F)
    - Active objective (Phase L)
    - Last execution state (Phase H)

    The AI uses this to "remember" what happened before, without the
    runtime having to dump every prior message into the context window.
    """

    principal_id: str
    conversation_id: str | None = None
    conversation_summary: str | None = None
    active_objective_id: str | None = None
    active_objective_description: str | None = None
    last_execution_id: str | None = None
    last_execution_status: str | None = None
    recent_memories: list[dict[str, Any]] = Field(default_factory=list)
    # Outstanding durable work (mission Phase 5 ACTIVE_WORK section, §99
    # pending actions) — metadata only, never payload contents.
    active_work: list[dict[str, Any]] = Field(default_factory=list)
    # Known artifacts (mission Phase 5 ARTIFACTS section) — filename,
    # integrity prefix, size; never file bytes.
    recent_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    # Environment facts (interface attached to this interaction).
    environment: dict[str, Any] = Field(default_factory=dict)
    is_new_conversation: bool = True
    days_since_last_message: float | None = None
