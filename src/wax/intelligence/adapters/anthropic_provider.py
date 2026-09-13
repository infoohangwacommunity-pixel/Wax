"""Anthropic LLM provider adapter.

The ONLY place in the codebase that speaks Anthropic's /v1/messages wire
format. The contract (LLMProvider Protocol) is what WAX sees — this file
exists to prove the model abstraction is real: adding a vendor with a
fundamentally different wire protocol (system-as-top-level-argument,
content-block arrays, tool_use/tool_result blocks) changes NOTHING outside
this file.

Wire translation (Anthropic Messages API):
- system prompt → top-level `system` argument (not a message)
- MessageRole.TOOL results → user turn with tool_result content blocks
- assistant tool calls → assistant turn with tool_use content blocks
- tools → top-level `tools` with input_schema
- usage → input_tokens/output_tokens
- stop_reason → finish_reason (tool_use | end_turn | stop_sequence | max_tokens)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderKind,
    ToolCall,
)

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


class AnthropicProvider:
    """Anthropic Messages API adapter (httpx, no SDK dependency)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        default_model: str = "claude-3-5-haiku-latest",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._default_model = default_model

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.ANTHROPIC

    @property
    def context_limit_tokens(self) -> int:
        """Advertised context window for the Claude 3 family. The runtime
        uses this to derive its evidence budget (capability negotiation);
        there is no SDK import here — this is adapter knowledge."""
        return 200_000

    def estimate_tokens(self, text: str) -> int:
        """Conservative Anthropic-side estimate (~3.5 chars/token for the
        Claude tokenizers; we round to a safe 3.5 → use 4 to stay portable
        and never over-claim capacity)."""
        if not text:
            return 0
        return max(1, int(len(text) / 4))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request, stream=False)
        response = await self._client.post("/messages", json=payload)
        response.raise_for_status()
        data = response.json()

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for block in data.get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )

        usage = data.get("usage", {})
        return LLMResponse(
            content="".join(text_parts),
            model=data.get("model", request.model or self._default_model),
            provider=ProviderKind.ANTHROPIC,
            finish_reason=_STOP_REASON_MAP.get(
                data.get("stop_reason", "end_turn"), "stop"
            ),
            usage={
                "tokens_prompt": usage.get("input_tokens", 0),
                "tokens_completion": usage.get("output_tokens", 0),
                "tokens_total": usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0),
            },
            request_id=request.request_id,
            tool_calls=tool_calls,
            raw_metadata={"id": data.get("id")},
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        payload = self._build_payload(request, stream=True)
        async with self._client.stream("POST", "/messages", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                chunk = json.loads(data_str)
                event_type = chunk.get("type")
                if event_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield LLMStreamChunk(
                            content=delta.get("text", ""), finish_reason=None
                        )
                elif event_type == "message_delta":
                    stop = _STOP_REASON_MAP.get(
                        chunk.get("delta", {}).get("stop_reason") or "end_turn",
                        "stop",
                    )
                    yield LLMStreamChunk(content="", finish_reason=stop)
                elif event_type == "message_stop":
                    return

    async def close(self) -> None:
        await self._client.aclose()

    # --- wire translation -------------------------------------------------

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, object]:
        system_parts: list[str] = []
        turns: list[dict[str, object]] = []

        for m in request.messages:
            if m.role == MessageRole.SYSTEM:
                system_parts.append(m.content)
            elif m.role == MessageRole.USER:
                turns.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
            elif m.role == MessageRole.ASSISTANT:
                blocks: list[dict[str, object]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                if blocks:
                    turns.append({"role": "assistant", "content": blocks})
            elif m.role == MessageRole.TOOL:
                turns.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )

        payload: dict[str, object] = {
            "model": request.model or self._default_model,
            "max_tokens": request.max_tokens or 4096,
            "messages": turns,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in request.tools
            ]
        return payload
