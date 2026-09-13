"""IntelligenceService — the runtime's LLM routing layer.

The runtime owns the provider. The AI requests intelligence via this
service — never directly via a provider SDK.

Architecture:
- Build via settings: pick provider from WAX_LLM_DEFAULT_PROVIDER
- Routes LLMRequest to the configured provider
- Tracks usage (for resource accounting, Phase J)
- Will support routing, fallback, retry in future phases

INVARIANT: This is the ONLY entrypoint for LLM calls. Code outside
wax.intelligence MUST NOT import openai, anthropic, etc.
"""

from __future__ import annotations

from typing import Any

from wax.core.config import WaxSettings
from wax.core.exceptions import WaxConfigurationError
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderKind,
)
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class IntelligenceService:
    """Routes LLM requests to the configured provider.

    Use:
        svc = IntelligenceService.from_settings(settings)
        response = await svc.complete(request)
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    @classmethod
    def from_settings(cls, settings: WaxSettings) -> IntelligenceService:
        """Build the service from WaxSettings.

        Provider selection:
        - If WAX_LLM_DEFAULT_PROVIDER is "mock" or empty → MockLLMProvider
        - If "openai" → OpenAIProvider (requires WAX_OPENAI_API_KEY)
        - Others raise WaxConfigurationError (not yet implemented)
        """
        provider_kind = settings.llm_default_provider.strip().lower()
        if not provider_kind or provider_kind == ProviderKind.MOCK.value:
            log.warning("intelligence.using_mock_provider")
            return cls(MockLLMProvider())

        if provider_kind == ProviderKind.OPENAI.value:
            if not settings.openai_api_key:
                raise WaxConfigurationError(
                    "WAX_LLM_DEFAULT_PROVIDER=openai requires WAX_OPENAI_API_KEY"
                )
            from wax.intelligence.adapters.openai_provider import OpenAIProvider

            return cls(
                OpenAIProvider(
                    api_key=settings.openai_api_key,
                    # Configurable base URL keeps the runtime model-agnostic
                    # by configuration: any OpenAI-compatible endpoint works
                    # (vLLM, Together, OpenRouter, ...) without code changes.
                    base_url=settings.llm_base_url or "https://api.openai.com/v1",
                    default_model=settings.llm_model or "gpt-4o-mini",
                )
            )

        raise WaxConfigurationError(
            f"Unknown LLM provider: {provider_kind!r}. Supported: mock, openai"
        )

    @property
    def provider_kind(self) -> ProviderKind:
        return self._provider.kind

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a non-streaming completion request."""
        log.info(
            "intelligence.complete.start",
            provider=self._provider.kind.value,
            model=request.model,
            message_count=len(request.messages),
        )
        try:
            response = await self._provider.complete(request)
        except Exception as e:
            log.error(
                "intelligence.complete.error",
                provider=self._provider.kind.value,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise
        log.info(
            "intelligence.complete.ok",
            provider=response.provider.value,
            model=response.model,
            finish_reason=response.finish_reason,
            tokens_total=response.usage.get("tokens_total", 0),
        )
        return response

    async def stream(self, request: LLMRequest) -> Any:
        """Send a streaming completion request. Yields LLMStreamChunk."""
        async for chunk in self._provider.stream(request):
            yield chunk

    async def close(self) -> None:
        await self._provider.close()
