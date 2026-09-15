"""Persistence for environment leases (ADR-0038, Phase 5).

An environment lease is a bounded, owned record of a provisioned
environment. The intelligence sees only the opaque `environment_id`;
the host-level details (filesystem paths, isolation boundaries,
credential handles) live in the `plan_json` column as opaque metadata
the runtime reads, never the model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class EnvironmentLeaseRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A provisioned environment with owner + TTL + state."""

    __tablename__ = "environment_leases"
    __table_args__ = (
        Index("ix_env_leases_principal", "principal_id"),
        Index("ix_env_leases_status_expires", "status", "expires_at"),
        Index("ix_env_leases_execution", "execution_id"),
    )

    # Who asked for this environment and under which execution.
    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The intelligence's purpose (bounded, validated — never secrets).
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)

    # Lifecycle state (EnvironmentState enum values).
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planned", server_default="planned"
    )

    # The full plan (opaque to the model; the runtime reads this).
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # When the lease expires (TTL). NULL = promoted (intentional permanent).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional: which workspace resource this environment is bound to
    # (FK to provisioning_resources.id). NULL = no workspace required.
    workspace_resource_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Optional: error message if status == "failed".
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EnvironmentCapabilityBindingRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A binding: capability X is bound to environment Y.

    Capabilities that require an environment (terminal.execute,
    code.run) check this table before running. A binding is owned by
    the environment's lease; releasing the environment releases all
    its bindings.
    """

    __tablename__ = "environment_capability_bindings"
    __table_args__ = (
        Index("ix_env_bindings_env", "environment_id"),
        Index("ix_env_bindings_capability", "capability_name"),
    )

    environment_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("environment_leases.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Opaque handle the intelligence uses to invoke the capability
    # against this environment. The handle is NOT a secret; it's a
    # scoped reference.
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
