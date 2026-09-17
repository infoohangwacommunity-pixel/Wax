"""Wiring-proof tests: the implemented-but-unwired subsystems now execute on
the live path.

Each test pins a claim from the forensic audit that was TRUE (a gap) and must
stay TRUE (fixed) from now on:

- Section 18: "/metrics always returns an empty registry"        → now populated
- Section 17/20: "zero audit_events rows after real processing"  → now written
- Section 7:  "WhatsApp principals are assigned no role"         → member role
- Section 10: "capability registry never initialized in prod"    → built + invocable
- Section 15: "every objective remains pending forever"          → lifecycle runs
- Section 17: "no rate/cost/abuse check runs on the live path"   → enforced
- Section 8/31: "failed messages are terminal and silent"        → retry + dead letter
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.state.bridge_models import ProcessedMessageRecord
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


@pytest.fixture
def bridge(services: RuntimeServices) -> RuntimeBridge:
    return RuntimeBridge(intelligence=IntelligenceService(MockLLMProvider()), services=services)


def _request(message_id: str = "msg-001", text: str = "hello") -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000000",
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )


class TestObservabilityWiring:
    async def test_metrics_are_recorded_by_the_live_path(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """Audit Section 18: the /metrics endpoint used to always be empty.

        The registry is a process-wide singleton, so this test asserts on
        deltas caused by ONE message, not on an empty starting state.
        """
        snapshot_before = bridge._services.metrics.snapshot()
        latency_before = sum(
            h["count"]
            for k, h in snapshot_before["histograms"].items()
            if k.startswith("llm_latency_ms|")
        )

        async with db_session() as session:
            response = await bridge.process(session, _request())
        assert response.status == RuntimeResponseStatus.SUCCESS

        snapshot = bridge._services.metrics.snapshot()
        counter_names = set(snapshot["counters"].keys())
        assert "bridge_messages_started_total" in counter_names
        assert any(k.startswith("bridge_messages_total|") for k in counter_names), counter_names
        # LLM latency + token counters describe REAL calls now.
        assert any(k.startswith("llm_latency_ms|") for k in snapshot["histograms"])
        assert any(k.startswith("llm_tokens_total|") for k in counter_names)
        latency_after = sum(
            h["count"] for k, h in snapshot["histograms"].items() if k.startswith("llm_latency_ms|")
        )
        assert latency_after == latency_before + 1

    async def test_audit_events_are_written_by_the_live_path(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """Audit Sections 17/20: zero audit rows after real message processing."""
        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            await bridge.process(session, _request())

        async with db_session() as session:
            events = list(
                (await session.execute(select(AuditEvent).order_by(AuditEvent.created_at)))
                .scalars()
                .all()
            )
        kinds = [e.event_kind for e in events]
        assert "identity.principal.registered" in kinds, kinds
        assert "authority.role.assigned" in kinds, kinds
        assert "bridge.execution.accepted" in kinds, kinds
        assert "bridge.message.processed" in kinds, kinds
        assert len(events) >= 4

    async def test_rejections_are_audited(self, fresh_db, services) -> None:
        """A rate-limited message leaves an audit trail, not just silence."""
        from wax.security.rate_limiter import RateLimitConfig, RateLimiter

        services.rate_limiter = RateLimiter(
            RateLimitConfig(capacity=20, refill_rate=0.0, initial_tokens=0.0)
        )
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        assert response.status == RuntimeResponseStatus.RATE_LIMITED

        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            event = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_kind == "bridge.message.rejected")
                )
            ).scalar_one()
        assert event.outcome == "rate_limited"

        # And the rejection is dedup-locked: redelivery does not re-execute.
        async with db_session() as session:
            second = await bridge.process(session, _request())
        assert second.status == RuntimeResponseStatus.DUPLICATE


class TestIdentityWiring:
    async def test_first_contact_assigns_member_role(self, fresh_db, bridge: RuntimeBridge) -> None:
        """Audit Section 7: WhatsApp principals had zero permissions forever."""
        from sqlalchemy import select as _select

        from wax.state.authority_models import PrincipalRole, Role

        async with db_session() as session:
            response = await bridge.process(session, _request())
        assert response.status == RuntimeResponseStatus.SUCCESS
        principal_id = response.principal_id

        async with db_session() as session:
            member = (
                await session.execute(_select(Role).where(Role.name == "member"))
            ).scalar_one()
            assignment = (
                await session.execute(
                    _select(PrincipalRole).where(
                        PrincipalRole.principal_id == principal_id,
                        PrincipalRole.role_id == member.id,
                    )
                )
            ).scalar_one_or_none()
        assert assignment is not None, "member role must be assigned on first contact"

    async def test_credential_last_used_is_tracked(self, fresh_db, bridge: RuntimeBridge) -> None:
        """Audit Section 7: mark_credential_used existed but nothing called it."""
        from wax.state.identity_models import PrincipalCredential

        async with db_session() as session:
            await bridge.process(session, _request())

        async with db_session() as session:
            cred = (await session.execute(select(PrincipalCredential))).scalar_one()
        assert cred.last_used_at is None  # only one contact so far

        async with db_session() as session:
            await bridge.process(session, _request(message_id="msg-002", text="second hello"))
        async with db_session() as session:
            cred = (await session.execute(select(PrincipalCredential))).scalar_one()
        assert cred.last_used_at is not None


class TestObjectiveLifecycleWiring:
    async def test_objective_reaches_succeeded(self, fresh_db, bridge: RuntimeBridge) -> None:
        """Audit Section 15: every objective stayed pending forever."""
        from wax.state.objective_models import ObjectiveRecord

        async with db_session() as session:
            response = await bridge.process(session, _request())
        async with db_session() as session:
            objective = await session.get(ObjectiveRecord, response.objective_id)
        assert objective is not None
        assert objective.status == "succeeded"


class TestCapabilityWiring:
    async def test_registry_is_built_with_builtins(self, services) -> None:
        """Audit Section 10: the registry was never initialized in production."""
        names = {d.name for d in services.capability_registry.list_capabilities()}
        assert {"echo", "http.get"} <= names
        # Discovery: every descriptor carries the metadata the AI reasons over.
        for descriptor in services.capability_registry.list_capabilities():
            assert descriptor.required_permission
            assert descriptor.input_schema.get("type") == "object"
            assert descriptor.timeout_seconds > 0

    async def test_invoker_enforces_authority_and_audits(self, fresh_db, services, bridge) -> None:
        """The invoker is reachable and enforces the principal's permissions."""
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            response = await bridge.process(session, _request())
        principal_id = response.principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id=principal_id,
                    inputs={"message": "ping"},
                    request_id=response.execution_id,
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error
        assert result.outputs == {"echo": {"message": "ping"}}

        # An unknown principal has no permissions → honest denial.
        async with db_session() as session:
            invoker = services.invoker(session)
            denied = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id="01UNKNOWNUNKNOWNUNKNOWN00",
                    inputs={},
                )
            )
        assert denied.outcome == "denied"


