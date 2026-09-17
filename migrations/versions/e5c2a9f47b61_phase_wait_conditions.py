"""phase: wake conditions (durable waiting beyond time) + runtime signal ledger

Revision ID: e5c2a9f47b61
Revises: c3a95f1e8b21
Create Date: 2026-09-13

Wake conditions extend work_items from time-only waiting to condition-based
waiting (ADR-0011):
- wake_kind: 'time' (existing behavior, default for all pre-existing rows)
  or 'event' (wake when a named runtime signal is emitted after the item's
  watermark).
- wake_event / wake_watermark: the condition and its no-retroactive-wake
  watermark (only for event items).
- expires_at: optional deadline for the WAIT itself; an unmet condition at
  the deadline dies honestly instead of waiting forever.

runtime_signals is the runtime's append-only event ledger (interface
messages processed, work terminal states, gated AI emissions).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c2a9f47b61"
down_revision: str | None = "c3a95f1e8b21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_items",
        sa.Column("wake_kind", sa.String(length=16), nullable=False, server_default="time"),
    )
    op.add_column(
        "work_items",
        sa.Column("wake_event", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "work_items",
        sa.Column("wake_watermark", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "work_items",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_work_items_status_expires",
        "work_items",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "runtime_signals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emitted_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_signals_name_time",
        "runtime_signals",
        ["name", "emitted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_signals_name_time", table_name="runtime_signals")
    op.drop_table("runtime_signals")
    op.drop_index("ix_work_items_status_expires", table_name="work_items")
    op.drop_column("work_items", "expires_at")
    op.drop_column("work_items", "wake_watermark")
    op.drop_column("work_items", "wake_event")
    op.drop_column("work_items", "wake_kind")
