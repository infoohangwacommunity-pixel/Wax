"""Integration tests for Phase L (Objective)."""

from __future__ import annotations

import pytest

from wax.authority.service import AuthorizationService
from wax.core.exceptions import WaxNotFoundError, WaxPermissionDeniedError
from wax.identity.repository import PrincipalRepository
from wax.objective.contracts import ObjectiveCreate, ObjectiveKind, ObjectiveStatus
from wax.objective.repository import ObjectiveRepository
from wax.objective.service import ObjectiveService
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
        p = await repo.create_principal(display_name="Objective Test User")
        await session.commit()
        return p.id


class TestObjectiveRepository:
    async def test_create_returns_record(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="Help me understand photosynthesis",
                    kind=ObjectiveKind.MULTI_TURN,
                )
            )
            await session.commit()
            assert obj.id is not None
            assert obj.status == ObjectiveStatus.PENDING.value
            assert obj.description == "Help me understand photosynthesis"
            assert obj.kind == "multi_turn"

    async def test_get_returns_created(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id, description="test objective"
                )
            )
            await session.commit()

        async with db_session() as session:
            repo = ObjectiveRepository(session)
            fetched = await repo.get(obj.id)
            assert fetched is not None
            assert fetched.id == obj.id

    async def test_valid_transition(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="test")
            )
            ok = await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ObjectiveRepository(session)
            fetched = await repo.get(obj.id)
            assert fetched.status == ObjectiveStatus.IN_PROGRESS.value

    async def test_invalid_transition_rejected(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="test")
            )
            # pending → succeeded is invalid (must go through in_progress first)
            ok = await repo.transition(obj.id, ObjectiveStatus.SUCCEEDED)
            await session.commit()
            assert not ok

    async def test_terminal_states_are_terminal(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="test")
            )
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await repo.transition(obj.id, ObjectiveStatus.SUCCEEDED)
            # Succeeded is terminal — should not transition to anything
            ok = await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await session.commit()
            assert not ok

    async def test_attach_execution(self, principal_id) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="test")
            )
            ok = await repo.attach_execution(obj.id, "01HXY" + "0" * 21)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ObjectiveRepository(session)
            fetched = await repo.get(obj.id)
            assert fetched.execution_id == "01HXY" + "0" * 21


class TestObjectiveService:
    async def test_create_for_principal(self, principal_id) -> None:
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = ObjectiveService(session, auth)
            obj = await svc.create_for_principal(
                principal_id,
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="help me plan a trip",
                ),
            )
            await session.commit()
            assert obj.principal_id == principal_id

    async def test_create_for_different_principal_denied(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            p1 = await repo.create_principal()
            p2 = await repo.create_principal()
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = ObjectiveService(session, auth)
            with pytest.raises(WaxPermissionDeniedError):
                await svc.create_for_principal(
                    p1.id,
                    ObjectiveCreate(
                        principal_id=p2.id,
                        description="attempted cross-principal objective",
                    ),
                )

    async def test_get_for_principal_hides_other_principals(
        self, fresh_db
    ) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            p1 = await repo.create_principal()
            p2 = await repo.create_principal()
            auth = AuthorizationService(session)
            svc = ObjectiveService(session, auth)
            obj = await svc.create_for_principal(
                p1.id,
                ObjectiveCreate(principal_id=p1.id, description="p1 secret objective"),
            )
            await session.commit()
            obj_id = obj.id

        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = ObjectiveService(session, auth)
            with pytest.raises(WaxNotFoundError):
                await svc.get_for_principal(p2.id, obj_id)


class TestObjectiveIsOpenWorld:
    """INV-08: Unknown legitimate objectives must not require modifying
    the universal ontology. The Objective model is generic — it accepts
    ANY description without domain-specific kinds."""

    async def test_objective_accepts_arbitrary_description(self, principal_id) -> None:
        """The runtime accepts any objective description without parsing for
        keywords or requiring a domain-specific 'kind'."""
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            # Education-flavored objective
            await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="Help me understand photosynthesis for my WAEC exam",
                )
            )
            # Business-flavored objective
            await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="Research competitors in the Lagos food delivery market",
                )
            )
            # Coding-flavored objective
            await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="Build a Python CLI tool that converts CSV to JSON",
                )
            )
            # Completely unexpected objective
            await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="Find me a reputable piano teacher in my area",
                )
            )
            await session.commit()

        async with db_session() as session:
            repo = ObjectiveRepository(session)
            objectives = await repo.list_for_principal(principal_id)
            assert len(objectives) == 4
            # All should be pending — none should be rejected or require a
            # new 'kind' to be added.
            for o in objectives:
                assert o.status == ObjectiveStatus.PENDING.value
