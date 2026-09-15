"""Tests for Phase U — Continuity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wax.continuity.contracts import (
    ConversationStatus,
)
from wax.continuity.repository import ConversationRepository
from wax.continuity.service import ContinuityService, ConversationService
from wax.identity.repository import PrincipalRepository
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture
async def principal_id(fresh_db) -> str:
    async with db_session() as session:
        repo = PrincipalRepository(session)
        p = await repo.create_principal(display_name="Continuity User")
        await session.commit()
        return p.id


class TestConversationRepository:
    async def test_create_returns_active_conversation(self, principal_id) -> None:
        async with db_session() as session:
            repo = ConversationRepository(session)
            conv = await repo.create(principal_id, "whatsapp")
            await session.commit()
            assert conv.status == ConversationStatus.ACTIVE.value
            assert conv.principal_id == principal_id
            assert conv.message_count == 0

    async def test_get_active_for_principal_returns_most_recent(self, principal_id) -> None:
        async with db_session() as session:
            repo = ConversationRepository(session)
            await repo.create(principal_id, "whatsapp")
            # Touch the new one's last_message_at to be later
            new = await repo.create(principal_id, "whatsapp")
            await session.commit()

        async with db_session() as session:
            repo = ConversationRepository(session)
            active = await repo.get_active_for_principal(principal_id)
            assert active is not None
            # Most recent first
            assert active.id == new.id

    async def test_touch_updates_message_count_and_last_message_at(self, principal_id) -> None:
        async with db_session() as session:
            repo = ConversationRepository(session)
            conv = await repo.create(principal_id, "whatsapp")
            original_last = conv.last_message_at
            await session.commit()

        import time

        time.sleep(0.01)

        async with db_session() as session:
            repo = ConversationRepository(session)
            ok = await repo.touch(conv.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ConversationRepository(session)
            fetched = await repo.get(conv.id)
            assert fetched.message_count == 1

            # Normalize timezones for comparison (SQLite may return naive)
            def _aware(dt: datetime) -> datetime:
                return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

            assert _aware(fetched.last_message_at) > _aware(original_last)

    async def test_close_marks_closed(self, principal_id) -> None:
        async with db_session() as session:
            repo = ConversationRepository(session)
            conv = await repo.create(principal_id, "whatsapp")
            ok = await repo.close(conv.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ConversationRepository(session)
            fetched = await repo.get(conv.id)
            assert fetched.status == ConversationStatus.CLOSED.value


class TestConversationService:
    async def test_open_or_resume_creates_new(self, principal_id) -> None:
        async with db_session() as session:
            svc = ConversationService(session)
            conv = await svc.open_or_resume(principal_id, "whatsapp")
            await session.commit()
            assert conv.message_count == 0
            assert conv.principal_id == principal_id

    async def test_open_or_resume_resumes_existing(self, principal_id) -> None:
        async with db_session() as session:
            svc = ConversationService(session)
            first = await svc.open_or_resume(principal_id, "whatsapp")
            await session.commit()

        async with db_session() as session:
            svc = ConversationService(session)
            second = await svc.open_or_resume(principal_id, "whatsapp")
            await session.commit()

        assert first.id == second.id  # same conversation resumed


class TestContinuityService:
    async def test_new_principal_gets_empty_context(self, principal_id) -> None:
        async with db_session() as session:
            svc = ContinuityService(session)
            context, conv_id = await svc.build_context(principal_id, "whatsapp")

        assert context.principal_id == principal_id
        assert context.is_new_conversation is True
        assert conv_id is None
        assert context.recent_memories == []
        assert context.conversation_id is None

    async def test_returning_principal_resumes_conversation(self, principal_id) -> None:
        # First, create a conversation
        async with db_session() as session:
            conv_svc = ConversationService(session)
            conv = await conv_svc.open_or_resume(principal_id, "whatsapp")
            await session.commit()
            conv_id = conv.id

        # Then, build context — should find the conversation
        async with db_session() as session:
            cont_svc = ContinuityService(session)
            context, returned_conv_id = await cont_svc.build_context(principal_id, "whatsapp")

        assert context.is_new_conversation is False
        assert returned_conv_id == conv_id
        assert context.conversation_id == conv_id

    async def test_days_since_last_message_computed(self, principal_id) -> None:
        """A returning user after days should have days_since_last_message set."""
        async with db_session() as session:
            conv_svc = ConversationService(session)
            conv = await conv_svc.open_or_resume(principal_id, "whatsapp")
            # Manually set last_message_at to 3 days ago
            conv.last_message_at = datetime.now(UTC) - timedelta(days=3)
            await session.commit()

        async with db_session() as session:
            cont_svc = ContinuityService(session)
            context, _ = await cont_svc.build_context(principal_id, "whatsapp")

        assert context.is_new_conversation is False
        assert context.days_since_last_message is not None
        assert 2.9 < context.days_since_last_message < 3.1

    async def test_recent_memories_included_in_context(self, principal_id) -> None:
        # Create some memories
        async with db_session() as session:
            mem_repo = MemoryRepository(session)
            for i in range(3):
                await mem_repo.create(
                    MemoryCreate(
                        principal_id=principal_id,
                        kind=MemoryKind.EPISODIC,
                        content={"event": f"event {i}"},
                        provenance="user_statement",
                        summary=f"event {i}",
                    )
                )
            await session.commit()

        async with db_session() as session:
            cont_svc = ContinuityService(session)
            context, _ = await cont_svc.build_context(principal_id, "whatsapp")

        assert len(context.recent_memories) == 3
        # Most recent first
        assert context.recent_memories[0]["summary"] in {"event 0", "event 1", "event 2"}

    async def test_continuity_is_per_principal(self, fresh_db) -> None:
        """Conversations and memory do not leak across principals."""
        async with db_session() as session:
            principal_repo = PrincipalRepository(session)
            p1 = await principal_repo.create_principal(display_name="P1")
            p2 = await principal_repo.create_principal(display_name="P2")
            await session.commit()

        # P1 has memories + active conversation
        async with db_session() as session:
            mem_repo = MemoryRepository(session)
            await mem_repo.create(
                MemoryCreate(
                    principal_id=p1.id,
                    kind=MemoryKind.EPISODIC,
                    content={"secret": "p1's secret"},
                    provenance="user_statement",
                    summary="p1 secret",
                )
            )
            conv_svc = ConversationService(session)
            await conv_svc.open_or_resume(p1.id, "whatsapp")
            await session.commit()

        # P2 should see NONE of P1's data
        async with db_session() as session:
            cont_svc = ContinuityService(session)
            p2_context, _ = await cont_svc.build_context(p2.id, "whatsapp")

        assert p2_context.recent_memories == []
        assert p2_context.conversation_id is None
        assert p2_context.is_new_conversation is True
