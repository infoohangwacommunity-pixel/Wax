"""Typed memory links + importance/observation time (ADR-0022).

Mission Phase 3 and §6.3: memories can carry typed EVIDENCE
relationships (supports / contradicts / derived_from / related_to) that
retrieval traverses — a relational abstraction, not a graph database —
plus importance (ranking weight) and observed_at (when the fact was
true/seen, distinct from when it was written).

Supersession stays a lifecycle mechanism and is deliberately NOT a
link kind. Consolidation writes derived_from edges, so derived knowledge
remains traceable to its evidence through a queryable structure, not
only JSON-in-content.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
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


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


def _memory(principal_id: str, summary: str, **kw) -> MemoryCreate:
    return MemoryCreate(
        principal_id=principal_id,
        kind=MemoryKind.EPISODIC,
        content={"text": summary},
        provenance="user_statement",
        summary=summary,
        **kw,
    )


async def _store(repo: MemoryRepository, principal_id: str, summary: str, **kw):
    record = await repo.create(_memory(principal_id, summary, **kw))
    await session_commit()
    return record


async def session_commit():
    pass  # commits are handled by the caller holding the session


class TestLinkMechanics:
    async def test_link_is_idempotent_and_directional(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory("p", "user passed physics mock"))
            b = await repo.create(_memory("p", "user celebrated the result"))
            edge1 = await repo.link(a.id, b.id, "supports")
            edge2 = await repo.link(a.id, b.id, "supports")
            await session.commit()

        assert edge1 is not None and edge2 is not None
        assert edge1.id == edge2.id  # same edge returned, not duplicated

    async def test_self_link_and_invalid_kind_denied(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory("p", "fact"))
            await session.commit()
            assert await repo.link(a.id, a.id, "supports") is None
            assert await repo.link(a.id, a.id, "knows_kung_fu") is None
            await session.commit()

    async def test_cross_principal_edge_denied(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory("p1", "mine"))
            b = await repo.create(_memory("p2", "theirs"))
            await session.commit()
            assert await repo.link(a.id, b.id, "related_to") is None
            await session.commit()

    async def test_forgotten_endpoint_denied(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory("p", "live"))
            b = await repo.create(_memory("p", "soon gone"))
            await repo.forget(b.id)
            await session.commit()
            assert await repo.link(a.id, b.id, "supports") is None
            await session.commit()

    async def test_unlink_and_traversal(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory("p", "claim"))
            b = await repo.create(_memory("p", "evidence"))
            await repo.link(a.id, b.id, "supports")
            await session.commit()

            edges = await repo.links_for(a.id)
            assert len(edges) == 1
            edge, direction = edges[0]
            assert direction == "outgoing" and edge.kind == "supports"

            edges_b = await repo.links_for(b.id)
            assert edges_b[0][1] == "incoming"

            assert await repo.unlink(a.id, b.id, "supports") is True
            assert await repo.links_for(a.id) == []
            await session.commit()


class TestRetrievalIntegration:
    async def test_linked_neighbor_joins_the_evidence_set(self, fresh_db) -> None:
        """One-hop expansion: a hit pulls its ACTIVE linked neighbor into
        the evidence set with a damped score (mission §49 — 'which
        memories matter for this objective', traversed)."""
        async with db_session() as session:
            repo = MemoryRepository(session)
            hit = await repo.create(_memory("p", "physics exam schedule may june session"))
            neighbor = await repo.create(_memory("p", "user registered for practicals too"))
            unrelated = await repo.create(_memory("p", "favorite color is green apparently"))
            await repo.link(hit.id, neighbor.id, "related_to")
            await session.commit()

            results = await repo.search_relevant("p", "physics exam schedule", limit=5)
            await session.commit()

        ids = [r.id for r, _ in results]
        assert hit.id in ids
        assert neighbor.id in ids, "the linked neighbor of a hit must join the evidence set"
        assert unrelated.id not in ids
        scores = {r.id: s for r, s in results}
        assert scores[hit.id] > scores[neighbor.id], (
            "the direct hit must outrank its damped neighbor"
        )

    async def test_superseded_neighbors_are_not_expanded(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            hit = await repo.create(_memory("p", "exam preparation plan"))
            old = await repo.create(_memory("p", "older plan draft"))
            await repo.supersede(old.id, hit.id)
            await repo.link(hit.id, old.id, "related_to")
            await session.commit()

            results = await repo.search_relevant("p", "exam preparation", limit=5)
            await session.commit()

        ids = [r.id for r, _ in results]
        assert old.id not in ids, (
            "expansion must respect the lifecycle: superseded evidence stays out"
        )


class TestImportanceAndObservationTime:
    async def test_importance_lifts_and_sinks_ranking(self, fresh_db) -> None:
        async with db_session() as session:
            repo = MemoryRepository(session)
            plain = await repo.create(_memory("p", "waec physics syllabus topic"))
            await session.commit()

            # Equal wording, different importance: higher ranks first.
            strong = await repo.create(
                _memory(
                    "p",
                    "waec physics syllabus topic",
                    importance=1.0,
                )
            )
            weak = await repo.create(
                _memory(
                    "p",
                    "waec physics syllabus topic",
                    importance=0.0,
                )
            )
            await session.commit()

            results = await repo.search_relevant("p", "waec physics syllabus topic", limit=5)
            await session.commit()

        order = [r.id for r, _ in results]
        assert order.index(strong.id) < order.index(plain.id) < order.index(weak.id), (
            f"importance must move the rank: {order}"
        )

    async def test_observed_at_drives_temporal_ranking(self, fresh_db) -> None:
        """Late-arriving evidence: written today, observed weeks ago —
        recency must read the OBSERVATION time (mission §6.3/§4.3)."""
        async with db_session() as session:
            repo = MemoryRepository(session)
            old_observation = await repo.create(
                _memory(
                    "p",
                    "physics mock result released",
                    observed_at=datetime.now(UTC) - timedelta(days=40),
                )
            )
            fresh_observation = await repo.create(
                _memory(
                    "p",
                    "physics mock result released",
                    observed_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await session.commit()

            results = await repo.search_relevant("p", "physics mock result released", limit=5)
            await session.commit()

        order = [r.id for r, _ in results]
        assert order.index(fresh_observation.id) < order.index(old_observation.id), (
            "the more recently OBSERVED fact must rank higher"
        )

    async def test_observed_at_never_fabricates_relevance(self, fresh_db) -> None:
        """A memory that does not match the query stays out, however
        important or recent it is (no importance-based hallucination)."""
        async with db_session() as session:
            repo = MemoryRepository(session)
            await repo.create(
                _memory(
                    "p",
                    "critical deliverable due friday",
                    importance=1.0,
                    observed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            results = await repo.search_relevant("p", "quantum entanglement", limit=5)
            await session.commit()
        assert results == []


class TestLinkCapabilities:
    async def _invoke(self, services, principal_id, name, inputs):
        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name=name,
                    principal_id=principal_id,
                    inputs=inputs,
                    request_id=f"exec-{ULID()}",
                )
            )
            await session.commit()
            return result

    async def _principal(self) -> str:
        from wax.authority.seed import (
            DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
            ensure_principal_role,
        )
        from wax.identity.repository import PrincipalRepository

        async with db_session() as session:
            p = await PrincipalRepository(session).create_principal(display_name="Link User")
            await ensure_principal_role(session, p.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
            await session.commit()
            return p.id

    async def test_store_with_links_creates_edges(self, fresh_db, services) -> None:
        from wax.memory.contracts import MemoryLinkKind

        principal_id = await self._principal()
        async with db_session() as session:
            repo = MemoryRepository(session)
            existing = await repo.create(_memory(principal_id, "earlier claim"))
            await session.commit()
        existing_id = existing.id

        result = await self._invoke(
            services,
            principal_id,
            "memory.store",
            {
                "content": {"text": "supporting detail"},
                "summary": "supporting detail",
                "importance": 0.8,
                "links": [{"memory_id": existing_id, "kind": "supports"}],
            },
        )
        assert result.outcome == "success", result.error
        assert result.outputs["linked"][0]["memory_id"] == existing_id
        assert result.outputs["linked"][0]["kind"] == "supports"

        async with db_session() as session:
            repo = MemoryRepository(session)
            edges = await repo.links_for(existing_id)
            await session.commit()
        assert edges[0][0].kind == MemoryLinkKind.SUPPORTS.value
        assert edges[0][1] == "incoming"

    async def test_store_cross_principal_link_refused(self, fresh_db, services) -> None:
        principal_id = await self._principal()
        async with db_session() as session:
            repo = MemoryRepository(session)
            foreign = await repo.create(_memory("01_otherprincipal00000000", "not mine"))
            await session.commit()
        foreign_id = foreign.id

        result = await self._invoke(
            services,
            principal_id,
            "memory.store",
            {
                "content": {"text": "my memory"},
                "links": [{"memory_id": foreign_id, "kind": "related_to"}],
            },
        )
        assert result.outcome != "success"

    async def test_memory_link_capability_end_to_end(self, fresh_db, services) -> None:
        principal_id = await self._principal()
        async with db_session() as session:
            repo = MemoryRepository(session)
            a = await repo.create(_memory(principal_id, "observation alpha"))
            b = await repo.create(_memory(principal_id, "observation beta"))
            await session.commit()
        a_id, b_id = a.id, b.id

        result = await self._invoke(
            services,
            principal_id,
            "memory.link",
            {"from_memory_id": a_id, "to_memory_id": b_id, "kind": "contradicts"},
        )
        assert result.outcome == "success", result.error
        assert result.outputs["kind"] == "contradicts"

        # Idempotent relink reports existed.
        again = await self._invoke(
            services,
            principal_id,
            "memory.link",
            {"from_memory_id": a_id, "to_memory_id": b_id, "kind": "contradicts"},
        )
        assert again.outcome == "success"
        assert again.outputs["existed"] is True

    async def test_consolidation_writes_derived_from_edges(self, fresh_db, services) -> None:
        principal_id = await self._principal()
        ids = []
        async with db_session() as session:
            repo = MemoryRepository(session)
            for text in ("observed x", "observed y", "observed z"):
                record = await repo.create(_memory(principal_id, text))
                ids.append(record.id)
            await session.commit()

        result = await self._invoke(
            services,
            principal_id,
            "memory.consolidate",
            {
                "source_ids": ids,
                "content": {"text": "consolidated conclusion"},
                "summary": "consolidated conclusion",
            },
        )
        assert result.outcome == "success", result.error
        consolidated_id = result.outputs["memory_id"]

        async with db_session() as session:
            repo = MemoryRepository(session)
            edges = await repo.links_for(consolidated_id, kind="derived_from")
            await session.commit()

        linked_ids = {e.to_memory_id for e, _ in edges}
        assert linked_ids == set(ids), (
            "the consolidation must remain traceable to every source via derived_from edges"
        )
