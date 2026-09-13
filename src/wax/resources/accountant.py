"""Resource accountant — enforces resource budgets.

The accountant is the SOLE authority on "may this execution consume X more
of resource Y?". Every resource-consuming operation must call `try_consume()`
BEFORE performing the operation. If it returns False, the operation MUST be
skipped (or the execution terminated).

INVARIANT: Resource enforcement is a runtime responsibility (Directive §41).
The AI cannot grant itself more resources by producing text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from wax.resources.contracts import (
    BudgetAllocation,
    ResourceBudget,
    ResourceKind,
    ResourceUsage,
)
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class ResourceAccountant:
    """Tracks resource budgets and enforces limits.

    Thread-safe via a single Lock. For high-throughput scenarios, consider
    per-execution locks — but for now, simplicity wins.
    """

    def __init__(self) -> None:
        self._allocations: dict[str, BudgetAllocation] = {}
        self._lock = Lock()

    def allocate(self, execution_id: str, **limits: float) -> BudgetAllocation:
        """Create a new budget allocation for an execution.

        Keyword arguments are ResourceKind value → limit. Example:
            accountant.allocate("exec_123", execution_time_seconds=300.0,
                                 llm_tokens=100_000, llm_calls=50)
        """
        with self._lock:
            if execution_id in self._allocations:
                # Idempotent: if same limits, return existing; else replace.
                log.warning(
                    "resources.reallocating",
                    execution_id=execution_id,
                    existing_budgets=len(self._allocations[execution_id].budgets),
                )

            allocation = BudgetAllocation(execution_id=execution_id)
            for kind_str, limit in limits.items():
                kind = ResourceKind(kind_str)
                allocation.set_limit(kind, float(limit))

            self._allocations[execution_id] = allocation
            log.info(
                "resources.allocated",
                execution_id=execution_id,
                budgets={k: v.limit for k, v in allocation.budgets.items()},
            )
            return allocation

    def get_allocation(self, execution_id: str) -> BudgetAllocation | None:
        with self._lock:
            return self._allocations.get(execution_id)

    def try_consume(self, usage: ResourceUsage) -> bool:
        """Atomically check + consume.

        Returns True if the consumption was allowed (and the budget was
        updated), False if the budget would have been exceeded (and no
        consumption was recorded).
        """
        with self._lock:
            allocation = self._allocations.get(usage.execution_id)
            if allocation is None:
                log.warning(
                    "resources.no_allocation",
                    execution_id=usage.execution_id,
                    kind=usage.kind.value,
                )
                # If no allocation exists, deny by default.
                return False

            budget = allocation.budgets.get(usage.kind.value)
            if budget is None:
                # No limit set for this kind — allow without tracking.
                return True

            if budget.consumed + usage.amount > budget.limit:
                log.warning(
                    "resources.exhausted",
                    execution_id=usage.execution_id,
                    kind=usage.kind.value,
                    consumed=budget.consumed,
                    limit=budget.limit,
                    requested=usage.amount,
                )
                return False

            budget.consumed += usage.amount
            log.debug(
                "resources.consumed",
                execution_id=usage.execution_id,
                kind=usage.kind.value,
                consumed=budget.consumed,
                limit=budget.limit,
                remaining=budget.remaining,
            )
            return True

    def remaining(self, execution_id: str, kind: ResourceKind) -> float | None:
        """Return remaining budget for a (execution, kind) pair, or None if no allocation."""
        with self._lock:
            allocation = self._allocations.get(execution_id)
            if allocation is None:
                return None
            budget = allocation.budgets.get(kind.value)
            if budget is None:
                return None
            return budget.remaining

    def release(self, execution_id: str) -> BudgetAllocation | None:
        """Release an allocation (call when an execution ends).

        Returns the final allocation (for audit logging) or None if
        no allocation existed.
        """
        with self._lock:
            allocation = self._allocations.pop(execution_id, None)
            if allocation is not None:
                log.info(
                    "resources.released",
                    execution_id=execution_id,
                    final_usage={
                        k: {"consumed": v.consumed, "limit": v.limit}
                        for k, v in allocation.budgets.items()
                    },
                )
            return allocation
