"""Circuit breaker — protects against cascading failures.

When a dependency is failing repeatedly, the circuit opens and fast-fails
subsequent calls instead of waiting for them to time out. After a cooldown
period, the circuit half-opens: one trial call is allowed. If it succeeds,
the circuit closes; if it fails, the circuit re-opens.

States:
- CLOSED: normal operation; calls go through
- OPEN: calls fast-fail with CircuitOpenError; no calls attempted
- HALF_OPEN: one trial call is allowed; if it succeeds → CLOSED, else → OPEN

INVARIANT: The circuit breaker prevents retry storms. When the LLM is
down, we don't pile up requests waiting for timeouts.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from threading import Lock
from typing import TypeVar

from wax.core.exceptions import WaxError

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(WaxError):
    """Raised when the circuit is open and a call is attempted."""


class CircuitBreaker:
    """A circuit breaker for a single dependency.

    Use:
        breaker = CircuitBreaker(name="openai_api", failure_threshold=5)
        result = await breaker.call(lambda: openai_client.complete(req))
    """

    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout < 1.0:
            raise ValueError("recovery_timeout must be at least 1 second")

        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """Execute operation through the circuit breaker.

        Raises CircuitOpenError if the circuit is open.
        Raises the original exception if the operation fails.
        """
        if not self._allow_call():
            raise CircuitOpenError(
                f"Circuit '{self._name}' is OPEN. Failing fast."
            )

        try:
            result = await operation()
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _allow_call(self) -> bool:
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                return False
            # HALF_OPEN
            if self._half_open_calls < self._half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def _maybe_transition_to_half_open(self) -> None:
        """If circuit is OPEN and recovery_timeout has passed, go HALF_OPEN."""
        if self._state != CircuitState.OPEN:
            return
        if self._last_failure_time is None:
            return
        if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                # Trial failed — re-open
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN

    def _record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                # Trial succeeded — close the circuit
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._half_open_calls = 0
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success (sliding window)
                self._failure_count = 0

    def reset(self) -> None:
        """Manually reset the circuit to CLOSED."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            self._half_open_calls = 0
