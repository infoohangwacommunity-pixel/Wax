"""Generic artifact model — universal media/file representation (spec §21).

Artifacts are how media enters the intelligence's environment. Instead
of a hardcoded MIME-specific decision tree, the runtime stores a
generic artifact record and the intelligence decides how to work with
it through the terminal.

Artifact
 ├── id
 ├── principal_id
 ├── source (whatsapp, upload, terminal, url, etc.)
 ├── media_type (text, image, audio, video, document, archive, etc.)
 ├── mime_type (e.g. image/png, application/pdf)
 ├── filename
 ├── size_bytes
 ├── storage_ref (local path or remote URL)
 ├── metadata (JSON — dimensions, duration, pages, etc.)
 ├── derived_representations (JSON — extracted text, thumbnails, etc.)
 ├── processing_status (pending, processed, failed)
 ├── source_message_id (which inbound message produced it)
 ├── work_id (which work item it belongs to)
 ├── created_at
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class ArtifactRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A universal artifact — media/file/document in the runtime."""

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_principal", "principal_id"),
        Index("ix_artifacts_work", "work_id"),
        Index("ix_artifacts_status", "processing_status"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Where the artifact came from
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    # whatsapp, upload, terminal, url, internal

    # What kind of thing it is
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # text, image, audio, video, document, archive, code, data, other
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Where the content is stored
    storage_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local file path, remote URL, or content-addressed reference

    # Flexible metadata (dimensions, duration, pages, hash, etc.)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Derived representations (extracted text, thumbnails, transcriptions)
    derived_representations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Processing status
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # pending, processed, failed

    # Associations
    source_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    work_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Content hash for deduplication
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
