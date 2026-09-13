"""Mock LLM provider — for tests and development without API keys.

CRITICAL: This is NOT a real LLM. It produces deterministic canned responses.
Marked clearly in the response metadata so callers cannot claim "the LLM
said X" when actually they used the mock.

INVARIANT INV-10: No mock may be claimed as production infrastructure.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator

from wax.intelligence.contracts import (
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderKind,
    ToolCall,
)


class MockLLMProvider:
    """A deterministic LLM provider for tests.

    Produces an echo of the last user message with a fixed prefix.
    NOT a real LLM — never use in production.

    Test seam: `scripted_tool_calls` is an ordered list of tool-call batches.
    While non-empty, each complete() call pops the leftmost batch and returns
    it as the response (finish_reason="tool_calls") instead of echoing. This
    lets tests drive the runtime's tool-calling loop deterministically — it
    is explicitly test infrastructure, never production behavior.
    """

    def __init__(
        self,
        *,
        default_model: str = "mock-1.0",
        scripted_tool_calls: list[list[ToolCall]] | None = None,
        context_limit_tokens: int | None = None,
    ) -> None:
        self._default_model = default_model
        self._call_count = 0
        self._script: deque[list[ToolCall]] = deque(scripted_tool_calls or [])
        # Optional advertised limit — lets tests exercise the runtime's
        # provider-context negotiation. None = provider advertises nothing
        # and the runtime uses its configured fallback budget.
        self.context_limit_tokens = context_limit_tokens

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.MOCK

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._call_count += 1

        if self._script:
            batch = self._script.popleft()
            return LLMResponse(
                content="",
                model=request.model or self._default_model,
                provider=ProviderKind.MOCK,
                finish_reason="tool_calls",
                usage={"tokens_prompt": 0, "tokens_completion": 0, "tokens_total": 0},
                request_id=request.request_id,
                tool_calls=batch,
                raw_metadata={"mock": True, "call_count": self._call_count},
            )

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
        for _i, word in enumerate(words):
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
