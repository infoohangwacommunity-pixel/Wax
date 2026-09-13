"""Contracts for the agency system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ApprovalLevel(StrEnum):
    """How much human approval an action requires.

    Lower levels = less approval needed. Higher levels = more approval.
    The runtime policy maps each decision kind to a required level.
    """

    NONE = "none"  # no approval required (e.g., read memory)
    INFORMATIONAL = "informational"  # inform the user, no approval needed
    REVERSIBLE = "reversible"  # requires approval if setting is strict
    EXTERNALLY_VISIBLE = "externally_visible"  # sends a message, makes an API call
    FINANCIALLY_CONSEQUENTIAL = "financially_consequential"  # could spend money
    DESTRUCTIVE = "destructive"  # could delete data
    IRREVERSIBLE = "irreversible"  # cannot be undone


class AgencyDecisionKind(StrEnum):
    """What kind of action the AI wants to take.

    Universal — not domain-specific. No 'tutor.explain' or 'lesson.create'.
    """

    READ_MEMORY = "read_memory"
    WRITE_MEMORY = "write_memory"
    INVOKE_CAPABILITY = "invoke_capability"
    START_EXECUTION = "start_execution"
    CANCEL_EXECUTION = "cancel_execution"
    SEND_MESSAGE = "send_message"  # externally visible
    CALL_EXTERNAL_API = "call_external_api"  # externally visible
    PROVISION_ENVIRONMENT = "provision_environment"
    DESTRUCTIVE_ACTION = "destructive_action"
    OTHER = "other"


@dataclass
class AgencyDecision:
    """A decision the AI wants to make.

    The AI submits this to the AgencyService. The service consults the
    policy and returns a verdict (approved / needs_human_approval / denied).
    """

    principal_id: str
    kind: AgencyDecisionKind
    description: str
    capability_name: str | None = None
    inputs_summary: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0  # AI's stated confidence (0.0–1.0)
    rationale: str | None = None


@dataclass
class AgencyVerdict:
    """The runtime's verdict on an agency decision.

    The AI cannot override this. The runtime's decision is final.
    """

    decision_id: str
    approved: bool
    requires_human_approval: bool
    level: ApprovalLevel
    reason: str
    request_id: str | None = None


@dataclass
class AgencyPolicy:
    """Maps decision kinds to required approval levels.

    The default policy is conservative — externally-visible actions
    require approval; destructive/irreversible actions always require
    human approval.
    """

    levels: dict[str, ApprovalLevel] = field(default_factory=dict)

    @classmethod
    def default(cls) -> AgencyPolicy:
        """The default conservative policy."""
        return cls(
            levels={
                AgencyDecisionKind.READ_MEMORY.value: ApprovalLevel.NONE,
                AgencyDecisionKind.WRITE_MEMORY.value: ApprovalLevel.INFORMATIONAL,
                AgencyDecisionKind.INVOKE_CAPABILITY.value: ApprovalLevel.REVERSIBLE,
                AgencyDecisionKind.START_EXECUTION.value: ApprovalLevel.REVERSIBLE,
                AgencyDecisionKind.CANCEL_EXECUTION.value: ApprovalLevel.REVERSIBLE,
                AgencyDecisionKind.SEND_MESSAGE.value: ApprovalLevel.EXTERNALLY_VISIBLE,
                AgencyDecisionKind.CALL_EXTERNAL_API.value: ApprovalLevel.EXTERNALLY_VISIBLE,
                AgencyDecisionKind.PROVISION_ENVIRONMENT.value: ApprovalLevel.REVERSIBLE,
                AgencyDecisionKind.DESTRUCTIVE_ACTION.value: ApprovalLevel.IRREVERSIBLE,
                AgencyDecisionKind.OTHER.value: ApprovalLevel.REVERSIBLE,
            }
        )

    def level_for(self, kind: AgencyDecisionKind | str) -> ApprovalLevel:
        kind_value = kind.value if isinstance(kind, AgencyDecisionKind) else kind
        return self.levels.get(kind_value, ApprovalLevel.REVERSIBLE)
