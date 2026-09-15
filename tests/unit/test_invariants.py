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


class TestIdentityBoundaryDerivation:
    """CV-16: credential-kind legality derives from the interface boundary
    table — attaching a new interface must never require editing identity
    allowlist semantics."""

    def test_allowed_kinds_derive_from_boundary_table(self) -> None:
        from wax.identity.contracts import (
            ALLOWED_CREDENTIAL_KINDS,
            CREDENTIAL_KIND_INTERFACES,
            INTERFACE_CREDENTIAL_KINDS,
        )

        # INTERFACE_CREDENTIAL_KINDS maps interface → credential kind.
        # Every declared interface's credential kind is automatically legal.
        for interface, kind in INTERFACE_CREDENTIAL_KINDS.items():
            assert kind in ALLOWED_CREDENTIAL_KINDS
            assert CREDENTIAL_KIND_INTERFACES[kind] == interface

        # The boundary table is involutive: interface→kind→interface
        # round-trips.
        for interface, kind in INTERFACE_CREDENTIAL_KINDS.items():
            assert CREDENTIAL_KIND_INTERFACES[kind] == interface

    def test_new_interface_mapping_is_the_only_edit_required(self) -> None:
        """The constitutional property: one mapping addition admits the new
        credential kind — no separate allowlist to keep in sync."""
        from wax.identity.contracts import (
            ALLOWED_CREDENTIAL_KINDS,
            INTERFACE_CREDENTIAL_KINDS,
            NON_INTERFACE_CREDENTIAL_KINDS,
        )

        assert (
            frozenset(INTERFACE_CREDENTIAL_KINDS.values()) | NON_INTERFACE_CREDENTIAL_KINDS
            == ALLOWED_CREDENTIAL_KINDS
        )
