"""Persistence for the credential vault (ADR-0040, Phase 7).

Four tables:

- connector_definitions: declared resource types (git_host,
  package_registry, cloud_deployment, file_storage, messaging).
  Seeded at startup with the universal set. NEVER brands.

- principal_connections: a principal has connected a service.
  The secret_blob column stores the encrypted secret; the runtime
  NEVER exposes the plaintext to the model.

- credential_grants: a temporary objective/execution-scoped
  authorization with TTL + revocation.

- credential_events: append-only audit log of credential lifecycle
  events (connected, verified, injected, used, rotated, revoked,
  expired, denied).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ConnectorDefinitionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A declared resource type (git_host, package_registry, etc.).

    The vault NEVER hardcodes a brand. Connector definitions are
    declarative; the runtime resolves them to actual services at
    injection time. New connectors absorb by inserting a row, not
    by changing code.
    """

    __tablename__ = "connector_definitions"
    __table_args__ = (
        Index("ix_connectors_name", "name", unique=True),
    )

    # Stable resource type name: "git_host", "package_registry", etc.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Human-readable description (for the intelligence).
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    # Supported scopes (JSON list of strings).
    supported_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Authentication methods (JSON list: "oauth2", "api_key", "bearer_token").
    auth_methods: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Version of the connector definition.
    version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1.0.0", server_default="1.0.0"
    )


class PrincipalConnectionRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A principal has connected a service.

    The secret_blob column stores the encrypted secret. The runtime
    NEVER exposes the plaintext to the model — only the connection_id
    (opaque).
    """

    __tablename__ = "principal_connections"
    __table_args__ = (
        Index("ix_pc_principal", "principal_id"),
        Index("ix_pc_connector", "connector_name"),
        Index("ix_pc_status", "status"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # The connector resource type (FK to connector_definitions.name, but
    # stored as a string to allow open-world connector addition without
    # migration).
    connector_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Lifecycle: active / revoked / expired
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    # Granted scopes (JSON list of strings — the scopes the principal
    # consented to).
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )

    # The encrypted secret. NEVER exposed to the model.
    secret_blob: Mapped[str] = mapped_column(Text, nullable=False)

    # Consent + expiry.
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Last-used time (for observability).
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Revocation evidence.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class CredentialGrantRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A temporary objective/execution-scoped authorization.

    Grants are scoped to a specific objective + execution. They expire
    after the TTL. The intelligence receives an opaque `handle` (not
    the secret). The vault injects the actual secret into the
    environment boundary at provisioning time.
    """

    __tablename__ = "credential_grants"
    __table_args__ = (
        Index("ix_grants_principal", "principal_id"),
        Index("ix_grants_connection", "connection_id"),
        Index("ix_grants_status_expires", "status", "expires_at"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    connection_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principal_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The objective + execution this grant is scoped to.
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The scopes this grant authorizes (subset of the connection's scopes).
    scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    # The purpose the intelligence declared (bounded, audited).
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Opaque handle the intelligence uses to reference this grant.
    # NEVER the secret; a ULID-based opaque token.
    handle: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Lifecycle: active / revoked / expired
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CredentialEventRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """Append-only audit log of credential lifecycle events.

    The vault NEVER logs secret values. Events record the connection_id
    + grant_id + scopes, never the secret.
    """

    __tablename__ = "credential_events"
    __table_args__ = (
        Index("ix_cred_events_principal", "principal_id"),
        Index("ix_cred_events_connection", "connection_id"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    grant_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Event kind: connected, verified, injected, used, rotated,
    # revoked, expired, denied.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Connector resource type (for filtering).
    connector_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Scopes involved (JSON list).
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Optional detail (e.g. denial reason).
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
