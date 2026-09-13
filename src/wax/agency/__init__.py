"""wax.agency — controlled agency for the AI.

The AI may plan, request actions, and compose capabilities. The runtime
decides what actually executes (Directive §45, §96).

Architecture:
- AgencyDecision: what the AI wants to do, with a confidence + rationale
- ApprovalLevel: none | informational | reversible | externally_visible |
  financially_consequential | destructive | irreversible
- AgencyPolicy: maps decision categories to required approval levels
- AgencyService: routes a decision through policy → returns approval/denial

INVARIANT: The AI never executes directly. Every agency decision passes
through this service, which consults the runtime's policy.
"""

from wax.agency.contracts import (
    AgencyDecision,
    AgencyDecisionKind,
    AgencyPolicy,
    AgencyVerdict,
    ApprovalLevel,
)
from wax.agency.service import AgencyService

__all__ = [
    "AgencyDecision",
    "AgencyDecisionKind",
    "AgencyPolicy",
    "AgencyService",
    "AgencyVerdict",
    "ApprovalLevel",
]
