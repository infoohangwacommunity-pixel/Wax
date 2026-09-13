"""Phase S — Complete WhatsApp Cloud API feature support.

Extends the original Phase Q WhatsApp adapter to support every officially
documented WhatsApp Cloud API feature:

INCOMING (receive + normalize):
- text
- image (with optional caption)
- video (with optional caption)
- audio (voice notes are audio messages; transcription in Phase T)
- document (with optional caption + filename)
- sticker
- contacts (single or multiple)
- location (latitude + longitude + optional name + address)
- interactive (button_reply, list_reply)
- reply context (when user replies to a specific message)
- reactions (when user reacts to a message)
- system events (customer phone number update, etc.)
- status events (sent, delivered, read, failed)
- errors (when WhatsApp rejects an outbound message)
- message metadata (forwarded, frequently_forwarded)

OUTBOUND (send):
- text
- image (media_id or URL)
- video (media_id or URL)
- audio (media_id or URL)
- document (media_id or URL)
- sticker (media_id or URL)
- location (send a location pin)
- contacts (single or multiple)
- interactive (buttons, lists, CTAs, single-product, multi-product)
- templates (with components: header, body, button)
- reactions (react to user's message)
- reply context (reply to a specific user message)
- typing indicators (typing, mark_as_read)

MEDIA LIFECYCLE:
- upload_media: upload binary to Meta for sending later
- download_media: fetch binary by media_id (for Phase T processing)
- get_media_url: get the temporary URL for a media_id
- delete_media: revoke a media_id

INVARIANT: All WhatsApp-specific shapes live INSIDE this package. The
runtime never sees WhatsApp objects — only RuntimeRequest / RuntimeResponse
from wax.runtime.bridge.
"""

from wax.interfaces.whatsapp.contracts import (
    WhatsAppIncomingMessage,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
    WhatsAppOutgoingMessage,
    WhatsAppWebhookEvent,
    WhatsAppReaction,
    WhatsAppContact,
    WhatsAppLocation,
    WhatsAppReplyContext,
    WhatsAppSystemEvent,
    WhatsAppError,
)
from wax.interfaces.whatsapp.adapter import WhatsAppAdapter
from wax.interfaces.whatsapp.client import WhatsAppClient

__all__ = [
    "WhatsAppAdapter",
    "WhatsAppClient",
    "WhatsAppIncomingMessage",
    "WhatsAppOutgoingMessage",
    "WhatsAppWebhookEvent",
    "WhatsAppMessageStatus",
    "WhatsAppMessageType",
    "WhatsAppReaction",
    "WhatsAppContact",
    "WhatsAppLocation",
    "WhatsAppReplyContext",
    "WhatsAppSystemEvent",
    "WhatsAppError",
]
