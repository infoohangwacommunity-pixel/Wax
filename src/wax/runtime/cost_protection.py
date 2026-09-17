"""Cost protection — lightweight global spending protection (Part 25).

Protects the business, NOT the intelligence. The AI does not negotiate
execution budgets. This is a simple daily spend cap per principal.

If a principal exceeds their daily cap, the bridge returns a friendly
message without calling the LLM. No complex subsystem. No authority.
Just a number and a counter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CostProtector:
    """Per-principal daily cost protection.

    In-memory only. Resets on restart. Tracks estimated LLM cost per
    principal per day. If over budget, refuses to call the LLM.

    The budget is in USD cents (1 = $0.01). Default: $5/day per principal.
    """

    daily_budget_cents: int = 500  # $5.00/day
    _spend: dict[str, list[tuple[float, int]]] = field(default_factory=lambda: {})

    def record_spend(self, principal_id: str, cents: int) -> None:
        """Record a cost for a principal."""
        now = time.monotonic()
        cutoff = now - 86400.0  # 24 hours
        self._spend[principal_id] = [
            (t, c) for t, c in self._spend.get(principal_id, []) if t > cutoff
        ]
        self._spend[principal_id].append((now, cents))

    def daily_spend_cents(self, principal_id: str) -> int:
        """Return total spend in the last 24 hours."""
        now = time.monotonic()
        cutoff = now - 86400.0
        return sum(c for t, c in self._spend.get(principal_id, []) if t > cutoff)

    def remaining_cents(self, principal_id: str) -> int:
        """Return remaining daily budget in cents."""
        return max(0, self.daily_budget_cents - self.daily_spend_cents(principal_id))

    def can_spend(self, principal_id: str, estimated_cents: int = 0) -> bool:
        """Check if the principal has budget remaining."""
        return self.daily_spend_cents(principal_id) + estimated_cents <= self.daily_budget_cents

    def reset_if_new_day(self, principal_id: str) -> None:
        """Prune old entries (called automatically by record_spend)."""
        # record_spend already prunes; this is a no-op kept for clarity.
        pass


def estimate_llm_cost_cents(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    rate_per_1k_prompt_cents: float = 0.5,
    rate_per_1k_completion_cents: float = 1.5,
) -> int:
    """Estimate the cost of an LLM call in USD cents.

    Default rates are rough averages for GPT-4-class models. The actual
    rate depends on the provider. This is a rough estimate for cost
    protection — not a billing system.
    """
    prompt_cost = (prompt_tokens / 1000.0) * rate_per_1k_prompt_cents
    completion_cost = (completion_tokens / 1000.0) * rate_per_1k_completion_cents
    return max(1, int((prompt_cost + completion_cost) * 100))
