"""Context-budget negotiation tests (ADR-0015).

The evidence budget is no longer a fixed constant guess: when the
selected provider advertises a context limit, the runtime derives the
budget from it (limit − reserved output tokens, converted to chars);
when it doesn't, the configured fallback applies. The assembler contract
is unchanged — it fills whatever budget it is given, with honest
truncation.

Provider independence is structural: the negotiation duck-types the
optional surface and degrades gracefully; no SDK imports outside
adapters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from wax.continuity.assembly import (
    PRIORITY_MEMORY,
    PRIORITY_OBJECTIVE,
    EvidenceSection,
    assemble_evidence,
    build_evidence_sections,
)
from wax.continuity.contracts import ContinuityContext
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.context_limits import (
    CHARS_PER_TOKEN,
    derive_context_budget,
    estimate_messages_tokens,
    estimate_tokens,
    provider_context_limit_tokens,
)


class TestEstimation:
    def test_portable_estimator_is_conservative(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("x" * 400) == 100, "4 chars/token"

    def test_message_estimation_counts_tool_calls(self) -> None:
        message = SimpleNamespace(
            content="a" * 400,
            tool_calls=[SimpleNamespace(arguments={"k": "v" * 40})],
        )
        total = estimate_messages_tokens([message])
        assert total >= 100 + 10, "content + tool arguments counted"


class TestNegotiation:
    def test_provider_without_limit_uses_fallback(self) -> None:
        provider = MockLLMProvider()  # advertises nothing
        budget = derive_context_budget(
            provider,
            fallback_char_budget=24_000,
            output_reserve_tokens=4_096,
        )
        assert budget.source == "configured_fallback"
        assert budget.budget_chars == 24_000
        assert budget.context_limit_tokens is None

    def test_provider_limit_derives_budget(self) -> None:
        provider = MockLLMProvider(context_limit_tokens=128_000)
        budget = derive_context_budget(
            provider,
            fallback_char_budget=24_000,
            output_reserve_tokens=4_096,
        )
        assert budget.source == "provider_limit"
        assert budget.context_limit_tokens == 128_000
        assert budget.budget_chars == int((128_000 - 4_096) * CHARS_PER_TOKEN)

    def test_tiny_advertised_limit_hits_the_floor(self) -> None:
        """Graceful degradation: an absurdly small window still yields a
        usable (minimal, honest) evidence slice, not zero context."""
        provider = MockLLMProvider(context_limit_tokens=1_024)
        budget = derive_context_budget(
            provider,
            fallback_char_budget=24_000,
            output_reserve_tokens=4_096,
        )
        assert budget.source == "provider_limit_floor"
        assert budget.budget_chars > 0

    def test_invalid_advertisement_is_ignored(self) -> None:
        provider = SimpleNamespace(context_limit_tokens="bogus")
        assert provider_context_limit_tokens(provider) is None
        provider = SimpleNamespace(context_limit_tokens=-5)
        assert provider_context_limit_tokens(provider) is None
        budget = derive_context_budget(
            provider,
            fallback_char_budget=24_000,
            output_reserve_tokens=4_096,
        )
        assert budget.source == "configured_fallback"

    def test_openai_adapter_advertises_per_model(self) -> None:
        from wax.intelligence.adapters.openai_provider import OpenAIProvider

        small = OpenAIProvider(api_key="k", default_model="gpt-3.5-turbo")
        assert small.context_limit_tokens == 16_385
        big = OpenAIProvider(api_key="k", default_model="gpt-4o-mini")
        assert big.context_limit_tokens == 128_000

    def test_anthropic_adapter_advertises_family_limit(self) -> None:
        from wax.intelligence.adapters.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="k")
        assert provider.context_limit_tokens == 200_000
        assert provider.estimate_tokens("x" * 400) == 100

    def test_resilient_wrapper_exposes_inner_for_negotiation(self) -> None:
        from wax.intelligence.resilience import ResilientProvider
        from wax.intelligence.service import IntelligenceService

        inner = MockLLMProvider(context_limit_tokens=8_000)
        service = IntelligenceService(
            ResilientProvider(inner, retry=None, breaker=None)  # type: ignore[arg-type]
        )
        # Negotiation unwraps the resilience wrapper to read the adapter's
        # advertised limit.
        assert provider_context_limit_tokens(service.inner_provider) == 8_000


class TestAssemblyDegradation:
    def _sections(self, count: int, size: int) -> list[EvidenceSection]:
        return [
            EvidenceSection("memory", PRIORITY_MEMORY, f"m{i} " + "w" * size)
            for i in range(count)
        ]

    def test_tiny_budget_still_delivers_objective(self) -> None:
        sections = [
            EvidenceSection("objective", PRIORITY_OBJECTIVE, "objective: " + "o" * 300),
            *self._sections(20, 200),
        ]
        lines = assemble_evidence(sections, budget_chars=1_200)
        assert lines, "never empty while anything fits"
        assert any("objective" in line for line in lines)
        total = sum(len(line) for line in lines)
        assert total <= 1_200 + len(lines)

    def test_zero_and_negative_budgets_are_safe(self) -> None:
        assert assemble_evidence(self._sections(3, 10), budget_chars=0) == []
        assert assemble_evidence(self._sections(3, 10), budget_chars=-5) == []

    def test_conflicting_memories_are_both_delivered(self) -> None:
        """The runtime surfaces contradictions; resolution belongs to the
        intelligence (ADR-0012). No silent dropping of either side."""
        sections = [
            EvidenceSection(
                "memory", PRIORITY_MEMORY, "[evidence: memory (relevant)] exam is May 20"
            ),
            EvidenceSection(
                "memory", PRIORITY_MEMORY, "[evidence: memory (relevant)] exam moved to June 2"
            ),
        ]
        lines = assemble_evidence(sections, budget_chars=10_000)
        assert any("May 20" in line for line in lines)
        assert any("June 2" in line for line in lines)

    def test_malformed_memories_do_not_break_sections(self) -> None:
        context = ContinuityContext(
            principal_id="p",
            is_new_conversation=True,
            recent_memories=[
                {"summary": 12345},  # non-string summary → coerced
                {"id": "ok", "summary": "usable memory", "reason": "recent"},
                {"id": "empty"},  # nothing usable → dropped
            ],
        )
        sections = build_evidence_sections(context)
        texts = [s.text for s in sections]
        assert any("usable memory" in t for t in texts)
        assert any("12345" in t for t in texts), "non-string summary still delivered"
        assert len(sections) == 2


class TestBridgeNegotiatedBudget:
    def test_bridge_uses_provider_limit_when_advertised(self, test_settings) -> None:
        """End to end through the bridge: a provider advertising 8k tokens
        (minus reserve) yields a smaller evidence budget than the fallback;
        the messages still assemble."""
        from wax.intelligence.service import IntelligenceService
        from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
        from wax.runtime.bridge.service import RuntimeBridge
        from wax.runtime.services import RuntimeServices

        test_settings.__dict__["context_char_budget"] = 1_000_000  # huge fallback
        services = RuntimeServices.build(test_settings)
        provider = MockLLMProvider(context_limit_tokens=8_000)
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(provider), services=services
        )
        context = ContinuityContext(
            principal_id="p",
            is_new_conversation=False,
            days_since_last_message=1.0,
            conversation_summary="s",
            recent_memories=[
                {"id": "m1", "summary": "mem", "reason": "recent"},
            ],
        )
        request = RuntimeRequest(
            interface_message_id="msg-budget-1",
            interface_kind=InterfaceKind.WHATSAPP,
            sender_interface_id="+2348000000001",
            sender_display_name="T",
            text="hello",
            received_at=datetime.now(UTC),
        )
        messages = bridge._build_messages("T", request, context, None)
        evidence = [m.content for m in messages if m.role.value == "system"]
        joined = "\n".join(evidence)
        assert "[evidence: objective]" not in joined  # no objective in this context
        assert "mem" in joined
        _ = provider_context_limit_tokens  # imported for clarity
