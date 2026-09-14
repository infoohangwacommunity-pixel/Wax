"""wax.continuity — conversation continuity across sessions and interfaces.

A user returning after days should continue naturally. This requires:
1. Conversation threading (group messages by principal + recent time window)
2. Context stitching (recent memory + active objective + last execution state)
3. Recovery of interrupted executions
4. Reply correlation (link responses to original user messages)

Architecture:
- ConversationRecord: groups messages within a time window per principal
- ConversationRepository: persistence
- ConversationService: opens/closes conversations, stitches context
- ContinuityService: top-level "what context should the AI see?" API

INVARIANT: Continuity is per-principal. The runtime never mixes contexts
across principals (privacy + correctness).
"""

from wax.continuity.contracts import (
    ContinuityContext,
    ConversationRecord,
    ConversationStatus,
)
from wax.continuity.repository import ConversationRepository
from wax.continuity.service import ContinuityService, ConversationService

__all__ = [
    "ContinuityContext",
    "ContinuityService",
    "ConversationRecord",
    "ConversationRepository",
    "ConversationService",
    "ConversationStatus",
]
