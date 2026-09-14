"""Persistence for dynamic provisioning (Phase S).

A ProvisionedResource is a temporary runtime resource (e.g. a scratch
directory) with a first-class lifecycle. Philosophy:

- Nothing should live forever unless intentionally promoted.
- Every resource has: owner, lifecycle, limits, cleanup, audit.
- The kind is a runtime mechanism name ("scratch_dir"), not a domain
  category — new provisionable kinds need no schema change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ProvisionedResourceRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A provisioned ephemeral resource with owner + TTL + audit trail."""

    __tablename__ = "provisioned_resources"
    __table_args__ = (
        Index("ix_provisioned_status_expires", "status", "expires_at"),
        Index("ix_provisioned_principal", "principal_id"),
    )

    # Runtime mechanism kind: "scratch_dir" today; future kinds (container,
    # browser session, temp memory) need no migration.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    # Ownership: who asked, under which execution.
    principal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Where the resource lives (e.g. filesystem path) — opaque to the AI.
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Lifecycle. expires_at=None means PROMOTED (intentionally permanent).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Declared limits (bytes, files, ...) — enforced by the runtime, and
    # recorded here so accounting is inspectable.
    limits: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
