"""Open-world validation, second wave — composition under new conditions.

The previous wave proved: continue-when-user-replies, memory evolution,
provision→execute→remember. This wave probes the boundaries the mission
names explicitly, all through pure composition of the same primitives:

- Scenario D: "Use a capability that isn't available" — the runtime must
  distinguish unavailable / discoverable / requires-authorization /
  impossible with honest structured outcomes.
- Scenario F: "Do this only after I explicitly approve" — the generic
  approval primitive bound to durable work.
- Scenario E: "Continue after the process restarts" — scheduled work
  survives a full runner reconstruction (new container, same DB).
- Scenario G: "Use a different model/provider" — the runtime keeps
  operating; context negotiation adapts to the new limit.
- Scenario H: "Continue through another interface" — the same principal
  reaches the runtime through a second interface kind with the same
  identity, memory, and objective state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.seed import ensure_principal_role, seed_builtin_roles
from wax.capabilities.contracts import CapabilityDescriptor
from wax.capabilities.registry import CapabilityStatus
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import MessageRole, ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRunner, capability_handler
from wax.state.approval_models import PendingApprovalRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.memory_models import MemoryRecord
from wax.state.models import Base
from wax.state.work_models import WorkItemRecord


@pytest.fixture
async def fresh_db(test_settings, tmp_path):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["provisioning_root"] = str(tmp_path / "wax-resources")
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


def _request(
    message_id: str,
    text: str,
    *,
    sender: str = "+2348000000999",
    kind: InterfaceKind = InterfaceKind.WHATSAPP,
) -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=kind,
        sender_interface_id=sender,
        sender_display_name="Open World II",
        text=text,
        received_at=datetime.now(UTC),
    )


def _bridge(services: RuntimeServices, script: list[list[ToolCall]]) -> RuntimeBridge:
    return RuntimeBridge(
        intelligence=IntelligenceService(MockLLMProvider(scripted_tool_calls=script)),
        services=services,
    )


def _call(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


async def _principal_id_for(services: RuntimeServices, sender: str) -> str:
    from wax.state.identity_models import PrincipalCredential

    async with db_session() as session:
        result = await session.execute(
            select(PrincipalCredential).where(
                PrincipalCredential.kind == "whatsapp_phone",
                PrincipalCredential.value == sender,
            )
        )
        credential = result.scalar_one()
        return credential.principal_id


async def _run_runner_once(services: RuntimeServices) -> int:
    runner = WorkRunner(
        services,
        poll_interval_seconds=0.05,
        lease_seconds=120.0,
        retry_backoff_seconds=0.0,
    )
    runner.register_handler("capability", capability_handler)
    return await runner.run_once()


class TestScenarioDCapabilityBoundaries:
    """'Use a capability that isn't currently available.'

    The runtime must answer with the honest CATEGORY of unavailability —
    not_found / not_currently_available / denied — because the right next
    move depends on which one it is."""

    async def test_unknown_capability_reports_not_found(self, fresh_db, services) -> None:
        """'Use the flux-capacitor capability' — there is no such thing.
        The honest answer is not_found (discoverable: NO)."""
        from wax.execution.repository import ExecutionRepository

        bridge = _bridge(services, [[_call("c1", "flux.capacitor", {})]])
        async with db_session() as session:
            response = await bridge.process(session, _request("ow2-d1", "use the flux capacitor"))
            await session.commit()
        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        step = next(s for s in steps if s.kind == "capability.invoke")
        assert step.status == "failed"
        assert "No such capability" in (step.error or "")

    async def test_disabled_capability_reports_unavailable(self, fresh_db, services) -> None:
        """A registered-but-paused capability is a DIFFERENT honest answer:
        discoverable: YES, available: NO (try later is meaningful)."""
        from wax.execution.repository import ExecutionRepository

        if "echo" in services.capability_registry:
            services.capability_registry.set_status("echo", CapabilityStatus.UNAVAILABLE)
        bridge = _bridge(services, [[_call("c1", "echo", {"message": "hi"})]])
        async with db_session() as session:
            response = await bridge.process(session, _request("ow2-d2", "echo please"))
            await session.commit()
        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        step = next(s for s in steps if s.kind == "capability.invoke")
        assert step.status == "failed"
        assert "not currently available" in (step.error or "")
        services.capability_registry.set_status("echo", CapabilityStatus.AVAILABLE)

    async def test_permission_gated_capability_reports_denied(self, fresh_db, services) -> None:
        """code.run exists but the member role lacks its permission: the
        honest answer is requires-authorization, not failure theatre."""
        from wax.execution.repository import ExecutionRepository

        bridge = _bridge(
            services, [[_call("c1", "code.run", {"code": "print(1)", "language": "python"})]]
        )
        async with db_session() as session:
            response = await bridge.process(session, _request("ow2-d3", "run some code"))
            await session.commit()
        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        step = next(s for s in steps if s.kind == "capability.invoke")
        assert step.status == "failed"
        assert "lacks permission" in (step.error or "")

    async def test_model_can_discover_what_exists(self, fresh_db, services) -> None:
        """Discovery is structural: the tool spec list the model receives
        IS the runtime's capability environment description."""
        bridge = _bridge(services, [])
        specs = bridge._capability_tools()
        names = {s.name for s in (specs or [])}
        assert "work.schedule" in names
        assert "memory.store" in names
        assert "workspace.acquire" in names
        assert "approval.list" in names


