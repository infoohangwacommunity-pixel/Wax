"""Context-budget negotiation — how much evidence fits this model?

The runtime assembles evidence under a BUDGET (ADR-0012). But the honest
budget depends on the model in play: 24k characters is comfortable for a
128k-token model and absurd for an 8k-token one. This module is the
negotiation layer between the runtime and whatever provider is selected:

- If the provider ADVERTISES a context limit (`context_limit_tokens`),
  the evidence budget is derived from it: (limit − reserved output tokens)
  converted to characters with the estimator's chars/token ratio.
- If it does not (mock providers, unusual endpoints), the configured
  `context_char_budget` fallback applies — provider-independent, errs safe.

Provider independence rules (INV-03):
- This module never imports a provider SDK. It duck-types the optional
  surface (`context_limit_tokens`, `estimate_tokens`) and degrades
  gracefully when absent.
- A provider adapter may expose a REAL tokenizer inside its own file;
  the runtime core stays tokenizer-free.

The estimator is deliberately conservative (≈4 chars/token English prose).
Over-estimating tokens shrinks evidence; under-estimating would overflow
the model's window. Budgeting errors must fall on the safe side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Conservative ratio for the estimator-free path. Measured English prose
# averages ~4 characters per token for cl100k-style tokenizers; code and
# non-Latin scripts can be denser. We choose the SAFE side.
CHARS_PER_TOKEN = 4.0

# Hard floor: below this character budget the evidence mechanism would
# deliver nothing useful — keep a minimal honest slice instead.
MIN_BUDGET_CHARS = 1200


def estimate_tokens(text: str) -> int:
    """Estimate token count for text with the portable estimator."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list[Any]) -> int:
    """Estimate token count for a message list (uses .content attributes)."""
    total = 0
    for message in messages:
        content = getattr(message, "content", "") or ""
        total += estimate_tokens(content)
        for call in getattr(message, "tool_calls", None) or []:
            total += estimate_tokens(str(getattr(call, "arguments", "")))
    return total


@dataclass(frozen=True)
class ContextBudget:
    """The negotiated evidence budget, with its provenance for observability."""

    budget_chars: int
    source: str  # "provider_limit" | "configured_fallback"
    context_limit_tokens: int | None
    reserved_output_tokens: int


def unwrap_provider(provider: Any) -> Any:
    """Follow `inner_provider` chains (e.g. ResilientProvider wrappers) to
    the concrete adapter, so negotiation reads the ADAPTER's advertised
    limit rather than the wrapper's absence of one."""
    seen: set[int] = set()
    current = provider
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        inner = getattr(current, "inner_provider", None)
        if inner is None or inner is current:
            break
        current = inner
    return current


def provider_context_limit_tokens(provider: Any) -> int | None:
    """Read the provider's advertised context limit, if it exposes one.

    Duck-typed: providers MAY expose `context_limit_tokens` (an int
    property). Wrappers are unwrapped first. Anything missing/invalid
    → None (no claim made).
    """
    raw = getattr(unwrap_provider(provider), "context_limit_tokens", None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def provider_estimate_tokens(provider: Any, text: str) -> int:
    """Estimate tokens for text, preferring the provider's own estimator.

    An adapter that bundles a real tokenizer exposes `estimate_tokens`;
    the portable estimator is the fallback. Either way the caller gets a
    number without importing any provider SDK here.
    """
    estimator = getattr(provider, "estimate_tokens", None)
    if callable(estimator):
        try:
            value = int(estimator(text))
            if value >= 0:
                return value
        except Exception:
            pass  # a broken estimator must never break assembly
    return estimate_tokens(text)


def derive_context_budget(
    provider: Any,
    *,
    fallback_char_budget: int,
    output_reserve_tokens: int,
) -> ContextBudget:
    """Derive the evidence char budget for the provider in play.

    Provider limit known:
        input_tokens_for_evidence = limit − output_reserve
        budget_chars = input_tokens_for_evidence × CHARS_PER_TOKEN
        (the estimator is chars-based, so the char budget is simply the
        token budget times the ratio — conservative by construction)
    Provider limit unknown:
        budget_chars = fallback_char_budget (the configured constant)

    The result never falls below MIN_BUDGET_CHARS so degradation stays
    graceful rather than collapsing to an empty context.
    """
    limit = provider_context_limit_tokens(provider)
    if limit is not None:
        evidence_tokens = max(limit - max(output_reserve_tokens, 0), 0)
        budget = int(evidence_tokens * CHARS_PER_TOKEN)
        if budget >= MIN_BUDGET_CHARS:
            return ContextBudget(
                budget_chars=budget,
                source="provider_limit",
                context_limit_tokens=limit,
                reserved_output_tokens=max(output_reserve_tokens, 0),
            )
        # A tiny advertised limit still gets the honest floor.
        return ContextBudget(
            budget_chars=MIN_BUDGET_CHARS,
            source="provider_limit_floor",
            context_limit_tokens=limit,
            reserved_output_tokens=max(output_reserve_tokens, 0),
        )
    return ContextBudget(
        budget_chars=max(int(fallback_char_budget), MIN_BUDGET_CHARS),
        source="configured_fallback",
        context_limit_tokens=None,
        reserved_output_tokens=0,
    )
