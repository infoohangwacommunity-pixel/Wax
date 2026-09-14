"""Context assembly — the runtime's evidence-delivery mechanism.

WAX separates two responsibilities that were historically conflated:

- The RUNTIME decides WHAT evidence exists and reaches the model, within
  a budget. It owns relevance ranking, recency, objective linkage, and
  size accounting. It never interprets.
- The INTELLIGENCE interprets the evidence it is given.

This module is the budget-aware half of that contract. ContinuityService
collects evidence (ContinuityContext); `build_evidence_sections` turns
that context into prioritized, per-item sections; `assemble_evidence`
fills a character budget by priority — whole entries first, honest
truncation when the budget runs out. Nothing here is a prompt recipe:
no "always use these five memories", no universal persona. The evidence
lines are labelled facts with their reason attached, so the model can
weigh them.

Budget accounting is character-based (≈4 chars per token) on purpose:
it is provider-independent, requires no tokenizer dependency in the
runtime core, and errs on the safe side for modern context windows.
The budget itself is NEGOTIATED (wax.intelligence.context_limits):
when the selected provider advertises a context limit, the budget is
derived from it (limit − reserved output tokens); otherwise the
configured `context_char_budget` fallback applies. This module's
contract is unchanged — it fills whatever budget it is given.
"""

from __future__ import annotations

from dataclasses import dataclass

from wax.continuity.contracts import ContinuityContext

# Priorities: lower is kept first when the budget is tight (mission §12:
# security constraints outrank objective, then active work, then memory,
# then interaction, then artifacts — implemented from repository evidence;
# the current-user-input slot is the message itself, delivered outside
# this budget). The objective outranks outstanding work, which outranks
# memory and conversation state; artifacts and environment facts come last.
PRIORITY_OBJECTIVE = 0
PRIORITY_ACTIVE_WORK = 1
PRIORITY_CONVERSATION = 2
PRIORITY_MEMORY = 3
PRIORITY_ARTIFACTS = 4
PRIORITY_ENVIRONMENT = 5

_MIN_TAIL = 200  # below this remaining budget, stop including evidence
_TRUNCATION_MARKER = "[evidence truncated: context budget reached]"


@dataclass
class EvidenceSection:
    """One labelled piece of runtime evidence, deliverable to a model."""

    kind: str
    # "objective" | "active_work" | "conversation" | "memory" |
    # "artifacts" | "environment"
    priority: int
    text: str


def build_evidence_sections(context: ContinuityContext) -> list[EvidenceSection]:
    """Translate collected continuity context into evidence sections.

    Only facts the runtime actually verified are emitted; absent evidence
    produces no section (no fabrication, no filler).
    """
    sections: list[EvidenceSection] = []

    if context.active_objective_description:
        text = f"[evidence: objective] {context.active_objective_description}"
        if context.last_execution_status:
            text += f" (last execution: {context.last_execution_status})"
        sections.append(EvidenceSection(kind="objective", priority=PRIORITY_OBJECTIVE, text=text))

    if not context.is_new_conversation:
        parts: list[str] = []
        if context.days_since_last_message is not None:
            if context.days_since_last_message < 1.0 / 24.0:
                parts.append("this conversation is ongoing")
            elif context.days_since_last_message < 1.0:
                parts.append("last message was earlier today")
            else:
                parts.append(f"last message was {context.days_since_last_message:.0f} days ago")
        if context.conversation_summary:
            parts.append(f"summary: {context.conversation_summary}")
        if parts:
            sections.append(
                EvidenceSection(
                    kind="conversation",
                    priority=PRIORITY_CONVERSATION,
                    text=f"[evidence: conversation] {'; '.join(parts)}",
                )
            )

    for memory in context.recent_memories:
        # Malformed evidence must never break assembly: non-dict entries
        # and entries without a usable summary are skipped (the runtime
        # delivers what it can verify, nothing else).
        if not isinstance(memory, dict):
            continue
        reason = memory.get("reason", "context")
        summary = memory.get("summary") or ""
        if not isinstance(summary, str):
            # A non-string summary is still evidence — coerce it rather
            # than silently dropping it.
            summary = str(summary)
        if not summary:
            summary = str(memory.get("content") or "")[:200]
        if not summary:
            continue
        sections.append(
            EvidenceSection(
                kind="memory",
                priority=PRIORITY_MEMORY,
                text=f"[evidence: memory ({reason})] {summary}",
            )
        )

    # Outstanding durable work (mission §4: critical active work state).
    # Metadata only — what pends, what wakes it — never payload contents.
    if context.active_work:
        parts: list[str] = []
        for item in context.active_work:
            wake = item.get("wake_at") or "on event"
            capability = item.get("capability") or "capability"
            parts.append(f"{capability} [{item.get('status')}] wakes {wake}")
        text = f"[evidence: active_work] {len(context.active_work)} pending: " + "; ".join(parts)
        sections.append(
            EvidenceSection(kind="active_work", priority=PRIORITY_ACTIVE_WORK, text=text)
        )

    # Known artifacts (mission §8: relevant artifacts). Integrity prefix
    # + size — never bytes.
    if context.recent_artifacts:
        parts = []
        for artifact in context.recent_artifacts:
            parts.append(
                f"{artifact.get('filename')} (sha256:{artifact.get('sha256')}, "
                f"{artifact.get('bytes')}B)"
            )
        text = "[evidence: artifacts] " + "; ".join(parts)
        sections.append(
            EvidenceSection(kind="artifacts", priority=PRIORITY_ARTIFACTS, text=text)
        )

    # Environment facts (interface the interaction arrived on).
    if context.environment:
        interface = context.environment.get("interface")
        if interface:
            text = f"[evidence: environment] interface={interface}"
            sections.append(
                EvidenceSection(
                    kind="environment", priority=PRIORITY_ENVIRONMENT, text=text
                )
            )

    return sections


def assemble_evidence(
    sections: list[EvidenceSection],
    *,
    budget_chars: int,
) -> list[str]:
    """Fill the budget by priority. Whole entries are preferred; when the
    budget would otherwise be wasted, the next entry is truncated to fit
    and truncation is ANNOUNCED — the model is never silently shown a
    partial picture, and the runtime never exceeds its budget.

    Returns the list of evidence strings (one per system evidence line).
    """
    if budget_chars <= 0:
        return []

    kept: list[str] = []
    remaining = budget_chars
    dropped_something = False

    for section in sorted(sections, key=lambda s: s.priority):
        if remaining < _MIN_TAIL:
            dropped_something = True
            break
        candidate = section.text
        if len(candidate) + 1 <= remaining:
            kept.append(candidate)
            remaining -= len(candidate) + 1
            continue
        # Does not fit whole. Truncate it honestly to use the remaining
        # budget, then stop (lower-priority evidence cannot follow).
        space = remaining - len(_TRUNCATION_MARKER) - 2
        if space >= _MIN_TAIL:
            kept.append(candidate[:space] + "… " + _TRUNCATION_MARKER)
        else:
            dropped_something = True
        dropped_something = True
        break

    # Never let the model silently miss evidence: if anything was cut,
    # say so (when the budget allows even the announcement).
    if dropped_something and remaining >= len(_TRUNCATION_MARKER) + 1:
        kept.append(_TRUNCATION_MARKER)

    return kept
