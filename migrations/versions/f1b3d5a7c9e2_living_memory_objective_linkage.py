"""phase 3: living memory — objective linkage + consolidation provenance

Adds two columns to memory_records:

- objective_id: optional FK to objectives.id. Links a memory to the
  objective it supports/evidences. This is the "objective linkage"
  the directive (Phase 3) requires: "what objective I support".

- consolidation_sources: JSON list of memory IDs that were consolidated
  into this one. When memory.consolidate creates a new record from
  multiple sources, this column records the source IDs. The old
  memory.consolidate already supersedes the sources (writes their
  superseded_by); this column is the FORWARD provenance chain (the
  new record's "where I came from"), complementing the existing
  backward chain (the old records' "what replaced me").

Uses batch_alter_table because SQLite does not support ALTER TABLE
ADD CONSTRAINT directly. Batch mode performs the alter via copy-and-
move, which works on both SQLite and PostgreSQL.

Revision ID: f1b3d5a7c9e2
Revises: a8c2e6f0b4d6
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1b3d5a7c9e2"
down_revision = "a8c2e6f0b4d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use batch_alter_table for SQLite compatibility (ADD CONSTRAINT
    # is not supported by SQLite's ALTER TABLE; batch mode does
    # copy-and-move). On PostgreSQL, batch mode is a no-op wrapper.
    with op.batch_alter_table("memory_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "objective_id",
                sa.String(26),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "consolidation_sources",
                sa.JSON,
                nullable=True,
            )
        )
        # ADR-0036: foreign key to objectives.id with ON DELETE SET NULL
        # — if the objective is deleted, the memory stays but is no
        # longer linked (the objective_id column becomes NULL).
        batch_op.create_foreign_key(
            "fk_memory_objective",
            "objectives",
            ["objective_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_memory_objective",
            ["objective_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("memory_records", schema=None) as batch_op:
        batch_op.drop_index("ix_memory_objective")
        batch_op.drop_constraint("fk_memory_objective", type_="foreignkey")
        batch_op.drop_column("consolidation_sources")
        batch_op.drop_column("objective_id")
