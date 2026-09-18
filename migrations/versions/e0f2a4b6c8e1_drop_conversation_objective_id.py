"""Drop objective_id from conversations (objective subsystem removed).

The objective subsystem was removed in the open-world reset. The
conversations.objective_id column is now dead — no code reads or writes
it. This migration drops it.

Note: the prior migration ``d9e1f3b5c7d9`` ALSO drops
``conversations.objective_id`` via a portable helper. So on a fresh
install, by the time this migration runs, the column is already gone.
On an existing database that ran ``d9e1f3b5c7d9`` BEFORE that helper
was added (i.e. the column survived), this migration is the one that
drops it. Either way, we must use ``IF EXISTS`` to avoid a crash on
one of the two paths.

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


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # The column MAY have already been dropped by d9e1f3b5c7d9's
    # _drop_column_if_exists helper. Use IF EXISTS on PG, and
    # introspect on SQLite (which doesn't support IF EXISTS on
    # DROP COLUMN).
    if _is_postgres():
        op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS objective_id")
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        existing = {c["name"] for c in inspector.get_columns("conversations")}
        if "objective_id" in existing:
            op.drop_column("conversations", "objective_id")


def downgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("objective_id", sa.String(26), nullable=True),
    )
