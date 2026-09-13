"""WhatsApp adapter — the first interface into WAX.

Architecture:
    WhatsApp Cloud API
        ↓ (webhook)
    WhatsAppAdapter
        ↓ (resolve phone → Principal)
    WAX Runtime (Objective, Execution, Intelligence)
        ↓ (response)
    WhatsAppAdapter
        ↓ (send message)
    WhatsApp Cloud API

The adapter is stateless. All state lives in the WAX runtime.

Advanced WhatsApp features supported:
- Text messages (send/receive)
- Message templates (pre-approved by Meta for marketing/utility/auth)
- Interactive messages (buttons, lists)
- Media messages (image, audio, document, video) — receive only for now
- Status webhooks (sent, delivered, read)
- Webhook signature verification (X-Hub-Signature-256)
- Idempotent webhook processing (via message_id)

INVARIANT: The adapter does NOT authorize. It identifies. Authorization
happens inside the WAX runtime.
"""

from wax.interfaces.whatsapp.adapter import WhatsAppAdapter
from wax.interfaces.whatsapp.client import WhatsAppClient
from wax.interfaces.whatsapp.contracts import (
    WhatsAppIncomingMessage,
    WhatsAppOutgoingMessage,
    WhatsAppWebhookEvent,
)

__all__ = [
    "WhatsAppAdapter",
    "WhatsAppClient",
    "WhatsAppIncomingMessage",
    "WhatsAppOutgoingMessage",
    "WhatsAppWebhookEvent",
]
