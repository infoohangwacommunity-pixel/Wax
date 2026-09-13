"""Architectural invariants for WAX.

These are the constitutional rules the system must preserve. They are enforced
by:

1. Architecture tests (tests/architecture/) — verify invariants at test time.
2. Code review — every PR that touches an invariant-protected boundary must
   be reviewed against this list.
3. Runtime checks — some invariants are also enforced at runtime (e.g.,
   authorization cannot be bypassed by model requests).

Each invariant has:
- An ID (INV-NN)
- A statement
- Why it exists (constitutional reason)
- How it is enforced (test name or runtime check)
- Status: PROVISIONAL / VALIDATED / ADOPTED

These invariants are derived from the WAX foundation documents and may be
refined as research reveals better formulations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

InvariantStatus = Literal["PROPOSED", "PROVISIONAL", "VALIDATED", "ADOPTED"]


@dataclass(frozen=True)
class Invariant:
    """A declared WAX architectural invariant."""

    id: str
    statement: str
    reason: str
    enforcement: str
    status: InvariantStatus


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        id="INV-01",
        statement="Universal runtime mechanisms must not require education.",
        reason=(
            "Education is the first domain WAX serves, not the boundary of WAX. "
            "If the runtime requires education concepts, it has become WaxPrep under a new name. "
            "(Foundation §5.1, Directive §4, §10, §109)"
        ),
        enforcement="tests/architecture/test_no_domain_coupling.py",
        status="PROVISIONAL",
    ),
    Invariant(
        id="INV-02",
        statement="Core WAX must not depend on any specific interface (WhatsApp, web, etc.).",
        reason=(
            "Interfaces are adapters into WAX, not the definition of WAX. "
            "If WhatsApp disappears, WAX must still exist as a runtime. "
            "(Foundation §11, Directive §49, §50, §73)"
        ),
        enforcement="tests/architecture/test_no_interface_coupling.py",
        status="PROVISIONAL",
    ),
    Invariant(
        id="INV-03",
        statement="Core WAX must not depend on any specific model provider.",
        reason=(
            "Models are replaceable infrastructure. The model adapter boundary must be real. "
            "(Foundation §21, Directive §42, §43, §72)"
        ),
        enforcement="tests/architecture/test_no_provider_coupling.py",
        status="PROVISIONAL",
    ),
    Invariant(
        id="INV-04",
        statement="AI-requested actions must pass through runtime authorization.",
        reason=(
            "The model cannot grant itself authority by producing text. "
            "Authorization is a runtime responsibility, not a model responsibility. "
            "(Directive §7, §45, §147)"
        ),
        enforcement="tests/architecture/test_authorization_boundary.py (future)",
        status="PROPOSED",
    ),
    Invariant(
        id="INV-05",
        statement="Important state must survive process interruption.",
        reason=(
            "In-memory state is disposable. Durable state requires real persistence. "
            "(Directive §23, §56, §75)"
        ),
        enforcement="tests/integration/test_persistence_durability.py (future)",
        status="PROPOSED",
    ),
    Invariant(
        id="INV-06",
        statement="Security-sensitive actions must be attributable to an identity.",
        reason=(
            "Without auditability, security guarantees are unverifiable. "
            "(Directive §23, §47, §56)"
        ),
        enforcement="tests/integration/test_audit_attribution.py (future)",
        status="PROPOSED",
    ),
    Invariant(
        id="INV-07",
        statement="Major external dependencies must have a replacement boundary.",
        reason=(
            "Replaceability is a constitutional property of WAX. A dependency that cannot "
            "be replaced is not a dependency — it is part of WAX, and must be justified as such. "
            "(Directive §23, §93)"
        ),
        enforcement="tests/architecture/test_replaceability.py (future)",
        status="PROPOSED",
    ),
    Invariant(
        id="INV-08",
        statement="Unknown legitimate objectives must not require modifying the universal ontology.",
        reason=(
            "If WAX requires a new core primitive for every new objective, it is not open-world. "
            "(Foundation §7, Directive §8, §71)"
        ),
        enforcement="tests/open_world/test_unanticipated_objectives.py (future)",
        status="PROPOSED",
    ),
    Invariant(
        id="INV-09",
        statement="wax.core must contain no I/O. Network, filesystem, database, subprocess "
        "are all forbidden in core.",
        reason=(
            "The core boundary is what makes WAX replaceable. If core couples to any I/O "
            "mechanism, that mechanism becomes constitutional. "
            "(Directive §26, §27, §159)"
        ),
        enforcement="tests/architecture/test_core_boundary.py",
        status="ADOPTED",
    ),
    Invariant(
        id="INV-10",
        statement="No mock may be claimed as production infrastructure.",
        reason=(
            "Mocks in tests are fine. Mocks standing in for real persistence, authorization, "
            "isolation, or recovery are not. (Directive §61, §110)"
        ),
        enforcement="tests/architecture/test_no_mock_world.py (future) + manual review",
        status="PROPOSED",
    ),
)


def all_invariants() -> tuple[Invariant, ...]:
    """Return the full tuple of declared invariants."""
    return INVARIANTS


def invariant_by_id(invariant_id: str) -> Invariant:
    """Look up an invariant by its ID (e.g. 'INV-09')."""
    for inv in INVARIANTS:
        if inv.id == invariant_id:
            return inv
    raise KeyError(f"Unknown invariant: {invariant_id!r}")
