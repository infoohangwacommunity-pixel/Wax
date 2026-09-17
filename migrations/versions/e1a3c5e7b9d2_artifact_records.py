"""first-class artifact records (ADR-0023, mission §56/§100)

Revision ID: e1a3c5e7b9d2
Revises: d6f8a2b4c9e1
Create Date: 2026-09-14

Artifacts become queryable runtime state with owner, integrity, and
lifecycle — not arbitrary filesystem paths. Records are created at the
acquisition boundary (workspace.acquire), where the sha256 is computed
anyway. Artifact state stays DISTINCT from objective / execution /
memory / event state (mission §100).

downgrade drops exactly the table that was added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a3c5e7b9d2"
down_revision: str | None = "d6f8a2b4c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("principal_id", sa.String(length=26), nullable=False),
        sa.Column("workspace_resource_id", sa.String(length=26), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("execution_id", sa.String(length=26), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_principal", "artifacts", ["principal_id"])
    op.create_index("ix_artifacts_workspace", "artifacts", ["workspace_resource_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_workspace", table_name="artifacts")
    op.drop_index("ix_artifacts_principal", table_name="artifacts")
    op.drop_table("artifacts")
