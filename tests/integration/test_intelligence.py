"""Integration tests for Phase K (Intelligence)."""

from __future__ import annotations

import pytest

from wax.core.config import settings_for_testing
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderKind,
)
from wax.intelligence.service import IntelligenceService


class TestMockProvider:
    async def test_complete_returns_response(self) -> None:
        provider = MockLLMProvider()
        response = await provider.complete(
            LLMRequest(
                messages=[
                    LLMMessage(role=MessageRole.SYSTEM, content="be helpful"),
                    LLMMessage(role=MessageRole.USER, content="hello there"),
                ],
            )
        )
        assert response.provider == ProviderKind.MOCK
        assert "hello there" in response.content
        assert response.finish_reason == "stop"
        assert response.usage["tokens_total"] > 0
        assert response.raw_metadata["mock"] is True

    async def test_stream_yields_chunks(self) -> None:
        provider = MockLLMProvider()
        chunks: list[LLMStreamChunk] = []
        async for chunk in provider.stream(
            LLMRequest(messages=[LLMMessage(MessageRole.USER, "stream me")])
        ):
            chunks.append(chunk)
        assert len(chunks) > 1
        # Final chunk has finish_reason
        assert chunks[-1].finish_reason == "stop"

    async def test_close_is_noop(self) -> None:
        provider = MockLLMProvider()
        await provider.close()  # no exception


class TestIntelligenceService:
    async def test_from_settings_default_uses_mock(self) -> None:
        s = settings_for_testing()
        svc = IntelligenceService.from_settings(s)
        assert svc.provider_kind == ProviderKind.MOCK

    async def test_from_settings_explicit_mock(self) -> None:
        s = settings_for_testing(llm_default_provider="mock")
        svc = IntelligenceService.from_settings(s)
        assert svc.provider_kind == ProviderKind.MOCK

    async def test_from_settings_openai_requires_key(self) -> None:
        from wax.core.exceptions import WaxConfigurationError

        s = settings_for_testing(llm_default_provider="openai", openai_api_key="")
        with pytest.raises(WaxConfigurationError, match="OPENAI_API_KEY"):
            IntelligenceService.from_settings(s)

    async def test_from_settings_unknown_raises(self) -> None:
        from wax.core.exceptions import WaxConfigurationError

        s = settings_for_testing(llm_default_provider="unknown_provider")
        with pytest.raises(WaxConfigurationError, match="Unknown LLM provider"):
            IntelligenceService.from_settings(s)

    async def test_complete_routes_to_provider(self) -> None:
        svc = IntelligenceService(MockLLMProvider())
        response = await svc.complete(
            LLMRequest(messages=[LLMMessage(MessageRole.USER, "test message")])
        )
        assert isinstance(response, LLMResponse)
        assert response.provider == ProviderKind.MOCK

    async def test_stream_routes_to_provider(self) -> None:
        svc = IntelligenceService(MockLLMProvider())
        chunks = []
        async for chunk in svc.stream(LLMRequest(messages=[LLMMessage(MessageRole.USER, "test")])):
            chunks.append(chunk)
        assert len(chunks) > 0


class TestProviderIsolationArchitecture:
    """INV-03: provider SDKs must not be imported outside wax.intelligence.adapters."""

    def test_openai_sdk_not_in_core(self) -> None:
        """wax.core must not import openai."""
        import importlib
        import pkgutil

        import wax.core

        violations = []
        for module_info in pkgutil.walk_packages(wax.core.__path__, prefix="wax.core."):
            try:
                mod = importlib.import_module(module_info.name)
            except Exception:
                continue
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name, None)
                if attr is None:
                    continue
                module_str = getattr(attr, "__module__", "") or ""
                if module_str.startswith("openai") or module_str.startswith("anthropic"):
                    violations.append((module_info.name, module_str))

        assert not violations, (
            "wax.core imports provider SDKs (violates INV-03):\n  "
            + "\n  ".join(f"{m} -> {s}" for m, s in violations)
        )
