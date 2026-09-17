"""wax.reliability — production reliability primitives.

Production reliability is mandatory (Directive §56, §143, §144):
- retries with exponential backoff + jitter (no retry storms)
- terminal-failure audit recording (via observability.audit)
- circuit breakers for failing dependencies
- webhook idempotency (already in Phase R bridge)
- queue recovery + restart safety

INVARIANTS:
- Retries are bounded (max attempts, max total time)
- Retry storms are impossible (jitter + circuit breakers)
- Failed operations are recorded in the audit ledger
- The runtime never silently swallows failures (Directive §142)
"""

from wax.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from wax.reliability.retry import (
    RetryConfig,
    RetryExhaustedError,
    retry_with_backoff,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "RetryConfig",
    "RetryExhaustedError",
    "retry_with_backoff",
]
