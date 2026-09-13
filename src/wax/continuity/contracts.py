"""Contracts for the continuity system."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConversationStatus(StrEnum):
    """Lifecycle of a conversation.

    A conversation is "active" while the user is exchanging messages within
    a short time window. After a configurable idle period (default 30 min),
    it transitions to "idle". After a longer period (default 7 days), it
    transitions to "archived".
    """

    ACTIVE = "active"
    IDLE = "idle"  # no messages for a while, but still resumable
    ARCHIVED = "archived"  # older than retention; not loaded by default
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
    is_new_conversation: bool = True
    days_since_last_message: float | None = None
