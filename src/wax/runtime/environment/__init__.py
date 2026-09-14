"""Environment Negotiation package (ADR-0038, Phase 5).

Universal contracts for the intelligence to DECLARE what it needs and
the runtime to PLAN resources. The intelligence never chooses unsafe
host-level details.
"""

from __future__ import annotations

from wax.runtime.environment.contracts import (
    ENVIRONMENT_PLAN_MAX_BYTES,
    ENVIRONMENT_PURPOSE_MAX_CHARS,
    ENVIRONMENT_REQUIREMENT_MAX_BYTES,
    CredentialRequirement,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequirement,
    EnvironmentState,
    EnvironmentValidationError,
    ExecutionRequirement,
    IsolationGrade,
    NetworkPolicy,
    NetworkPolicyKind,
    ToolRequirement,
    WorkspaceRequirement,
    validate_environment_requirement,
)

__all__ = [
    "ENVIRONMENT_PLAN_MAX_BYTES",
    "ENVIRONMENT_PURPOSE_MAX_CHARS",
    "ENVIRONMENT_REQUIREMENT_MAX_BYTES",
    "CredentialRequirement",
    "EnvironmentLease",
    "EnvironmentPlan",
    "EnvironmentRequirement",
    "EnvironmentState",
    "EnvironmentValidationError",
    "ExecutionRequirement",
    "IsolationGrade",
    "NetworkPolicy",
    "NetworkPolicyKind",
    "ToolRequirement",
    "WorkspaceRequirement",
    "validate_environment_requirement",
]
