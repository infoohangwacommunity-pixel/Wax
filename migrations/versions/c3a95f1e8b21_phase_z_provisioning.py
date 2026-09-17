"""phase_z_provisioning

Revision ID: c3a95f1e8b21
Revises: b7f21c9d4e02
Create Date: 2026-09-13

Creates the provisioned_resources table for dynamic provisioning
(Phase S): temporary runtime resources with owner, TTL, limits, cleanup,
and audit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a95f1e8b21"
down_revision: str | None = "b7f21c9d4e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provisioned_resources",
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("principal_id", sa.String(length=26), nullable=True),
        sa.Column("execution_id", sa.String(length=26), nullable=True),
        sa.Column("uri", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("limits", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provisioned_status_expires",
        "provisioned_resources",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provisioned_principal"),
        "provisioned_resources",
        ["principal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_provisioned_principal"), table_name="provisioned_resources")
    op.drop_index("ix_provisioned_status_expires", table_name="provisioned_resources")
    op.drop_table("provisioned_resources")
