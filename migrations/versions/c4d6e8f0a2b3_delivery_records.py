"""durable outbound delivery records (ADR-0021)

Revision ID: c4d6e8f0a2b3
Revises: b9c1d3e5f7a2
Create Date: 2026-09-14

A completed result that could not be delivered becomes recoverable
state, not a graveyard row: delivery_records carries the real outbound
lifecycle (pending → retrying → delivered | failed) that the runtime's
maintenance loop retries (mission §55).

downgrade drops exactly the table that was added.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d6e8f0a2b3"
down_revision: Union[str, None] = "b9c1d3e5f7a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_records",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("principal_id", sa.String(length=26), nullable=False),
        sa.Column("interface_kind", sa.String(length=32), nullable=False),
        sa.Column("recipient_id", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("execution_id", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delivery_status_next_attempt",
        "delivery_records",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_delivery_principal", "delivery_records", ["principal_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_principal", table_name="delivery_records")
    op.drop_index(
        "ix_delivery_status_next_attempt", table_name="delivery_records"
    )
    op.drop_table("delivery_records")
