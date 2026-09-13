"""Durable work runtime (Phase R / Phase V).

Exports the WorkRunner (background worker), WorkRepository, and the
generic capability handler. The runner is started by the app lifespan;
work survives restarts via the work_items table + leases.
"""

from __future__ import annotations

from wax.runtime.work.handlers import capability_handler
from wax.runtime.work.repository import WorkRepository
from wax.runtime.work.runner import WorkExecutionError, WorkRunner

__all__ = [
    "WorkExecutionError",
    "WorkRepository",
    "WorkRunner",
    "capability_handler",
]
