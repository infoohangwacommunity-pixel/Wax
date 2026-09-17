"""approval primitive: pending_approvals table

Revision ID: f8d3b7a9c1e4
Revises: e5c2a9f47b61
Create Date: 2026-09-14

The human-approval authority boundary (generic, domain-free): when the
agency gate requires explicit human authorization for an AI-requested
action, the runtime creates a pending approval — durable, expiring,
fingerprint-bound (idempotent + replay-protected), auditable, and
decidable only by the human principal it belongs to.

- status lifecycle: pending → approved|denied|expired|cancelled
- request_fingerprint: sha256 of (principal, capability, canonical inputs)
  — identical pending requests deduplicate; approved rows authorize only
  the exact same request.
- consumed_at/consumed_by_execution_id: one-time-use replay guard.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8d3b7a9c1e4"
down_revision: str | None = "e5c2a9f47b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("principal_id", sa.String(length=26), nullable=False),
        sa.Column("requested_by_execution_id", sa.String(length=26), nullable=True),
        sa.Column("capability_name", sa.String(length=128), nullable=False),
        sa.Column("action_kind", sa.String(length=64), nullable=False),
        sa.Column("scope_summary", sa.JSON(), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=26), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_execution_id", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pending_approvals_principal_status",
        "pending_approvals",
        ["principal_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pending_approvals_fingerprint_status",
        "pending_approvals",
        ["request_fingerprint", "status"],
        unique=False,
    )
    op.create_index(
        "ix_pending_approvals_status_expires",
        "pending_approvals",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pending_approvals_status_expires", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_fingerprint_status", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_principal_status", table_name="pending_approvals")
    op.drop_table("pending_approvals")
