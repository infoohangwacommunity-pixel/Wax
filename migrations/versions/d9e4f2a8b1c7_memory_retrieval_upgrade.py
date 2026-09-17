"""memory retrieval upgrade: PG tsvector search column (ADR-0019)

Revision ID: d9e4f2a8b1c7
Revises: f8d3b7a9c1e4
Create Date: 2026-09-14

Adds the Postgres-side full-text search infrastructure for memory
retrieval (ADR-0019):

- memory_records.search_vector: a GENERATED ALWAYS tsvector over the
  summary + serialized content — maintained by the DATABASE on every
  write, never by the application. The ORM metadata deliberately does
  NOT declare it (it is a derived index column, not application state;
  SQLAlchemy create_all on SQLite must not render a tsvector).
- a GIN index over search_vector for ranked candidate recall.

Dialect-guarded: on PostgreSQL this creates both; on SQLite (tests,
local dev) the migration is an honest no-op — the portable BM25 recall
path in MemoryRepository serves those deployments (ADR-0019 documents
the split).

downgrade removes exactly what was added.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e4f2a8b1c7"
down_revision: str | None = "f8d3b7a9c1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_memory_records_search_vector"
_COLUMN_DDL = (
    "ALTER TABLE memory_records ADD COLUMN search_vector tsvector "
    "GENERATED ALWAYS AS ( "
    "to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(content::text, '')) "
    ") STORED"
)


def _is_postgres_for(dialect) -> bool:
    """Dialect guard, injectable for tests."""
    return dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres_for(op.get_bind().dialect):
        # SQLite (tests / local dev): no tsvector support; the portable
        # recall path in MemoryRepository covers these deployments.
        return
    op.execute(_COLUMN_DDL)
    op.execute(f"CREATE INDEX {_INDEX_NAME} ON memory_records USING GIN (search_vector)")


def downgrade() -> None:
    if not _is_postgres_for(op.get_bind().dialect):
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    op.execute("ALTER TABLE memory_records DROP COLUMN IF EXISTS search_vector")
