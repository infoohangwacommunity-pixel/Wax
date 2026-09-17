"""objective lifecycle: execution history table (ADR-0020)

Revision ID: b9c1d3e5f7a2
Revises: d9e4f2a8b1c7
Create Date: 2026-09-14

An objective is NOT one execution (mission §16): it may produce retries,
resumptions, and parallel work. This migration adds the append-only
participation history:

- objective_executions: one row per (objective, execution) participation,
  with kind (bridge | work), started_at, ended_at (NULL = open), and the
  honest outcome (succeeded | failed | cancelled | superseded).

The objectives.execution_id column remains the CURRENT-execution pointer.
No destructive change: pre-existing rows are untouched, and the table
starts empty (history begins at deployment time).

downgrade drops exactly the table that was added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c1d3e5f7a2"
down_revision: str | None = "d9e4f2a8b1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "objective_executions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column(
            "objective_id",
            sa.String(length=26),
            nullable=False,
        ),
        sa.Column("execution_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["objective_id"],
            ["objectives.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_objexec_objective", "objective_executions", ["objective_id"])
    op.create_index("ix_objexec_execution", "objective_executions", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_objexec_execution", table_name="objective_executions")
    op.drop_index("ix_objexec_objective", table_name="objective_executions")
    op.drop_table("objective_executions")
