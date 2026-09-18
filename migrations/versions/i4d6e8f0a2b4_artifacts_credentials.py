"""artifacts + secure_credentials tables (spec §21, §24)

Revision ID: i4d6e8f0a2b4
Revises: h3c5d7e9f1a3
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i4d6e8f0a2b4"
down_revision: str | None = "h3c5d7e9f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Artifacts table (spec §21)
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("derived_representations", sa.JSON(), nullable=True),
        sa.Column("processing_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("source_message_id", sa.String(256), nullable=True),
        sa.Column("work_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_artifacts_principal", "artifacts", ["principal_id"])
    op.create_index("ix_artifacts_work", "artifacts", ["work_id"])
    op.create_index("ix_artifacts_status", "artifacts", ["processing_status"])

    # Secure credentials table (spec §24)
    op.create_table(
        "secure_credentials",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("service_name", sa.String(128), nullable=False),
        sa.Column("credential_type", sa.String(64), nullable=False),
        sa.Column("encrypted_data", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("identifier", sa.String(512), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_credentials_principal", "secure_credentials", ["principal_id"])
    op.create_index("ix_credentials_service", "secure_credentials", ["service_name"])


def downgrade() -> None:
    op.drop_index("ix_credentials_service", table_name="secure_credentials")
    op.drop_index("ix_credentials_principal", table_name="secure_credentials")
    op.drop_table("secure_credentials")

    op.drop_index("ix_artifacts_status", table_name="artifacts")
    op.drop_index("ix_artifacts_work", table_name="artifacts")
    op.drop_index("ix_artifacts_principal", table_name="artifacts")
    op.drop_table("artifacts")
