"""cleanup: drop tables for removed subsystems (directive §46)

Removes tables that belonged to subsystems cut during the cleanup phase:
- human_handoffs, authority_materials, authority_grants (authority broker — §7)
- environment_leases, environment_capability_bindings (environment negotiation — §15)
- terminal_sessions (terminal runtime — §15)
- connector_definitions, principal_connections, credential_grants,
  credential_events (credential vault + connectors — §33, §34)
- rate_limit_counters, cost_tracking (multi-instance shared_state — §12, UNWIRED)
- dead_letter_entries (dead-letter graveyard — §13, no production reader)

All of these tables' only consumers were deleted during the cleanup.
Historical migrations that created them remain in place — fresh installs
still run them and then this migration drops the tables, so the migration
chain stays reproducible. Existing deployments get the drop applied here.

Revision ID: c8d0e2f4a6b8
Revises: b7c9d1e3f5a7
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d0e2f4a6b8"
down_revision: str | None = "b7c9d1e3f5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop FKs first if any reference these tables from elsewhere.
    # (Verified: no surviving table references any of these — all FK
    # consumers were also removed during cleanup.)
    op.drop_table("human_handoffs")
    op.drop_table("authority_materials")
    op.drop_table("authority_grants")
    op.drop_table("environment_leases")
    op.drop_table("environment_capability_bindings")
    op.drop_table("terminal_sessions")
    op.drop_table("connector_definitions")
    op.drop_table("principal_connections")
    op.drop_table("credential_grants")
    op.drop_table("credential_events")
    op.drop_table("rate_limit_counters")
    op.drop_table("cost_tracking")
    op.drop_table("dead_letter_entries")


def downgrade() -> None:
    # Recreate the tables with the same shape as their original migrations.
    # This is a best-effort restore — the runtime no longer reads/writes them,
    # so a downgrade without restoring the code that used them is incomplete.

    op.create_table(
        "dead_letter_entries",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("error_type", sa.String(128), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("reprocessed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_dead_letter_kind", "dead_letter_entries", ["kind"])
    op.create_index("ix_dead_letter_principal", "dead_letter_entries", ["principal_id"])
    op.create_index("ix_dead_letter_created", "dead_letter_entries", ["created_at"])

    op.create_table(
        "cost_tracking",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_usd_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "credential_events",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "credential_grants",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("handle", sa.String(255), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "principal_connections",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("connector_name", sa.String(64), nullable=False),
        sa.Column("identifier", sa.String(255), nullable=False),
        sa.Column("auth_method", sa.String(32), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "principal_id", "connector_name", name="uq_principal_connections_principal_connector"
        ),
    )
    op.create_table(
        "connector_definitions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("supported_scopes", sa.JSON(), nullable=False),
        sa.Column("auth_methods", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "terminal_sessions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "environment_capability_bindings",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("lease_id", sa.String(26), nullable=False),
        sa.Column("capability_name", sa.String(128), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "environment_leases",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("environment_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "authority_grants",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("handoff_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "authority_materials",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("grant_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "human_handoffs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_actions", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
