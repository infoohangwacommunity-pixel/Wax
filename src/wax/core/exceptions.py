"""Core WAX exception hierarchy.

All WAX errors descend from `WaxError`. Subclasses are grouped by category so
callers can catch at the appropriate level of specificity.

These are pure data classes — no I/O, no logging, no side effects.
"""

from __future__ import annotations


class WaxError(Exception):
    """Root of all WAX exceptions.

    Every WAX-raised error descends from this so callers can catch the
    entire domain with `except WaxError`.
    """


# ---------------------------------------------------------------------------
# Configuration / environment
# ---------------------------------------------------------------------------


class WaxConfigurationError(WaxError):
    """Raised when required configuration is missing or invalid.

    Examples: missing WAX_SECRET_KEY, malformed WAX_DATABASE_URL, unknown
    WAX_ENV value.
    """


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class WaxValidationError(WaxError):
    """Raised when input fails contract validation.

    Use this for *internal* contract violations (not user input — for HTTP
    input, FastAPI's standard 422 is preferred). Examples: an ADR-defined
    invariant was violated, a value's structure is wrong, etc.
    """


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class WaxPermissionDeniedError(WaxError):
    """Raised when an action is not authorized.

    This MUST be raised by the runtime, never by the model. The runtime is
    the sole authority on what an identity may do.
    """


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class WaxNotFoundError(WaxError):
    """Raised when a requested entity does not exist."""


class WaxStateConflictError(WaxError):
    """Raised when a state mutation conflicts with existing state.

    Examples: duplicate idempotency key, version mismatch, optimistic
    concurrency conflict.
    """


# ---------------------------------------------------------------------------
# Timeouts / execution
# ---------------------------------------------------------------------------


class WaxTimeoutError(WaxError):
    """Raised when an operation exceeds its allowed time budget."""


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class WaxInvariantViolation(WaxError):
    """Raised when a declared architectural invariant is violated.

    This is the most serious WAX error class. It indicates the architecture
    itself has been violated, not merely a runtime failure. Examples:
    - `wax.core` imported an I/O module
    - A model was found to be making authorization decisions
    - A capability silently redefined its own authority
    """
