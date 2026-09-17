"""Contracts for the continuity system.

Stripped down after the open-world reset. ContinuityService and
ContinuityContext are gone (dead code). What remains: ConversationStatus
and the default timeouts used by ConversationRepository.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum


class ConversationStatus(StrEnum):
    """Lifecycle of a conversation.

    active → idle (no messages for a while, still resumable)
    idle → archived (idle past the archive horizon; not resumed)
    active/idle → closed (explicitly closed)

    State markings only: no conversation row is ever deleted here;
    retention is a founder policy boundary.
    """

    ACTIVE = "active"
    IDLE = "idle"
    ARCHIVED = "archived"
    CLOSED = "closed"


# Default timeouts
DEFAULT_IDLE_TIMEOUT = timedelta(minutes=30)
DEFAULT_ARCHIVE_TIMEOUT = timedelta(days=7)
