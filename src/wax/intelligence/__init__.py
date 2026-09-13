"""wax.intelligence — LLM provider abstraction.

The model must be replaceable. A specific provider, model family, or vendor
must NOT become WAX's identity (Directive §42, §43, INV-03).

Architecture:
- `LLMProvider` (Protocol): the contract every adapter must implement
- `LLMRequest` / `LLMResponse`: stable input/output contracts
- `LLMMessage`: universal message format (role + content)
- `adapters/`: concrete implementations (OpenAI, Anthropic, Mock for tests)
- `IntelligenceService`: routes requests to the configured provider

INVARIANT INV-03: Core WAX must not depend on any specific model provider.
The provider SDK (openai, anthropic, etc.) MUST NOT be imported outside
wax.intelligence.adapters. Architecture tests will enforce this.
"""

from wax.intelligence.contracts import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderKind,
)
from wax.intelligence.service import IntelligenceService

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamChunk",
    "ProviderKind",
    "IntelligenceService",
]
