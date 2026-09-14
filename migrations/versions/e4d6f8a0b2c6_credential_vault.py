"""phase 7: credential vault — connectors + connections + grants + events

Four new tables for the provider-neutral credential vault (ADR-0040).
The vault stores secrets encrypted at rest; the runtime NEVER exposes
secrets to the model — only opaque connection_ids and grant handles.

Revision ID: e4d6f8a0b2c6
Revises: d3c5e7b9f1a5
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "e4d6f8a0b2c6"
down_revision = "d3c5e7b9f1a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_definitions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("supported_scopes", sa.JSON, nullable=False),
        sa.Column("auth_methods", sa.JSON, nullable=False),
        sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connectors_name", "connector_definitions", ["name"], unique=True)

    op.create_table(
        "principal_connections",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("connector_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("granted_scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("secret_blob", sa.Text, nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pc_principal", "principal_connections", ["principal_id"])
    op.create_index("ix_pc_connector", "principal_connections", ["connector_name"])
    op.create_index("ix_pc_status", "principal_connections", ["status"])

    op.create_table(
        "credential_grants",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column(
            "connection_id",
            sa.String(26),
            sa.ForeignKey("principal_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("objective_id", sa.String(26), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("purpose", sa.String(500), nullable=True),
        sa.Column("handle", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grants_principal", "credential_grants", ["principal_id"])
    op.create_index("ix_grants_connection", "credential_grants", ["connection_id"])
    op.create_index(
        "ix_grants_status_expires", "credential_grants", ["status", "expires_at"]
    )

    op.create_table(
        "credential_events",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=True),
        sa.Column("grant_id", sa.String(26), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("connector_name", sa.String(64), nullable=True),
        sa.Column("scopes", sa.JSON, nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cred_events_principal", "credential_events", ["principal_id"])
    op.create_index("ix_cred_events_connection", "credential_events", ["connection_id"])


def downgrade() -> None:
    op.drop_table("credential_events")
    op.drop_table("credential_grants")
    op.drop_table("principal_connections")
    op.drop_table("connector_definitions")
