"""Retry with exponential backoff + jitter.

INVARIANTS (Directive §143):
- Bounded max attempts (no infinite retries)
- Exponential backoff (2^attempt seconds, capped)
- Jitter (random 0-50% of delay) to avoid thundering herd
- Idempotency-aware (caller decides if operation is safe to retry)
- Retry storm prevention (circuit breaker is separate, but this layer
  refuses to retry forever)
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from wax.core.exceptions import WaxError

T = TypeVar("T")


class RetryExhaustedError(WaxError):
    """Raised when all retry attempts are exhausted."""


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retry behavior.

    Defaults are conservative:
    - max_attempts: 3 (caller can override up to 10)
    - base_delay: 1.0s (first retry waits ~1s)
    - max_delay: 30.0s (never wait longer than 30s between retries)
    - jitter_factor: 0.5 (jitter is 0-50% of the delay)
    - retryable_exceptions: which exceptions trigger a retry
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter_factor: float = 0.5
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_attempts > 10:
            raise ValueError("max_attempts must be at most 10 (no retry storms)")
        if self.base_delay < 0:
            raise ValueError("base_delay must be non-negative")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")
        if not 0 <= self.jitter_factor <= 1:
            raise ValueError("jitter_factor must be in [0, 1]")

    def compute_delay(self, attempt: int) -> float:
        """Compute the delay before the next retry.

        attempt is 1-indexed (attempt=1 is the first retry, after the
        initial failure).
        """
        # Exponential: 2^(attempt-1) * base_delay
        # attempt=1: 1*base = 1s
        # attempt=2: 2*base = 2s
        # attempt=3: 4*base = 4s
        # ...
        raw_delay = (2 ** (attempt - 1)) * self.base_delay
        delay = min(raw_delay, self.max_delay)
        # Add jitter: random 0% to jitter_factor% of the delay
        jitter = random.uniform(0, self.jitter_factor) * delay
        return delay + jitter


async def retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    config: RetryConfig | None = None,
    *,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run an async operation with bounded retry + exponential backoff.

    Args:
        operation: async callable returning T
        config: RetryConfig (defaults to conservative 3 attempts)
        on_retry: optional callback(attempt, exception, delay_seconds)
            called before each retry for observability

    Returns:
        The result of `operation()` on success.

    Raises:
        RetryExhaustedError: if all attempts fail. The last exception is
            chained as `__cause__`.
    """
    cfg = config or RetryConfig()
    last_exception: BaseException | None = None

    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return await operation()
        except cfg.retryable_exceptions as e:
            last_exception = e
            if attempt >= cfg.max_attempts:
                break
            delay = cfg.compute_delay(attempt)
            if on_retry:
                on_retry(attempt, e, delay)
            await asyncio.sleep(delay)

    assert last_exception is not None
    raise RetryExhaustedError(
        f"All {cfg.max_attempts} attempts failed. Last error: {last_exception}"
    ) from last_exception
