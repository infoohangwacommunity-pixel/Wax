"""Durable work runtime (Phase R / Phase V; ADR-0034 re-entry).

Exports the WorkRunner (background worker), WorkRepository, the generic
capability handler, and the durable intelligence re-entry handler
(ADR-0034). The runner is started by the app lifespan; work survives
restarts via the work_items table + leases.
"""

from __future__ import annotations

from wax.runtime.work.handlers import capability_handler, intelligence_handler
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
    "capability_handler",
    "intelligence_handler",
    "validate_reentry_payload",
]
