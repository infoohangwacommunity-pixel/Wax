"""approval creation idempotency — partial unique index (CV-11 fix)

Revision ID: f2b4d6a8c0e2
Revises: e1a3c5e7b9d2
Create Date: 2026-09-14

The approval primitive's `create_or_get_pending` was a find-then-insert:
two concurrent gate passes (live bridge + durable-work handler, or two
replicas) could both observe "no pending row" and both insert, creating
two pending approvals — and two human notifications — for one request.

This migration makes idempotent creation a DATABASE property: at most
ONE pending approval may exist per (principal_id, request_fingerprint).
Historical terminal rows (approved/consumed/denied/expired/cancelled)
keep the same fingerprint freely — hence a PARTIAL unique index scoped
to status = 'pending'.

downgrade drops exactly the index that was added.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision: str = "f2b4d6a8c0e2"
down_revision: Union[str, None] = "e1a3c5e7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_pending_approvals_principal_fp_pending",
        "pending_approvals",
        ["principal_id", "request_fingerprint"],
        unique=True,
        postgresql_where=sa_text("status = 'pending'"),
        sqlite_where=sa_text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pending_approvals_principal_fp_pending",
        table_name="pending_approvals",
    )
