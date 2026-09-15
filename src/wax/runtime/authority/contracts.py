"""Authority broker contracts (ADR-0048, P0-Authority).

The authority broker is the SOLE path through which the intelligence
requests external authority. The model NEVER supplies a raw secret.

Design principle: The runtime provides affordances, not workflows.
The intelligence decides WHEN authority is needed; the runtime decides
HOW authority is safely acquired and used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class HandoffStatus(StrEnum):
    PENDING = "pending"
    OPENED = "opened"
    AWAITING_HUMAN = "awaiting_human"
    SUBMITTED = "submitted"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class AuthorityStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SUSPENDED = "suspended"
    DESTROYED = "destroyed"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"
    INVALID = "invalid"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


class EffectClass(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


class MaterialType(StrEnum):
    OPAQUE_SECRET = "opaque_secret"
    SESSION_MATERIAL = "session_material"
    DELEGATED_GRANT = "delegated_grant"
    BROWSER_SESSION_REFERENCE = "browser_session_reference"


@dataclass(frozen=True)
class AuthorityRequest:
    purpose: str
    origin_reference: str | None = None
    requested_actions: list[dict[str, Any]] = field(default_factory=list)
    expires_at: datetime | None = None
    human_required: bool = True


@dataclass(frozen=True)
class AuthorityRequestResult:
    status: str
    handoff_ref: str | None = None
    authority_ref: str | None = None
    expires_at: datetime | None = None
    allowed_actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class AuthorityUseRequest:
    authority_ref: str
    action_ref: str
    environment_id: str | None = None
    purpose: str = ""


@dataclass(frozen=True)
class AuthorityUseResult:
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    evidence_ref: str | None = None
