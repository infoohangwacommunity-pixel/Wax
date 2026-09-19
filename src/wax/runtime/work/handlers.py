"""Work handlers — what the runner can DO with durable work.

The capability handler is GONE (the capability architecture was removed).
Two handlers remain:

1. ``intelligence_handler`` — wakes the intelligence for RE-ENTRY (a
   scheduled wake that re-runs the LLM + terminal loop with a prompt +
   observation). Used for durable re-entry, provider-outage retry, and
   scheduled intelligence wakes.

2. ``inbound_handler`` — processes a freshly-accepted inbound message
   end-to-end: identity (already resolved at webhook time), idempotency
   (already established at webhook time), execution, intelligence loop,
   memory extraction, and delivery. The webhook schedules this; the
   worker runs it. The webhook NEVER runs intelligence inline.

Durable work is the ONLY mechanism for "continue later". The AI
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


async def inbound_handler(services: RuntimeServices, item: WorkItemRecord) -> dict[str, Any]:
    """Process a freshly-accepted inbound message end-to-end.

    Scheduled by the webhook (``wax.runtime.inbound.accept_inbound_message``)
    after the message has been idempotently persisted. This handler:

    1. Reconstructs the RuntimeRequest from the work payload.
    2. Calls ``services.resume_callback``... wait, no — for inbound
       messages we call the BRIDGE's ``process_inbound`` method, which
       runs the full intelligence ↔ terminal loop and enqueues a
       DeliveryRecord for the outbound reply.

    The webhook does NOT run intelligence. This handler does.

    The handler never imports the bridge directly — it uses
    ``services.process_inbound_callback`` (set by the composition root)
    so the work runner stays decoupled from the bridge implementation.
    """
    if services.process_inbound_callback is None:
        raise WorkExecutionError(
            "inbound processing not configured in this runtime "
            "(services.process_inbound_callback is None)"
        )

    observation = item.payload.get("observation", {}) if isinstance(item.payload, dict) else {}
    prompt = item.payload.get("prompt", "") if isinstance(item.payload, dict) else ""

    if not item.principal_id:
        raise WorkExecutionError("inbound work item has no principal_id")

    interface_message_id = observation.get("interface_message_id")
    if not interface_message_id:
        raise WorkExecutionError("inbound work item payload missing interface_message_id")

    log.info(
        "inbound.process.start",
        work_id=item.id,
        principal_id=item.principal_id,
        message_id=interface_message_id,
        interface=observation.get("interface_kind"),
    )

    try:
        result = await services.process_inbound_callback(
            work_id=item.id,
            principal_id=item.principal_id,
            interface_kind=observation.get("interface_kind", "whatsapp"),
            interface_message_id=interface_message_id,
            sender_interface_id=observation.get("sender_interface_id", ""),
            sender_display_name=observation.get("sender_display_name"),
            text=prompt,
            received_at_iso=observation.get("received_at"),
        )
    except Exception as e:
        log.warning(
            "inbound.process.exception",
            work_id=item.id,
            message_id=interface_message_id,
            error=str(e)[:500],
            error_type=type(e).__name__,
        )
        raise WorkExecutionError(f"inbound processing failed: {type(e).__name__}: {e}") from e

    outcome = getattr(result, "outcome", "unknown")
    log.info(
        "inbound.process.complete",
        work_id=item.id,
        message_id=interface_message_id,
        outcome=outcome,
        delivery_id=getattr(result, "delivery_id", None),
    )

    if outcome == "failed":
        raise WorkExecutionError(
            f"inbound processing returned outcome=failed: {getattr(result, 'error', 'unknown')}"
        )

    return {
        "outcome": outcome,
        "execution_id": getattr(result, "execution_id", None),
        "delivery_id": getattr(result, "delivery_id", None),
        "response_text": (getattr(result, "response_text", "") or "")[:1000],
        "error": getattr(result, "error", None),
    }
