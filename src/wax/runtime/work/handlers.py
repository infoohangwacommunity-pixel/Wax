"""Work handlers — what the runner can DO with durable work.

The capability handler is GONE (the capability architecture was removed).
Only the intelligence handler remains: it wakes the intelligence and
re-enters the LLM + terminal loop.

Durable work is now the ONLY mechanism for "continue later". The AI
schedules work through the terminal (a local runtime helper or direct
DB write), and the runner wakes the intelligence when the wake condition
fires (time or signal).
"""

from __future__ import annotations

from typing import Any

from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.runtime.work.reentry import (
    ReentryRequest,
    ReentryValidationError,
    validate_reentry_payload,
)
from wax.runtime.work.runner import WorkExecutionError
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)


async def intelligence_handler(services: RuntimeServices, item: WorkItemRecord) -> dict[str, Any]:
    """Wake the intelligence and re-run the LLM + terminal loop.

    The handler validates the payload, builds a neutral ReentryRequest, and
    calls services.reentry_callback (set by the composition root to the
    bridge's run_reentry method). The handler never imports the bridge.
    """
    if services.reentry_callback is None:
        raise WorkExecutionError(
            "intelligence re-entry not configured in this runtime "
            "(services.reentry_callback is None)"
        )

    # Validate the payload at WAKE time
    try:
        prompt, observation = validate_reentry_payload(item.payload)
    except ReentryValidationError as e:
        raise WorkExecutionError(f"invalid intelligence work payload: {e}") from e

    if not item.principal_id:
        raise WorkExecutionError("intelligence work item has no principal_id; cannot re-enter")
    if not item.execution_id:
        raise WorkExecutionError(
            "intelligence work item has no execution_id; cannot resolve originating execution"
        )

    request = ReentryRequest(
        principal_id=item.principal_id,
        originating_execution_id=item.execution_id,
        work_item_id=item.id,
        prompt=prompt,
        observation=observation,
    )

    log.info(
        "intelligence.reentry.start",
        work_id=item.id,
        principal_id=item.principal_id,
        originating_execution_id=item.execution_id,
        observation_event=observation.get("event"),
    )

    try:
        result = await services.reentry_callback(request)
    except Exception as e:
        log.warning(
            "intelligence.reentry.exception",
            work_id=item.id,
            error=str(e)[:500],
            error_type=type(e).__name__,
        )
        raise WorkExecutionError(f"intelligence re-entry failed: {type(e).__name__}: {e}") from e

    log.info(
        "intelligence.reentry.complete",
        work_id=item.id,
        outcome=getattr(result, "outcome", "unknown"),
    )

    # A failed re-entry outcome must NOT be treated as work success.
    outcome = getattr(result, "outcome", None)
    if outcome == "failed":
        raise WorkExecutionError(
            f"intelligence re-entry returned outcome=failed: {getattr(result, 'error', 'unknown')}"
        )

    return {
        "execution_id": getattr(result, "execution_id", None),
        "outcome": outcome,
        "response_text": (getattr(result, "response_text", "") or "")[:1000],
        "error": getattr(result, "error", None),
    }
