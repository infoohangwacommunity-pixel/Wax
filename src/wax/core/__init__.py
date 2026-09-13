"""wax.core — universal runtime contracts and invariants.

This package MUST NOT perform I/O.

Allowed:
  - Data classes, enums, type definitions
  - Pure validation functions
  - Exception definitions
  - Configuration schemas (no loading from disk)
  - Invariant declarations

Forbidden (enforced by tests/architecture/test_core_boundary.py):
  - import of fastapi, sqlalchemy, httpx, aiosqlite, asyncpg, uvicorn, structlog
  - any network call, file read, database query, subprocess invocation
  - any module-level side effect (instantiation of resources, etc.)

This boundary is what makes WAX replaceable: if `wax.core` ever grows a
dependency on a specific database, model provider, or interface, that
dependency has crossed into the universal layer and must be moved out.
"""

from wax.core.exceptions import (
    WaxConfigurationError,
    WaxError,
    WaxInvariantViolation,
    WaxNotFoundError,
    WaxNotImplementedError,
    WaxPermissionDeniedError,
    WaxStateConflictError,
    WaxTimeoutError,
    WaxValidationError,
)

__all__ = [
    "WaxConfigurationError",
    "WaxError",
    "WaxInvariantViolation",
    "WaxNotFoundError",
    "WaxNotImplementedError",
    "WaxPermissionDeniedError",
    "WaxStateConflictError",
    "WaxTimeoutError",
    "WaxValidationError",
]
