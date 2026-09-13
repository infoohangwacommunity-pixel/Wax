"""wax.isolation — execution isolation for AI-invoked code.

The AI may request to run code, execute shell commands, or perform other
arbitrary computational work. This package provides the boundary that
keeps such work isolated from the runtime.

Architecture:
- `IsolationBoundary` (abstract): the contract for any isolation mechanism.
- `SubprocessBoundary`: simplest isolation — separate process, restricted env.
- `NoopBoundary`: for tests; runs the code in-process (NO real isolation).
  Marked EXPERIMENTAL — must NEVER be used for production AI work.
- `IsolationService`: chooses the right boundary based on configuration.

INVARIANT: The runtime owns isolation. The AI may request execution, but
the runtime decides HOW that execution is sandboxed.
"""

from wax.isolation.contracts import (
    ExecutionResult,
    IsolationBoundary,
    IsolationKind,
)
from wax.isolation.service import IsolationService
from wax.isolation.subprocess_boundary import SubprocessBoundary

__all__ = [
    "ExecutionResult",
    "IsolationBoundary",
    "IsolationKind",
    "IsolationService",
    "SubprocessBoundary",
]
