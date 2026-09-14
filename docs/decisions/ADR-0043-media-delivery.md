# ADR-0043: Media + Delivery Completion

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 10 — Media and Delivery (OMEGA Implementation Directive)

## Context

WAX already has:
- `wax.media.pipeline` (extraction pipeline: text/images/PDF/audio/video → evidence → context)
- `wax.runtime.delivery.DeliveryRouter` (interface-neutral outbound delivery)
- `wax.state.delivery_models.DeliveryRecord` (ADR-0021: pending → delivered | failed)
- `wax.runtime.delivery_queue` (retry queue with backoff)
- `message.send` capability (the intelligence can send a message)

The directive (Phase 10) requires "Complete interface-neutral delivery.
Execution success and delivery success remain separate."

The existing infrastructure already enforces execution ≠ delivery separation
(ADR-0021). What's missing is intelligence-facing visibility into delivery
state — the intelligence cannot ask "did my message get delivered?" or
"retry the failed delivery."

## Decision

Add 2 capabilities:

### `delivery.status` (v1.0.0)

Input: optional `delivery_id` OR `execution_id`
Output: delivery record(s) with status, attempts, last_error, delivered_at

The intelligence uses this to check whether a message was delivered.
Returns metadata only — never the message text (which may contain
sensitive content the runtime owns, not the model).

### `delivery.retry` (v1.0.0)

Input: `delivery_id`
Output: `retried`, `status`, `next_attempt_at`

Manually triggers a retry of a failed delivery. The runtime clears
the `next_attempt_at` backoff and sets the delivery to `pending` for
the maintenance loop to pick up. The intelligence cannot bypass the
max_attempts limit — once exhausted, the delivery is terminal `failed`.

## Tests

`tests/integration/test_media_delivery.py` covers:
- delivery.status returns delivery metadata
- delivery.status by execution_id returns all deliveries for that execution
- delivery.retry resets a failed delivery to pending
- delivery.retry on an exhausted delivery (max_attempts reached) is rejected
- delivery metadata never includes message text
