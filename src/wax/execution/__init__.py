"""wax.execution — durable execution for WAX.

Long-running intelligence must survive:
- process crashes (Directive §39, §47)
- deployment restarts
- provider failures
- network failure

Architecture:
- `Execution` is a unit of work that can persist across process lifetimes.
- `ExecutionStatus` tracks lifecycle: pending → running → succeeded | failed | cancelled
- `ExecutionStep` records each step within an execution (for resumability)
- `ExecutionRepository` handles persistence
- A future `ExecutionEngine` will manage workers, scheduling, retry

Durable execution means: if the process dies mid-execution, on restart
the runtime can reconstruct state from the persisted steps and resume
intelligently rather than regenerating from scratch.
"""

from wax.execution.contracts import (
    Execution,
    ExecutionKind,
    ExecutionStatus,
    ExecutionStep,
    StepStatus,
)
from wax.execution.repository import ExecutionRepository

__all__ = [
    "Execution",
    "ExecutionKind",
    "ExecutionRepository",
    "ExecutionStatus",
    "ExecutionStep",
    "StepStatus",
]
