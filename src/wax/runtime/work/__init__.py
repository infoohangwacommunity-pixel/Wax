"""Durable work runtime.

Exports the WorkRunner (background worker), WorkRepository, and the
durable intelligence re-entry handler. The runner is started by the app
lifespan; work survives restarts via the work_items table + leases.

The capability handler is GONE — the capability architecture was removed.
Only the intelligence handler remains.
"""

from __future__ import annotations

from wax.runtime.work.handlers import intelligence_handler
from wax.runtime.work.reentry import (
    ReentryCallback,
    ReentryRequest,
    ReentryResult,
    ReentryValidationError,
    validate_reentry_payload,
)
from wax.runtime.work.repository import WorkRepository
from wax.runtime.work.runner import WorkExecutionError, WorkRunner

__all__ = [
    "ReentryCallback",
    "ReentryRequest",
    "ReentryResult",
    "ReentryValidationError",
    "WorkExecutionError",
    "WorkRepository",
    "WorkRunner",
    "intelligence_handler",
    "validate_reentry_payload",
]
