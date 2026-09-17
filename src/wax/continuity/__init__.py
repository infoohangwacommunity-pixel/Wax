"""wax.continuity — conversation continuity across sessions and interfaces.

A user returning after days should continue naturally. This requires:
1. Conversation threading (group messages by principal + recent time window)
2. Conversation lifecycle (active → idle → archived)

The bridge handles context assembly directly (memory retrieval + enriched
system prompt). The old ContinuityService is gone — it was dead code with
crash bugs. ConversationRepository is the surviving piece.
"""

from wax.continuity.contracts import DEFAULT_IDLE_TIMEOUT, ConversationStatus
from wax.continuity.repository import ConversationRepository

__all__ = ["DEFAULT_IDLE_TIMEOUT", "ConversationRepository", "ConversationStatus"]
