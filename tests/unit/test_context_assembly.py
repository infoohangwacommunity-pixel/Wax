"""Context assembly tests (ADR-0012).

The runtime owns WHAT evidence reaches the model: prioritized sections,
budget-filling, honest truncation. The bridge must deliver the evidence
it already collected (objective, conversation state, memories) instead of
discarding it — and never exceed the configured budget.
"""

from __future__ import annotations

from wax.continuity.assembly import (
    PRIORITY_CONVERSATION,
    PRIORITY_MEMORY,
    PRIORITY_OBJECTIVE,
    EvidenceSection,
    assemble_evidence,
    build_evidence_sections,
)
from wax.continuity.contracts import ContinuityContext


def _ctx(**overrides) -> ContinuityContext:
    base = dict(
        principal_id="p-assemble",
        is_new_conversation=True,
        recent_memories=[],
    )
    base.update(overrides)
    return ContinuityContext(**base)


class TestBuildEvidenceSections:
    def test_objective_evidence_is_emitted_with_execution_state(self) -> None:
        sections = build_evidence_sections(
            _ctx(
                active_objective_description="Help me prepare a DSS report",
                active_objective_id="obj-1",
                last_execution_status="succeeded",
            )
        )
        kinds = {s.kind for s in sections}
        assert "objective" in kinds
        obj = next(s for s in sections if s.kind == "objective")
        assert "DSS report" in obj.text
        assert "last execution: succeeded" in obj.text
        assert obj.priority == PRIORITY_OBJECTIVE

    def test_resumed_conversation_emits_facts_not_chatter(self) -> None:
        sections = build_evidence_sections(
            _ctx(
                is_new_conversation=False,
                days_since_last_message=3.5,
                conversation_summary="planning a birthday for Ngozi",
            )
        )
        conv = [s for s in sections if s.kind == "conversation"]
        assert len(conv) == 1
        assert "days ago" in conv[0].text
        assert "Ngozi" in conv[0].text

    def test_new_conversation_has_no_conversation_section(self) -> None:
        sections = build_evidence_sections(_ctx(is_new_conversation=True))
        assert all(s.kind != "conversation" for s in sections)

    def test_absent_evidence_produces_no_filler(self) -> None:
        assert build_evidence_sections(_ctx()) == []

    def test_memories_carry_reasons(self) -> None:
        sections = build_evidence_sections(
            _ctx(
                recent_memories=[
                    {"id": "m1", "summary": "exam is May 20", "reason": "recent+relevant"},
                    {"id": "m2", "summary": "", "reason": "recent"},  # empty → dropped
                ]
            )
        )
        memories = [s for s in sections if s.kind == "memory"]
        assert len(memories) == 1
        assert "recent+relevant" in memories[0].text
        assert memories[0].priority == PRIORITY_MEMORY


class TestAssembleEvidence:
    def test_fills_all_when_budget_is_generous(self) -> None:
        sections = [
            EvidenceSection("memory", PRIORITY_MEMORY, "a" * 50),
            EvidenceSection("objective", PRIORITY_OBJECTIVE, "b" * 50),
        ]
        lines = assemble_evidence(sections, budget_chars=10_000)
        assert len(lines) == 2
        # Priority order: objective first regardless of input order.
        assert lines[0].startswith("[evidence") or "b" in lines[0]
        assert "b" * 50 in lines[0]

    def test_tight_budget_keeps_priority_and_announces_truncation(self) -> None:
        sections = [
            EvidenceSection("objective", PRIORITY_OBJECTIVE, "OBJECTIVE-" + "x" * 400),
            EvidenceSection("conversation", PRIORITY_CONVERSATION, "CONV-" + "y" * 400),
            EvidenceSection("memory", PRIORITY_MEMORY, "MEM-" + "z" * 400),
        ]
        lines = assemble_evidence(sections, budget_chars=500)
        # The objective survives (highest priority)…
        assert any("OBJECTIVE" in line for line in lines)
        # …lower-priority evidence was cut, and the cut is visible —
        # the model is never silently shown a partial picture.
        assert any("truncated" in line for line in lines)
        total = sum(len(line) for line in lines)
        assert total <= 500 + len(lines)  # budget respected

    def test_zero_budget_delivers_nothing(self) -> None:
        lines = assemble_evidence(
            [EvidenceSection("objective", PRIORITY_OBJECTIVE, "x")],
            budget_chars=0,
        )
        assert lines == []

    def test_no_memory_dumps_beyond_budget(self) -> None:
        """The anti-'database dump' guarantee: 500 memories with a small
        budget produce bounded context, not 500 lines."""
        sections = [
            EvidenceSection("memory", PRIORITY_MEMORY, f"memory {i} " + "w" * 100)
            for i in range(500)
        ]
        lines = assemble_evidence(sections, budget_chars=3000)
        total = sum(len(line) for line in lines)
        assert len(lines) < 500
        assert total <= 3000 + len(lines)


class TestBridgeDeliversEvidence:
    def test_build_messages_includes_objective_and_memory_evidence(self, test_settings) -> None:
        from datetime import UTC, datetime

        from wax.intelligence.adapters.mock_provider import MockLLMProvider
        from wax.intelligence.contracts import MessageRole
        from wax.intelligence.service import IntelligenceService
        from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
        from wax.runtime.bridge.service import RuntimeBridge
        from wax.runtime.services import RuntimeServices

        services = RuntimeServices.build(test_settings)
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        context = _ctx(
            active_objective_description="Help me analyze a PDF",
            is_new_conversation=False,
            days_since_last_message=2.0,
            recent_memories=[
                {"id": "m1", "summary": "the PDF is a lab report", "reason": "relevant"}
            ],
        )
        request = RuntimeRequest(
            interface_message_id="msg-asm-1",
            interface_kind=InterfaceKind.WHATSAPP,
            sender_interface_id="+2348000000001",
            sender_display_name="Test",
            text="continue with the PDF",
            received_at=datetime.now(UTC),
        )
        messages = bridge._build_messages("Test", request, context, None)

        assert messages[0].role is MessageRole.SYSTEM  # minimal system prompt
        assert "analyze a PDF" not in messages[0].content  # not baked into persona
        evidence = [m.content for m in messages[1:-1]]
        assert any("[evidence: objective]" in e for e in evidence)
        assert any("[evidence: conversation]" in e for e in evidence)
        assert any("lab report" in e for e in evidence)
        assert messages[-1].content == "continue with the PDF"  # the user turn
