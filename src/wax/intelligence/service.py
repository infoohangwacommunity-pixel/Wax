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
    """Routes LLM requests to the configured provider, with failover.

    Use:
        svc = IntelligenceService.from_settings(settings)
        response = await svc.complete(request)

    Failover (ADR-0024, mission §33/§34): candidates are tried IN ORDER —
    the primary first, then each configured fallback. Each candidate
    already carries its own classified retry + circuit breaker, so a
    candidate is only abandoned after its own retry budget exhausts;
    when every candidate fails, the LAST error is raised honestly. The
    response records which provider actually served the request.
    """

    def __init__(
        self, provider: LLMProvider, fallbacks: list[LLMProvider] | None = None
    ) -> None:
        self._provider = provider
        self._fallbacks: list[LLMProvider] = list(fallbacks or [])

    @property
    def candidates(self) -> list[LLMProvider]:
        """The provider candidates in failover order (primary first)."""
        return [self._provider, *self._fallbacks]

    @property
    def inner_provider(self) -> LLMProvider:
        """The underlying provider (ResilientProvider wrapping the adapter,
        or a bare adapter). Read-only access for capability negotiation —
        never a second invocation path."""
        return self._provider

    @classmethod
    def from_settings(cls, settings: WaxSettings) -> IntelligenceService:
        """Build the service from WaxSettings.

        Provider selection:
        - If WAX_LLM_DEFAULT_PROVIDER is "mock" or empty → MockLLMProvider
        - If "openai" → OpenAIProvider (requires WAX_OPENAI_API_KEY)
        - If "anthropic" → AnthropicProvider (requires WAX_ANTHROPIC_API_KEY)
        - Others raise WaxConfigurationError (not yet implemented)

        Every provider is wrapped in ResilientProvider (classified retry +
        per-provider circuit breaker) — the same contract regardless of
        vendor, proving the model abstraction holds under failure.
        """
        provider_kind = settings.llm_default_provider.strip().lower()
        if not provider_kind or provider_kind == ProviderKind.MOCK.value:
            log.warning("intelligence.using_mock_provider")
            return cls(
                cls._resilient(MockLLMProvider(), settings),
                fallbacks=cls._build_fallbacks(settings, exclude={provider_kind}),
            )

        if provider_kind == ProviderKind.OPENAI.value:
            if not settings.openai_api_key:
                raise WaxConfigurationError(
                    "WAX_LLM_DEFAULT_PROVIDER=openai requires WAX_OPENAI_API_KEY"
                )
            from wax.intelligence.adapters.openai_provider import OpenAIProvider

            return cls(
                cls._resilient(
                    OpenAIProvider(
                        api_key=settings.openai_api_key,
                        # Configurable base URL keeps the runtime model-agnostic
                        # by configuration: any OpenAI-compatible endpoint works
                        # (vLLM, Together, OpenRouter, ...) without code changes.
                        base_url=settings.llm_base_url or "https://api.openai.com/v1",
                        default_model=settings.llm_model or "gpt-4o-mini",
                    ),
                    settings,
                ),
                fallbacks=cls._build_fallbacks(settings, exclude={provider_kind}),
            )

        if provider_kind == ProviderKind.ANTHROPIC.value:
            if not settings.anthropic_api_key:
                raise WaxConfigurationError(
                    "WAX_LLM_DEFAULT_PROVIDER=anthropic requires WAX_ANTHROPIC_API_KEY"
                )
            from wax.intelligence.adapters.anthropic_provider import AnthropicProvider

            return cls(
                cls._resilient(
                    AnthropicProvider(
                        api_key=settings.anthropic_api_key,
                        base_url=settings.llm_base_url
                        or "https://api.anthropic.com/v1",
                        default_model=settings.llm_model
                        or "claude-3-5-haiku-latest",
                    ),
                    settings,
                ),
                fallbacks=cls._build_fallbacks(settings, exclude={provider_kind}),
            )

        raise WaxConfigurationError(
            f"Unknown LLM provider: {provider_kind!r}. Supported: mock, openai, anthropic"
        )

    @staticmethod
    def _resilient(provider: LLMProvider, settings: WaxSettings) -> LLMProvider:
        """Wrap a provider with classified retry + circuit breaker.

        All providers get the same resilience contract from configuration —
        replacing a vendor never re-opens the failure-mode question.
        """
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
    def _build_fallbacks(
        settings: WaxSettings, *, exclude: set[str]
    ) -> list[LLMProvider]:
        """Parse WAX_LLM_PROVIDER_FALLBACKS into resilient candidates.

        A misconfigured fallback is a BOOT error, not a 3am surprise:
        unknown kinds raise WaxConfigurationError loudly. Duplicates of
        the primary (or of an earlier fallback) are skipped — failing
        over to the same broken provider helps nobody.
        """
        raw = (getattr(settings, "llm_provider_fallbacks", "") or "").strip()
        if not raw:
            return []
        fallbacks: list[LLMProvider] = []
        seen: set[str] = set(exclude)
        for part in raw.split(","):
            kind = part.strip().lower()
            if not kind or kind in seen:
                continue
            seen.add(kind)
            fallbacks.append(
                IntelligenceService._resilient(
                    IntelligenceService._build_provider(kind, settings), settings
                )
            )
            log.info("intelligence.fallback_registered", provider=kind)
        return fallbacks

    @staticmethod
    def _build_provider(kind: str, settings: WaxSettings) -> LLMProvider:
        """Construct one provider adapter by kind (boot-time, loud)."""
        if kind == ProviderKind.MOCK.value:
            return MockLLMProvider()
        if kind == ProviderKind.OPENAI.value:
            if not settings.openai_api_key:
                raise WaxConfigurationError(
                    "provider fallback 'openai' requires WAX_OPENAI_API_KEY"
                )
            from wax.intelligence.adapters.openai_provider import OpenAIProvider

            return OpenAIProvider(
                api_key=settings.openai_api_key,
                base_url=settings.llm_base_url or "https://api.openai.com/v1",
                default_model=settings.llm_model or "gpt-4o-mini",
            )
        if kind == ProviderKind.ANTHROPIC.value:
            if not settings.anthropic_api_key:
                raise WaxConfigurationError(
                    "provider fallback 'anthropic' requires WAX_ANTHROPIC_API_KEY"
                )
            from wax.intelligence.adapters.anthropic_provider import (
                AnthropicProvider,
            )

            return AnthropicProvider(
                api_key=settings.anthropic_api_key,
                base_url=settings.llm_base_url or "https://api.anthropic.com/v1",
                default_model=settings.llm_model or "claude-3-5-haiku-latest",
            )
        raise WaxConfigurationError(
            f"Unknown LLM provider fallback: {kind!r}. Supported: mock, openai, anthropic"
        )

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
            # Provider-aware input accounting: uses the adapter's own
            # token counter when it has one (exact for tiktoken-backed
            # adapters), so the log records the request's token cost by
            # the same math the budget was built with.
            estimated_input_tokens=provider_estimate_messages_tokens(
                self._provider, request.messages
            ),
        )
        last_error: Exception | None = None
        for candidate in self.candidates:
            try:
                response = await candidate.complete(request)
            except Exception as e:  # noqa: BLE001 — failover is the point
                last_error = e
                log.error(
                    "intelligence.complete.error",
                    provider=candidate.kind.value,
                    error=str(e),
                    error_type=type(e).__name__,
                    failover_remaining=len(self.candidates)
                    - self.candidates.index(candidate)
                    - 1,
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
