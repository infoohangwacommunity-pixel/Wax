"""Shared approval gate + delivery-honesty tests (OMEGA §58 fixes).

The four tracked audit items, as code-level guarantees:

CV-11 — approval creation is idempotent by DATABASE constraint: at most
one PENDING approval per (principal, request fingerprint); the race
loser returns the winner's row instead of multiplying authority rows.

CV-12 — ONE approval-gate component (wax.authority.gate.ApprovalGate)
serves both the live bridge and the durable-work handler; the work path
behaves exactly like the bridge path (same evidence syncs, same ledger
announcement, same notification).

CV-14 — an approval created by background work REACHES the human: via a
live delivery channel when one is attached, otherwise as durable retry
state (delivery_records) the maintenance loop retries.

CV-13 — a message.send transport failure becomes recoverable delivery
state (ADR-0021), not a dead letter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.approvals import ApprovalService, fingerprint_request
from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import (
    CapabilityDescriptor,
    CapabilityInvocationRequest,
)
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.runtime.work.handlers import capability_handler
from wax.runtime.work.runner import WorkExecutionError
from wax.state.approval_models import PendingApprovalRecord
from wax.state.delivery_models import DeliveryRecord
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


async def _make_principal_with_credential(value: str = "+15550001") -> str:
    async with db_session() as session:
        from wax.authority.seed import ensure_principal_role

        repo = PrincipalRepository(session)
        principal = await repo.create_principal(display_name="Human")
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=value, is_verified=True
        )
        await ensure_principal_role(session, principal.id, "member")
        await session.commit()
        return principal.id


async def _noop_impl(inputs: dict, ctx=None) -> dict:
    return {"done": True}


# ---------------------------------------------------------------------------
# CV-11 — the database enforces pending-approval idempotency
# ---------------------------------------------------------------------------


class TestApprovalCreationIdempotency:
    async def test_partial_unique_index_blocks_two_pending(self, fresh_db) -> None:
        """Two concurrent gate passes must not produce two pending rows."""
        from sqlalchemy.exc import IntegrityError

        fp = fingerprint_request("p1", "test.wipe", {"x": 1})
        async with db_session() as session:
            session.add(
                PendingApprovalRecord(
                    id="a1",
                    principal_id="p1",
                    capability_name="test.wipe",
                    action_kind="destructive",
                    request_fingerprint=fp,
                    status="pending",
                    requested_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.flush()
            session.add(
                PendingApprovalRecord(
                    id="a2",
                    principal_id="p1",
                    capability_name="test.wipe",
                    action_kind="destructive",
                    request_fingerprint=fp,
                    status="pending",
                    requested_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_partial_unique_index_allows_terminal_history(self, fresh_db) -> None:
        """A consumed approval does not block a fresh request for the same
        action — only ONE pending may exist at a time."""
        fp = fingerprint_request("p1", "test.wipe", {"x": 1})
        async with db_session() as session:
            session.add(
                PendingApprovalRecord(
                    id="a1",
                    principal_id="p1",
                    capability_name="test.wipe",
                    action_kind="destructive",
                    request_fingerprint=fp,
                    status="approved",
                    requested_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    decided_at=datetime.now(UTC),
                    consumed_at=datetime.now(UTC),
                )
            )
            session.add(
                PendingApprovalRecord(
                    id="a2",
                    principal_id="p1",
                    capability_name="test.wipe",
                    action_kind="destructive",
                    request_fingerprint=fp,
                    status="pending",
                    requested_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.flush()  # no IntegrityError

    async def test_race_loser_returns_winner_row(self, fresh_db, monkeypatch) -> None:
        """The savepoint path: when the pre-insert check races (returns
        None although a pending row exists), the unique index decides and
        create_or_get_pending returns the winner — created=False."""
        fp = fingerprint_request("p1", "test.wipe", {"x": 1})
        async with db_session() as session:
            winner = PendingApprovalRecord(
                id="winner",
                principal_id="p1",
                capability_name="test.wipe",
                action_kind="destructive",
                request_fingerprint=fp,
                status="pending",
                requested_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(winner)
            await session.commit()

            service = ApprovalService(session)
            real_find = service.find_pending
            calls = {"n": 0}

            async def racing_find(principal_id, fingerprint):
                # First call (pre-insert check): pretend the row is not
                # visible yet — the concurrent gate pass is mid-commit.
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                return await real_find(principal_id, fingerprint)

            monkeypatch.setattr(service, "find_pending", racing_find)
            record, created = await service.create_or_get_pending(
                principal_id="p1",
                capability_name="test.wipe",
                action_kind="destructive",
                inputs={"x": 1},
                requested_by_execution_id=None,
            )
            assert created is False
            assert record.id == "winner"
            assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# CV-12 / CV-14 — the work path notifies through the SAME gate
# ---------------------------------------------------------------------------


def _destructive_capability(services: RuntimeServices) -> None:
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


def _work_item(principal_id: str) -> object:
    from wax.state.work_models import WorkItemRecord

    return WorkItemRecord(
        id="w1",
        kind="capability",
        principal_id=principal_id,
        execution_id=None,
        payload={
            "capability_name": "test.wipe",
            "inputs": {"target": "scratch"},
        },
        wake_at=datetime.now(UTC),
    )


class TestSharedGateNotification:
    async def test_work_created_approval_notifies_human(self, fresh_db, services) -> None:
        """CV-14: an approval created by durable work REACHES the human
        through the delivery router — exactly like the bridge path."""
        _destructive_capability(services)
        sent: list[tuple[str, str]] = []

        def _sender(recipient: str, text: str) -> dict:
            sent.append((recipient, text))
            return {"ok": True}

        services.delivery.register("whatsapp", _sender)
        principal_id = await _make_principal_with_credential()

        with pytest.raises(WorkExecutionError) as err:
            await capability_handler(services, _work_item(principal_id))
        assert "requires explicit human authorization" in str(err.value)
        # The notification went out with the generic decision grammar.
        assert len(sent) == 1
        recipient, text = sent[0]
        assert recipient == "+15550001"
        assert "/approve" in text and "/deny" in text
        # Exactly one pending approval exists.
        async with db_session() as session:
            rows = (
                (await session.execute(select(PendingApprovalRecord))).scalars().all()
            )
            assert len(rows) == 1
            assert rows[0].status == "pending"

    async def test_offline_notification_becomes_retry_state(self, fresh_db, services) -> None:
        """CV-14: no live channel → the notification lands on the
        DeliveryQueue as durable retry state, not a silent log line."""
        _destructive_capability(services)
        principal_id = await _make_principal_with_credential()

        with pytest.raises(WorkExecutionError):
            await capability_handler(services, _work_item(principal_id))

        async with db_session() as session:
            rows = (
                (await session.execute(select(DeliveryRecord))).scalars().all()
            )
            notifications = [r for r in rows if r.source == "approval_notification"]
            assert len(notifications) == 1
            record = notifications[0]
            assert record.status == "pending"
            assert record.interface_kind == "whatsapp"
            assert record.recipient_id == "+15550001"
            assert "/approve" in record.text

    async def test_pending_approval_is_not_duplicated_by_second_pass(
        self, fresh_db, services
    ) -> None:
        """CV-12: the work path's idempotency matches the bridge path —
        a second pass over the same request returns the SAME approval and
        does not multiply rows or notifications."""
        _destructive_capability(services)
        sent: list[tuple[str, str]] = []
        services.delivery.register("whatsapp", lambda r, t: _record(sent, r, t))
        principal_id = await _make_principal_with_credential()

        for _ in range(2):
            with pytest.raises(WorkExecutionError):
                await capability_handler(services, _work_item(principal_id))

        assert len(sent) == 1  # one human notification, not two
        async with db_session() as session:
            rows = (
                (await session.execute(select(PendingApprovalRecord))).scalars().all()
            )
            assert len(rows) == 1


async def _record(sent: list, recipient: str, text: str) -> dict:
    sent.append((recipient, text))
    return {"ok": True}


# ---------------------------------------------------------------------------
# CV-13 — message.send failures are recoverable delivery state
# ---------------------------------------------------------------------------


class TestMessageSendRecoverable:
    async def test_failed_send_enqueues_delivery_record(self, fresh_db, services) -> None:
        principal_id = await _make_principal_with_credential()

        async def _always_fails(recipient: str, text: str) -> dict:
            raise RuntimeError("provider unreachable")

        services.delivery.register("whatsapp", _always_fails)

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="message.send",
                    principal_id=principal_id,
                    inputs={"recipient_id": "+15550001", "text": "here is the report"},
                    request_id="t1",
                )
            )
            assert result.outcome == "success"  # the runtime accepted the debt
            assert result.outputs["sent"] is False
            assert result.outputs["queued_for_retry"] is True
            delivery_id = result.outputs["delivery_id"]

        async with db_session() as session:
            record = await session.get(DeliveryRecord, delivery_id)
            assert record is not None
            assert record.source == "capability:message.send"
            assert record.status == "pending"
            assert record.attempts == 1  # the failed send counted honestly
            assert record.execution_id == result.execution_id
            assert "RuntimeError" in (record.last_error or "")

    async def test_successful_send_writes_no_delivery_record(self, fresh_db, services) -> None:
        principal_id = await _make_principal_with_credential()

        async def _works(recipient: str, text: str) -> dict:
            return {"ok": True}

        services.delivery.register("whatsapp", _works)

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="message.send",
                    principal_id=principal_id,
                    inputs={"recipient_id": "+15550001", "text": "hello"},
                    request_id="t2",
                )
            )
            assert result.outcome == "success"
            assert result.outputs["sent"] is True

        async with db_session() as session:
            rows = (
                (await session.execute(select(DeliveryRecord))).scalars().all()
            )
            assert rows == []


# ---------------------------------------------------------------------------
# Idempotency machine — in-flight redelivery is a duplicate
# ---------------------------------------------------------------------------


class TestInflightRedelivery:
    async def test_redelivery_of_inflight_message_is_duplicate(
        self, fresh_db, services
    ) -> None:
        """A redelivery that arrives while the first attempt is still
        running must observe DUPLICATE — never adopt or corrupt the
        in-flight record."""
        from wax.runtime.bridge.contracts import (
            InterfaceKind,
            RuntimeRequest,
            RuntimeResponseStatus,
        )
        from wax.runtime.bridge.service import RuntimeBridge
        from wax.state.bridge_models import ProcessedMessageRecord

        bridge = RuntimeBridge(
            intelligence=None, services=services, max_response_chars=1000
        )
        request = RuntimeRequest(
            interface_message_id="wamid.INFLIGHT",
            interface_kind=InterfaceKind.WHATSAPP,
            sender_interface_id="+15550001",
            text="hello",
            received_at=datetime.now(UTC),
        )
        async with db_session() as session:
            session.add(
                ProcessedMessageRecord(
                    id="rec1",
                    interface_kind="whatsapp",
                    interface_message_id="wamid.INFLIGHT",
                    principal_id="p1",
                    outcome="pending",
                    request_text="hello",
                    received_at=datetime.now(UTC),
                )
            )
            await session.commit()

            response = await bridge.process(session, request)
            assert response.status is RuntimeResponseStatus.DUPLICATE
            assert response.duplicate_of_execution_id is None

            # The in-flight record is untouched — still pending, not
            # flipped to failed by the observer.
            record = await session.get(ProcessedMessageRecord, "rec1")
            assert record.outcome == "pending"
