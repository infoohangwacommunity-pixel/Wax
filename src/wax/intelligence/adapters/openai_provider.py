"""OpenAI LLM provider adapter.

This is the ONLY place in the codebase that imports `openai` or `httpx`
to talk to the OpenAI API. The contract (LLMProvider Protocol) is what
the rest of WAX sees.

INVARIANT INV-03: openai SDK is NOT imported outside this file. An
architecture test will enforce this.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from wax.intelligence.contracts import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderKind,
)


class OpenAIProvider:
    """OpenAI-compatible LLM adapter.

    Uses httpx directly (no openai SDK dependency) to minimize coupling
    and keep the contract stable across provider SDKs. This also means
    the same adapter works against any OpenAI-compatible endpoint
    (together.ai, anyscale, local vLLM, etc.) by changing the base_url.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        default_model: str = "gpt-4o-mini",
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._default_model = default_model

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.OPENAI

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request, stream=False)
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        return LLMResponse(
            content=choice["message"]["content"],
            model=data.get("model", request.model or self._default_model),
            provider=ProviderKind.OPENAI,
            finish_reason=choice.get("finish_reason", "stop"),
            usage={
                "tokens_prompt": data.get("usage", {}).get("prompt_tokens", 0),
                "tokens_completion": data.get("usage", {}).get("completion_tokens", 0),
                "tokens_total": data.get("usage", {}).get("total_tokens", 0),
            },
            request_id=request.request_id,
            raw_metadata={"id": data.get("id")},
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        payload = self._build_payload(request, stream=True)
        async with self._client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    yield LLMStreamChunk(content="", finish_reason="stop")
                    return
                # Parse JSON — keeping it minimal to avoid dependency
                import json

                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                finish = choices[0].get("finish_reason")
                yield LLMStreamChunk(content=content, finish_reason=finish)

    async def close(self) -> None:
        await self._client.aclose()

    def _build_payload(self, request: LLMRequest, *, stream: bool) -> dict[str, object]:
        return {
            "model": request.model or self._default_model,
            "messages": [
                {"role": m.role.value, "content": m.content}
                for m in request.messages
            ],
            "temperature": request.temperature,
            "stream": stream,
            **({"max_tokens": request.max_tokens} if request.max_tokens else {}),
        }
