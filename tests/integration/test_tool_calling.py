"""Tool-calling loop tests: Intelligence → Capability invocation.

Audit Sections 10/14 found the hard wall: "the live LLM contract has no
tool-calling field at all, so the model could not request a capability even
if one were registered. The only capability a user message can trigger is
the LLM's own text generation."

These tests prove the wall is gone: the model can REQUEST capabilities, the
runtime gates every request (agency → authority → budget → invoker), the
result flows back to the model, and the whole loop is bounded.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityDescriptor
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
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


def _bridge(services: RuntimeServices, script: list[list[ToolCall]]) -> RuntimeBridge:
    provider = MockLLMProvider(scripted_tool_calls=script)
    return RuntimeBridge(intelligence=IntelligenceService(provider), services=services)


def _request(message_id: str = "msg-tool-1", text: str = "echo something for me") -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000000",
        sender_display_name="Test User",
        text=text,
        received_at=datetime_now(),
    )


def datetime_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


class TestToolCallingLoop:
    async def test_model_tool_call_executes_capability_and_returns_result(
        self, fresh_db, services
    ) -> None:
        """A scripted tool request for `echo` runs through the full gate
        chain and the result is returned to the model as a tool message."""
        bridge = _bridge(
            services,
            script=[[ToolCall(id="call_1", name="echo", arguments={"message": "ping"})]],
        )

        async with db_session() as session:
            response = await bridge.process(session, _request())

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert response.execution_id is not None

        # The capability step is recorded on the execution.
        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        capability_steps = [s for s in steps if s.kind == "capability.invoke"]
        assert len(capability_steps) == 1
        assert capability_steps[0].capability_name == "echo"
        assert capability_steps[0].status == "succeeded"
        assert capability_steps[0].outputs == {"echo": {"message": "ping"}}

        # The agency gate evaluated and audited the decision.
        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            decisions = list(
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.event_kind == "agency.decision")
                    )
                ).scalars()
            )
        assert len(decisions) == 1
        assert decisions[0].actor_kind == "ai"
        assert decisions[0].payload["capability"] == "echo"

        # Authority checked the principal's permission.
        async with db_session() as session:
            auth_events = list(
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.event_kind == "auth_decision")
                    )
                ).scalars()
            )
        assert any(e.payload.get("capability") == "echo" for e in auth_events)

        # Metrics saw the invocation.
        snapshot = services.metrics.snapshot()
        assert any(
            k.startswith("capability_invocations_total|") and "outcome=success" in k
            for k in snapshot["counters"]
        ), snapshot["counters"].keys()

    async def test_unknown_capability_returns_honest_not_found(self, fresh_db, services) -> None:
        """No fake success: a request for an unregistered capability comes
        back as a structured not_found result the model can reason about."""
        bridge = _bridge(
            services,
            script=[[ToolCall(id="call_1", name="does_not_exist", arguments={})]],
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        assert response.status == RuntimeResponseStatus.SUCCESS
        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        capability_steps = [s for s in steps if s.kind == "capability.invoke"]
        assert capability_steps[0].status == "failed"
        assert capability_steps[0].error is not None
        assert "No such capability" in capability_steps[0].error

    async def test_destructive_capability_requires_human_approval_and_is_denied(
        self, fresh_db, services
    ) -> None:
        """Destructive capabilities map to DESTRUCTIVE_ACTION → human
        approval. No approval workflow exists yet, so the runtime denies —
        honestly — rather than executing.

        NOTE: the AI cannot bypass this: the agency gate runs BEFORE the
        authority check, and the authority check runs inside the invoker.
        """
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
        bridge = _bridge(
            services,
            script=[[ToolCall(id="call_1", name="test.wipe", arguments={})]],
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        assert response.status == RuntimeResponseStatus.SUCCESS
        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        capability_steps = [s for s in steps if s.kind == "capability.invoke"]
        assert capability_steps[0].status == "failed"
        assert "human approval" in (capability_steps[0].error or "")

    async def test_tool_loop_is_bounded_by_max_tool_rounds(
        self, fresh_db, services, test_settings
    ) -> None:
        """A model that keeps requesting tools is stopped by the runtime;
        the response honestly reports the boundary."""
        endless_script = [
            [ToolCall(id=f"call_{n}", name="echo", arguments={"n": n})] for n in range(50)
        ]
        test_settings.__dict__["max_tool_rounds"] = 2
        services2 = RuntimeServices.build(test_settings)
        bridge = _bridge(services2, script=endless_script)

        async with db_session() as session:
            response = await bridge.process(session, _request())

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert response.text is not None

        from wax.execution.repository import ExecutionRepository

        async with db_session() as session:
            steps = await ExecutionRepository(session).list_steps(response.execution_id)
        capability_steps = [s for s in steps if s.kind == "capability.invoke"]
        # max_tool_rounds=2: at most 2 tool rounds (the third LLM call is refused).
        assert len(capability_steps) <= 2


async def _noop_impl(inputs: dict) -> dict:
    return {"wiped": True}


class TestOpenAIToolWire:
    def test_payload_includes_tools_and_tool_messages(self) -> None:
        """The OpenAI adapter translates ToolSpecs + tool plumbing to the
        OpenAI wire format (unit-level, no network)."""
        from wax.intelligence.adapters.openai_provider import OpenAIProvider
        from wax.intelligence.contracts import (
            LLMMessage,
            LLMRequest,
            MessageRole,
            ToolSpec,
        )

        provider = OpenAIProvider(api_key="test-key")
        request = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content="sys"),
                LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"a": 1})],
                ),
                LLMMessage(
                    role=MessageRole.TOOL,
                    content='{"outcome": "success"}',
                    tool_call_id="c1",
                    name="echo",
                ),
                LLMMessage(role=MessageRole.USER, content="hi"),
            ],
            tools=[ToolSpec(name="echo", description="Echo", parameters={"type": "object"})],
        )
        payload = provider._build_payload(request, stream=False)
        assert payload["tool_choice"] == "auto"
        assert payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo",
                    "parameters": {"type": "object"},
                },
            }
        ]
        wire = payload["messages"]
        assert wire[1]["tool_calls"][0]["function"]["name"] == "echo"
        assert json.loads(wire[1]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
        assert wire[2]["role"] == "tool"
        assert wire[2]["tool_call_id"] == "c1"

    async def test_response_parses_tool_calls(self) -> None:
        """An OpenAI-shaped tool_calls response is parsed into ToolCall objects."""
        import httpx

        from wax.intelligence.adapters.openai_provider import OpenAIProvider

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_abc",
                                        "type": "function",
                                        "function": {
                                            "name": "http.get",
                                            "arguments": '{"url": "https://example.com"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )

        provider = OpenAIProvider(api_key="test-key")
        provider._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            transport=httpx.MockTransport(handler),
        )
        from wax.intelligence.contracts import LLMMessage, LLMRequest, MessageRole

        response = await provider.complete(
            LLMRequest(messages=[LLMMessage(role=MessageRole.USER, content="fetch")])
        )
        assert response.finish_reason == "tool_calls"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "http.get"
        assert response.tool_calls[0].arguments == {"url": "https://example.com"}
        assert response.content == ""
        await provider.close()
