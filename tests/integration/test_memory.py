"""Integration tests for Phase F (Memory).

Tests verify:
- Memory records have rich structure (kind, provenance, confidence, expiry)
- Memory is per-principal (no cross-principal leakage)
- Supersession preserves provenance (both records retained, old marked superseded)
- Forgetting is a soft delete (record retained for audit, excluded from retrieval)
- Expiry retrieval finds memories whose retention policy has fired
- Memory is NOT just a vector DB — it carries semantic structure
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from wax.core.config import settings_for_testing
from wax.identity.repository import PrincipalRepository
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.memory_models import MemoryRecord


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


# Need Base for schema creation fixture
from wax.state.models import Base  # noqa: E402


@pytest.fixture
async def principal_id(fresh_db) -> str:
    """Create a principal for memory tests."""
    async with db_session() as session:
        repo = PrincipalRepository(session)
        p = await repo.create_principal(display_name="Memory Test User")
        await session.commit()
        return p.id


class TestMemoryCreate:
    async def test_create_returns_record_with_id(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "user said hello", "timestamp": "2026-09-13T10:00:00Z"},
                    provenance="user_statement",
                    confidence=0.9,
                )
            )
            await session.commit()

            assert record.id is not None
            assert len(record.id) == 26
            assert record.kind == "episodic"
            assert record.status == "active"
            assert record.confidence == 0.9
            assert record.content["event"] == "user said hello"

    async def test_create_with_all_fields(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "user is a Python developer"},
                    provenance="model_observation",
                    confidence=0.7,
                    expires_at=expiry,
                    sensitivity=2,
                    summary="user is a Python developer",
                )
            )
            await session.commit()

            assert record.kind == "semantic"
            assert record.sensitivity == 2
            assert record.expires_at is not None
            assert record.summary == "user is a Python developer"


class TestMemoryRetrieval:
    async def test_list_active_excludes_superseded(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            old = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "old value"},
                    provenance="user_statement",
                )
            )
            new = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "new value"},
                    provenance="user_statement",
                )
            )
            await repo.supersede(old.id, new.id)
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            active = await repo.list_active_for_principal(principal_id)
            assert len(active) == 1
            assert active[0].content["fact"] == "new value"

    async def test_list_active_excludes_forgotten(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            keep = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "keep this"},
                    provenance="user_statement",
                )
            )
            forget = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "forget this"},
                    provenance="user_statement",
                )
            )
            await repo.forget(forget.id)
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            active = await repo.list_active_for_principal(principal_id)
            assert len(active) == 1
            assert active[0].content["event"] == "keep this"

    async def test_filter_by_kind(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "episode 1"},
                    provenance="user_statement",
                )
            )
            await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "fact 1"},
                    provenance="model_observation",
                )
            )
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            episodic = await repo.list_active_for_principal(
                principal_id, kind="episodic"
            )
            semantic = await repo.list_active_for_principal(
                principal_id, kind="semantic"
            )
            assert len(episodic) == 1
            assert len(semantic) == 1


class TestMemorySupersession:
    """Supersession is the WAX approach to memory conflict resolution.

    Instead of overwriting (loses provenance) or deleting (loses audit),
    we mark the older record as superseded and link to the newer one.
    """

    async def test_supersede_marks_old_links_to_new(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            old = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "old"},
                    provenance="user_statement",
                )
            )
            new = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "new"},
                    provenance="user_statement",
                )
            )
            ok = await repo.supersede(old.id, new.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            old_record = await session.get(MemoryRecord, old.id)
            assert old_record is not None
            assert old_record.status == "superseded"
            assert old_record.superseded_by == new.id

    async def test_supersede_unknown_returns_false(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            ok = await repo.supersede("01HXY" + "0" * 21, "01HXY" + "1" * 21)
            assert not ok

    async def test_both_records_retained_after_supersession(
        self, principal_id
    ) -> None:
        """Critical: supersession does NOT delete the old record.

        Both records remain in the DB — the old one is just marked. This
        preserves provenance and audit trail.
        """
        async with db_session() as session:
            repo = MemoryRepository(session)
            old = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "v1"},
                    provenance="user_statement",
                )
            )
            new = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "v2"},
                    provenance="user_statement",
                )
            )
            await repo.supersede(old.id, new.id)
            await session.commit()

        async with db_session() as session:
            old_record = await session.get(MemoryRecord, old.id)
            new_record = await session.get(MemoryRecord, new.id)
            assert old_record is not None  # still exists
            assert new_record is not None
            assert old_record.status == "superseded"
            assert new_record.status == "active"


class TestMemoryForgetting:
    """Forgetting is a real operation, not a deletion (Directive §34)."""

    async def test_forget_marks_record_but_retains_for_audit(
        self, principal_id
    ) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "should be forgotten"},
                    provenance="user_statement",
                )
            )
            ok = await repo.forget(record.id)
            await session.commit()
            assert ok

        # Record still exists in DB
        async with db_session() as session:
            r = await session.get(MemoryRecord, record.id)
            assert r is not None
            assert r.status == "forgotten"

    async def test_forget_excluded_from_default_retrieval(
        self, principal_id
    ) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            active = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "active"},
                    provenance="user_statement",
                )
            )
            forgotten = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "forgotten"},
                    provenance="user_statement",
                )
            )
            await repo.forget(forgotten.id)
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            records = await repo.list_active_for_principal(principal_id)
            assert len(records) == 1
            assert records[0].content["event"] == "active"


class TestMemoryExpiry:
    """Memories with expires_at should be discoverable by the expiry worker."""

    async def test_expire_due_finds_expired_memories(self, principal_id) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            # Already expired (1 hour ago)
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            expired = MemoryRecord(
                id="01HXY" + "0" * 21,
                principal_id=principal_id,
                kind="episodic",
                status="active",
                content={"event": "should be expired"},
                provenance="user_statement",
                expires_at=past,
                sensitivity=0,
            )
            session.add(expired)

            # Not yet expired (1 hour in future)
            future = datetime.now(timezone.utc) + timedelta(hours=1)
            fresh = MemoryRecord(
                id="01HXY" + "1" * 21,
                principal_id=principal_id,
                kind="episodic",
                status="active",
                content={"event": "still fresh"},
                provenance="user_statement",
                expires_at=future,
                sensitivity=0,
            )
            session.add(fresh)
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            due = await repo.expire_due()
            ids = [m.id for m in due]
            assert "01HXY" + "0" * 21 in ids
            assert "01HXY" + "1" * 21 not in ids

    async def test_expire_due_excludes_already_forgotten(
        self, principal_id
    ) -> None:
        """A forgotten memory should not appear in the expiry queue."""
        async with db_session() as session:
            repo = MemoryRepository(session)
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "expired and forgotten"},
                    provenance="user_statement",
                    expires_at=past,
                )
            )
            await repo.forget(record.id)
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            due = await repo.expire_due()
            assert record.id not in [m.id for m in due]


class TestMemoryIsPerPrincipal:
    """Memory is per-principal. No cross-principal leakage."""

    async def test_list_does_not_leak_across_principals(self, fresh_db) -> None:
        async with db_session() as session:
            principal_repo = PrincipalRepository(session)
            p1 = await principal_repo.create_principal(display_name="P1")
            p2 = await principal_repo.create_principal(display_name="P2")
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            await repo.create(
                MemoryCreate(
                    principal_id=p1.id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "p1's secret"},
                    provenance="user_statement",
                    sensitivity=3,
                )
            )
            await repo.create(
                MemoryCreate(
                    principal_id=p2.id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "p2's secret"},
                    provenance="user_statement",
                    sensitivity=3,
                )
            )
            await session.commit()

        async with db_session() as session:
            repo = MemoryRepository(session)
            p1_memories = await repo.list_active_for_principal(p1.id)
            p2_memories = await repo.list_active_for_principal(p2.id)
            assert len(p1_memories) == 1
            assert len(p2_memories) == 1
            assert p1_memories[0].content["fact"] == "p1's secret"
            assert p2_memories[0].content["fact"] == "p2's secret"
