"""Tokenizer-exact context accounting tests (ADR-0018).

The primitive: each adapter owns its token accounting. The OpenAI
adapter counts EXACTLY when tiktoken is available (optional dependency,
lazy-cached, failure-safe); every adapter declares its counter's
provenance; the negotiation converts the adapter's token limit to chars
at the ADAPTER'S OWN calibrated ratio — never a core constant applied
to a foreign tokenizer.
"""

from __future__ import annotations

import pytest

from wax.intelligence.adapters.anthropic_provider import AnthropicProvider
from wax.intelligence.adapters.openai_provider import OpenAIProvider
from wax.intelligence.context_limits import (
    CHARS_PER_TOKEN,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    calibrate_chars_per_token,
    derive_context_budget,
    provider_estimate_messages_tokens,
    provider_token_counter,
)


def _openai(model: str = "gpt-4o") -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", default_model=model)


@pytest.fixture(autouse=True)
def _clean_encoding_cache():
    """The encoding cache is class-level by design (process-lifetime);
    tests must not leak load-failures into each other."""
    OpenAIProvider._encodings.clear()
    yield
    OpenAIProvider._encodings.clear()


class _Simple:
    """A duck-typed provider whose counter is exact under ITS scheme:
    one token per two characters. Deterministic, no SDK."""

    def __init__(self, *, broken: bool = False, no_counter: bool = False) -> None:
        self._broken = broken
        self.context_limit_tokens = 10_000
        if not no_counter:
            self.token_counter = "exact:test:2chars"

    def estimate_tokens(self, text: str) -> int:
        if self._broken:
            raise RuntimeError("counter exploded")
        return max(1, len(text) // 2)


class _NoEstimator:
    context_limit_tokens = 8_000


class TestAdapterOwnedCounters:
    def test_openai_counter_provenance(self):
        provider = _openai("gpt-4o")
        assert provider.token_counter == "tiktoken:o200k_base"

    def test_openai_unknown_model_falls_back_to_estimator(self):
        provider = _openai("totally-unknown-model")
        assert provider.token_counter == "estimate:4chars"
        # Estimator errors on the safe side (never returns 0 for text).
        assert provider.estimate_tokens("hello world") >= 1

    def test_openai_exact_counts_match_real_tokenizer(self):
        tiktoken = pytest.importorskip("tiktoken")
        provider = _openai("gpt-4o")
        for text in ["hello world", "hello world!", "", "def f(x):\n    return x*2"]:
            expected = len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
            assert provider.estimate_tokens(text) == expected

    def test_known_token_count_is_stable_across_versions(self):
        # A pinned, hand-verified count guards against silent encoding
        # drift: "hello world" is 2 tokens in both cl100k and o200k.
        provider_cl = _openai("gpt-4-turbo")
        provider_o = _openai("gpt-4o")
        assert provider_cl.estimate_tokens("hello world") == 2
        assert provider_o.estimate_tokens("hello world") == 2

    def test_openai_falls_back_when_tiktoken_missing(self, monkeypatch):
        provider = _openai("gpt-4o")
        import builtins

        real_import = builtins.__import__

        def _no_tiktoken(name, *args, **kwargs):
            if name == "tiktoken":
                raise ImportError("tiktoken is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_tiktoken)
        provider._encodings["o200k_base"] = None  # forget any cache
        # Wait — None means "not cached" to the loader; simulate the
        # failure cache explicitly instead:
        provider._encodings["o200k_base"] = False
        assert provider.token_counter == "estimate:4chars"
        assert provider.estimate_tokens("hello world") == max(1, len("hello world") // 4)

    def test_anthropic_counter_is_documented_estimator(self):
        provider = AnthropicProvider(api_key="test-key")
        assert provider.token_counter == "estimate:4chars"
        assert provider.estimate_tokens("hello world") == max(1, len("hello world") // 4)


class TestNegotiation:
    def test_budget_carries_counter_provenance(self):
        budget = derive_context_budget(
            _Simple(), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        assert budget.source == "provider_limit"
        assert budget.counter == "exact:test:2chars"
        assert 2.0 <= budget.chars_per_token <= 2.01

    def test_budget_uses_adapters_own_ratio(self):
        # The adapter counts 2 chars/token (floor-rounded); its 10k-token
        # limit therefore converts at ITS ratio, not the core's 4.0.
        budget = derive_context_budget(
            _Simple(), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        expected_ratio = calibrate_chars_per_token(_Simple())
        assert budget.chars_per_token == expected_ratio
        assert budget.budget_chars == int((10_000 - 1000) * expected_ratio)

    def test_calibration_clamped_low(self):
        class _Dense:
            context_limit_tokens = 10_000
            token_counter = "exact:test:dense"

            def estimate_tokens(self, text: str) -> int:
                return max(1, len(text))  # claims 1 token per char (max density)

        budget = derive_context_budget(
            _Dense(), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        assert budget.chars_per_token == MIN_CHARS_PER_TOKEN

    def test_calibration_clamped_high(self):
        class _Sparse:
            context_limit_tokens = 10_000
            token_counter = "exact:test:sparse"

            def estimate_tokens(self, text: str) -> int:
                return 1  # claims everything is one token

        budget = derive_context_budget(
            _Sparse(), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        assert budget.chars_per_token == MAX_CHARS_PER_TOKEN

    def test_broken_counter_degrades_to_portable_ratio(self):
        budget = derive_context_budget(
            _Simple(broken=True), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        assert budget.chars_per_token == CHARS_PER_TOKEN
        assert budget.source == "provider_limit"

    def test_no_counter_at_all_falls_back_cleanly(self):
        budget = derive_context_budget(
            _NoEstimator(), fallback_char_budget=1000, output_reserve_tokens=1000
        )
        assert budget.source == "provider_limit"
        assert budget.counter is None
        assert budget.chars_per_token == CHARS_PER_TOKEN
        assert budget.budget_chars == (8_000 - 1000) * CHARS_PER_TOKEN

    def test_floor_source_and_budget(self):
        class _Tiny:
            context_limit_tokens = 500
            token_counter = "estimate:4chars"

            def estimate_tokens(self, text: str) -> int:
                return max(1, len(text) // 4)

        # 500 - 300 = 200 evidence tokens x 4 chars = 800 chars < floor.
        budget = derive_context_budget(
            _Tiny(), fallback_char_budget=1000, output_reserve_tokens=300
        )
        assert budget.source == "provider_limit_floor"
        assert budget.budget_chars == 1200


class TestProvenanceHelpers:
    def test_provider_token_counter_duck_typing(self):
        assert provider_token_counter(_Simple()) == "exact:test:2chars"
        assert provider_token_counter(_NoEstimator()) is None
        assert provider_token_counter(object()) is None

    def test_messages_counted_by_provider_counter(self):
        from types import SimpleNamespace

        messages = [
            SimpleNamespace(content="a" * 100, tool_calls=None),
            SimpleNamespace(content="b" * 50, tool_calls=None),
        ]
        # _Simple counts 1 token per 2 chars → 50 + 25.
        assert provider_estimate_messages_tokens(_Simple(), messages) == 75

    def test_messages_counted_with_broken_counter_still_works(self):
        from types import SimpleNamespace

        messages = [SimpleNamespace(content="hello world", tool_calls=None)]
        result = provider_estimate_messages_tokens(_Simple(broken=True), messages)
        assert result == max(1, len("hello world") // CHARS_PER_TOKEN)


class TestOpenAIInNegotiation:
    def test_openai_budget_uses_exact_ratio(self):
        pytest.importorskip("tiktoken")
        provider = _openai("gpt-4o")
        budget = derive_context_budget(
            provider, fallback_char_budget=1000, output_reserve_tokens=4096
        )
        assert budget.source == "provider_limit"
        assert budget.counter == "tiktoken:o200k_base"
        # The calibrated ratio is consistent with the adapter's counter
        # (o200k compresses English prose densely; the ratio lands under
        # the 6.0 safety clamp).
        assert 3.0 <= budget.chars_per_token <= MAX_CHARS_PER_TOKEN
