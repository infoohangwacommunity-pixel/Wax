"""wax.state.identity_models — persistence models for the identity system.

These are SQLAlchemy 2.0 declarative models. The identity *contracts*
(what a Principal IS, conceptually) live in wax.core or wax.identity as
Pydantic models; the persistence shape lives here.

Tables:
- principals: the universal identity (a human or service)
- principal_credentials: interface-specific identifiers (phone, email, ...)

INV-02 (interface independence): No column in `principals` may reference
a specific interface (no `whatsapp_phone` column). Interface identifiers
live in `principal_credentials` with a `kind` discriminator.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class Principal(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A universal WAX principal — a human or service that can be authorized.

    A principal is intentionally minimal: it has an ID, timestamps, and
    nothing else. All interface-specific identifiers (phone, email, etc.)
    are stored separately in `principal_credentials`.

    This is what makes WAX identity interface-independent: if WhatsApp
    disappears, the principal survives. Only the credential with
    kind="whatsapp_phone" is lost.
    """

    __tablename__ = "principals"

    # Status of the principal (active, suspended, revoked)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    # Optional display name (set by user or admin, not required)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Soft-delete timestamp. If set, principal is considered deleted.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationship to credentials (one principal → many credentials)
    credentials: Mapped[list[PrincipalCredential]] = relationship(
        "PrincipalCredential",
        back_populates="principal",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.deleted_at is None


class PrincipalCredential(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """An interface-specific identifier attached to a principal.

    Examples:
        kind="whatsapp_phone", value="+2348000000000"
        kind="email", value="user@example.com"
        kind="web_session", value="<opaque session token>"
        kind="oauth_subject", value="google_12345"

    The (kind, value) pair is unique — a credential maps to exactly one
    principal. But a principal can have many credentials of different kinds,
    which is what allows interface handoff (Foundation §51, §52).
    """

    __tablename__ = "principal_credentials"
    __table_args__ = (
        UniqueConstraint("kind", "value", name="uq_principal_credentials_kind_value"),
    )

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The "kind" of credential. Examples:
    # whatsapp_phone, email, web_session, oauth_subject, api_key
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # The credential value (phone number, email, token, etc.)
    value: Mapped[str] = mapped_column(String(512), nullable=False)

    # Whether this credential is currently usable
    is_verified: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )

    # When this credential was last used (UTC)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationship back to principal
    principal: Mapped[Principal] = relationship("Principal", back_populates="credentials")
