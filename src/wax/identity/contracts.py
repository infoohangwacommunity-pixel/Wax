"""Pydantic contracts for the identity system.

These are the API-level representations of identity — what callers send
and receive. They are decoupled from the ORM models in
`wax.state.identity_models`, which represent the persistence shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Allowed credential kinds. Adding a new kind here is a deliberate
# architectural act — it means a new interface is being integrated.
ALLOWED_CREDENTIAL_KINDS: frozenset[str] = frozenset(
    {
        "whatsapp_phone",
        "email",
        "web_session",
        "oauth_subject",
        "api_key",
        "service_id",
    }
)

# The identity boundary mapping (Invariant INV-02): an interface kind maps
# to exactly one PrincipalCredential kind, and vice versa. This constant is
# the SINGLE source of truth for that mapping — the bridge, the capability
# layer, and delivery paths derive their views from here instead of each
# re-declaring their own copy (drift on the identity boundary is a
# constitutional risk: this table IS the "interface credentials attach to
# a principal" invariant).
INTERFACE_CREDENTIAL_KINDS: dict[str, str] = {
    "whatsapp": "whatsapp_phone",
    "web": "web_session",
    "telegram": "telegram_chat",
    "api": "api_key",
}

# Reverse view: credential kind → interface kind.
CREDENTIAL_KIND_INTERFACES: dict[str, str] = {
    v: k for k, v in INTERFACE_CREDENTIAL_KINDS.items()
}


class PrincipalRead(BaseModel):
    """A principal as returned from the API."""

    id: str
    status: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    is_active: bool

    @classmethod
    def from_orm(cls, principal: object) -> PrincipalRead:
        return cls(
            id=principal.id,  # type: ignore[attr-defined]
            status=principal.status,  # type: ignore[attr-defined]
            display_name=principal.display_name,  # type: ignore[attr-defined]
            created_at=principal.created_at,  # type: ignore[attr-defined]
            updated_at=principal.updated_at,  # type: ignore[attr-defined]
            is_active=principal.is_active,  # type: ignore[attr-defined]
        )



class PrincipalCredentialRead(BaseModel):
    """A credential as returned from the API (value may be masked)."""

    id: str
    principal_id: str
    kind: str
    value_masked: str  # masked for safety
    is_verified: bool
    last_used_at: datetime | None
    created_at: datetime

    @classmethod
    def from_orm(cls, cred: object) -> PrincipalCredentialRead:
        return cls(
            id=cred.id,  # type: ignore[attr-defined]
            principal_id=cred.principal_id,  # type: ignore[attr-defined]
            kind=cred.kind,  # type: ignore[attr-defined]
            value_masked=_mask(cred.value),  # type: ignore[attr-defined]
            is_verified=cred.is_verified,  # type: ignore[attr-defined]
            last_used_at=cred.last_used_at,  # type: ignore[attr-defined]
            created_at=cred.created_at,  # type: ignore[attr-defined]
        )


def _mask(value: str) -> str:
    """Mask a credential value for safe display.

    Keeps the first 2 and last 2 chars, masks the middle.
    - "+2348000000000" → "+2************00"
    - "user@example.com" → "us**************om"
    - short values → all-masked except first char.
    """
    if len(value) <= 4:
        return value[0] + "*" * (len(value) - 1) if value else ""
    return value[:2] + "*" * (len(value) - 4) + value[-2:]
