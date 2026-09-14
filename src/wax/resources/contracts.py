"""Contracts for the resource system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ResourceKind(StrEnum):
    """Discriminator for the kinds of resources WAX manages.

    Adding a new kind here is a deliberate architectural act — it means
    a new resource category needs runtime enforcement.
    """

    EXECUTION_TIME_SECONDS = "execution_time_seconds"
    CPU_SECONDS = "cpu_seconds"
    MEMORY_BYTES = "memory_bytes"
    LLM_TOKENS = "llm_tokens"
    LLM_CALLS = "llm_calls"
    CAPABILITY_INVOCATIONS = "capability_invocations"
    NETWORK_BYTES = "network_bytes"
    STORAGE_BYTES = "storage_bytes"


@dataclass
class ResourceBudget:
    """A limit on how much of a resource an execution may consume.

    The budget is a hard cap. The accountant rejects requests that would
    exceed the cap. The AI cannot negotiate — it may either proceed within
    the budget or terminate the execution.
    """

    execution_id: str
    kind: ResourceKind
    limit: float
    consumed: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.consumed)

    @property
    def exhausted(self) -> bool:
        return self.consumed >= self.limit


@dataclass
class ResourceUsage:
    """A record of resource consumption.

    Submitted to the accountant after a unit of work is performed.
    """

    execution_id: str
    kind: ResourceKind
    amount: float
    notes: str | None = None


@dataclass
class BudgetAllocation:
    """A bundle of ResourceBudgets for a single execution.

    Created when an execution starts; consulted before each resource-consuming
    operation; deleted (or archived) when the execution ends.
    """

    execution_id: str
    budgets: dict[str, ResourceBudget] = field(default_factory=dict)

    def set_limit(self, kind: ResourceKind, limit: float) -> None:
        self.budgets[kind.value] = ResourceBudget(
            execution_id=self.execution_id,
            kind=kind,
            limit=limit,
            consumed=0.0,
        )

    def get(self, kind: ResourceKind) -> ResourceBudget | None:
        return self.budgets.get(kind.value)

    def all_budgets(self) -> list[ResourceBudget]:
        return list(self.budgets.values())
