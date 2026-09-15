"""Memory retrieval upgrade tests (ADR-0019).

The upgrade is two-stage: RECALL (newest pool U lexical term matches —
portable; Postgres deployments additionally get the GIN-indexed tsvector
path from the migration) then RANK (BM25-style idf-weighted scoring
blended with recency + confidence).

These tests pin the properties the upgrade exists for:
- idf weighting: a RARE-term memory outranks a common-term one
- recall beyond the recency pool: an old but on-topic memory is found
- no false positives, principal isolation, and honest emptiness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.state.engine import db_session


@pytest.fixture
async def fresh_db(test_settings):
    from wax.authority.seed import seed_builtin_roles
    from wax.state.engine import dispose_engine, init_engine
    from wax.state.models import Base

    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield
    await dispose_engine()


async def _store(
    principal_id: str,
    content: str,
    *,
    summary: str | None = None,
    days_old: float = 0.0,
    confidence: float | None = None,
) -> str:
    async with db_session() as session:
        record = await MemoryRepository(session).create(
            MemoryCreate(
                principal_id=principal_id,
                kind=MemoryKind.EPISODIC,
                content={"text": content},
                provenance="test",
                confidence=confidence,
                summary=summary,
            )
        )
        if days_old:
            record.created_at = datetime.now(UTC) - timedelta(days=days_old)
        await session.commit()
        return record.id


class TestBM25Ranking:
    async def test_rare_term_outranks_common_term(self, fresh_db):
        # "zyzzogeton" appears in exactly one memory; "project" appears
        # in ALL of them. IDF must make the rare hit win even when the
        # common-term memory mentions the query too.
        for i in range(6):
            await _store("P1", f"project planning meeting number {i} notes")
        await _store("P1", "zyzzogeton specimen arrived for the collection")

        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                "P1", "zyzzogeton project", limit=3
            )
        assert results
        top_content = str(results[0][0].content)
        assert "zyzzogeton" in top_content

    async def test_term_frequency_and_length_effects(self, fresh_db):
        # Focused, shorter memory about the query topic should outrank a
        # long rambler that mentions the term once.
        await _store("P1", "kayak kayak kayak kayak repairs done right")
        await _store(
            "P1",
            ("kayak mention buried at the end. " + "unrelated filler words. " * 40),
        )
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant("P1", "kayak", limit=5)
        assert "kayak kayak" in str(results[0][0].content)

    async def test_recency_and_confidence_still_blend(self, fresh_db):
        # Equal lexical match: the fresher memory with higher confidence
        # outranks the stale one.
        await _store("P1", "elevator pitch final version", days_old=30, confidence=0.2)
        await _store("P1", "elevator pitch final version", days_old=0, confidence=0.9)
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                "P1", "elevator pitch", limit=5
            )
        assert len(results) == 2
        assert results[0][1] > results[1][1]
        fresh_record = results[0][0]
        assert fresh_record.confidence == 0.9


class TestRecallBeyondNewestPool:
    async def test_old_on_topic_memory_is_recalled(self, fresh_db):
        # candidate_pool=3 newest memories; the on-topic memory is old
        # (rank ~9 by recency). The lexical recall arm must surface it —
        # the previous implementation searched only the newest N.
        for i in range(8):
            await _store("P1", f"chat message {i} about lunch plans")
        await _store("P1", "the ferrofluid demonstration captivated everyone", days_old=0.5)

        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                "P1", "ferrofluid demonstration", limit=5, candidate_pool=3
            )
        assert results, "on-topic memory outside the newest pool was lost"
        assert "ferrofluid" in str(results[0][0].content)

    async def test_recall_is_principal_scoped(self, fresh_db):
        await _store("P1", "marmalade recipe secrets")
        await _store("P2", "marmalade recipe secrets of the other principal")
        for i in range(8):
            await _store("P2", f"unrelated chat {i}")

        async with db_session() as session:
            results_p2 = await MemoryRepository(session).search_relevant(
                "P2", "marmalade recipe", limit=5, candidate_pool=3
            )
            results_p1 = await MemoryRepository(session).search_relevant(
                "P1", "marmalade recipe", limit=5, candidate_pool=3
            )
        assert "marmalade recipe secrets of" in str(results_p2[0][0].content)
        assert "marmalade recipe secrets" in str(results_p1[0][0].content)
        # Cross-principal leak check: P1 never sees P2's memory.
        assert "other principal" not in str(results_p1[0][0].content)


class TestHonestEdges:
    async def test_no_match_returns_empty(self, fresh_db):
        await _store("P1", "completely unrelated content here")
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                "P1", "quantum entanglement lab", limit=5
            )
        assert results == []

    async def test_stopword_only_query_returns_empty(self, fresh_db):
        await _store("P1", "some memory")
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                "P1", "the and for with", limit=5
            )
        assert results == []

    async def test_superseded_memories_not_recalled(self, fresh_db):
        old_id = await _store("P1", "terraform plan for the garden")
        await _store("P1", "terraform plan for the garden, REVISED")
        async with db_session() as session:
            repo = MemoryRepository(session)
            records = await repo.list_active_for_principal("P1")
            old_record = next(r for r in records if r.id == old_id)
            new_record = next(r for r in records if r.id != old_id)
            await repo.supersede(old_record.id, new_record.id)
            await session.commit()
            results = await repo.search_relevant("P1", "terraform plan", limit=5)
        assert len(results) == 1
        assert "REVISED" in str(results[0][0].content)


class TestMigrationDialectGuard:
    """The tsvector migration is dialect-guarded (PG-only DDL; honest
    no-op on SQLite). Its real behavior is exercised by the alembic
    subprocess discipline tests (fresh / upgrade / rollback) which run
    the actual chain over SQLite; here we pin the GUARD itself."""

    def test_guard_function_reads_dialect(self):
        import importlib.util
        import pathlib

        from sqlalchemy.dialects.sqlite import dialect as SQLiteDialect

        path = pathlib.Path(__file__).resolve().parents[2] / (
            "migrations/versions/d9e4f2a8b1c7_memory_retrieval_upgrade.py"
        )
        spec = importlib.util.spec_from_file_location("memory_retrieval_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # The guard must report not-postgres for SQLite (the real
        # no-op behavior is exercised by the alembic subprocess tests).
        assert not module._is_postgres_for(SQLiteDialect())
