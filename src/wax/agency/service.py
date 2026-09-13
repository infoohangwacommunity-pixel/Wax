"""Agency service — the runtime's approval gate for AI decisions.

The AI submits an AgencyDecision. The service:
1. Looks up the required approval level for the decision kind
2. If level is NONE → approve immediately
3. If level is INFORMATIONAL → approve + record for audit
4. If level is REVERSIBLE or higher → require human approval
5. Records every decision + verdict to the audit log

INVARIANT: The AI cannot execute directly. Every decision passes here.
"""

from __future__ import annotations

from ulid import ULID

from wax.agency.contracts import (
    AgencyDecision,
    AgencyPolicy,
    AgencyVerdict,
    ApprovalLevel,
)
from wax.authority.service import AuthorizationService
from wax.runtime.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)


# Levels at or above this threshold require explicit human approval.
HUMAN_APPROVAL_THRESHOLD = {ApprovalLevel.EXTERNALLY_VISIBLE,
                            ApprovalLevel.FINANCIALLY_CONSEQUENTIAL,
                            ApprovalLevel.DESTRUCTIVE,
                            ApprovalLevel.IRREVERSIBLE}


class AgencyService:
    """The runtime's gate for AI agency decisions.

    Usage:
        agency = AgencyService(session, auth, policy)
        verdict = await agency.evaluate(decision)
        if not verdict.approved:
            return f"Denied: {verdict.reason}"
        if verdict.requires_human_approval:
            return "Pending human approval"
        # proceed
    """

    def __init__(
        self,
        session: AsyncSession,
        auth: AuthorizationService,
        policy: AgencyPolicy | None = None,
    ) -> None:
        self._session = session
        self._auth = auth
        self._policy = policy or AgencyPolicy.default()

    async def evaluate(
        self, decision: AgencyDecision
    ) -> AgencyVerdict:
        """Evaluate an agency decision. Returns a verdict; never raises."""
        decision_id = str(ULID())
        level = self._policy.level_for(decision.kind)

        requires_human = level in HUMAN_APPROVAL_THRESHOLD
        approved = not requires_human  # auto-approve if below threshold

        verdict = AgencyVerdict(
            decision_id=decision_id,
            approved=approved,
            requires_human_approval=requires_human,
            level=level,
            reason=self._reason_for(level, approved, requires_human),
        )

        # Record to audit log
        await self._auth._audit(
            actor_principal_id=decision.principal_id,
            actor_kind="ai",
            event_kind="agency.decision",
            outcome="success" if approved else ("pending" if requires_human else "denied"),
            payload={
                "decision_id": decision_id,
                "decision_kind": decision.kind.value,
                "description": decision.description,
                "capability": decision.capability_name,
                "approval_level": level.value,
                "requires_human_approval": requires_human,
                "confidence": decision.confidence,
            },
        )

        log.info(
            "agency.decision.evaluated",
            decision_id=decision_id,
            kind=decision.kind.value,
            level=level.value,
            approved=approved,
            requires_human=requires_human,
        )

        return verdict

    def _reason_for(
        self, level: ApprovalLevel, approved: bool, requires_human: bool
    ) -> str:
        if approved and not requires_human:
            return f"Auto-approved (level={level.value})"
        if requires_human:
            return f"Requires human approval (level={level.value})"
        return f"Denied (level={level.value})"
