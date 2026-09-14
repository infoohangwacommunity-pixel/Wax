"""Persistence for workspace snapshots (ADR-0042, Phase 9).

A workspace snapshot is a content-addressed capture of a workspace's
file state at a point in time. Used for restore (resumability) and
for artifact provenance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class WorkspaceSnapshotRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A snapshot of a workspace's file state."""

    __tablename__ = "workspace_snapshots"
    __table_args__ = (
        Index("ix_wsnap_principal", "principal_id"),
        Index("ix_wsnap_workspace", "workspace_resource_id"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_resource_id: Mapped[str] = mapped_column(String(26), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Content-addressed: the snapshot_id is a ULID, but the content_hash
    # is a SHA-256 of the file list (same files → same hash → idempotent).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # JSON list of {path, sha256, size} — the files in the snapshot.
    files_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    file_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # When the snapshot was captured (UTC).
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
