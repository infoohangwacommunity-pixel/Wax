"""Phase B-J: world state, conversation ledger, memory 2.0, work 2.0

Adds:
- contexts table (durable threads of meaning — Phase B)
- conversation_messages table (the missing conversation ledger — Part 2/4)
- memory_records columns: context_id, valid_from, valid_until,
  last_confirmed_at, evidence (Phase E — temporal truth + evidence)
- work_items columns: context_id, parent_work_id (Phase F — context-bound work)

Revision ID: f1a3b5c7d9e1
Revises: e0f2a4b6c8e1
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a3b5c7d9e1"
down_revision: str | None = "e0f2a4b6c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Phase B: contexts table
    op.create_table(
        "contexts",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column(
            "principal_id",
            sa.String(26),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("workspace_path", sa.String(512), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "parent_context_id",
            sa.String(26),
            sa.ForeignKey("contexts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_contexts_principal_status", "contexts", ["principal_id", "status"])
    op.create_index("ix_contexts_principal_updated", "contexts", ["principal_id", "updated_at"])

    # Part 2/4: conversation_messages table (the missing conversation ledger)
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column(
            "principal_id",
            sa.String(26),
            sa.ForeignKey("principals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(26),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "context_id",
            sa.String(26),
            sa.ForeignKey("contexts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String(10000), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("interface_kind", sa.String(32), nullable=True),
        sa.Column("interface_message_id", sa.String(256), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_conv_messages_principal_time", "conversation_messages", ["principal_id", "created_at"]
    )
    op.create_index("ix_conv_messages_conversation", "conversation_messages", ["conversation_id"])
    op.create_index("ix_conv_messages_context", "conversation_messages", ["context_id"])

    # Phase E: memory_records additions (temporal truth + evidence + context)
    # Use batch mode for SQLite compatibility (add_column with FK doesn't work on SQLite)
    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.add_column(sa.Column("context_id", sa.String(26), nullable=True))
        batch_op.add_column(sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("evidence", sa.JSON(), nullable=True))

    # Phase F: work_items additions (context-bound work + parent chains)
    with op.batch_alter_table("work_items") as batch_op:
        batch_op.add_column(sa.Column("context_id", sa.String(26), nullable=True))
        batch_op.add_column(sa.Column("parent_work_id", sa.String(26), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("work_items") as batch_op:
        batch_op.drop_column("parent_work_id")
        batch_op.drop_column("context_id")

    with op.batch_alter_table("memory_records") as batch_op:
        batch_op.drop_column("evidence")
        batch_op.drop_column("last_confirmed_at")
        batch_op.drop_column("valid_until")
        batch_op.drop_column("valid_from")
        batch_op.drop_column("context_id")

    with op.batch_alter_table("conversation_messages") as batch_op:
        batch_op.drop_index("ix_conv_messages_context")
        batch_op.drop_index("ix_conv_messages_conversation")
        batch_op.drop_index("ix_conv_messages_principal_time")
    op.drop_table("conversation_messages")

    with op.batch_alter_table("contexts") as batch_op:
        batch_op.drop_index("ix_contexts_principal_updated")
        batch_op.drop_index("ix_contexts_principal_status")
    op.drop_table("contexts")