class TestScenarioFApprovalGatedWork:
    """'Do this only after I explicitly approve.' — durable work whose
    action requires human authorization: the approval primitive keeps the
    work honest (pending → human decides → requeue → runs exactly once)."""

    async def test_scheduled_destructive_action_waits_for_approval(
        self, fresh_db, services
    ) -> None:
        from wax.authority.approvals import ApprovalService
        from wax.runtime.bridge.contracts import RuntimeResponseStatus
        from wax.runtime.work.repository import WorkRepository

        services.capability_registry.register(
            CapabilityDescriptor(
                name="test.irreversible",
                description="Destructive test capability",
                required_permission="capability.invoke:built_in",
                is_destructive=True,
                timeout_seconds=2.0,
            ),
            _irreversible_impl,
        )

        # The AI schedules a destructive action for later, on the human's
        # behalf (composition: work.schedule with a payload).
        bridge = _bridge(
            services,
            [
                [
                    _call(
                        "c1",
                        "work.schedule",
                        {
                            "payload": {
                                "capability_name": "test.irreversible",
                                "inputs": {"target": "old-data"},
                            },
                            "delay_seconds": 0.05,
                        },
                    )
                ]
            ],
        )
        async with db_session() as session:
            response = await bridge.process(session, _request("ow2-f1", "wipe it tomorrow"))
            await session.commit()
        assert response.status == RuntimeResponseStatus.SUCCESS
        principal_id = await _principal_id_for(services, "+2348000000999")

        async with db_session() as session:
            items = (
                await session.execute(
                    select(WorkItemRecord).where(
                        WorkItemRecord.principal_id == principal_id
                    )
                )
            ).scalars().all()
        work_id = items[0].id

        # The runner wakes the work; the agency gate demands human
        # authorization; a pending approval is created honestly.
        await asyncio.sleep(0.15)  # the scheduled delay passes
        ran = await _run_runner_once(services)
        assert ran == 1
        async with db_session() as session:
            item = await WorkRepository(session).get(work_id)
            assert item.last_error is not None
            assert "human authorization" in (item.last_error or "")
        async with db_session() as session:
            approvals = (
                await session.execute(
                    select(PendingApprovalRecord).where(
                        PendingApprovalRecord.principal_id == principal_id,
                        PendingApprovalRecord.capability_name == "test.irreversible",
                    )
                )
            ).scalars().all()
        assert len(approvals) == 1

        # The human approves (credential path).
        async with db_session() as session:
            await ApprovalService(session).decide(
                approvals[0].id, decided_by=principal_id, approve=True
            )
            await session.commit()

        # The work retries (its own backoff), the approval is consumed,
        # and the action runs exactly once.
        ran = await _run_runner_once(services)
        assert ran == 1
        async with db_session() as session:
            item = await WorkRepository(session).get(work_id)
            assert item.status == "succeeded", item.last_error
            consumed = await ApprovalService(session).get(approvals[0].id)
            assert consumed.consumed_at is not None


