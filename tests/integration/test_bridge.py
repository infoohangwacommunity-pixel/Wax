"""Tests for Phase R — WhatsApp Runtime Bridge.

Tests verify:
- One user message → one runtime execution (idempotency)
- Duplicate message → DUPLICATE status (no re-execution)
- First contact creates a new Principal + credential
- Returning user resolves to existing Principal
- RuntimeResponse contains execution_id, objective_id, principal_id
- Bridge survives LLM failures (records internal_error, doesn't retry)
- Bridge is interface-agnostic (accepts WhatsApp, web, API, ... kinds)
- Effective text includes extracted media text
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
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
    yield
    await dispose_engine()


@pytest.fixture
def intelligence() -> IntelligenceService:
    return IntelligenceService(MockLLMProvider())


@pytest.fixture
def bridge(intelligence: IntelligenceService) -> RuntimeBridge:
    return RuntimeBridge(intelligence=intelligence)


def _make_request(
    *,
    message_id: str = "msg-001",
    interface: InterfaceKind = InterfaceKind.WHATSAPP,
    sender_id: str = "+2348000000000",
    text: str = "hello from WhatsApp",
    sender_name: str | None = "Test User",
) -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=interface,
        sender_interface_id=sender_id,
        sender_display_name=sender_name,
        text=text,
        received_at=datetime.now(UTC),
    )


class TestBridgeIdempotency:
    """One user message → one runtime execution."""

    async def test_first_message_succeeds(self, fresh_db, bridge: RuntimeBridge) -> None:
        async with db_session() as session:
            response = await bridge.process(session, _make_request())

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert response.execution_id is not None
        assert response.objective_id is not None
        assert response.principal_id is not None
        assert response.text is not None
        assert len(response.text) > 0

    async def test_duplicate_message_returns_duplicate(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """The same interface_message_id must NOT trigger a second execution."""
        request = _make_request(message_id="msg-dup-001")

        async with db_session() as session:
            first = await bridge.process(session, request)

        async with db_session() as session:
            second = await bridge.process(session, request)

        assert first.status == RuntimeResponseStatus.SUCCESS
        assert second.status == RuntimeResponseStatus.DUPLICATE
        # The duplicate response must point to the original execution
        assert second.duplicate_of_execution_id == first.execution_id
        assert second.execution_id == first.execution_id

    async def test_duplicate_does_not_create_second_execution(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """Verify the duplicate path skips objective/execution creation."""
        request = _make_request(message_id="msg-dup-002")

        async with db_session() as session:
            await bridge.process(session, request)

        async with db_session() as session:
            await bridge.process(session, request)  # duplicate

        # Should have exactly ONE processed_message row
        async with db_session() as session:
            result = await session.execute(
                select(ProcessedMessageRecord).where(
                    ProcessedMessageRecord.interface_message_id == "msg-dup-002"
                )
            )
            records = list(result.scalars().all())
        assert len(records) == 1
        assert records[0].outcome == "success"


class TestBridgeIdentityResolution:
    async def test_first_contact_creates_principal(self, fresh_db, bridge: RuntimeBridge) -> None:
        """A brand-new sender should result in a new Principal + credential."""
        async with db_session() as session:
            response = await bridge.process(
                session,
                _make_request(sender_id="+2348999999999", sender_name="Ada"),
            )

        from wax.identity.repository import PrincipalRepository

        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.resolve_principal_by_credential(
                "whatsapp_phone", "+2348999999999"
            )
            assert principal is not None
            assert principal.display_name == "Ada"
            assert principal.id == response.principal_id

    async def test_returning_user_resolves_to_same_principal(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """Same phone number on second message must resolve to same principal."""
        async with db_session() as session:
            first = await bridge.process(
                session,
                _make_request(
                    message_id="msg-a",
                    sender_id="+2348777777777",
                    text="hi",
                ),
            )

        async with db_session() as session:
            second = await bridge.process(
                session,
                _make_request(
                    message_id="msg-b",
                    sender_id="+2348777777777",
                    text="hello again",
                ),
            )

        assert first.principal_id == second.principal_id
        # But executions are distinct
        assert first.execution_id != second.execution_id


class TestBridgeResponseContract:
    async def test_response_truncated_to_max_chars(
        self, fresh_db, intelligence: IntelligenceService
    ) -> None:
        """Bridge truncates responses for interface limits (WhatsApp 4096)."""
        bridge = RuntimeBridge(intelligence=intelligence, max_response_chars=50)

        async with db_session() as session:
            response = await bridge.process(
                session, _make_request(text="say something long please")
            )

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert len(response.text or "") <= 50

    async def test_processed_at_set(self, fresh_db, bridge: RuntimeBridge) -> None:
        async with db_session() as session:
            response = await bridge.process(session, _make_request())
        assert response.processed_at is not None
        # Should be very recent
        delta = datetime.now(UTC) - response.processed_at
        assert delta < timedelta(seconds=5)


class TestBridgeInterfaceAgnosticism:
    async def test_bridge_accepts_web_interface(self, fresh_db, bridge: RuntimeBridge) -> None:
        """The bridge is interface-agnostic — accepts any InterfaceKind."""
        async with db_session() as session:
            response = await bridge.process(
                session,
                _make_request(
                    interface=InterfaceKind.WEB,
                    sender_id="web-session-xyz",
                    message_id="web-msg-001",
                ),
            )
        assert response.status == RuntimeResponseStatus.SUCCESS

    async def test_bridge_accepts_api_interface(self, fresh_db, bridge: RuntimeBridge) -> None:
        async with db_session() as session:
            response = await bridge.process(
                session,
                _make_request(
                    interface=InterfaceKind.API,
                    sender_id="api-key-xyz",
                    message_id="api-msg-001",
                ),
            )
        assert response.status == RuntimeResponseStatus.SUCCESS


class TestBridgeEffectiveText:
    def test_effective_text_includes_media(
        self,
    ) -> None:
        """RuntimeRequest.effective_text combines message text + media text."""
        req = _make_request(text="what's in this image?")
        req.media = [
            {"kind": "image", "extracted_text": "a cat sitting on a mat"},
            {"kind": "audio", "extracted_text": "transcription: meow meow"},
        ]
        effective = req.effective_text
        assert "what's in this image?" in effective
        assert "a cat sitting on a mat" in effective
        assert "transcription: meow meow" in effective


class TestBridgeErrorHandling:
    async def test_llm_failure_marks_work_failed_and_redelivery_retries(self, fresh_db) -> None:
        """A failed LLM call must NOT poison the message.

        The forensic audit found the opposite: the idempotency record was
        finalized as internal_error, so redelivering the same message ID
        returned DUPLICATE forever and the user never got a reply. The new
        semantics: the attempt is recorded (execution + objective failed),
        the record becomes retryable (failed), and a redelivery of the SAME
        message ID retries the work.
        """
        from collections.abc import AsyncIterator

        from wax.intelligence.contracts import LLMRequest, LLMResponse

        # Build a provider that always fails
        class FailingProvider:
            @property
            def kind(self):  # type: ignore[no-untyped-def]
                from wax.intelligence.contracts import ProviderKind

                return ProviderKind.MOCK

            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("LLM is down")

            async def stream(self, request: LLMRequest) -> AsyncIterator:  # type: ignore[no-untyped-def]
                raise RuntimeError("LLM is down")
                yield  # never reached

            async def close(self) -> None:
                pass

        intel = IntelligenceService(FailingProvider())  # type: ignore[arg-type]
        bridge = RuntimeBridge(intelligence=intel)

        async with db_session() as session:
            response = await bridge.process(session, _make_request())

        assert response.status == RuntimeResponseStatus.INTERNAL_ERROR
        assert "RuntimeError" in (response.error or "")

        # The attempt is recorded as FAILED (retryable), not internal_error.
        async with db_session() as session:
            result = await session.execute(
                select(ProcessedMessageRecord).where(
                    ProcessedMessageRecord.interface_message_id == "msg-001"
                )
            )
            record = result.scalar_one()
        assert record.outcome == "failed"
        assert record.metadata_json["attempts"] == 2  # 1st attempt counted on failure

        # The execution and objective must be honestly failed — not left
        # running/pending forever (audit Sections 8, 15).
        from wax.state.execution_models import ExecutionRecord
        from wax.state.objective_models import ObjectiveRecord

        async with db_session() as session:
            execution = await session.get(ExecutionRecord, record.execution_id)
            objective = await session.get(ObjectiveRecord, record.objective_id)
        assert execution is not None
        assert execution.status == "failed"
        assert "RuntimeError" in (execution.error or "")
        assert objective is not None
        assert objective.status == "failed"

        # THE FIX: redelivering the SAME message ID retries the work and
        # succeeds when the transient failure clears.
        from wax.intelligence.adapters.mock_provider import MockLLMProvider

        bridge_ok = RuntimeBridge(intelligence=IntelligenceService(MockLLMProvider()))
        async with db_session() as session:
            retry_response = await bridge_ok.process(session, _make_request())

        assert retry_response.status == RuntimeResponseStatus.SUCCESS
        assert retry_response.text is not None

        async with db_session() as session:
            result = await session.execute(
                select(ProcessedMessageRecord).where(
                    ProcessedMessageRecord.interface_message_id == "msg-001"
                )
            )
            record_after = result.scalar_one()
        assert record_after.outcome == "success"
        assert record_after.id == record.id  # same message, same record

    async def test_internal_error_then_replay_succeeds(self, fresh_db) -> None:
        """A failed message can be re-sent with a different message_id."""
        from collections.abc import AsyncIterator

        from wax.intelligence.contracts import (
            LLMRequest,
            LLMResponse,
            ProviderKind,
        )

        class FailingProvider:
            @property
            def kind(self):
                return ProviderKind.MOCK

            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("transient")

            async def stream(self, request: LLMRequest) -> AsyncIterator:  # type: ignore[no-untyped-def]
                raise RuntimeError("transient")
                yield

            async def close(self) -> None:
                pass

        # First attempt fails
        intel_fail = IntelligenceService(FailingProvider())  # type: ignore[arg-type]
        bridge_fail = RuntimeBridge(intelligence=intel_fail)

        async with db_session() as session:
            await bridge_fail.process(session, _make_request(message_id="msg-err-1"))

        # Second attempt with mock provider succeeds
        from wax.intelligence.adapters.mock_provider import MockLLMProvider

        intel_ok = IntelligenceService(MockLLMProvider())
        bridge_ok = RuntimeBridge(intelligence=intel_ok)

        async with db_session() as session:
            response = await bridge_ok.process(session, _make_request(message_id="msg-err-2"))

        assert response.status == RuntimeResponseStatus.SUCCESS


class TestBridgePersistenceSurvival:
    async def test_idempotency_survives_session_close(
        self, fresh_db, bridge: RuntimeBridge
    ) -> None:
        """The idempotency record must survive DB session close.

        This is what makes the bridge retry-safe: if Meta retries the
        webhook after we've already processed it, we'll find the existing
        record even across DB connection cycles.
        """
        async with db_session() as session:
            first = await bridge.process(session, _make_request(message_id="msg-survive-1"))

        # New DB session — simulates process restart between webhooks
        async with db_session() as session:
            second = await bridge.process(session, _make_request(message_id="msg-survive-1"))

        assert first.status == RuntimeResponseStatus.SUCCESS
        assert second.status == RuntimeResponseStatus.DUPLICATE
        assert second.execution_id == first.execution_id
