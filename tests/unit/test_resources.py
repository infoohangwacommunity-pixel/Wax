"""Tests for Phase J (Resources)."""

from __future__ import annotations

import pytest

from wax.resources.accountant import ResourceAccountant
from wax.resources.contracts import ResourceBudget, ResourceKind, ResourceUsage


class TestResourceAccountant:
    def test_allocate_creates_budgets(self) -> None:
        acc = ResourceAccountant()
        alloc = acc.allocate(
            "exec_1",
            execution_time_seconds=300.0,
            llm_tokens=100_000,
        )
        assert alloc.execution_id == "exec_1"
        assert len(alloc.budgets) == 2
        assert alloc.budgets["execution_time_seconds"].limit == 300.0
        assert alloc.budgets["llm_tokens"].limit == 100_000

    def test_try_consume_within_budget_succeeds(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_2", llm_tokens=1000.0)
        ok = acc.try_consume(
            ResourceUsage(execution_id="exec_2", kind=ResourceKind.LLM_TOKENS, amount=300.0)
        )
        assert ok
        remaining = acc.remaining("exec_2", ResourceKind.LLM_TOKENS)
        assert remaining == 700.0

    def test_try_consume_exact_budget_succeeds(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_3", llm_tokens=1000.0)
        ok = acc.try_consume(
            ResourceUsage(execution_id="exec_3", kind=ResourceKind.LLM_TOKENS, amount=1000.0)
        )
        assert ok
        assert acc.remaining("exec_3", ResourceKind.LLM_TOKENS) == 0.0

    def test_try_consume_over_budget_fails(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_4", llm_tokens=1000.0)
        ok = acc.try_consume(
            ResourceUsage(execution_id="exec_4", kind=ResourceKind.LLM_TOKENS, amount=1001.0)
        )
        assert not ok
        # Budget should NOT have been updated on failed consume
        assert acc.remaining("exec_4", ResourceKind.LLM_TOKENS) == 1000.0

    def test_try_consume_after_exhausted_fails(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_5", llm_calls=2)
        assert acc.try_consume(ResourceUsage("exec_5", ResourceKind.LLM_CALLS, 1))
        assert acc.try_consume(ResourceUsage("exec_5", ResourceKind.LLM_CALLS, 1))
        assert not acc.try_consume(ResourceUsage("exec_5", ResourceKind.LLM_CALLS, 1))

    def test_try_consume_no_allocation_denies(self) -> None:
        """If no allocation exists for an execution, consume is denied."""
        acc = ResourceAccountant()
        ok = acc.try_consume(
            ResourceUsage(execution_id="ghost", kind=ResourceKind.LLM_TOKENS, amount=10.0)
        )
        assert not ok

    def test_try_consume_no_budget_for_kind_allows(self) -> None:
        """If allocation exists but no budget for this kind, allow without tracking.

        Rationale: budgets are explicit; if a kind isn't budgeted, it's
        not limited. To restrict it, allocate a budget for it.
        """
        acc = ResourceAccountant()
        acc.allocate("exec_6", llm_tokens=1000.0)
        ok = acc.try_consume(
            ResourceUsage(execution_id="exec_6", kind=ResourceKind.CPU_SECONDS, amount=999.0)
        )
        assert ok

    def test_release_returns_final_usage(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_7", llm_tokens=1000.0)
        acc.try_consume(ResourceUsage("exec_7", ResourceKind.LLM_TOKENS, 250.0))
        final = acc.release("exec_7")
        assert final is not None
        assert final.budgets["llm_tokens"].consumed == 250.0
        # After release, allocation is gone
        assert acc.get_allocation("exec_7") is None

    def test_release_unknown_returns_none(self) -> None:
        acc = ResourceAccountant()
        assert acc.release("ghost") is None

    def test_reallocate_replaces(self) -> None:
        acc = ResourceAccountant()
        acc.allocate("exec_8", llm_tokens=1000.0)
        # Reallocate with different limits
        acc.allocate("exec_8", llm_tokens=5000.0)
        alloc = acc.get_allocation("exec_8")
        assert alloc is not None
        assert alloc.budgets["llm_tokens"].limit == 5000.0


class TestResourceBudget:
    def test_remaining(self) -> None:
        b = ResourceBudget(
            execution_id="e", kind=ResourceKind.LLM_TOKENS, limit=100.0, consumed=30.0
        )
        assert b.remaining == 70.0

    def test_exhausted(self) -> None:
        b = ResourceBudget(
            execution_id="e", kind=ResourceKind.LLM_TOKENS, limit=100.0, consumed=100.0
        )
        assert b.exhausted

    def test_not_exhausted_when_under(self) -> None:
        b = ResourceBudget(
            execution_id="e", kind=ResourceKind.LLM_TOKENS, limit=100.0, consumed=99.0
        )
        assert not b.exhausted

    def test_remaining_floored_at_zero(self) -> None:
        b = ResourceBudget(
            execution_id="e", kind=ResourceKind.LLM_TOKENS, limit=100.0, consumed=150.0
        )
        assert b.remaining == 0.0


class TestBudgetAllocation:
    def test_set_limit_creates_budget(self) -> None:
        from wax.resources.contracts import BudgetAllocation

        alloc = BudgetAllocation(execution_id="e1")
        alloc.set_limit(ResourceKind.LLM_TOKENS, 5000.0)
        b = alloc.get(ResourceKind.LLM_TOKENS)
        assert b is not None
        assert b.limit == 5000.0
        assert b.consumed == 0.0

    def test_get_unknown_returns_none(self) -> None:
        from wax.resources.contracts import BudgetAllocation

        alloc = BudgetAllocation(execution_id="e2")
        assert alloc.get(ResourceKind.CPU_SECONDS) is None
