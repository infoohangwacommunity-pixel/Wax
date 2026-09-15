"""Persistence for the authority broker (ADR-0048, P0-Authority).

Three tables:
- human_handoffs: requests for a human to complete an external step
- authority_materials: encrypted authority material (AES-GCM envelope)
- authority_grants: opaque permission to use authority within a scope

The raw secret NEVER enters model context. It is encrypted immediately
upon submission and stored only in authority_materials.ciphertext.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class HumanHandoffRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A request for a human to complete an external step."""

    __tablename__ = "human_handoffs"
    __table_args__ = (
        Index("ix_handoffs_principal", "principal_id"),
        Index("ix_handoffs_status", "status"),
        Index("ix_handoffs_objective", "objective_id"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    origin_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requested_actions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )

    challenge_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at_col: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at_col: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    completion_evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_reason_safe: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AuthorityMaterialRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """Encrypted authority material (AES-GCM envelope)."""

    __tablename__ = "authority_materials"
    __table_args__ = (
        Index("ix_auth_materials_principal", "principal_id"),
        Index("ix_auth_materials_status", "status"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    created_by_handoff_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("human_handoffs.id", ondelete="SET NULL"), nullable=True
    )
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    origin_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    material_type: Mapped[str] = mapped_column(String(64), nullable=False)

    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_data_key: Mapped[str] = mapped_column(Text, nullable=False)
    wrapped_key_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unverified", server_default="unverified"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthorityGrantRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """Opaque permission to use authority within a specific scope."""

    __tablename__ = "authority_grants"
    __table_args__ = (
        Index("ix_auth_grants_principal", "principal_id"),
        Index("ix_auth_grants_status", "status"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    objective_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    environment_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    authority_material_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("authority_materials.id", ondelete="CASCADE"), nullable=False
    )

    handle: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    allowed_actions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    effect_class: Mapped[str] = mapped_column(String(32), nullable=False, default="read_only")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
