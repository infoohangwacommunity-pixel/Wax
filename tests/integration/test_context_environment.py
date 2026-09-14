"""Context Becomes Environment — integration tests (ADR-0037, Phase 4).

Verifies that the ContinuityService.build_context composes the full
environment the AI wakes into:

- interface + current_time + available_capabilities + recent_signals
  + waiting_work_count (new in Phase 4)
- existing: recent_memories, active_work, recent_artifacts, objective
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.continuity.service import ContinuityService
from wax.identity.repository import PrincipalRepository
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work.repository import WorkRepository
from wax.runtime.work.signals import SignalRepository
from wax.state.engine import db_session, dispose_engine, init_engine
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


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


class TestContextEnvironment:
    """Phase 4: context carries environment facts, not just chat history."""

    async def test_context_includes_current_time(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            assert "current_time" in context.environment
            # ISO-8601 with timezone
            assert "T" in context.environment["current_time"]

    async def test_context_includes_interface(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            assert context.environment["interface"] == "whatsapp"

    async def test_context_includes_available_capabilities(self, fresh_db, services):
        """When the session has access to a RuntimeServices container,
        the environment lists available capabilities."""
        principal_id = await _create_principal()

        async with db_session() as s:
            # Attach the services container to the session — the
            # ContinuityService looks for it as `wax_services`.
            s.wax_services = services  # type: ignore[attr-defined]
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            assert "available_capabilities" in context.environment
            cap_names = [c["name"] for c in context.environment["available_capabilities"]]
            # The runtime registers these built-ins + runtime capabilities
            assert "echo" in cap_names
            assert "work.schedule" in cap_names
            assert "memory.store" in cap_names
            assert "message.send" in cap_names

    async def test_context_includes_recent_signals(self, fresh_db, services):
        principal_id = await _create_principal()
        # Emit a signal for this principal
        signal_name = f"interface.message:{principal_id}"
        async with db_session() as s:
            await SignalRepository(s).emit(
                signal_name,
                payload={"interface": "whatsapp", "message_id": "test-1"},
                emitted_by="bridge",
            )
            await s.commit()

        async with db_session() as s:
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            assert "recent_signals" in context.environment
            assert len(context.environment["recent_signals"]) >= 1
            assert context.environment["recent_signals"][0]["name"] == signal_name

    async def test_context_includes_waiting_work_count(self, fresh_db, services):
        principal_id = await _create_principal()
        # Schedule 2 work items (none will be in 'waiting' status initially —
        # they start as 'pending')
        async with db_session() as s:
            repo = WorkRepository(s)
            await repo.schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {}},
                wake_at=datetime.now(UTC) + timedelta(hours=1),
                principal_id=principal_id,
                max_attempts=1,
                wake_kind="time",
            )
            await repo.schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {}},
                wake_at=datetime.now(UTC) + timedelta(hours=2),
                principal_id=principal_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()

        async with db_session() as s:
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            # No items are in 'waiting' status — they are 'pending'
            assert context.environment.get("waiting_work_count", 0) == 0

    async def test_context_degrades_gracefully_without_services(self, fresh_db, services):
        """A session without the services container still builds context —
        the available_capabilities field is just absent."""
        principal_id = await _create_principal()
        async with db_session() as s:
            # Do NOT attach services to the session
            continuity = ContinuityService(s)
            context, _ = await continuity.build_context(principal_id, "whatsapp")
            # The environment still has the basics
            assert context.environment["interface"] == "whatsapp"
            assert "current_time" in context.environment
            # available_capabilities is absent — graceful degradation
            assert "available_capabilities" not in context.environment

    async def test_context_priority_objective_first(self, fresh_db, services):
        """When a conversation has an active objective, the context
        surfaces it (the directive's "Objective" priority item)."""
        from wax.runtime.bridge.contracts import (
            InterfaceKind,
            RuntimeRequest,
        )

        principal_id = await _create_principal()
        # Send a message through the bridge to create an objective
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()),
            services=services,
        )
        request = RuntimeRequest(
            interface_kind=InterfaceKind.WHATSAPP,
            interface_message_id="msg-test-1",
            sender_interface_id="1234567890",
            sender_display_name="Test",
            text="help me with something",
            received_at=datetime.now(UTC),
        )
        async with db_session() as s:
            response = await bridge.process(s, request)
            await s.commit()
            objective_id = response.objective_id

        # Now build context — the objective should be surfaced
        async with db_session() as s:
            continuity = ContinuityService(s)
            context, _conversation_id = await continuity.build_context(
                principal_id, "whatsapp", current_message="continue helping"
            )
            assert context.active_objective_id == objective_id
            assert context.active_objective_description is not None
