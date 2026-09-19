"""process_registry table for durable detached process lifecycle (spec §5)

Revision ID: h3c5d7e9f1a3
Revises: g2b4c6d8e0f2
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h3c5d7e9f1a3"
down_revision: str | None = "g2b4c6d8e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "process_registry",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("work_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("termination_reason", sa.String(255), nullable=True),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_processes_principal", "process_registry", ["principal_id"])
    op.create_index("ix_processes_work", "process_registry", ["work_id"])
    op.create_index("ix_processes_status", "process_registry", ["status"])


def downgrade() -> None:
    op.drop_index("ix_processes_status", table_name="process_registry")
    op.drop_index("ix_processes_work", table_name="process_registry")
    op.drop_index("ix_processes_principal", table_name="process_registry")
    op.drop_table("process_registry")
