"""Contracts for the WhatsApp adapter.

These are the data structures the adapter works with internally. They
are NOT the runtime's contracts — they are WhatsApp-specific.

The adapter's job is to translate between WhatsApp-specific shapes and
the WAX runtime's universal contracts (Objective, Execution, etc.).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WhatsAppMessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    VIDEO = "video"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACTS = "contacts"
    INTERACTIVE = "interactive"
    TEMPLATE = "template"
    REACTION = "reaction"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class WhatsAppMessageStatus(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class WhatsAppIncomingMessage(BaseModel):
    """A normalized incoming message from the WhatsApp Cloud API webhook."""

    message_id: str  # wamid.XXXXX — used for idempotency
    from_phone: str  # the sender's phone number (E.164)
    from_name: str | None = None  # profile name (optional)
    timestamp: datetime
    type: WhatsAppMessageType
    text_body: str | None = None  # for type=text
    media_id: str | None = None  # for image/audio/document/video
    media_mime_type: str | None = None
    media_caption: str | None = None  # for media with caption
    interactive_id: str | None = None  # for button/list responses
    interactive_title: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_text(self) -> str:
        """The text content to send to the runtime, regardless of type."""
        if self.type == WhatsAppMessageType.TEXT:
            return self.text_body or ""
        if self.media_caption:
            return self.media_caption
        if self.interactive_title:
            return self.interactive_title
        return f"[received {self.type.value} message]"


class WhatsAppOutgoingMessage(BaseModel):
    """A message to be sent via the WhatsApp Cloud API."""

    to_phone: str
    type: WhatsAppMessageType = WhatsAppMessageType.TEXT
    text_body: str | None = None
    media_id: str | None = None
    media_caption: str | None = None
    template_name: str | None = None
    template_language: str = "en_US"
    template_components: list[dict[str, Any]] | None = None
    interactive_buttons: list[dict[str, Any]] | None = None
    reply_to_message_id: str | None = None  # for threading
    context_message_id: str | None = None  # for reply context


class WhatsAppWebhookEvent(BaseModel):
    """A normalized webhook event from the WhatsApp Cloud API."""

    event_type: str  # "message" | "status"
    message: WhatsAppIncomingMessage | None = None
    status: WhatsAppMessageStatus | None = None
    message_id: str | None = None  # for status events
    recipient_phone: str | None = None  # for status events
    timestamp: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