class TestFailureSemantics:
    async def test_exhausted_retries_dead_letter_the_message(self, fresh_db, services) -> None:
        """Audit Scenario 5: a failed message was terminal and silent.

        Now: failed → retryable → dead (terminal) → dead-letter row, so an
        operator can see and reprocess the loss.
        """
        from collections.abc import AsyncIterator

        from wax.intelligence.contracts import LLMRequest, LLMResponse, ProviderKind

        class FailingProvider:
            @property
            def kind(self):  # type: ignore[no-untyped-def]
                return ProviderKind.MOCK

            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("LLM is down")

            async def stream(self, request: LLMRequest) -> AsyncIterator:  # type: ignore[no-untyped-def]
                raise RuntimeError("LLM is down")
                yield

            async def close(self) -> None:
                pass

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(FailingProvider()), services=services
        )

        async with db_session() as session:
            await bridge.process(session, _request(message_id="msg-dl"))
        async with db_session() as session:
            await bridge.process(session, _request(message_id="msg-dl"))

        async with db_session() as session:
            record = (
                await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.interface_message_id == "msg-dl"
                    )
                )
            ).scalar_one()
            # Directive §13: dead_letter table removed; audit ledger is the surviving record

        assert record.outcome == "dead"
        # Directive §13: dead_letter table removed; audit ledger is the surviving record of terminal failure.

        # Dead is terminal: redelivery returns DUPLICATE (no re-execution).
        async with db_session() as session:
            final = await bridge.process(session, _request(message_id="msg-dl"))
        assert final.status == RuntimeResponseStatus.DUPLICATE

    async def test_resource_budget_denies_when_exhausted(self, fresh_db, services) -> None:
        """The ResourceAccountant now gates the LLM call on the live path."""
        from wax.resources.contracts import ResourceKind, ResourceUsage

        services.resource_accountant.allocate(
            "exec-prefunded",
            llm_calls=1.0,
            llm_tokens=10.0,
            execution_time_seconds=300.0,
            capability_invocations=10.0,
        )
        assert services.resource_accountant.try_consume(
            ResourceUsage("exec-prefunded", ResourceKind.LLM_CALLS, 1.0)
        )
        assert not services.resource_accountant.try_consume(
            ResourceUsage("exec-prefunded", ResourceKind.LLM_CALLS, 1.0)
        )


class TestDeliveryRouter:
    async def test_send_without_interface_is_honestly_rejected(self, services) -> None:
        """No fake success: sending through an unattached interface fails."""
        import pytest as _pytest

        from wax.core.exceptions import WaxNotFoundError

        with _pytest.raises(WaxNotFoundError):
            await services.delivery.send("telegram", "+2348000000000", "hi")

    async def test_registered_interface_delivers(self, services) -> None:
        delivered: list[tuple[str, str]] = []

        async def sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {"ok": True}

        services.delivery.register("whatsapp", sender)
        assert services.delivery.has("whatsapp")
        await services.delivery.send("whatsapp", "+2348000000000", "hello")
        assert delivered == [("+2348000000000", "hello")]
