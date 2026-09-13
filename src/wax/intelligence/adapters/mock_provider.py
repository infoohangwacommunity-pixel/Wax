"""Mock LLM provider — for tests and development without API keys.

CRITICAL: This is NOT a real LLM. It produces deterministic canned responses.
Marked clearly in the response metadata so callers cannot claim "the LLM
said X" when actually they used the mock.

INVARIANT INV-10: No mock may be claimed as production infrastructure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from wax.intelligence.contracts import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderKind,
)


class MockLLMProvider:
    """A deterministic LLM provider for tests.

    Produces an echo of the last user message with a fixed prefix.
    NOT a real LLM — never use in production.
    """

    def __init__(self, *, default_model: str = "mock-1.0") -> None:
        self._default_model = default_model
        self._call_count = 0

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.MOCK

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._call_count += 1
        # Find last user message
        last_user = ""
        for msg in reversed(request.messages):
            if msg.role == MessageRole.USER:
                last_user = msg.content
                break

        content = f"[MOCK LLM RESPONSE] You said: {last_user!r}"
        prompt_tokens = sum(len(m.content.split()) for m in request.messages)
        completion_tokens = len(content.split())

        return LLMResponse(
            content=content,
            model=request.model or self._default_model,
            provider=ProviderKind.MOCK,
            finish_reason="stop",
            usage={
                "tokens_prompt": prompt_tokens,
                "tokens_completion": completion_tokens,
                "tokens_total": prompt_tokens + completion_tokens,
            },
            request_id=request.request_id,
            raw_metadata={"mock": True, "call_count": self._call_count},
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        response = await self.complete(request)
        # Stream word-by-word
        words = response.content.split()
        for i, word in enumerate(words):
            yield LLMStreamChunk(content=word + " ")
        yield LLMStreamChunk(
            content="",
            finish_reason="stop",
            usage=response.usage,
        )

    async def close(self) -> None:
        pass

    # Implementation note: this class implements the LLMProvider Protocol
    # structurally — Python's Protocol with @runtime_checkable will
    # recognize it without inheritance.