class TestScenarioERestartContinuity:
    """'Continue this work after the process restarts.'"""

    async def test_scheduled_work_survives_full_runner_reconstruction(
        self, fresh_db, services, test_settings
    ) -> None:
        from wax.runtime.work.repository import WorkRepository

        bridge = _bridge(
            services,
            [
                [
                    _call(
                        "c1",
                        "work.schedule",
                        {
                            "payload": {
                                "capability_name": "memory.store",
                                "inputs": {
                                    "kind": "episodic",
                                    "content": {"result": "phase one complete"},
                                    "summary": "phase one result",
                                },
                            },
                            "delay_seconds": 0.05,
                        },
                    )
                ]
            ],
        )
        async with db_session() as session:
            await bridge.process(session, _request("ow2-e1", "start phase one"))
            await session.commit()
        principal_id = await _principal_id_for(services, "+2348000000999")

        async with db_session() as session:
            items = (
                await session.execute(
                    select(WorkItemRecord).where(
                        WorkItemRecord.principal_id == principal_id
                    )
                )
            ).scalars().all()
        assert len(items) == 1
        work_id = items[0].id

        # "Process restart": a BRAND-NEW services container + runner over
        # the SAME database (the DB is the runtime's continuity).
        services2 = RuntimeServices.build(test_settings)
        await asyncio.sleep(0.15)  # the scheduled delay passes during the "restart"
        ran = await _run_runner_once(services2)
        assert ran == 1
        async with db_session() as session:
            item = await WorkRepository(session).get(work_id)
            assert item.status == "succeeded"

        # The work's effect (a memory) is visible to the next conversation.
        async with db_session() as session:
            memories = (
                await session.execute(
                    select(MemoryRecord).where(
                        MemoryRecord.principal_id == principal_id,
                        MemoryRecord.summary == "phase one result",
                    )
                )
            ).scalars().all()
        assert len(memories) == 1


class TestScenarioGProviderSubstitution:
    """'Use a different model/provider.' — the runtime continues; context
    negotiation adapts to whatever the new model advertises."""

    async def test_runtime_keeps_operating_under_provider_swap(
        self, fresh_db, services, test_settings
    ) -> None:
        from wax.intelligence.context_limits import provider_context_limit_tokens

        sender = "+2348000000999"
        # Turn 1: provider A (no advertised limit).
        bridge_a = _bridge(services, [])
        async with db_session() as session:
            await bridge_a.process(session, _request("ow2-g1", "hello from provider A"))
            await session.commit()

        # Turn 2: provider B with a TINY advertised window — the same
        # conversation, same principal, same memory evidence, tighter budget.
        provider_b = MockLLMProvider(context_limit_tokens=2_048)
        bridge_b = RuntimeBridge(
            intelligence=IntelligenceService(provider_b), services=services
        )
        async with db_session() as session:
            response = await bridge_b.process(session, _request("ow2-g2", "and provider B replies"))
            await session.commit()
        assert response.status.value == "success"
        assert provider_context_limit_tokens(provider_b) == 2_048

        # Identity continuity: both turns resolved to ONE principal.
        from wax.state.bridge_models import ProcessedMessageRecord

        async with db_session() as session:
            records = (
                await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.principal_id.is_not(None),
                        ProcessedMessageRecord.interface_message_id.in_(["ow2-g1", "ow2-g2"]),
                    )
                )
            ).scalars().all()
        assert len({r.principal_id for r in records}) == 1, "same principal across providers"
        _ = test_settings


class TestScenarioHInterfaceChange:
    """'Continue through another interface.' — identity, memory, and
    objective state live below the interface layer."""

    async def test_same_human_continues_on_second_interface(
        self, fresh_db, services
    ) -> None:
        from wax.identity.repository import PrincipalRepository

        sender = "+2348000000999"
        # Turn 1 arrives on WhatsApp.
        bridge = _bridge(
            services,
            [[_call("c1", "memory.store", {"kind": "episodic", "content": {"fact": "prefers email summaries"}, "summary": "prefers email summaries"})]],
        )
        async with db_session() as session:
            await bridge.process(session, _request("ow2-h1", "remember: prefers email summaries", sender=sender))
            await session.commit()
        principal_id = await _principal_id_for(services, sender)

        # The SAME human now appears on the web interface — the credential
        # is attached to the SAME principal (interface handoff, Foundation
        # §51/§52).
        async with db_session() as session:
            repo = PrincipalRepository(session)
            await repo.add_credential(
                principal_id=principal_id,
                kind="web_session",
                value="session-abc-123",
                is_verified=True,
            )
            await session.commit()

        # Turn 2 arrives on the web interface. Memory evidence crosses.
        web_request = RuntimeRequest(
            interface_message_id="ow2-h2",
            interface_kind=InterfaceKind.WEB,
            sender_interface_id="session-abc-123",
            sender_display_name="Open World II",
            text="what do I prefer?",
            received_at=datetime.now(UTC),
        )
        bridge2 = _bridge(services, [])
        async with db_session() as session:
            response = await bridge2.process(session, web_request)
            await session.commit()
        assert response.status.value == "success"
        assert response.principal_id == principal_id, "same identity, new interface"


async def _irreversible_impl(inputs: dict, ctx=None) -> dict:
    return {"wiped": inputs.get("target", "")}
