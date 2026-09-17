"""phase 6: terminal runtime — persistent sessions

Adds the terminal_sessions table. A terminal session is a governed
shell bound to an environment lease (ADR-0038). The session's
working_dir is workspace-relative; env_vars are bounded + validated.

Revision ID: d3c5e7b9f1a5
Revises: c2b4d6a8e0f4
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "d3c5e7b9f1a5"
down_revision = "c2b4d6a8e0f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "terminal_sessions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column(
            "environment_id",
            sa.String(26),
            sa.ForeignKey("environment_leases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("working_dir", sa.String(512), nullable=False, server_default="."),
        sa.Column("env_vars", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_command_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_exit_code", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_terminal_principal", "terminal_sessions", ["principal_id"])
    op.create_index("ix_terminal_env", "terminal_sessions", ["environment_id"])
    op.create_index("ix_terminal_status_expires", "terminal_sessions", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_table("terminal_sessions")
