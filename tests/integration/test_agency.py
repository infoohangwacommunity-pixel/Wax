"""Tests for Phase N (Agency)."""

from __future__ import annotations

import pytest

from wax.agency.contracts import (
    AgencyDecision,
    AgencyDecisionKind,
    AgencyPolicy,
    ApprovalLevel,
)
from wax.agency.service import AgencyService
from wax.authority.service import AuthorizationService
from wax.identity.repository import PrincipalRepository
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
        p = await repo.create_principal()
        await session.commit()
        return p.id


class TestAgencyPolicy:
    def test_default_policy_has_all_decision_kinds(self) -> None:
        policy = AgencyPolicy.default()
        for kind in AgencyDecisionKind:
            assert kind.value in policy.levels, f"Missing {kind}"

    def test_read_memory_is_none(self) -> None:
        policy = AgencyPolicy.default()
        assert policy.level_for(AgencyDecisionKind.READ_MEMORY) == ApprovalLevel.NONE

    def test_destructive_is_irreversible(self) -> None:
        policy = AgencyPolicy.default()
        assert policy.level_for(AgencyDecisionKind.DESTRUCTIVE_ACTION) == ApprovalLevel.IRREVERSIBLE

    def test_send_message_requires_human(self) -> None:
        policy = AgencyPolicy.default()
        level = policy.level_for(AgencyDecisionKind.SEND_MESSAGE)
        assert level == ApprovalLevel.EXTERNALLY_VISIBLE


class TestAgencyService:
    async def test_read_memory_auto_approved(self, principal_id) -> None:
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = AgencyService(session, auth)
            verdict = await svc.evaluate(
                AgencyDecision(
                    principal_id=principal_id,
                    kind=AgencyDecisionKind.READ_MEMORY,
                    description="Read my recent memories",
                )
            )
            await session.commit()

        assert verdict.approved
        assert not verdict.requires_human_approval
        assert verdict.level == ApprovalLevel.NONE

    async def test_write_memory_auto_approved_informational(self, principal_id) -> None:
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = AgencyService(session, auth)
            verdict = await svc.evaluate(
                AgencyDecision(
                    principal_id=principal_id,
                    kind=AgencyDecisionKind.WRITE_MEMORY,
                    description="Save what we discussed",
                )
            )
            await session.commit()

        assert verdict.approved
        assert not verdict.requires_human_approval
        assert verdict.level == ApprovalLevel.INFORMATIONAL

    async def test_send_message_requires_human_approval(self, principal_id) -> None:
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = AgencyService(session, auth)
            verdict = await svc.evaluate(
                AgencyDecision(
                    principal_id=principal_id,
                    kind=AgencyDecisionKind.SEND_MESSAGE,
                    description="Send a WhatsApp message to my friend",
                )
            )
            await session.commit()

        assert not verdict.approved
        assert verdict.requires_human_approval
        assert verdict.level == ApprovalLevel.EXTERNALLY_VISIBLE

    async def test_destructive_action_requires_human(self, principal_id) -> None:
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = AgencyService(session, auth)
            verdict = await svc.evaluate(
                AgencyDecision(
                    principal_id=principal_id,
                    kind=AgencyDecisionKind.DESTRUCTIVE_ACTION,
                    description="Delete all my memories",
                )
            )
            await session.commit()

        assert not verdict.approved
        assert verdict.requires_human_approval
        assert verdict.level == ApprovalLevel.IRREVERSIBLE

    async def test_every_decision_audited(self, principal_id) -> None:
        """Every agency decision must produce an audit record (INV-06)."""
        async with db_session() as session:
            auth = AuthorizationService(session)
            svc = AgencyService(session, auth)
            await svc.evaluate(
                AgencyDecision(
                    principal_id=principal_id,
                    kind=AgencyDecisionKind.READ_MEMORY,
                    description="test read",
                )
            )
            await session.commit()

        async with db_session() as session:
            from sqlalchemy import select

            from wax.state.audit_models import AuditEvent

            result = await session.execute(
                select(AuditEvent)
                .where(AuditEvent.actor_principal_id == principal_id)
                .where(AuditEvent.event_kind == "agency.decision")
            )
            events = list(result.scalars().all())
            assert len(events) >= 1
