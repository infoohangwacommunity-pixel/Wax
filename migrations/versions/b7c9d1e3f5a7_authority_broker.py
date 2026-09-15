"""phase: authority broker — human handoffs + authority materials + grants

Three new tables for the authority broker (ADR-0048). The raw secret
NEVER enters model context — it is encrypted immediately (AES-GCM)
and stored only in authority_materials.ciphertext.

Revision ID: b7c9d1e3f5a7
Revises: a6f8b0c2d4e8
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa

revision = "b7c9d1e3f5a7"
down_revision = "a6f8b0c2d4e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_handoffs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("objective_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("origin_reference", sa.String(512), nullable=True),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("requested_actions_json", sa.JSON, nullable=True),
        sa.Column("instructions_text", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("challenge_hash", sa.String(128), nullable=True),
        sa.Column("challenge_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at_col", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at_col", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_evidence_json", sa.JSON, nullable=True),
        sa.Column("failure_reason_safe", sa.String(500), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_handoffs_principal", "human_handoffs", ["principal_id"])
    op.create_index("ix_handoffs_status", "human_handoffs", ["status"])
    op.create_index("ix_handoffs_objective", "human_handoffs", ["objective_id"])

    op.create_table(
        "authority_materials",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column(
            "created_by_handoff_id",
            sa.String(26),
            sa.ForeignKey("human_handoffs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("objective_id", sa.String(26), nullable=True),
        sa.Column("origin_reference", sa.String(512), nullable=True),
        sa.Column("material_type", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("ciphertext_nonce", sa.String(64), nullable=False),
        sa.Column("wrapped_data_key", sa.Text, nullable=False),
        sa.Column("wrapped_key_nonce", sa.String(64), nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="unverified"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_materials_principal", "authority_materials", ["principal_id"])
    op.create_index("ix_auth_materials_status", "authority_materials", ["status"])

    op.create_table(
        "authority_grants",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("objective_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("environment_id", sa.String(26), nullable=True),
        sa.Column(
            "authority_material_id",
            sa.String(26),
            sa.ForeignKey("authority_materials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("handle", sa.String(64), nullable=False, unique=True),
        sa.Column("allowed_actions_json", sa.JSON, nullable=True),
        sa.Column("effect_class", sa.String(32), nullable=False, server_default="read_only"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_grants_principal", "authority_grants", ["principal_id"])
    op.create_index("ix_auth_grants_status", "authority_grants", ["status"])


def downgrade() -> None:
    op.drop_table("authority_grants")
    op.drop_table("authority_materials")
    op.drop_table("human_handoffs")
