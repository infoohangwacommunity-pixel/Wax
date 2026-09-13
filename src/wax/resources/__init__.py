"""wax.resources — runtime resource management.

The runtime owns resources. The AI requests them. (Directive §41, §95)

Architecture:
- ResourceBudget: a per-execution limit on CPU, memory, time, API calls, etc.
- ResourceUsage: how much of a budget has been consumed
- ResourceAccountant: tracks usage, enforces limits, rejects over-budget requests

INVARIANT: Resource enforcement is a runtime responsibility. The AI cannot
grant itself more resources by producing text. Over-budget requests are
rejected by the runtime, not by the model.
"""

from wax.resources.accountant import ResourceAccountant
from wax.resources.contracts import ResourceBudget, ResourceKind, ResourceUsage

__all__ = ["ResourceAccountant", "ResourceBudget", "ResourceKind", "ResourceUsage"]
