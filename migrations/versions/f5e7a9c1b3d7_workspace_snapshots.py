"""phase 9: workspace snapshots table

Adds the workspace_snapshots table for ADR-0042 (Phase 9). A snapshot
is a content-addressed capture of a workspace's file state.

Revision ID: f5e7a9c1b3d7
Revises: e4d6f8a0b2c6
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "f5e7a9c1b3d7"
down_revision = "e4d6f8a0b2c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_snapshots",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("workspace_resource_id", sa.String(26), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("files_json", sa.JSON, nullable=False),
        sa.Column("file_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wsnap_principal", "workspace_snapshots", ["principal_id"])
    op.create_index("ix_wsnap_workspace", "workspace_snapshots", ["workspace_resource_id"])


def downgrade() -> None:
    op.drop_table("workspace_snapshots")
