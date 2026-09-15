"""phase 11: multi-instance shared state — rate limits + cost tracking

Two new tables for multi-instance correctness:
- rate_limit_counters: per-principal per-minute counters (shared across
  processes via atomic DB updates)
- cost_tracking: per-principal per-day token/message totals

These replace the process-local in-memory counters that would double
the effective rate limit / cost cap in a two-process deployment.

Revision ID: a6f8b0c2d4e8
Revises: f5e7a9c1b3d7
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "a6f8b0c2d4e8"
down_revision = "f5e7a9c1b3d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("window_seconds", sa.Integer, nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_rlc_principal_window",
        "rate_limit_counters",
        ["principal_id", "window_start"],
        unique=True,
    )

    op.create_table(
        "cost_tracking",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_messages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_cost_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_cost_principal_day",
        "cost_tracking",
        ["principal_id", "day"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("cost_tracking")
    op.drop_table("rate_limit_counters")
