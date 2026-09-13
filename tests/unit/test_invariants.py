"""Unit tests for wax.core.invariants (Phase A)."""

from __future__ import annotations

import pytest

from wax.core.invariants import all_invariants, invariant_by_id


class TestInvariants:
    def test_all_invariants_is_tuple(self) -> None:
        invs = all_invariants()
        assert isinstance(invs, tuple)
        assert len(invs) >= 8

    def test_each_invariant_has_required_fields(self) -> None:
        for inv in all_invariants():
            assert inv.id.startswith("INV-")
            assert inv.statement
            assert inv.reason
            assert inv.enforcement
            assert inv.status in {"PROPOSED", "PROVISIONAL", "VALIDATED", "ADOPTED"}

    def test_invariant_ids_are_unique(self) -> None:
        ids = [inv.id for inv in all_invariants()]
        assert len(ids) == len(set(ids))

    def test_invariant_by_id_returns_known(self) -> None:
        inv = invariant_by_id("INV-09")
        assert inv.id == "INV-09"
        assert "no I/O" in inv.statement.lower() or "must contain no I/O" in inv.statement

    def test_invariant_by_id_raises_for_unknown(self) -> None:
        with pytest.raises(KeyError):
            invariant_by_id("INV-999")
