"""Tests for Phase V — Reliability."""

from __future__ import annotations

import pytest

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
from wax.state.engine import dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


# ===========================================================================
# Retry
# ===========================================================================


class TestRetryConfig:
    def test_defaults(self) -> None:
        cfg = RetryConfig()
        assert cfg.max_attempts == 3
        assert cfg.base_delay == 1.0
        assert cfg.max_delay == 30.0

    def test_rejects_zero_attempts(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            RetryConfig(max_attempts=0)

    def test_rejects_too_many_attempts(self) -> None:
        with pytest.raises(ValueError, match="at most 10"):
            RetryConfig(max_attempts=11)

    def test_compute_delay_grows_exponentially(self) -> None:
        cfg = RetryConfig(base_delay=1.0, jitter_factor=0.0)
        # Without jitter, delay is exactly 2^(attempt-1) * base
        d1 = cfg.compute_delay(1)  # 1.0
        d2 = cfg.compute_delay(2)  # 2.0
        d3 = cfg.compute_delay(3)  # 4.0
        assert d1 == 1.0
        assert d2 == 2.0
        assert d3 == 4.0

    def test_compute_delay_capped_at_max(self) -> None:
        cfg = RetryConfig(base_delay=1.0, max_delay=5.0, jitter_factor=0.0)
        d_high = cfg.compute_delay(10)  # would be 512 without cap
        assert d_high == 5.0


class TestRetryWithBackoff:
    async def test_succeeds_on_first_attempt(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = await retry_with_backoff(
            op, RetryConfig(max_attempts=3, base_delay=0.01, jitter_factor=0.0)
        )
        assert result == "ok"
        assert calls == 1

    async def test_retries_on_failure_then_succeeds(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("transient")
            return "ok"

        result = await retry_with_backoff(
            op, RetryConfig(max_attempts=3, base_delay=0.01, jitter_factor=0.0)
        )
        assert result == "ok"
        assert calls == 3

    async def test_exhausts_retries_raises(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("permanent")

        with pytest.raises(RetryExhaustedError, match="3 attempts failed"):
            await retry_with_backoff(
                op, RetryConfig(max_attempts=3, base_delay=0.01, jitter_factor=0.0)
            )
        assert calls == 3

    async def test_on_retry_callback_called(self) -> None:
        calls = 0
        retries: list[tuple[int, str, float]] = []

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("transient")
            return "ok"

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            retries.append((attempt, str(exc), delay))

        await retry_with_backoff(
            op,
            RetryConfig(max_attempts=3, base_delay=0.01, jitter_factor=0.0),
            on_retry=on_retry,
        )
        assert len(retries) == 2
        assert retries[0][0] == 1  # first retry after attempt 1
        assert "transient" in retries[0][1]

    async def test_non_retryable_exception_not_retried(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise ValueError("not retryable")

        cfg = RetryConfig(
            max_attempts=3,
            base_delay=0.01,
            retryable_exceptions=(RuntimeError,),  # ValueError not retryable
        )
        with pytest.raises(ValueError):
            await retry_with_backoff(op, cfg)
        assert calls == 1  # not retried


# ===========================================================================
# Circuit Breaker
# ===========================================================================


class TestCircuitBreaker:
    async def test_starts_closed(self) -> None:
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        assert breaker.state == CircuitState.CLOSED

    async def test_calls_pass_through_when_closed(self) -> None:
        breaker = CircuitBreaker(name="test", failure_threshold=3)

        async def op() -> str:
            return "ok"

        result = await breaker.call(op)
        assert result == "ok"

    async def test_opens_after_threshold_failures(self) -> None:
        breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=60.0)

        async def fail() -> str:
            raise RuntimeError("dep down")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(fail)

        assert breaker.state == CircuitState.OPEN

    async def test_open_circuit_fast_fails(self) -> None:
        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60.0)

        async def fail() -> str:
            raise RuntimeError("dep down")

        with pytest.raises(RuntimeError):
            await breaker.call(fail)
        assert breaker.state == CircuitState.OPEN

        # Now even a successful op should fast-fail
        async def ok() -> str:
            return "ok"

        with pytest.raises(CircuitOpenError):
            await breaker.call(ok)

    async def test_reset_returns_to_closed(self) -> None:
        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60.0)

        async def fail() -> str:
            raise RuntimeError("dep down")

        with pytest.raises(RuntimeError):
            await breaker.call(fail)
        assert breaker.state == CircuitState.OPEN

        breaker.reset()
        assert breaker.state == CircuitState.CLOSED


# ===========================================================================
# Dead-letter
# ===========================================================================
