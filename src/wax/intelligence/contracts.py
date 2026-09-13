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

    Adding a new kind here is a deliberate architectural act — it means
    a new provider has been integrated.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    MISTRAL = "mistral"
    LOCAL = "local"
    MOCK = "mock"  # for tests


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class LLMMessage:
    """A single message in a conversation.

    Content is a string for now; future versions may support structured
    content (images, tool calls, etc.).
    """

    role: MessageRole
    content: str
    name: str | None = None  # for tool messages
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
    # Tool calling will be added in Phase M (AI/Runtime Contract)
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

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamChunk]: ...

    async def close(self) -> None: ...
