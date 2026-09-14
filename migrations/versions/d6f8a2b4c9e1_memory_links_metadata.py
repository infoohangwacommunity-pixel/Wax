"""memory relationships + importance/observation time (ADR-0022)

Revision ID: d6f8a2b4c9e1
Revises: c4d6e8f0a2b3
Create Date: 2026-09-14

Mission Phase 3 + §6.3:

- memory_links: typed evidence relationships between memories (supports /
  contradicts / derived_from / related_to), principal-scoped, unique per
  (from, to, kind). Supersession stays a lifecycle column, not a link.
- memory_records.importance: 0.0-1.0 ranking weight (NULL = neutral).
- memory_records.observed_at: when the fact was observed, distinct from
  created_at (late-arriving evidence); NULL = observed at creation.

No destructive change; new columns are nullable, the new table starts
empty. downgrade removes exactly what was added.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f8a2b4c9e1"
down_revision: Union[str, None] = "c4d6e8f0a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_links",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("from_memory_id", sa.String(length=26), nullable=False),
        sa.Column("to_memory_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("principal_id", sa.String(length=26), nullable=False),
        sa.Column("created_by_execution_id", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_memory_id"], ["memory_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_memory_id"], ["memory_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_memory_id", "to_memory_id", "kind", name="uq_memlink_edge"
        ),
    )
    op.create_index("ix_memlinks_from", "memory_links", ["from_memory_id"])
    op.create_index("ix_memlinks_to", "memory_links", ["to_memory_id"])

    op.add_column(
        "memory_records", sa.Column("importance", sa.Float(), nullable=True)
    )
    op.add_column(
        "memory_records",
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_records", "observed_at")
    op.drop_column("memory_records", "importance")
    op.drop_index("ix_memlinks_to", table_name="memory_links")
    op.drop_index("ix_memlinks_from", table_name="memory_links")
    op.drop_table("memory_links")
