"""Context-budget negotiation — how much evidence fits this model?

The runtime assembles evidence under a BUDGET (ADR-0012). But the honest
budget depends on the model in play: 24k characters is comfortable for a
128k-token model and absurd for an 8k-token one. This module is the
negotiation layer between the runtime and whatever provider is selected:

- If the provider ADVERTISES a context limit (`context_limit_tokens`),
  the evidence budget is derived from it: (limit − reserved output tokens)
  converted to characters at the ADAPTER'S OWN chars/token ratio —
  calibrated from the adapter's token counter, which may be an exact
  tokenizer (tiktoken inside the OpenAI adapter, ADR-0018).
- If it does not (mock providers, unusual endpoints), the configured
  `context_char_budget` fallback applies — provider-independent, errs safe.

Provider independence rules (INV-03):
- This module never imports a provider SDK. It duck-types the optional
  surface (`context_limit_tokens`, `estimate_tokens`, `token_counter`)
  and degrades gracefully when absent.
- A provider adapter exposes a REAL tokenizer inside its own file; the
  runtime core stays tokenizer-free (INV-03). The negotiation only
  ASKS the adapter what things cost.

The estimator is deliberately conservative (≈4 chars/token English prose).
Over-estimating tokens shrinks evidence; under-estimating would overflow
the model's window. Budgeting errors must fall on the safe side — the
calibration clamp below enforces that even when an adapter lies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Conservative ratio for the estimator-free path. Measured English prose
# averages ~4 characters per token for cl100k-style tokenizers; code and
# non-Latin scripts can be denser. We choose the SAFE side.
CHARS_PER_TOKEN = 4.0

# Calibration sample for deriving an adapter's chars/token ratio. Plain
# English prose — the dominant shape of conversation and evidence text.
_CALIBRATION_TEXT = (
    "The runtime keeps the evidence under a budget so the model window "
    "is never silently exceeded. Relevant memories arrive first, then "
    "the current conversation, then the objective. When the space runs "
    "out the assembly announces exactly what was dropped and why."
)

# The calibrated ratio is clamped hard: below 2.0 chars/token an adapter
# would be claiming implausibly huge token counts (which would starve the
# evidence budget); above 6.0 it would be under-counting (which would
# overflow the window). Both edges lie; the clamp keeps errors safe.
MIN_CHARS_PER_TOKEN = 2.0
MAX_CHARS_PER_TOKEN = 6.0

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


def provider_estimate_messages_tokens(provider: Any, messages: list[Any]) -> int:
    """Token count for a message list under the provider's OWN counter.

    Same contract as `estimate_messages_tokens`, but each text is counted
    by the adapter's counter (exact when tiktoken-backed, ADR-0018) with
    the portable estimator as fallback. Broken adapters degrade per-text
    without failing the request path.
    """
    total = 0
    for message in messages:
        content = getattr(message, "content", "") or ""
        total += provider_estimate_tokens(provider, content)
        for call in getattr(message, "tool_calls", None) or []:
            total += provider_estimate_tokens(
                provider, str(getattr(call, "arguments", ""))
            )
    return total


@dataclass(frozen=True)
class ContextBudget:
    """The negotiated evidence budget, with its provenance for observability.

    `counter` records WHO did the token math: an exact tokenizer
    ("tiktoken:o200k_base"), the portable estimator ("estimate:4chars"),
    or None (no provider involvement — configured fallback).
    """

    budget_chars: int
    source: str  # "provider_limit" | "provider_limit_floor" | "configured_fallback"
    context_limit_tokens: int | None
    reserved_output_tokens: int
    chars_per_token: float = CHARS_PER_TOKEN
    counter: str | None = None


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
    """Estimate tokens for text, preferring the provider's own counter.

    An adapter that bundles a real tokenizer exposes `estimate_tokens`
    (exact when tiktoken-backed — ADR-0018); the portable estimator is
    the fallback. Either way the caller gets a number without importing
    any provider SDK here.
    """
    estimator = getattr(provider, "estimate_tokens", None)
    if callable(estimator):
        try:
            value = int(estimator(text))
            if value >= 0:
                return value
        except Exception:
            pass  # a broken counter must never break assembly
    return estimate_tokens(text)


def provider_token_counter(provider: Any) -> str | None:
    """Read the provider's token-accounting provenance, if it declares one.

    Adapters MAY expose `token_counter` (a string like
    "tiktoken:o200k_base" or "estimate:4chars"). Duck-typed; anything
    missing/non-string → None.
    """
    raw = getattr(unwrap_provider(provider), "token_counter", None)
    if isinstance(raw, str) and raw:
        return raw
    return None


def calibrate_chars_per_token(provider: Any) -> float:
    """Derive the adapter's OWN chars/token ratio from its counter.

    Asks the adapter to count a fixed calibration sample and computes
    the implied ratio. When the adapter's counter is exact (tiktoken),
    the char budget derived from its token limit is consistent with the
    SAME counter — no cross-tokenizer drift. When the adapter exposes
    no counter, the portable 4.0 applies.

    The result is clamped to [MIN_CHARS_PER_TOKEN, MAX_CHARS_PER_TOKEN]
    so a pathological adapter cannot starve the evidence budget or
    overflow the window.
    """
    estimator = getattr(unwrap_provider(provider), "estimate_tokens", None)
    if not callable(estimator):
        return CHARS_PER_TOKEN
    try:
        counted = int(estimator(_CALIBRATION_TEXT))
    except Exception:
        return CHARS_PER_TOKEN
    if counted <= 0:
        return CHARS_PER_TOKEN
    ratio = len(_CALIBRATION_TEXT) / counted
    return max(MIN_CHARS_PER_TOKEN, min(MAX_CHARS_PER_TOKEN, ratio))


def derive_context_budget(
    provider: Any,
    *,
    fallback_char_budget: int,
    output_reserve_tokens: int,
) -> ContextBudget:
    """Derive the evidence char budget for the provider in play.

    Provider limit known:
        input_tokens_for_evidence = limit − output_reserve
        budget_chars = input_tokens_for_evidence × (the ADAPTER'S OWN
        chars/token ratio, calibrated from its counter — exact when the
        adapter has a real tokenizer, ADR-0018)
    Provider limit unknown:
        budget_chars = fallback_char_budget (the configured constant)

    The result never falls below MIN_BUDGET_CHARS so degradation stays
    graceful rather than collapsing to an empty context.
    """
    limit = provider_context_limit_tokens(provider)
    counter = provider_token_counter(provider)
    if limit is not None:
        ratio = calibrate_chars_per_token(provider)
        evidence_tokens = max(limit - max(output_reserve_tokens, 0), 0)
        budget = int(evidence_tokens * ratio)
        if budget >= MIN_BUDGET_CHARS:
            return ContextBudget(
                budget_chars=budget,
                source="provider_limit",
                context_limit_tokens=limit,
                reserved_output_tokens=max(output_reserve_tokens, 0),
                chars_per_token=ratio,
                counter=counter,
            )
        # A tiny advertised limit still gets the honest floor.
        return ContextBudget(
            budget_chars=MIN_BUDGET_CHARS,
            source="provider_limit_floor",
            context_limit_tokens=limit,
            reserved_output_tokens=max(output_reserve_tokens, 0),
            chars_per_token=ratio,
            counter=counter,
        )
    return ContextBudget(
        budget_chars=max(int(fallback_char_budget), MIN_BUDGET_CHARS),
        source="configured_fallback",
        context_limit_tokens=None,
        reserved_output_tokens=0,
        chars_per_token=CHARS_PER_TOKEN,
        counter=counter,
    )
