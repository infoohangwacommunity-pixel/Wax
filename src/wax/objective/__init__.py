"""wax.objective — objective handling for WAX.

An objective is what a human wants the intelligence to pursue. WAX does
NOT hardcode "study mode" or any other domain-specific objective kind
(Directive §8, §9, INV-08).

Architecture:
- Objective: the universal representation of "what the human wants"
- ObjectiveStatus: lifecycle (pending → in_progress → succeeded | failed | abandoned)
- ObjectiveRepository: persistence (objectives survive process restart)
- ObjectiveService: create, list, transition, abandon

INVARIANT INV-08: Unknown legitimate objectives must not require modifying
the universal ontology. The Objective model is intentionally generic —
no `kind='tutoring'` or `kind='business_planning'` fields. The objective
is just a description the intelligence interprets.
"""

from wax.objective.contracts import (
    Objective,
    ObjectiveKind,
    ObjectiveStatus,
)
from wax.objective.repository import ObjectiveRepository
from wax.objective.service import ObjectiveService

__all__ = [
    "Objective",
    "ObjectiveKind",
    "ObjectiveRepository",
    "ObjectiveService",
    "ObjectiveStatus",
]
