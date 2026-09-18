"""OpenAI LLM provider adapter.

This is the ONLY place in the codebase that imports `openai` or `httpx`
to talk to the OpenAI API. The contract (LLMProvider Protocol) is what
the rest of WAX sees.

INVARIANT INV-03: openai SDK is NOT imported outside this file. An
architecture test will enforce this.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import ClassVar

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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._default_model = default_model

    @property
    def kind(self) -> ProviderKind:
        return ProviderKind.OPENAI

    # Conservative context windows per model family (adapter knowledge —
    # the runtime reads this through capability negotiation, never via an
    # SDK here). Keys are lowercase model prefixes.
    _CONTEXT_LIMITS: ClassVar[dict[str, int]] = {
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        "o1": 128_000,
        "o3": 200_000,
    }
    _DEFAULT_CONTEXT_LIMIT = 128_000

    # Tokenizer-exact accounting (ADR-0018): the adapter owns the real
    # tokenizer for its model families. tiktoken is an OPTIONAL
    # dependency — deployments that install it get EXACT token counts;
    # deployments that don't get the documented estimator. Load is lazy
    # and cached per encoding; ANY failure (not installed, model files
    # unavailable) degrades to the estimator, never breaks accounting.
    _MODEL_ENCODINGS: ClassVar[dict[str, str]] = {
        "gpt-4o": "o200k_base",
        "o1": "o200k_base",
        "o3": "o200k_base",
        "o4": "o200k_base",
        "gpt-4-turbo": "cl100k_base",
        "gpt-4": "cl100k_base",
        "gpt-3.5-turbo": "cl100k_base",
    }
    _encodings: ClassVar[
        dict[str, object]
    ] = {}  # encoding name -> loaded object (class-level cache)

    @property
    def token_counter(self) -> str:
        """Provenance of this adapter's token accounting.

        "tiktoken:<encoding>" = exact counts; "estimate:4chars" = the
        conservative fallback. The runtime records this in budget
        provenance instead of guessing which one it got.
        """
        enc = self._encoding_for_model()
        return f"tiktoken:{enc.name}" if enc is not None else "estimate:4chars"

    def _encoding_for_model(self):
        """Resolve + lazily load the tiktoken encoding for this model.

        Returns the encoding object, or None when tiktoken is absent or
        the model has no registered encoding. Never raises.
        """
        model = (self._default_model or "").lower()
        name = None
        for prefix, enc_name in self._MODEL_ENCODINGS.items():
            if model.startswith(prefix):
                name = enc_name
                break
        if name is None:
            return None
        cached = self._encodings.get(name)
        if cached is False:  # previously failed to load
            return None
        if cached is not None:
            return cached
        try:  # pragma: no cover - import path exercised via fallback test
            import tiktoken

            enc = tiktoken.get_encoding(name)
            self._encodings[name] = enc
            return enc
        except Exception:
            # Not installed, or the encoding files can't be fetched in
            # this deployment. The failure is cached for this process —
            # deterministic, and it avoids re-attempting a failed load on
            # every call. A restart (or new process) retries.
            self._encodings[name] = False
            return None

    @property
    def context_limit_tokens(self) -> int:
        """The advertised context window for the configured model."""
        model = (self._default_model or "").lower()
        for prefix, limit in self._CONTEXT_LIMITS.items():
            if model.startswith(prefix):
                return limit
        return self._DEFAULT_CONTEXT_LIMIT

    def estimate_tokens(self, text: str) -> int:
        """Count tokens for text under THIS model family.

        Exact when the tiktoken encoding for the model is available
        (offline, no API call); otherwise the conservative estimator.
        The accounting error is always on the SAFE side: the estimator
        (4 chars/token) over-counts tokens for typical English prose,
        shrinking rather than overflowing the window.
        """
        if not text:
            return 0
        enc = self._encoding_for_model()
        if enc is not None:
            return len(enc.encode(text, disallowed_special=()))
        return max(1, int(len(text) / 4))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request, stream=False)
        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code != 200:
            # Include the actual error body so we can see WHY the provider
            # rejected the request — not just the status code.
            try:
                error_body = response.json()
                error_text = json.dumps(error_body)[:500]
            except Exception:
                error_text = response.text[:500]
            raise RuntimeError(f"provider rejected request ({response.status_code}): {error_text}")
        data = response.json()

        choice = data["choices"][0]
        message = choice.get("message", {})

        # Parse tool calls (finish_reason == "tool_calls"). A tool call is
        # a REQUEST from the model — the runtime decides whether it executes.
        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls", []) or []:
            fn = raw_call.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments}
            except json.JSONDecodeError:
                arguments = {"_unparseable": str(fn.get("arguments"))[:500]}
            tool_calls.append(
                ToolCall(
                    id=raw_call.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=arguments,
                )
            )

        content = message.get("content") or ""
        return LLMResponse(
            content=content,
            model=data.get("model", request.model or self._default_model),
            provider=ProviderKind.OPENAI,
            finish_reason=choice.get("finish_reason", "stop"),
            usage={
                "tokens_prompt": data.get("usage", {}).get("prompt_tokens", 0),
                "tokens_completion": data.get("usage", {}).get("completion_tokens", 0),
                "tokens_total": data.get("usage", {}).get("total_tokens", 0),
            },
            request_id=request.request_id,
            tool_calls=tool_calls,
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
        payload: dict[str, object] = {
            "model": request.model or self._default_model,
            "messages": [self._message_to_wire(m) for m in request.messages],
            "temperature": request.temperature,
            "stream": stream,
            **({"max_tokens": request.max_tokens} if request.max_tokens else {}),
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _message_to_wire(self, m: LLMMessage) -> dict[str, object]:
        if m.role == MessageRole.ASSISTANT and m.tool_calls:
            return {
                "role": "assistant",
                "content": m.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ],
            }
        if m.role == MessageRole.TOOL:
            return {
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": m.content,
            }
        return {"role": m.role.value, "content": m.content}
