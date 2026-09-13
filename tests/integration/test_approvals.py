"""Human-approval primitive tests (the generic authority boundary).

The primitive: AI requests action → runtime requires human authorization →
pending approval (idempotent, expiring, fingerprint-bound) → human decides
through their own authority path → approved/denied/expired → the approved
action may be attempted EXACTLY ONCE.

Security properties under adversarial test:
- the AI can never decide (no approve capability; decisions authenticate
  the human via the interface credential path)
- replay is impossible (one-time consumption)
- scope creep is impossible (fingerprint binds capability + inputs)
- wrong principals cannot decide (ownership enforced)
- stale approvals expire (honest lifecycle)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wax.authority.approvals import (
    ApprovalDecisionError,
    ApprovalService,
    fingerprint_request,
)
from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityDescriptor
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.state.approval_models import PendingApprovalRecord
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


async def _noop_impl(inputs: dict, ctx=None) -> dict:
    return {"wiped": True}


def _destructive_bridge(services: RuntimeServices) -> RuntimeBridge:
    """A bridge whose scripted model always requests a destructive action."""
    if "test.wipe" not in services.capability_registry:
        services.capability_registry.register(
            CapabilityDescriptor(
                name="test.wipe",
                description="Destructive test capability",
                required_permission="capability.invoke:built_in",
                is_destructive=True,
                timeout_seconds=2.0,
            ),
            _noop_impl,
        )
    provider = MockLLMProvider(
        scripted_tool_calls=[[ToolCall(id="call_1", name="test.wipe", arguments={"target": "tmp/old"})]]
    )
    return RuntimeBridge(intelligence=IntelligenceService(provider), services=services)


def _request(message_id: str = "msg-approval-1") -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000001",
        sender_display_name="Approval Tester",
        text="please wipe the old data",
        received_at=datetime.now(UTC),
    )


async def _pending_ids() -> list[PendingApprovalRecord]:
    from sqlalchemy import select

    async with db_session() as session:
        result = await session.execute(select(PendingApprovalRecord))
        return list(result.scalars().all())


class TestFingerprint:
    async def test_identical_requests_share_fingerprint(self) -> None:
        a = fingerprint_request("p1", "cap.x", {"a": 1, "b": [2, 3]})
        b = fingerprint_request("p1", "cap.x", {"b": [2, 3], "a": 1})
        assert a == b, "key order must not matter"

    async def test_any_input_change_changes_fingerprint(self) -> None:
        base = fingerprint_request("p1", "cap.x", {"a": 1})
        assert fingerprint_request("p1", "cap.x", {"a": 2}) != base
        assert fingerprint_request("p1", "cap.x", {"a": 1, "b": 2}) != base
        assert fingerprint_request("p2", "cap.x", {"a": 1}) != base, "principal matters"
        assert fingerprint_request("p1", "cap.y", {"a": 1}) != base, "capability matters"


class TestPendingLifecycle:
    async def test_create_is_idempotent(self, fresh_db) -> None:
        async with db_session() as session:
            r1, c1 = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={"a": 1},
                requested_by_execution_id=None,
            )
            r2, c2 = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={"a": 1},
                requested_by_execution_id=None,
            )
            await session.commit()
        assert c1 is True
        assert c2 is False
        assert r1.id == r2.id, "same pending request → same row"

    async def test_decide_requires_ownership(self, fresh_db) -> None:
        async with db_session() as session:
            record, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
            )
            await session.commit()
            with pytest.raises(ApprovalDecisionError):
                await ApprovalService(session).decide(
                    record.id, decided_by="01NOTTHEOWNER", approve=True
                )

    async def test_decide_twice_is_refused(self, fresh_db) -> None:
        async with db_session() as session:
            record, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
            )
            await session.commit()
            await ApprovalService(session).decide(
                record.id, decided_by="01P", approve=True
            )
            await session.commit()
            with pytest.raises(ApprovalDecisionError):
                await ApprovalService(session).decide(
                    record.id, decided_by="01P", approve=True
                )

    async def test_consume_is_one_time(self, fresh_db) -> None:
        async with db_session() as session:
            record, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
            )
            await ApprovalService(session).decide(record.id, decided_by="01P", approve=True)
            await session.commit()
            approval_id = record.id

            ok1 = await ApprovalService(session).consume(approval_id, execution_id="01EA")
            await session.commit()
            ok2 = await ApprovalService(session).consume(approval_id, execution_id="01EB")
            await session.commit()
        assert ok1 is True
        assert ok2 is False, "replay must be refused"

    async def test_expiry_sweep(self, fresh_db) -> None:
        async with db_session() as session:
            dead, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
                expires_in_seconds=-0.01,
            )
            alive, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.y",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
                expires_in_seconds=3600.0,
            )
            expired = await ApprovalService(session).expire_due()
            await session.commit()
        assert len(expired) == 1
        assert expired[0].id == dead.id
        async with db_session() as session:
            alive_record = await ApprovalService(session).get(alive.id)
            assert alive_record.status == "pending"

    async def test_cancel_by_owner_only(self, fresh_db) -> None:
        async with db_session() as session:
            record, _ = await ApprovalService(session).create_or_get_pending(
                principal_id="01P",
                capability_name="cap.x",
                action_kind="destructive",
                inputs={},
                requested_by_execution_id=None,
            )
            await session.commit()
            with pytest.raises(ApprovalDecisionError):
                await ApprovalService(session).cancel(record.id, by_principal_id="01OTHER")
            cancelled = await ApprovalService(session).cancel(
                record.id, by_principal_id="01P"
            )
            await session.commit()
        assert cancelled.status == "cancelled"


class TestBridgeApprovalFlow:
    async def test_full_approve_flow_end_to_end(self, fresh_db, services) -> None:
        """Request → pending → human approves via credential path → the
        SAME request (new execution) succeeds → replay creates new pending."""
        bridge = _destructive_bridge(services)

        # 1. First attempt: pending approval created, nothing executed.
        async with db_session() as session:
            response1 = await bridge.process(session, _request("msg-1"))
            await session.commit()
        assert response1.status.value == "success"
        pendings = await _pending_ids()
        assert len(pendings) == 1
        approval = pendings[0]
        assert approval.status == "pending"
        assert approval.capability_name == "test.wipe"

        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response1.execution_id)
        wipe_steps = [s for s in steps if s.capability_name == "test.wipe"]
        assert wipe_steps[0].status == "failed"
        assert "human approval" in (wipe_steps[0].error or "")
        assert "pending" in (wipe_steps[0].error or "")

        # 2. The human decides through the credential path (same identity
        #    that sent the message).
        async with db_session() as session:
            ok, message = await bridge.submit_approval_decision(
                session,
                interface_kind=InterfaceKind.WHATSAPP,
                sender_interface_id="+2348000000001",
                approval_id=approval.id,
                approve=True,
            )
            await session.commit()
        assert ok is True, message

        # 3. The AI retries the SAME request in a new execution: the
        #    approval is consumed and the action runs exactly once.
        bridge = _destructive_bridge(services)  # fresh scripted provider
        async with db_session() as session:
            response2 = await bridge.process(session, _request("msg-2"))
            await session.commit()
        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response2.execution_id)
        wipe_steps = [s for s in steps if s.capability_name == "test.wipe"]
        assert wipe_steps[0].status == "succeeded", wipe_steps[0].error

        # 4. A THIRD identical attempt: approval was consumed — a fresh
        #    pending is created; nothing silently re-runs.
        bridge = _destructive_bridge(services)
        async with db_session() as session:
            response3 = await bridge.process(session, _request("msg-3"))
            await session.commit()
        pendings = await _pending_ids()
        pending_now = [p for p in pendings if p.status == "pending"]
        assert len(pending_now) == 1, "consumed approval cannot authorize twice"

    async def test_denial_blocks_the_action(self, fresh_db, services) -> None:
        bridge = _destructive_bridge(services)

        async with db_session() as session:
            await bridge.process(session, _request("msg-1"))
            await session.commit()
        approval = (await _pending_ids())[0]

        async with db_session() as session:
            ok, message = await bridge.submit_approval_decision(
                session,
                interface_kind=InterfaceKind.WHATSAPP,
                sender_interface_id="+2348000000001",
                approval_id=approval.id,
                approve=False,
                note="not today",
            )
            await session.commit()
        assert ok is True

        # Retry: no unconsumed approval (denied), pending returned honestly.
        bridge = _destructive_bridge(services)  # fresh scripted provider
        async with db_session() as session:
            response = await bridge.process(session, _request("msg-2"))
            await session.commit()
        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        wipe_steps = [s for s in steps if s.capability_name == "test.wipe"]
        assert wipe_steps[0].status == "failed"
        assert "human approval" in (wipe_steps[0].error or "")

    async def test_unknown_sender_cannot_decide(self, fresh_db, services) -> None:
        bridge = _destructive_bridge(services)
        async with db_session() as session:
            await bridge.process(session, _request("msg-1"))
            await session.commit()
        approval = (await _pending_ids())[0]

        async with db_session() as session:
            ok, message = await bridge.submit_approval_decision(
                session,
                interface_kind=InterfaceKind.WHATSAPP,
                sender_interface_id="+1999999999",  # a stranger
                approval_id=approval.id,
                approve=True,
            )
            await session.commit()
        assert ok is False
        assert "unknown sender" in message

    async def test_match_command_requires_known_sender(self, fresh_db, services) -> None:
        bridge = _destructive_bridge(services)
        request = RuntimeRequest(
            interface_message_id="msg-cmd-1",
            interface_kind=InterfaceKind.WHATSAPP,
            sender_interface_id="+1999999999",
            sender_display_name="Stranger",
            text="/approve 01SOMEAPPROVAL0000000000",
            received_at=datetime.now(UTC),
        )
        async with db_session() as session:
            response = await bridge.match_approval_command(session, request)
            await session.commit()
        assert response is not None
        assert "unknown sender" in response.text.lower()

    async def test_match_command_usage_message(self, fresh_db, services) -> None:
        bridge = _destructive_bridge(services)
        request = _request("msg-cmd-2").model_copy(update={"text": "/approve"})
        async with db_session() as session:
            response = await bridge.match_approval_command(session, request)
            await session.commit()
        assert response is not None
        assert "Usage" in response.text

    async def test_non_command_text_is_not_intercepted(self, fresh_db, services) -> None:
        bridge = _destructive_bridge(services)
        request = _request("msg-cmd-3").model_copy(
            update={"text": "hello there, what can you do?"}
        )
        async with db_session() as session:
            response = await bridge.match_approval_command(session, request)
        assert response is None


class TestApprovalCapabilities:
    async def test_surface_is_list_and_cancel_only(self, fresh_db, services) -> None:
        """The AI may inspect and withdraw its own requests; nothing in the
        capability registry can GRANT an approval."""
        from sqlalchemy import select

        from wax.capabilities.contracts import CapabilityInvocationRequest
        from wax.capabilities.registry import CapabilityRegistry
        from wax.state.bridge_models import ProcessedMessageRecord

        bridge = _destructive_bridge(services)
        async with db_session() as session:
            response = await bridge.process(session, _request("msg-1"))
            await session.commit()

        async with db_session() as session:
            record = (
                await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.execution_id == response.execution_id
                    )
                )
            ).scalar_one()
            principal_id = record.principal_id

        registry: CapabilityRegistry = services.capability_registry
        names = {d.name for d in registry.list_capabilities()}
        assert "approval.list" in names
        assert "approval.cancel" in names
        assert not any(
            n.startswith("approval.") and "approve" in n for n in names
        ), "no capability may grant approval"

        # List works for the owning principal.
        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="approval.list",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await session.commit()
        assert result.outcome == "success"
        assert len(result.outputs["approvals"]) == 1
        assert result.outputs["approvals"][0]["capability"] == "test.wipe"


class TestSignalNamespaceReservation:
    async def test_approval_namespace_is_runtime_owned(self) -> None:
        from wax.runtime.work.signals import SignalNameError, validate_signal_name

        with pytest.raises(SignalNameError):
            validate_signal_name("approval.granted:xyz", allow_reserved=False)
        assert (
            validate_signal_name("approval.requested:p1", allow_reserved=True)
            == "approval.requested:p1"
        )
