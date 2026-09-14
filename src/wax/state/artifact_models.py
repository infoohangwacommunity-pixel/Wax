"""Persistence for first-class artifacts (ADR-0023, mission §56/§100).

An artifact is a piece of durable work output (an acquired package, a
produced dataset) with owner, integrity, and lifecycle — NOT an
arbitrary filesystem path. Artifact state is deliberately DISTINCT from
objective / execution / memory / event state (mission §100): the same
fact can exist as a file in a workspace, an audit row, and a memory,
but the artifact record is what makes it queryable runtime state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ArtifactRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """One artifact the runtime knows about, owned by a principal."""

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_principal", "principal_id"),
        Index("ix_artifacts_workspace", "workspace_resource_id"),
    )

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The scratch workspace (provisioned resource) holding the file, if
    # the artifact lives in one; the workspace's TTL governs the FILE's
    # lifetime (the reaper destroys expired workspaces). This record's
    # expires_at is recorded evidence of that lifetime — the row itself
    # is retained and no reader enforces the timestamp yet.
    workspace_resource_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Workspace-relative path (never an absolute host path — the host
    # layout is an implementation detail, not evidence).
    path: Mapped[str] = mapped_column(String(512), nullable=False)

    # Integrity + size at acquisition (mission §56: integrity is part of
    # the record, not a recompute-on-demand hope).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Where the artifact came from: the capability/execution that
    # produced it ("workspace.acquire" today; produced outputs later).
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # Mirrors the workspace TTL when known; NULL = governed by the
    # workspace, not by this record.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
