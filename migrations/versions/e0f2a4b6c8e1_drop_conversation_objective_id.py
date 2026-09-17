"""Drop objective_id from conversations (objective subsystem removed).

The objective subsystem was removed in the open-world reset. The
conversations.objective_id column is now dead — no code reads or writes
it. This migration drops it.

Revision ID: e0f2a4b6c8e1
Revises: d9e1f3b5c7d9
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e0f2a4b6c8e1"
down_revision: str | None = "d9e1f3b5c7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("conversations", "objective_id")


def downgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("objective_id", sa.String(26), nullable=True),
    )
