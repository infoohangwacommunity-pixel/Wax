"""Provider failover (ADR-0024, mission §33/§34).

Scenario 8 of the mission: "Model A fails. Model B continues." Each
candidate carries its own classified retry + breaker; failover happens
after a candidate's own retry budget exhausts; the response records who
served; all-fail is an honest raise of the last error; misconfigured
fallbacks fail at boot, loudly.
"""

from __future__ import annotations

import pytest

from wax.core.config import settings_for_testing
from wax.intelligence.contracts import LLMRequest, LLMResponse, ProviderKind
from wax.intelligence.service import IntelligenceService


class FlakyProvider:
    """Fails `fail_times` completions, then serves."""

    kind = ProviderKind.MOCK

    def __init__(self, name: str, fail_times: int = 0) -> None:
        self._name = name
        self._fail_times = fail_times
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError(f"{self._name} is down")
        return LLMResponse(
            content=f"served by {self._name}",
            model="test-model",
            provider=ProviderKind.MOCK,
            finish_reason="stop",
            usage={"tokens_prompt": 1, "tokens_completion": 1, "tokens_total": 2},
        )

    def stream(self, request: LLMRequest):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def count_tokens(self, messages) -> int | None:  # pragma: no cover
        return None


def _request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}], model="test")


@pytest.mark.asyncio
async def test_primary_failure_fails_over_to_fallback() -> None:
    primary = FlakyProvider("A", fail_times=999)
    fallback = FlakyProvider("B")
    # No resilience wrapper: the failover boundary itself is under test.
    svc = IntelligenceService(primary, fallbacks=[fallback])

    response = await svc.complete(_request())
    assert response.content == "served by B"
    assert primary.calls >= 1, "the primary was tried first"
    assert fallback.calls == 1
    await svc.close()


@pytest.mark.asyncio
async def test_all_candidates_failing_raises_the_last_error() -> None:
    primary = FlakyProvider("A", fail_times=999)
    fallback = FlakyProvider("B", fail_times=999)
    svc = IntelligenceService(primary, fallbacks=[fallback])

    with pytest.raises(RuntimeError, match="B is down"):
        await svc.complete(_request())
    assert primary.calls >= 1 and fallback.calls >= 1
    await svc.close()


@pytest.mark.asyncio
async def test_primary_serving_means_no_failover_call() -> None:
    primary = FlakyProvider("A")
    fallback = FlakyProvider("B")
    svc = IntelligenceService(primary, fallbacks=[fallback])

    response = await svc.complete(_request())
    assert response.content == "served by A"
    assert fallback.calls == 0, "a healthy primary must not invoke fallbacks"
    await svc.close()


def test_fallback_list_parsing_skips_duplicates_and_rejects_unknown() -> None:
    from wax.core.exceptions import WaxConfigurationError

    settings = settings_for_testing()
    # Unknown kind raises at BOOT (loud), not at request time.
    settings.__dict__["llm_provider_fallbacks"] = "mistral, mock"
    with pytest.raises(WaxConfigurationError, match="mistral"):
        IntelligenceService._build_fallbacks(settings, exclude={"mock"})

    settings.__dict__["llm_provider_fallbacks"] = "mock, mock"
    candidates = IntelligenceService._build_fallbacks(settings, exclude={"mock"})
    assert candidates == [], "duplicates of the primary are skipped"


def test_empty_fallback_setting_yields_no_candidates() -> None:
    settings = settings_for_testing()
    settings.__dict__["llm_provider_fallbacks"] = ""
    assert IntelligenceService._build_fallbacks(settings, exclude={"mock"}) == []
