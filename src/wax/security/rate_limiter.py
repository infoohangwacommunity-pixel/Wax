"""Rate limiter — token-bucket per principal.

The token bucket algorithm: each principal has a bucket that refills at
a fixed rate. Each request consumes a token. If the bucket is empty,
the request is rate-limited.

This prevents a single principal from overwhelming the runtime (and
the LLM provider, which costs money per token).

INVARIANT: Rate limits are enforced by the runtime, never by the model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from wax.runtime.logging import get_logger

log = get_logger(__name__)


class RateLimitDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True)
class RateLimitConfig:
    """Token bucket configuration.

    Defaults: 10 messages per minute per principal (burst of 20).
    """

    capacity: int = 20  # max tokens in bucket
    refill_rate: float = 10.0 / 60.0  # tokens per second (10 per minute)
    initial_tokens: float | None = None  # default: full capacity


class RateLimiter:
    """Per-principal token bucket rate limiter.

    In-memory; for production, use Redis with INCRBY + EXPIRE.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._buckets: dict[
            str, tuple[float, float]
        ] = {}  # principal_id → (tokens, last_refill_time)
        self._lock = Lock()

    def check(self, principal_id: str) -> RateLimitDecision:
        """Check whether a principal may make a request now.

        Atomically consumes a token if allowed.
        """
        with self._lock:
            now = time.monotonic()
            if principal_id not in self._buckets:
                # First request — full bucket
                initial = (
                    self._config.initial_tokens
                    if self._config.initial_tokens is not None
                    else float(self._config.capacity)
                )
                self._buckets[principal_id] = (initial, now)

            tokens, last_refill = self._buckets[principal_id]
            # Refill based on elapsed time
            elapsed = now - last_refill
            new_tokens = min(
                float(self._config.capacity),
                tokens + elapsed * self._config.refill_rate,
            )

            if new_tokens >= 1.0:
                new_tokens -= 1.0
                self._buckets[principal_id] = (new_tokens, now)
                return RateLimitDecision.ALLOWED

            # Rate limited
            self._buckets[principal_id] = (new_tokens, now)
            log.warning(
                "rate_limited",
                principal_id=principal_id,
                tokens_remaining=round(new_tokens, 2),
            )
            return RateLimitDecision.DENIED

    def get_tokens(self, principal_id: str) -> float:
        """Return current token count (without consuming)."""
        with self._lock:
            if principal_id not in self._buckets:
                return float(self._config.capacity)
            tokens, last_refill = self._buckets[principal_id]
            now = time.monotonic()
            elapsed = now - last_refill
            return min(
                float(self._config.capacity),
                tokens + elapsed * self._config.refill_rate,
            )

    def reset(self, principal_id: str) -> None:
        """Reset a principal's bucket (manual override)."""
        with self._lock:
            self._buckets.pop(principal_id, None)
