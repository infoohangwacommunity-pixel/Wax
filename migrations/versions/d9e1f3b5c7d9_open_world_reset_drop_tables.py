"""open-world reset: drop tables for removed subsystems

Removes tables that belonged to subsystems cut during the open-world
architecture reset:
- roles, principal_roles (authority — §46)
- pending_approvals (approval primitive — §47)
- capability_invocations (capability registry — §48, §49)
- artifacts, workspace_snapshots (artifact/workspace machinery — §56, §57)
- provisioned_resources (provisioning — §54)
- objectives, objective_executions (objective subsystem — §58)

All of these tables' only consumers were deleted during the reset.
Historical migrations that created them remain in place — fresh installs
still run them and then this migration drops the tables, so the migration
chain stays reproducible.

Revision ID: d9e1f3b5c7d9
Revises: c8d0e2f4a6b8
Create Date: 2026-09-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e1f3b5c7d9"
down_revision: str | None = "c8d0e2f4a6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    """Dialect guard. SQLite (test suite / local dev) does not support
    `ALTER TABLE ... DROP CONSTRAINT IF EXISTS` — SQLite has no named
    constraints, only inline `REFERENCES` clauses that are advisory by
    default. The DROP CONSTRAINT statements are PG-only.
    """
    return op.get_bind().dialect.name == "postgresql"


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    """Drop a column if it exists, portable across PostgreSQL and SQLite.

    PostgreSQL supports `ALTER TABLE ... DROP COLUMN IF EXISTS` directly
    and ENFORCES foreign-key constraints, so dead FK columns must be
    dropped before the parent table can be dropped.

    SQLite (>= 3.35) supports `ALTER TABLE ... DROP COLUMN` but:
      - does NOT support the `IF EXISTS` clause (we introspect first)
      - refuses to drop a column referenced by an inline `REFERENCES`
        clause in the table's stored schema text, even with
        `PRAGMA foreign_keys=OFF`

    SQLite's foreign-key constraints are advisory by default (the
    runtime does not enable `PRAGMA foreign_keys=ON`), so a dead
    `objective_id` column pointing at a dropped `objectives` table is
    harmless on SQLite — the runtime never reads or writes it. We
    therefore skip the column drop entirely on SQLite and only drop
    columns on PostgreSQL, where FK enforcement makes it mandatory.
    """
    if not _is_postgres():
        # SQLite: dead FK columns are harmless (advisory FKs, never read).
        # Skipping avoids the "unknown column in foreign key definition"
        # error that SQLite raises when DROP COLUMN trips on the inline
        # REFERENCES clause stored in sqlite_schema.
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name not in existing_cols:
        return

    op.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {column_name}")


def upgrade() -> None:
    # PostgreSQL has FK constraints that prevent dropping parent tables
    # while children still reference them. We must drop ALL FK constraints
    # and columns that reference the tables we're about to drop, THEN drop
    # the tables.
    #
    # SQLite does not support `DROP CONSTRAINT IF EXISTS` (and its FK
    # constraints are advisory only), so we skip the constraint drops
    # entirely on SQLite and go straight to dropping columns + tables.
    # PostgreSQL needs the explicit constraint drops first.

    if _is_postgres():
        # 1. Drop the FK constraint: memory_records.objective_id -> objectives.id
        op.execute("ALTER TABLE memory_records DROP CONSTRAINT IF EXISTS fk_memory_objective")
        op.execute("DROP INDEX IF EXISTS ix_memory_objective")
        # 2. Drop the FK constraint: conversations.objective_id -> objectives.id
        op.execute(
            "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_objective_id_fkey"
        )
        # 3. Drop the FK constraint: executions.objective_id -> objectives.id
        op.execute("ALTER TABLE executions DROP CONSTRAINT IF EXISTS executions_objective_id_fkey")

    # Drop the dead objective_id columns on both backends. Use introspection
    # because SQLite doesn't support `DROP COLUMN IF EXISTS` (PG does).
    _drop_column_if_exists("memory_records", "objective_id")
    _drop_column_if_exists("conversations", "objective_id")

    # 4. Now drop tables — FK constraints (if any) are gone, so this will succeed.
    op.execute("DROP TABLE IF EXISTS objective_executions")
    op.execute("DROP TABLE IF EXISTS objectives")
    op.execute("DROP TABLE IF EXISTS workspace_snapshots")
    op.execute("DROP TABLE IF EXISTS artifacts")
    op.execute("DROP TABLE IF EXISTS provisioned_resources")
    op.execute("DROP TABLE IF EXISTS capability_invocations")
    op.execute("DROP TABLE IF EXISTS pending_approvals")
    op.execute("DROP TABLE IF EXISTS principal_roles")
    op.execute("DROP TABLE IF EXISTS roles")


def downgrade() -> None:
    # Recreate the tables with their original shape. The runtime no longer
    # reads/writes them, so a downgrade without restoring the code that used
    # them is incomplete.
    op.create_table(
        "roles",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "principal_roles",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("role_id", sa.String(26), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("principal_id", "role_id", name="uq_principal_roles_principal_role"),
    )
    op.create_table(
        "pending_approvals",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("capability_name", sa.String(128), nullable=False),
        sa.Column("action_kind", sa.String(64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("requested_by_execution_id", sa.String(26), nullable=True),
        sa.Column("decided_by", sa.String(26), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "capability_invocations",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("capability_name", sa.String(128), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("idempotency_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_id", sa.String(26), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "provisioned_resources",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("provisioned_resource_id", sa.String(26), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "workspace_snapshots",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("workspace_id", sa.String(26), nullable=False),
        sa.Column("files_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "objectives",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("principal_id", sa.String(26), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("conversation_id", sa.String(26), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "objective_executions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("objective_id", sa.String(26), nullable=False),
        sa.Column("execution_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
    )
