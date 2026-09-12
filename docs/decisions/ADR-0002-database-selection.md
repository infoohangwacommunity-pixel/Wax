# ADR-0002: Database Selection — PostgreSQL (production) + SQLite (tests)

- **Status:** ADOPTED
- **Date:** 2026-09-13
- **Decider:** Super Z (autonomous engineering agent)
- **Reversibility:** Moderately expensive (schema is portable via SQLAlchemy, but operational tooling differs)

## Context

Phase C (State/Persistence) requires a real transactional database. WAX's
invariants require:

- Durable state (INV-05): survives process interruption
- Audit attribution (INV-06): security-sensitive actions are attributable
- Replaceability (INV-07): the DB must have a replacement boundary

The foundation documents mention PostgreSQL explicitly as a current
technology but warn against letting it define the architecture. The
foundation also says "WAX is not defined by PostgreSQL, Redis, Railway,
WhatsApp, one model provider, or any current technology."

## Decision

- **Production database:** PostgreSQL 14+
- **Test database:** SQLite (in-memory or file-based, WAL mode)
- **ORM abstraction:** SQLAlchemy 2.0 async (insulates application code from DB choice)
- **Migration tool:** Alembic

## Reason

PostgreSQL is the strongest production choice for WAX's needs:

1. **ACID transactions** — required for durable state (INV-05)
2. **JSONB** — needed for storing capability contracts, memory entries, audit events
3. **Row-level security** — future option for multi-tenant isolation
4. **Mature async story** — asyncpg is rock-solid
5. **Open-source** — no licensing lock-in
6. **Operational maturity** — backups, replication, monitoring all well-understood

SQLite is used for tests because:

1. Zero-configuration (in-memory mode)
2. Fast test runs
3. Sufficient for testing the SQLAlchemy ORM layer
4. SQLAlchemy 2.0 makes the DB swap transparent at the ORM level

## Trade-offs

- SQLite ↔ PostgreSQL has subtle differences (e.g., JSON operators, concurrency model).
  Migration tests must run against PostgreSQL in CI (future).
- Using SQLite for tests means we test the ORM contract, not PostgreSQL-specific
  behavior. This is acceptable for unit/integration tests but insufficient for
  reliability tests (Phase Z — Production Hardening).

## Replacement Strategy

If PostgreSQL must be replaced:

- The SQLAlchemy 2.0 async abstraction means application code does not change.
- Alembic migrations would need to be re-targeted (or generated fresh for the new DB).
- Concrete replacement candidates: CockroachDB (for distributed state), YugabyteDB.

## Follow-up

- Install asyncpg for production use (already in pyproject.toml).
- CI must run a PostgreSQL service container for migration tests (future).
