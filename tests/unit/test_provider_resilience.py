"""Provider resilience + model-independence contract tests.

Proves:
1. ResilientProvider retries transient failures and succeeds (retry INSIDE
   the breaker never trips the circuit on a blip).
2. Sustained failure opens the breaker; calls fail fast (inner provider
   stops being hit); HALF_OPEN recovers after the cool-down.
3. Permanent errors (401) do NOT retry — retrying harm is still harm.
4. The Anthropic adapter speaks its real wire format (system as top-level
   argument, tool_use/tool_result blocks) through the SAME LLMProvider
   contract — adding a vendor changed nothing outside the adapter file.
5. from_settings wraps every provider in ResilientProvider.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from wax.core.config import WaxSettings
from wax.core.exceptions import WaxConfigurationError
from wax.intelligence.adapters.anthropic_provider import AnthropicProvider
from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    MessageRole,
    ProviderKind,
    ToolCall,
    ToolSpec,
)
from wax.intelligence.resilience import (
    PermanentLLMError,
    ResilientProvider,
    TransientLLMError,
)
from wax.intelligence.service import IntelligenceService
from wax.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from wax.reliability.retry import RetryConfig


def _req() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role=MessageRole.USER, content="hi")],
        request_id="req-test",
    )


class ScriptedProvider:
    """Test double: raises queued exceptions, then returns canned content."""

    def __init__(self, script: list[Exception | str], kind: ProviderKind = ProviderKind.MOCK):
        self._script = list(script)
        self.calls = 0
        self._kind = kind

    @property
    def kind(self) -> ProviderKind:
        return self._kind

    async def complete(self, request: LLMRequest):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        from wax.intelligence.contracts import LLMResponse

        return LLMResponse(
            content=item,
            model="scripted-1",
            provider=self._kind,
            finish_reason="stop",
            usage={"tokens_total": 1},
            request_id=request.request_id,
        )

    async def stream(self, request: LLMRequest):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


def _fast_retry() -> RetryConfig:
    return RetryConfig(
        max_attempts=3,
        base_delay=0.01,
        max_delay=0.02,
        retryable_exceptions=(TransientLLMError,),
    )


class TestRetryAndBreaker:
    async def test_transient_blip_retries_and_succeeds(self) -> None:
        inner = ScriptedProvider(
            [TransientLLMError("503"), TransientLLMError("timeout"), "recovered"]
        )
        provider = ResilientProvider(
            inner, retry=_fast_retry(), breaker=CircuitBreaker(name="t", failure_threshold=5)
        )
        response = await provider.complete(_req())
        assert response.content == "recovered"
        assert inner.calls == 3
        assert provider.breaker_state == "closed"  # a blip must NOT open the circuit

    async def test_sustained_failure_opens_breaker_and_fails_fast(self) -> None:
        inner = ScriptedProvider([TransientLLMError("503")] * 10)
        provider = ResilientProvider(
            inner,
            retry=RetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.02,
                retryable_exceptions=(TransientLLMError,),
            ),
            breaker=CircuitBreaker(name="t", failure_threshold=2, recovery_timeout=60.0),
        )
        for _ in range(2):  # two logical calls, each exhausting retries
            with pytest.raises(Exception):  # noqa: B017
                await provider.complete(_req())
        assert inner.calls == 4  # 2 attempts x 2 calls — no storm
        assert provider.breaker_state == "open"

        calls_before = inner.calls
        with pytest.raises(CircuitOpenError):
            await provider.complete(_req())
        assert inner.calls == calls_before  # fail fast: provider untouched

    async def test_breaker_half_open_recovers(self) -> None:
        inner = ScriptedProvider([TransientLLMError("503"), "back"])
        provider = ResilientProvider(
            inner,
            retry=RetryConfig(
                max_attempts=1,
                retryable_exceptions=(TransientLLMError,),
            ),
            breaker=CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=1.0),
        )
        with pytest.raises(Exception):  # noqa: B017
            await provider.complete(_req())
        assert provider.breaker_state == "open"

        import asyncio

        await asyncio.sleep(1.05)  # past recovery_timeout
        response = await provider.complete(_req())  # trial call
        assert response.content == "back"
        assert provider.breaker_state == "closed"

    async def test_permanent_error_does_not_retry(self) -> None:
        inner = ScriptedProvider([PermanentLLMError("provider rejected request (401)")])
        provider = ResilientProvider(
            inner, retry=_fast_retry(), breaker=CircuitBreaker(name="t", failure_threshold=5)
        )
        with pytest.raises(PermanentLLMError):
            await provider.complete(_req())
        assert inner.calls == 1  # classified permanent: no second attempt


class TestAnthropicWire:
    def _transport(self, capture: dict[str, Any], respond: dict[str, Any]):
        def handler(request: httpx.Request) -> httpx.Response:
            capture["payload"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
            return httpx.Response(200, json=respond)

        return httpx.MockTransport(handler)

    async def test_messages_translation_and_tool_use(self) -> None:
        captured: dict[str, Any] = {}
        respond = {
            "id": "msg_1",
            "model": "claude-3-5-haiku-latest",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "memory.store",
                    "input": {"content": {"fact": "x"}, "kind": "semantic"},
                },
            ],
            "usage": {"input_tokens": 12, "output_tokens": 34},
        }
        provider = AnthropicProvider(
            api_key="test-key",
            transport=self._transport(captured, respond),
        )

        request = LLMRequest(
            messages=[
                LLMMessage(role=MessageRole.SYSTEM, content="be brief"),
                LLMMessage(role=MessageRole.USER, content="remember x"),
                LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="toolu_1", name="memory.store", arguments={})],
                ),
                LLMMessage(
                    role=MessageRole.TOOL,
                    content='{"outcome": "success"}',
                    tool_call_id="toolu_1",
                ),
            ],
            tools=[
                ToolSpec(
                    name="memory.store",
                    description="store a memory",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            request_id="req-1",
        )
        response = await provider.complete(request)

        payload = captured["payload"]
        # System is a TOP-LEVEL argument, not a message.
        assert payload["system"] == "be brief"
        roles = [m["role"] for m in payload["messages"]]
        assert "system" not in roles
        # Tool result travels as a user tool_result block.
        assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
        # Tools use input_schema.
        assert payload["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
        assert captured["headers"]["x-api-key"] == "test-key"

        # Response translation back to the contract.
        assert response.provider == ProviderKind.ANTHROPIC
        assert response.finish_reason == "tool_calls"
        assert response.tool_calls[0].name == "memory.store"
        assert response.tool_calls[0].arguments == {
            "content": {"fact": "x"},
            "kind": "semantic",
        }
        assert response.usage["tokens_total"] == 46

    async def test_http_error_maps_to_classification(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        provider = ResilientProvider(
            AnthropicProvider(api_key="bad", transport=httpx.MockTransport(handler)),
            retry=_fast_retry(),
            breaker=CircuitBreaker(name="t", failure_threshold=5),
        )
        with pytest.raises(PermanentLLMError):
            await provider.complete(_req())

    async def test_429_is_transient(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"error": {"message": "slow down"}})
            return httpx.Response(
                200,
                json={
                    "id": "m",
                    "model": "claude-3-5-haiku-latest",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        provider = ResilientProvider(
            AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler)),
            retry=_fast_retry(),
            breaker=CircuitBreaker(name="t", failure_threshold=5),
        )
        response = await provider.complete(_req())
        assert response.content == "ok"
        assert calls["n"] == 3


class TestFromSettings:
    def test_every_provider_is_resilient(self) -> None:
        svc = IntelligenceService.from_settings(
            WaxSettings.model_validate({"llm_default_provider": "mock"})
        )
        assert isinstance(svc._provider, ResilientProvider)

    def test_anthropic_selection(self) -> None:
        svc = IntelligenceService.from_settings(
            WaxSettings.model_validate(
                {"llm_default_provider": "anthropic", "anthropic_api_key": "k"}
            )
        )
        assert svc.provider_kind == ProviderKind.ANTHROPIC
        assert isinstance(svc._provider, ResilientProvider)

    def test_anthropic_without_key_raises(self) -> None:
        with pytest.raises(WaxConfigurationError):
            IntelligenceService.from_settings(
                WaxSettings.model_validate({"llm_default_provider": "anthropic"})
            )
