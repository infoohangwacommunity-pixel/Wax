"""Contracts for the LLM intelligence layer.

These contracts are STABLE — they do not change when a provider adapter
is replaced. The adapter is responsible for translating between
provider-specific schemas and these contracts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ProviderKind(StrEnum):
    """Discriminator for LLM providers.

    The runtime is provider-agnostic. Any OpenAI-compatible endpoint
    (Groq, Together, OpenRouter, Mistral, vLLM, Ollama, etc.) works
    without code changes — just set env vars.
    """

    OPENAI = "openai"  # any OpenAI-compatible endpoint
    ANTHROPIC = "anthropic"  # Anthropic native API
    MOCK = "mock"  # for tests / dev without API keys


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolSpec:
    """A tool offered to the model as a callable.

    In the open-world architecture, the only tool is `terminal` — the
    universal environment interface. The model sees name + description +
    JSON-schema parameters; it NEVER sees the implementation.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class ToolCall:
    """A tool invocation requested by the model.

    The runtime validates and (maybe) executes it — a tool call is a
    REQUEST, never an effect. Effects happen only through the
    CapabilityInvoker after agency + authority gates.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMMessage:
    """A single message in a conversation.

    Content is a string; tool plumbing is carried in the optional fields:
    - tool_calls: set on ASSISTANT messages that request tool execution
      (needed so providers can replay the conversation verbatim).
    - tool_call_id: set on TOOL messages carrying a tool result.
    """

    role: MessageRole
    content: str
    name: str | None = None  # for tool messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMRequest:
    """A request to the LLM.

    Stable across providers — adapters translate to provider-specific shapes.
    """

    messages: list[LLMMessage]
    model: str | None = None  # provider-specific model name
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    # Capability discovery: tools the model may request. None = no tools.
    tools: list[ToolSpec] | None = None
    request_id: str | None = None


@dataclass
class LLMResponse:
    """A complete (non-streaming) LLM response."""

    content: str
    model: str
    provider: ProviderKind
    finish_reason: str  # stop | length | tool_call | error
    usage: dict[str, int]  # tokens_prompt, tokens_completion, tokens_total
    request_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMStreamChunk:
    """A single chunk in a streaming response."""

    content: str  # delta content
    finish_reason: str | None = None  # set on final chunk
    usage: dict[str, int] | None = None  # set on final chunk


@runtime_checkable
class LLMProvider(Protocol):
    """The contract every LLM adapter must implement.

    An adapter is a class (not a module) so it can carry state
    (auth credentials, base URL, default model).
    """

    @property
    def kind(self) -> ProviderKind: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]: ...

    async def close(self) -> None: ...
