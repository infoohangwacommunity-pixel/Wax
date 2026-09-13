"""Trust boundaries for WAX.

Defines which actors are trusted at what level. The runtime enforces
that low-trust actors cannot affect high-trust resources.

Trust levels (low → high):
- UNTRUSTED: web content, tool output, model-generated text, external APIs
- AUTHENTICATED: a principal with verified credentials
- PRIVILEGED: a service principal with explicit elevated role
- SYSTEM: the runtime itself (cron, internal cleanup)

INVARIANT: The AI is UNTRUSTED. Even if a principal authorized the AI,
the AI's outputs are still untrusted inputs to the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    AUTHENTICATED = "authenticated"
    PRIVILEGED = "privileged"
    SYSTEM = "system"


@dataclass(frozen=True)
class TrustBoundary:
    """A trust boundary declaration.

    The runtime consults this when deciding whether to allow an action.
    """

    actor_kind: str  # human | service | ai | system | external
    trust_level: TrustLevel

    @classmethod
    def for_external_input(cls) -> TrustBoundary:
        """Web content, tool output, external APIs — always untrusted."""
        return cls(actor_kind="external", trust_level=TrustLevel.UNTRUSTED)

    @classmethod
    def for_ai(cls) -> TrustBoundary:
        """The AI itself is always untrusted, even when acting for a principal."""
        return cls(actor_kind="ai", trust_level=TrustLevel.UNTRUSTED)

    @classmethod
    def for_authenticated_human(cls) -> TrustBoundary:
        return cls(actor_kind="human", trust_level=TrustLevel.AUTHENTICATED)

    @classmethod
    def for_system(cls) -> TrustBoundary:
        return cls(actor_kind="system", trust_level=TrustLevel.SYSTEM)

    def can_access(self, required: TrustLevel) -> bool:
        """Check if this boundary satisfies the required trust level.

        UNTRUSTED < AUTHENTICATED < PRIVILEGED < SYSTEM
        """
        order = {
            TrustLevel.UNTRUSTED: 0,
            TrustLevel.AUTHENTICATED: 1,
            TrustLevel.PRIVILEGED: 2,
            TrustLevel.SYSTEM: 3,
        }
        return order[self.trust_level] >= order[required]
