"""Provider resilience — retry + circuit breaker around LLM calls.

The forensic audit's IMPLEMENTED-BUT-UNWIRED category: CircuitBreaker and
retry_with_backoff existed with tests, but IntelligenceService called the
provider bare — one transient 429 or network blip failed the whole
execution, and a downed provider accumulated timeouts instead of failing
fast.

Composition order (learned from production resilience stacks):

    breaker.call( retry_with_backoff( provider.complete ) )

- Retry INSIDE: a single logical call may survive transient blips.
- Breaker OUTSIDE: it counts logical-call outcomes, so one blip that
  retries successfully never trips the circuit, while sustained failure
  opens it and fast-fails everything (no retry storms against a downed
  provider).

Classification (mechanism, not policy):
- Transient (retry): timeouts, transport errors, 408/409/429/5xx.
- Permanent (no retry — retrying is harm): 4xx validation/auth errors.
  They still count as breaker failures: sustained 401s deserve a fast
  failure just as much as sustained timeouts.
- Streaming is NEVER retried mid-flight (a partially-consumed stream
  cannot be safely replayed); the breaker still protects it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from wax.core.exceptions import WaxError
from wax.intelligence.contracts import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderKind,
)
from wax.observability.metrics import get_metrics
from wax.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from wax.reliability.retry import RetryConfig, retry_with_backoff
from wax.runtime.logging import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


class TransientLLMError(WaxError):
    """A retriable provider failure (timeout, 429, 5xx, transport)."""


class PermanentLLMError(WaxError):
    """A non-retriable provider failure (auth, invalid request)."""


def classify_llm_error(exc: BaseException) -> Exception:
    """Map a provider/transport exception to the retry taxonomy."""
    if isinstance(exc, (TransientLLMError, PermanentLLMError)):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in RETRYABLE_STATUS_CODES:
            return TransientLLMError(f"provider returned {status}")
        return PermanentLLMError(f"provider rejected request ({status})")
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return TransientLLMError(f"provider transport failure: {type(exc).__name__}")
    return PermanentLLMError(f"provider failure: {type(exc).__name__}")


class ResilientProvider:
    """Wraps any LLMProvider with classified retry + a per-provider breaker."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        retry: RetryConfig | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._inner = inner
        self._retry = retry or RetryConfig(
            max_attempts=3,
            base_delay=0.5,
            max_delay=8.0,
            retryable_exceptions=(TransientLLMError,),
        )
        self._breaker = breaker or CircuitBreaker(
            name=f"llm-{inner.kind.value}",
            failure_threshold=5,
            recovery_timeout=30.0,
        )
        provider = inner.kind.value
        self._m_retries = get_metrics().counter("llm_retry_total", provider=provider)
        self._m_open = get_metrics().counter("llm_breaker_open_total", provider=provider)
        self._was_open = False

    @property
    def kind(self) -> ProviderKind:
        return self._inner.kind

    @property
    def breaker_state(self) -> str:
        return self._breaker.state.value

    async def complete(self, request: LLMRequest) -> LLMResponse:
        async def _attempt() -> LLMResponse:
            try:
                return await self._inner.complete(request)
            except Exception as e:  # noqa: BLE001 — classified below
                raise classify_llm_error(e) from e

        def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            self._m_retries.inc()
            log.warning(
                "intelligence.retry",
                provider=self._inner.kind.value,
                attempt=attempt,
                delay_s=round(delay, 3),
                error=str(exc)[:200],
            )

        try:
            return await self._breaker.call(
                lambda: retry_with_backoff(_attempt, self._retry, on_retry=_on_retry)
            )
        except CircuitOpenError:
            self._m_open.inc()
            raise
        finally:
            self._observe_breaker()

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        # No mid-flight retry: a consumed stream cannot be replayed.
        # The breaker still gates the attempt.
        async def _attempt() -> AsyncIterator[LLMStreamChunk]:
            return self._inner.stream(request)

        try:
            iterator = await self._breaker.call(_attempt)
        except CircuitOpenError:
            self._m_open.inc()
            raise
        finally:
            self._observe_breaker()
        async for chunk in iterator:
            yield chunk

    async def close(self) -> None:
        await self._inner.close()

    def _observe_breaker(self) -> None:
        """Emit one metric+log event per OPEN transition (not per call)."""
        is_open = self._breaker.state.value == "open"
        if is_open and not self._was_open:
            log.error(
                "intelligence.breaker.opened",
                provider=self._inner.kind.value,
                failures=self._breaker.failure_count,
            )
        self._was_open = is_open
