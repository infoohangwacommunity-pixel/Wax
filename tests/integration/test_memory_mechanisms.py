"""Tests for the memory mechanisms: relevance retrieval, lifecycle
worker, and the memory.* capability surface.

These prove the runtime owns memory as a MECHANISM:
- the AI sees what is RELEVANT, not merely what is recent (context
  assembly), with reasons attached;
- expired memories are forgotten by the runtime, not by an AI's good
  intentions (lifecycle worker);
- memory.store / memory.search / memory.forget cross the same gate
  chain as every other effect, with ownership boundaries enforced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wax.core.config import settings_for_testing
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.state.engine import db_session, dispose_engine, init_engine


@pytest.fixture(autouse=True)
async def fresh_db():
    """Hermetic in-memory DB per test (same pattern as test_memory.py)."""
    test_settings = settings_for_testing()
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    from wax.state.models import Base

    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture
def principal_id() -> str:
    from ulid import ULID

    return str(ULID())


async def _store(
    principal_id: str,
    *,
    summary: str,
    content: dict,
    kind: str = "semantic",
    created_days_ago: float = 0.0,
    expires_at: datetime | None = None,
    confidence: float | None = None,
) -> None:
    async with db_session() as session:
        repo = MemoryRepository(session)
        record = await repo.create(
            MemoryCreate(
                principal_id=principal_id,
                kind=MemoryKind(kind),
                content=content,
                provenance="user_statement",
                summary=summary,
                expires_at=expires_at,
                confidence=confidence,
            )
        )
        if created_days_ago:
            # Backdate to simulate old memories.
            record.created_at = datetime.now(UTC) - timedelta(days=created_days_ago)
        await session.commit()


class TestRelevanceRetrieval:
    async def test_relevant_old_memory_beats_irrelevant_new_one(
        self, principal_id
    ) -> None:
        await _store(
            principal_id,
            summary="User is preparing for the WAEC physics exam in June",
            content={"topic": "physics", "exam": "WAEC"},
            created_days_ago=30,
        )
        await _store(
            principal_id,
            summary="User said hello and mentioned the weather today",
            content={"chat": "weather smalltalk"},
            created_days_ago=0.001,
        )
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                principal_id, "What did I tell you about my physics exam?"
            )
        assert results, "expected at least one relevant memory"
        top_record, top_score = results[0]
        assert "WAEC physics" in top_record.summary
        assert top_score > 0.0

    async def test_all_kinds_are_searchable(self, principal_id) -> None:
        await _store(
            principal_id,
            summary="Prefers evening study sessions",
            content={"preference": "evenings"},
            kind="semantic",
        )
        await _store(
            principal_id,
            summary="Requested a code review last Tuesday",
            content={"event": "code review"},
            kind="episodic",
        )
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                principal_id, "code review request"
            )
        assert any(r.kind == "episodic" for r, _ in results)

    async def test_garbage_query_returns_empty(self, principal_id) -> None:
        await _store(principal_id, summary="something", content={"a": 1})
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                principal_id, "??!"
            )
        assert results == []

    async def test_zero_overlap_excluded(self, principal_id) -> None:
        await _store(
            principal_id,
            summary="Completely unrelated banjo facts",
            content={"topic": "banjo"},
        )
        async with db_session() as session:
            results = await MemoryRepository(session).search_relevant(
                principal_id, "quantum chromodynamics confinement"
            )
        assert results == []


class TestContextComposition:
    async def test_context_merges_recency_and_relevance_with_reasons(
        self, principal_id
    ) -> None:
        from wax.continuity.service import ContinuityService

        await _store(
            principal_id,
            summary="User is preparing for the WAEC physics exam",
            content={"topic": "physics"},
            created_days_ago=20,
        )
        await _store(
            principal_id,
            summary="User just said hi and asked about jests",
            content={"chat": "recent greeting"},
        )
        async with db_session() as session:
            svc = ContinuityService(session)
            context, _conv = await svc.build_context(
                principal_id, "whatsapp", "tell me about my physics exam again"
            )
        by_id = {m["id"]: m for m in context.recent_memories}
        reasons = {m["reason"] for m in context.recent_memories}
        assert reasons <= {"recent", "relevant", "recent+relevant"}
        # The old physics memory must appear — via relevance, not recency.
        physics = [m for m in context.recent_memories if "WAEC physics" in m["summary"]]
        assert physics, "relevant-but-old memory missing from context"
        assert physics[0]["reason"] in {"relevant", "recent+relevant"}

    async def test_superseded_and_forgotten_never_surfaced(self, principal_id) -> None:
        await _store(principal_id, summary="old address Kano", content={"addr": "Kano"})
        async with db_session() as session:
            repo = MemoryRepository(session)
            old = (await repo.list_active_for_principal(principal_id))[0]
            # Replacement + forget the old one.
            new = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"addr": "Lagos"},
                    provenance="user_statement",
                    summary="new address Lagos",
                )
            )
            await repo.supersede(old.id, new.id)
            await session.commit()
            context_memories = await repo.search_relevant(principal_id, "address")
        assert all("Kano" not in (r.summary or "") for r, _ in context_memories)


class TestLifecycleWorker:
    async def test_expired_memory_is_forgotten_by_sweep(self, principal_id) -> None:
        await _store(
            principal_id,
            summary="temporary note",
            content={"note": "temp"},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await _store(principal_id, summary="durable note", content={"note": "keep"})

        from wax.memory.lifecycle import expire_due_memories

        async with db_session() as session:
            forgotten_count = await expire_due_memories(session)
            await session.commit()
            repo = MemoryRepository(session)
            still_active = await repo.list_active_for_principal(principal_id)

        assert forgotten_count == 1
        assert [m.summary for m in still_active] == ["durable note"]

    async def test_sweep_is_idempotent(self, principal_id) -> None:
        await _store(
            principal_id,
            summary="temp",
            content={"n": 1},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        from wax.memory.lifecycle import expire_due_memories

        async with db_session() as session:
            first = await expire_due_memories(session)
            await session.commit()
            second = await expire_due_memories(session)
        assert (first, second) == (1, 0)


class TestMemoryCapabilities:
    async def test_store_search_forget_roundtrip(self) -> None:
        from wax.capabilities.contracts import InvocationContext
        from wax.capabilities.runtime_capabilities import (
            memory_forget_impl,
            memory_search_impl,
            memory_store_impl,
        )

        ctx = InvocationContext(
            principal_id="p-cap-test",
            capability_name="memory.store",
            execution_id="exec-1",
            request_id="req-1",
        )
        stored = await memory_store_impl(
            {
                "kind": "semantic",
                "content": {"fact": "user's brother is called Tunde"},
                "summary": "Brother named Tunde",
                "confidence": 0.9,
            },
            ctx,
        )
        assert stored["kind"] == "semantic"

        found = await memory_search_impl({"query": "brother Tunde"}, ctx)
        assert found["count"] >= 1
        assert any("Tunde" in m["summary"] for m in found["memories"])

        forgotten = await memory_forget_impl(
            {"memory_id": stored["memory_id"]}, ctx
        )
        assert forgotten["forgotten"] is True

        after = await memory_search_impl({"query": "brother Tunde"}, ctx)
        assert all("Tunde" not in m["summary"] for m in after["memories"])

    async def test_cannot_forget_another_principals_memory(self) -> None:
        from wax.capabilities.contracts import InvocationContext
        from wax.capabilities.runtime_capabilities import (
            memory_forget_impl,
            memory_store_impl,
        )

        owner_ctx = InvocationContext(
            principal_id="p-owner",
            capability_name="memory.store",
            execution_id="e",
            request_id="r",
        )
        attacker_ctx = InvocationContext(
            principal_id="p-attacker",
            capability_name="memory.forget",
            execution_id="e2",
            request_id="r2",
        )
        stored = await memory_store_impl(
            {"content": {"secret": "diary entry"}}, owner_ctx
        )
        with pytest.raises(ValueError, match="different principal"):
            await memory_forget_impl({"memory_id": stored["memory_id"]}, attacker_ctx)

    async def test_store_validates_inputs(self) -> None:
        from wax.capabilities.contracts import InvocationContext
        from wax.capabilities.runtime_capabilities import memory_store_impl

        ctx = InvocationContext(
            principal_id="p-v",
            capability_name="memory.store",
            execution_id="e",
            request_id="r",
        )
        with pytest.raises(ValueError, match="content"):
            await memory_store_impl({"content": "not-an-object"}, ctx)
        with pytest.raises(ValueError, match="kind"):
            await memory_store_impl({"content": {"a": 1}, "kind": "nostalgic"}, ctx)
        with pytest.raises(ValueError, match="expires_at"):
            await memory_store_impl(
                {"content": {"a": 1}, "expires_at": "not-a-date"}, ctx
            )
