"""interaction_sessions table for DB-backed web pages (Bug 3 fix)

Revision ID: g2b4c6d8e0f2
Revises: f1a3b5c7d9e1
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g2b4c6d8e0f2"
down_revision: str | None = "f1a3b5c7d9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interaction_sessions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("secret_token", sa.String(64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("context_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("html_content", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_submissions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("submission_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("wake_event", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_interaction_sessions_token", "interaction_sessions", ["secret_token"])
    op.create_index("ix_interaction_sessions_principal", "interaction_sessions", ["principal_id"])


def downgrade() -> None:
    op.drop_index("ix_interaction_sessions_principal", table_name="interaction_sessions")
    op.drop_index("ix_interaction_sessions_token", table_name="interaction_sessions")
    op.drop_table("interaction_sessions")
