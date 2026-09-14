"""wax.reliability — production reliability primitives.

Production reliability is mandatory (Directive §56, §143, §144):
- retries with exponential backoff + jitter (no retry storms)
- dead-letter handling for terminal failures
- circuit breakers for failing dependencies
- webhook idempotency (already in Phase R bridge)
- queue recovery + restart safety

INVARIANTS:
- Retries are bounded (max attempts, max total time)
- Retry storms are impossible (jitter + circuit breakers)
- Failed operations are recorded (audit + dead-letter)
- The runtime never silently swallows failures (Directive §142)
"""

from wax.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from wax.reliability.dead_letter import (
    DeadLetterEntry,
    DeadLetterRepository,
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
    "DeadLetterEntry",
    "DeadLetterRepository",
    "RetryConfig",
    "RetryExhaustedError",
    "retry_with_backoff",
]
