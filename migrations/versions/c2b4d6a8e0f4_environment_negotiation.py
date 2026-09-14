"""phase 5: environment negotiation — leases + capability bindings

Adds two tables:

- environment_leases: a provisioned environment with owner + TTL + state.
  The intelligence sees only the opaque environment_id; the host-level
  details live in plan_json as opaque metadata the runtime reads.

- environment_capability_bindings: which capabilities are bound to
  which environment. Capabilities that require an environment
  (terminal.execute, code.run) check this table before running.

Uses batch_alter_table for SQLite compatibility where applicable
(not needed for fresh table creation, but kept for consistency).

Revision ID: c2b4d6a8e0f4
Revises: f1b3d5a7c9e2
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "c2b4d6a8e0f4"
down_revision = "f1b3d5a7c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_leases",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column("plan_json", sa.JSON, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_resource_id", sa.String(26), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_env_leases_principal",
        "environment_leases",
        ["principal_id"],
    )
    op.create_index(
        "ix_env_leases_status_expires",
        "environment_leases",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_env_leases_execution",
        "environment_leases",
        ["execution_id"],
    )

    op.create_table(
        "environment_capability_bindings",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column(
            "environment_id",
            sa.String(26),
            sa.ForeignKey("environment_leases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability_name", sa.String(128), nullable=False),
        sa.Column("handle", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_env_bindings_env",
        "environment_capability_bindings",
        ["environment_id"],
    )
    op.create_index(
        "ix_env_bindings_capability",
        "environment_capability_bindings",
        ["capability_name"],
    )


def downgrade() -> None:
    op.drop_table("environment_capability_bindings")
    op.drop_table("environment_leases")
