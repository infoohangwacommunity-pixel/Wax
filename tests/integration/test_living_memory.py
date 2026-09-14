"""Living Memory — integration tests (ADR-0036, Phase 3).

Covers:
- depends_on + conflicts_with link kinds (new in Phase 3)
- objective_id linkage in memory.store
- consolidation_sources forward provenance chain (memory.consolidate)
- Backward + forward chain are both queryable after consolidation
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import (
    CapabilityInvocationRequest,
)
from wax.identity.repository import PrincipalRepository
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.objective.contracts import ObjectiveCreate, ObjectiveKind
from wax.objective.repository import ObjectiveRepository
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.memory_models import MemoryLinkRecord, MemoryRecord
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(
    *, display_name: str = "Test", phone: str = "1234567890"
) -> str:
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id,
            kind="whatsapp_phone",
            value=phone,
            is_verified=True,
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


# ----------------------------------------------------------------------------
# 1. New link kinds: depends_on, conflicts_with
# ----------------------------------------------------------------------------


class TestNewLinkKinds:
    async def test_depends_on_link_accepted_by_memory_store(self, fresh_db, services):
        principal_id = await _create_principal()
        # Create two memories first
        async with db_session() as s:
            repo = MemoryRepository(s)
            await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "A"},
                    provenance="user_statement",
                )
            )
            m2 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "B"},
                    provenance="user_statement",
                )
            )
            await s.commit()
            m2_id = m2.id

        # Now link m1 → m2 via the memory.store capability (links parameter)
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.store",
                    principal_id=principal_id,
                    inputs={
                        "content": {"fact": "C depends on B"},
                        "links": [{"memory_id": m2_id, "kind": "depends_on"}],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error

        # Verify the depends_on link was created
        async with db_session() as s:
            links = (
                await s.execute(
                    select(MemoryLinkRecord).where(
                        MemoryLinkRecord.kind == "depends_on"
                    )
                )
            ).scalars().all()
            assert len(links) == 1
            assert links[0].to_memory_id == m2_id

    async def test_conflicts_with_link_accepted_by_memory_store(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            repo = MemoryRepository(s)
            m1 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.SEMANTIC,
                    content={"fact": "X"},
                    provenance="user_statement",
                )
            )
            await s.commit()
            m1_id = m1.id

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.store",
                    principal_id=principal_id,
                    inputs={
                        "content": {"fact": "Y conflicts with X"},
                        "links": [{"memory_id": m1_id, "kind": "conflicts_with"}],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error

        async with db_session() as s:
            links = (
                await s.execute(
                    select(MemoryLinkRecord).where(
                        MemoryLinkRecord.kind == "conflicts_with"
                    )
                )
            ).scalars().all()
            assert len(links) == 1


# ----------------------------------------------------------------------------
# 2. objective_id linkage
# ----------------------------------------------------------------------------


class TestObjectiveLinkage:
    async def test_memory_store_attaches_objective_id(self, fresh_db, services):
        principal_id = await _create_principal()
        # Create an objective first
        async with db_session() as s:
            obj_repo = ObjectiveRepository(s)
            objective = await obj_repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="study for exam",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            await s.commit()
            objective_id = objective.id

        # Store a memory linked to that objective
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.store",
                    principal_id=principal_id,
                    inputs={
                        "content": {"note": "I studied chapter 1"},
                        "objective_id": objective_id,
                    },
                )
            )
            await s.commit()
            memory_id = result.outputs["memory_id"]

        assert result.outcome == "success", result.error

        # Verify the memory's objective_id is set
        async with db_session() as s:
            memory = await s.get(MemoryRecord, memory_id)
            assert memory.objective_id == objective_id

    async def test_memory_store_rejects_objective_owned_by_other_principal(
        self, fresh_db, services
    ):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")

        # principal_b creates an objective
        async with db_session() as s:
            obj_repo = ObjectiveRepository(s)
            objective = await obj_repo.create(
                ObjectiveCreate(
                    principal_id=principal_b,
                    description="b's objective",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            await s.commit()
            objective_id = objective.id

        # principal_a tries to attach their memory to b's objective —
        # ownership check should reject
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.store",
                    principal_id=principal_a,
                    inputs={
                        "content": {"note": "a tries to link to b's objective"},
                        "objective_id": objective_id,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")

    async def test_memory_store_rejects_nonexistent_objective(
        self, fresh_db, services
    ):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.store",
                    principal_id=principal_id,
                    inputs={
                        "content": {"note": "test"},
                        "objective_id": "01NOSUCHOBJECTIVE0000000000A",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "No such objective" in (result.error or "")


# ----------------------------------------------------------------------------
# 3. Consolidation provenance forward chain
# ----------------------------------------------------------------------------


class TestConsolidationProvenance:
    async def test_consolidate_records_consolidation_sources(self, fresh_db, services):
        principal_id = await _create_principal()
        # Create three source memories
        async with db_session() as s:
            repo = MemoryRepository(s)
            m1 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "studied ch 1"},
                    provenance="user_statement",
                )
            )
            m2 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "studied ch 2"},
                    provenance="user_statement",
                )
            )
            m3 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "studied ch 3"},
                    provenance="user_statement",
                )
            )
            await s.commit()
            source_ids = [m1.id, m2.id, m3.id]

        # Consolidate them into one semantic memory
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.consolidate",
                    principal_id=principal_id,
                    inputs={
                        "source_ids": source_ids,
                        "content": {"summary": "studied ch 1-3"},
                        "summary": "Consolidated study session evidence",
                        "kind": "semantic",
                        "confidence": 0.85,
                    },
                )
            )
            await s.commit()
            consolidated_id = result.outputs["memory_id"]

        assert result.outcome == "success", result.error

        # Verify the forward chain (consolidation_sources) is recorded
        async with db_session() as s:
            consolidated = await s.get(MemoryRecord, consolidated_id)
            assert consolidated.consolidation_sources is not None
            assert set(consolidated.consolidation_sources) == set(source_ids)

            # Verify the backward chain (superseded_by) is also set
            for source_id in source_ids:
                source = await s.get(MemoryRecord, source_id)
                assert source.superseded_by == consolidated_id
                assert source.status == "superseded"

    async def test_consolidate_links_derived_from_each_source(self, fresh_db, services):
        """The consolidation creates a `derived_from` edge from the new
        record to each source. This complements the consolidation_sources
        column — the column is for fast forward-lookup; the edges are
        for graph traversal."""
        principal_id = await _create_principal()
        async with db_session() as s:
            repo = MemoryRepository(s)
            m1 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "observed X"},
                    provenance="user_statement",
                )
            )
            m2 = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=MemoryKind.EPISODIC,
                    content={"event": "observed Y"},
                    provenance="user_statement",
                )
            )
            await s.commit()
            source_ids = [m1.id, m2.id]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="memory.consolidate",
                    principal_id=principal_id,
                    inputs={
                        "source_ids": source_ids,
                        "content": {"summary": "X and Y observed"},
                    },
                )
            )
            await s.commit()
            consolidated_id = result.outputs["memory_id"]

        assert result.outcome == "success"

        # Verify derived_from edges exist
        async with db_session() as s:
            edges = (
                await s.execute(
                    select(MemoryLinkRecord).where(
                        MemoryLinkRecord.from_memory_id == consolidated_id,
                        MemoryLinkRecord.kind == "derived_from",
                    )
                )
            ).scalars().all()
            assert len(edges) == 2
            edge_targets = {e.to_memory_id for e in edges}
            assert edge_targets == set(source_ids)


# ----------------------------------------------------------------------------
# 4. Memory can be queried by objective_id
# ----------------------------------------------------------------------------


class TestObjectiveScopedRetrieval:
    async def test_query_memories_for_an_objective(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            obj_repo = ObjectiveRepository(s)
            obj1 = await obj_repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="objective 1",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            obj2 = await obj_repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="objective 2",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            await s.commit()
            obj1_id, obj2_id = obj1.id, obj2.id

        # Store 2 memories for obj1, 1 for obj2, 1 with no objective
        async with db_session() as s:
            invoker = services.invoker(s)
            for i, oid in enumerate([obj1_id, obj1_id, obj2_id, None]):
                result = await invoker.invoke(
                    CapabilityInvocationRequest(
                        capability_name="memory.store",
                        principal_id=principal_id,
                        inputs={
                            "content": {"note": f"memory {i}"},
                            **({"objective_id": oid} if oid else {}),
                        },
                    )
                )
                assert result.outcome == "success", result.error
            await s.commit()

        # Query memories for obj1
        async with db_session() as s:
            memories_for_obj1 = (
                await s.execute(
                    select(MemoryRecord).where(
                        MemoryRecord.objective_id == obj1_id,
                        MemoryRecord.status == "active",
                    )
                )
            ).scalars().all()
            assert len(memories_for_obj1) == 2

            memories_for_obj2 = (
                await s.execute(
                    select(MemoryRecord).where(
                        MemoryRecord.objective_id == obj2_id,
                        MemoryRecord.status == "active",
                    )
                )
            ).scalars().all()
            assert len(memories_for_obj2) == 1
