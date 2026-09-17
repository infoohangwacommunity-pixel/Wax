"""IntelligenceService — the runtime's LLM routing layer.

The runtime owns the provider. The AI requests intelligence via this
service — never directly via a provider SDK.

Architecture:
- Build via settings: pick provider from WAX_LLM_DEFAULT_PROVIDER
- Routes LLMRequest to the configured provider
- Tracks usage (for resource accounting, Phase J)
- Supports routing, fallback, retry

INVARIANT: This is the ONLY entrypoint for LLM calls. Code outside
wax.intelligence MUST NOT import openai, anthropic, etc.

PROVIDER-AGNOSTIC: The runtime is NOT hardcoded to any provider. You
pick the provider, API key, base URL, and model — all via env vars.
Any OpenAI-compatible endpoint (Groq, Together, OpenRouter, Mistral,
vLLM, Ollama, etc.) works without code changes.
"""

from __future__ import annotations

import os
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
    """Routes LLM requests to the configured provider, with failover.

    Use:
        svc = IntelligenceService.from_settings(settings)
        response = await svc.complete(request)

    Failover: candidates are tried IN ORDER — the primary first, then
    each configured fallback. Each candidate carries its own retry +
    circuit breaker. When every candidate fails, the LAST error is
    raised honestly.
    """

    def __init__(self, provider: LLMProvider, fallbacks: list[LLMProvider] | None = None) -> None:
        self._provider = provider
        self._fallbacks: list[LLMProvider] = list(fallbacks or [])

    @property
    def candidates(self) -> list[LLMProvider]:
        """The provider candidates in failover order (primary first)."""
        return [self._provider, *self._fallbacks]

    @property
    def inner_provider(self) -> LLMProvider:
        """The underlying provider. Read-only access for capability
        negotiation — never a second invocation path."""
        return self._provider

    @classmethod
    def from_settings(cls, settings: WaxSettings) -> IntelligenceService:
        """Build the service from WaxSettings.

        Provider selection is fully config-driven:
        - WAX_LLM_DEFAULT_PROVIDER: "openai", "anthropic", or "mock"
        - WAX_LLM_API_KEY: the API key for the primary provider
        - WAX_LLM_BASE_URL: the base URL (empty = provider default)
        - WAX_LLM_MODEL: the model name
        - WAX_LLM_PROVIDER_FALLBACKS: comma-separated fallback configs

        For backward compat, WAX_OPENAI_API_KEY and WAX_ANTHROPIC_API_KEY
        still work if WAX_LLM_API_KEY is not set.

        Any OpenAI-compatible endpoint works with provider="openai":
        - Groq: base_url="https://api.groq.com/openai/v1"
        - Together: base_url="https://api.together.xyz/v1"
        - OpenRouter: base_url="https://openrouter.ai/api/v1"
        - Mistral: base_url="https://api.mistral.ai/v1"
        - vLLM: base_url="http://localhost:8000/v1"
        - Ollama: base_url="http://localhost:11434/v1"
        """
        provider_kind = settings.llm_default_provider.strip().lower()

        # Mock / empty → mock provider
        if not provider_kind or provider_kind == ProviderKind.MOCK.value:
            log.warning("intelligence.using_mock_provider")
            return cls(
                cls._resilient(MockLLMProvider(), settings),
                fallbacks=cls._build_fallbacks(settings),
            )

        # Resolve the API key (new generic key first, then legacy keys)
        api_key = settings.llm_api_key
        if not api_key:
            if provider_kind == ProviderKind.OPENAI.value:
                api_key = settings.openai_api_key
            elif provider_kind == ProviderKind.ANTHROPIC.value:
                api_key = settings.anthropic_api_key

        if not api_key and provider_kind != ProviderKind.MOCK.value:
            raise WaxConfigurationError(
                f"WAX_LLM_DEFAULT_PROVIDER={provider_kind} requires an API key. "
                f"Set WAX_LLM_API_KEY (or WAX_{provider_kind.upper()}_API_KEY for legacy compat)."
            )

        # Build the primary provider
        provider = cls._build_provider(
            kind=provider_kind,
            api_key=api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            settings=settings,
        )

        return cls(
            cls._resilient(provider, settings),
            fallbacks=cls._build_fallbacks(settings),
        )

    @staticmethod
    def _build_provider(
        *,
        kind: str,
        api_key: str,
        base_url: str,
        model: str,
        settings: WaxSettings,
    ) -> LLMProvider:
        """Construct one provider adapter by kind."""
        if kind == ProviderKind.MOCK.value:
            return MockLLMProvider()

        if kind == ProviderKind.OPENAI.value:
            from wax.intelligence.adapters.openai_provider import OpenAIProvider

            # Default base URL if not specified
            effective_base_url = base_url or "https://api.openai.com/v1"
            effective_model = model or "gpt-4o-mini"

            log.info(
                "intelligence.provider_configured",
                kind="openai-compatible",
                base_url=effective_base_url,
                model=effective_model,
            )
            return OpenAIProvider(
                api_key=api_key,
                base_url=effective_base_url,
                default_model=effective_model,
            )

        if kind == ProviderKind.ANTHROPIC.value:
            from wax.intelligence.adapters.anthropic_provider import AnthropicProvider

            effective_base_url = (
                base_url or settings.anthropic_base_url or "https://api.anthropic.com/v1"
            )
            effective_model = model or "claude-3-5-haiku-latest"

            log.info(
                "intelligence.provider_configured",
                kind="anthropic",
                base_url=effective_base_url,
                model=effective_model,
            )
            return AnthropicProvider(
                api_key=api_key,
                base_url=effective_base_url,
                default_model=effective_model,
            )

        raise WaxConfigurationError(
            f"Unknown LLM provider: {kind!r}. "
            f"Supported: openai (any OpenAI-compatible endpoint), anthropic, mock. "
            f"For Groq/Together/OpenRouter/Mistral/vLLM/Ollama, use 'openai' with "
            f"WAX_LLM_BASE_URL set to the provider's API URL."
        )

    @staticmethod
    def _resilient(provider: LLMProvider, settings: WaxSettings) -> LLMProvider:
        """Wrap a provider with classified retry + circuit breaker."""
        from wax.intelligence.resilience import ResilientProvider, TransientLLMError
        from wax.reliability.circuit_breaker import CircuitBreaker
        from wax.reliability.retry import RetryConfig

        return ResilientProvider(
            provider,
            retry=RetryConfig(
                max_attempts=max(1, int(settings.llm_retry_max_attempts)),
                base_delay=0.5,
                max_delay=8.0,
                retryable_exceptions=(TransientLLMError,),
            ),
            breaker=CircuitBreaker(
                name=f"llm-{provider.kind.value}",
                failure_threshold=max(1, int(settings.llm_breaker_failure_threshold)),
                recovery_timeout=max(1.0, float(settings.llm_breaker_recovery_seconds)),
            ),
        )

    @staticmethod
    def _build_fallbacks(settings: WaxSettings) -> list[LLMProvider]:
        """Parse WAX_LLM_PROVIDER_FALLBACKS into resilient candidates.

        Format: "provider:base_url:model,provider:base_url:model,..."
        Each fallback needs its own API key: WAX_LLM_FALLBACK_<N>_API_KEY
        (1-indexed). A misconfigured fallback is a BOOT error, not a 3am
        surprise.
        """
        raw = (getattr(settings, "llm_provider_fallbacks", "") or "").strip()
        if not raw:
            return []

        fallbacks: list[LLMProvider] = []
        parts = [p.strip() for p in raw.split(",") if p.strip()]

        for i, part in enumerate(parts, 1):
            fields = part.split(":", 2)
            if len(fields) < 2:
                raise WaxConfigurationError(
                    f"Invalid fallback config #{i}: {part!r}. "
                    f"Expected format: provider:base_url:model"
                )
            kind = fields[0].strip().lower()
            base_url = fields[1].strip() if len(fields) > 1 else ""
            model = fields[2].strip() if len(fields) > 2 else ""

            # Look up the API key for this fallback
            api_key = os.environ.get(f"WAX_LLM_FALLBACK_{i}_API_KEY", "")
            if not api_key:
                raise WaxConfigurationError(
                    f"Fallback #{i} ({kind}:{base_url}) requires WAX_LLM_FALLBACK_{i}_API_KEY"
                )

            provider = IntelligenceService._build_provider(
                kind=kind,
                api_key=api_key,
                base_url=base_url,
                model=model,
                settings=settings,
            )
            fallbacks.append(IntelligenceService._resilient(provider, settings))
            log.info(
                "intelligence.fallback_registered",
                position=i,
                provider=kind,
                base_url=base_url,
                model=model,
            )

        return fallbacks

    @property
    def provider_kind(self) -> ProviderKind:
        return self._provider.kind

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a non-streaming completion request."""
        from wax.intelligence.context_limits import provider_estimate_messages_tokens

        log.info(
            "intelligence.complete.start",
            provider=self._provider.kind.value,
            model=request.model,
            message_count=len(request.messages),
            estimated_input_tokens=provider_estimate_messages_tokens(
                self._provider, request.messages
            ),
        )
        last_error: Exception | None = None
        for candidate in self.candidates:
            try:
                response = await candidate.complete(request)
            except Exception as e:
                last_error = e
                log.error(
                    "intelligence.complete.error",
                    provider=candidate.kind.value,
                    error=str(e),
                    error_type=type(e).__name__,
                    failover_remaining=len(self.candidates) - self.candidates.index(candidate) - 1,
                )
                continue
            if candidate is not self._provider:
                log.warning(
                    "intelligence.failover_served",
                    primary=self._provider.kind.value,
                    served_by=candidate.kind.value,
                    model=response.model,
                )
            break
        else:
            raise last_error  # type: ignore[misc]
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
        for candidate in self.candidates:
            await candidate.close()
