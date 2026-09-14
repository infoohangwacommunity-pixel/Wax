"""phase_y_durable_work

Revision ID: b7f21c9d4e02
Revises: a06446a7edd3
Create Date: 2026-09-13

Creates the work_items table for the durable work runtime (Phase R / V):
work that survives process restarts, crashes, and long waits, with wake
conditions, leases, retries, and terminal dead states.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7f21c9d4e02'
down_revision: Union[str, None] = 'a06446a7edd3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'work_items',
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('principal_id', sa.String(length=26), nullable=True),
        sa.Column('execution_id', sa.String(length=26), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('wake_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('lease_owner', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('id', sa.String(length=26), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_work_items_status_wake', 'work_items', ['status', 'wake_at'], unique=False
    )
    op.create_index(
        op.f('ix_work_items_principal'), 'work_items', ['principal_id'], unique=False
    )
    op.create_index(
        op.f('ix_work_items_execution'), 'work_items', ['execution_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_work_items_execution'), table_name='work_items')
    op.drop_index(op.f('ix_work_items_principal'), table_name='work_items')
    op.drop_index('ix_work_items_status_wake', table_name='work_items')
    op.drop_table('work_items')
