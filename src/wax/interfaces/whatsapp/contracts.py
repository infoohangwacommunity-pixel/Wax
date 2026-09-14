"""Contracts for the WhatsApp adapter.

These are the data structures the adapter works with internally. They
are NOT the runtime's contracts — they are WhatsApp-specific.

The adapter's job is to translate between WhatsApp-specific shapes and
the WAX runtime's universal contracts (Objective, Execution, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    DELETED = "deleted"
    PENDING = "pending"


@dataclass
class WhatsAppReplyContext:
    """When a user replies to a specific message, WhatsApp includes context.

    Used for quoted replies. The runtime may use this to fetch the original
    message and include its content in the LLM context.
    """

    message_id: str  # the message being replied to
    from_phone: str | None = None  # who sent the original (optional)


@dataclass
class WhatsAppReaction:
    """A reaction (emoji) on a message.

    The runtime may treat reactions as feedback signals on previous responses.
    """

    message_id: str  # the message being reacted to
    emoji: str  # the emoji string
    from_phone: str | None = None


@dataclass
class WhatsAppContact:
    """A contact (vCard-like) shared via WhatsApp.

    WhatsApp sends a list of contacts; each has one or more phone numbers,
    an optional name, and an optional email.
    """

    wa_id: str | None = None  # WhatsApp ID of the contact
    name_first: str | None = None
    name_last: str | None = None
    name_formatted: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


@dataclass
class WhatsAppLocation:
    """A location shared via WhatsApp."""

    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None
    url: str | None = None  # optional Google Maps URL


@dataclass
class WhatsAppSystemEvent:
    """A system event from WhatsApp (not a user message).

    Examples:
    - customer_changed_phone_number
    - customer_identity_changed
    """

    kind: str
    from_phone: str | None = None
    new_phone: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class WhatsAppError:
    """An error returned by the WhatsApp Cloud API.

    The runtime records these to the audit log; they help debug outbound
    delivery failures.
    """

    code: int
    title: str
    message: str
    error_data: dict[str, Any] = field(default_factory=dict)


class WhatsAppIncomingMessage(BaseModel):
    """A normalized incoming message from the WhatsApp Cloud API webhook.

    Every WhatsApp Cloud API message type is normalized into this single
    contract. The bridge converts this to a RuntimeRequest.
    """

    message_id: str  # wamid.XXXXX — used for idempotency
    from_phone: str  # the sender's phone number (E.164)
    from_name: str | None = None  # profile name (optional)
    timestamp: datetime
    type: WhatsAppMessageType
    text_body: str | None = None  # for type=text
    media_id: str | None = None  # for image/audio/document/video/sticker
    media_mime_type: str | None = None
    media_caption: str | None = None  # for media with caption
    media_sha256: str | None = None  # for verification
    media_filename: str | None = None  # for documents
    interactive_id: str | None = None  # for button/list responses
    interactive_title: str | None = None
    # Reply context (when user replies to a specific message)
    reply_context_message_id: str | None = None
    reply_context_from: str | None = None
    # Reaction (when user reacts with emoji to a message)
    reaction_emoji: str | None = None
    reaction_message_id: str | None = None  # the message being reacted to
    # Location (when type=location)
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
    # Contacts (when type=contacts) — list of normalized contacts
    contacts: list[dict[str, Any]] = Field(default_factory=list)
    # System events (when type=system)
    system_kind: str | None = None  # customer_changed_phone_number, etc.
    # Message metadata
    is_forwarded: bool = False
    is_frequently_forwarded: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_text(self) -> str:
        """The text content to send to the runtime, regardless of type.

        Phase T (Media Intelligence Pipeline) will replace this with a
        richer pipeline that extracts text from images (OCR), audio
        (transcription), and documents. For now, this returns the
        message text + any caption + a placeholder for media.
        """
        if self.type == WhatsAppMessageType.TEXT:
            return self.text_body or ""
        if self.reaction_emoji:
            return f"[reaction: {self.reaction_emoji}]"
        if self.system_kind:
            return f"[system event: {self.system_kind}]"
        if self.location_latitude is not None and self.location_longitude is not None:
            loc_str = f"[location: {self.location_latitude},{self.location_longitude}"
            if self.location_name:
                loc_str += f", name={self.location_name}"
            if self.location_address:
                loc_str += f", address={self.location_address}"
            return loc_str + "]"
        if self.contacts:
            return f"[shared {len(self.contacts)} contact(s)]"
        parts: list[str] = []
        if self.media_caption:
            parts.append(self.media_caption)
        if self.interactive_title:
            parts.append(f"[interactive: {self.interactive_title}]")
        if self.media_id:
            parts.append(f"[received {self.type.value} message, media_id={self.media_id}]")
        if parts:
            return " ".join(parts)
        return f"[received {self.type.value} message]"


class WhatsAppOutgoingMessage(BaseModel):
    """A message to be sent via the WhatsApp Cloud API.

    Supports all outbound message types:
    - text
    - image (media_id or link URL + optional caption)
    - video (media_id or link URL + optional caption)
    - audio (media_id or link URL)
    - document (media_id or link URL + optional caption + filename)
    - sticker (media_id or link URL)
    - location (latitude, longitude, name, address)
    - contacts (list of vCards)
    - interactive (buttons, lists, CTAs, product messages)
    - template (with components: header, body, button)
    - reaction (emoji on a user message)

    Reply context: set reply_to_message_id to reply to a specific user
    message. WhatsApp shows the original message as a quote.
    """

    to_phone: str
    type: WhatsAppMessageType = WhatsAppMessageType.TEXT
    text_body: str | None = None
    # For media: either media_id (uploaded) or link (URL Meta fetches)
    media_id: str | None = None
    media_link: str | None = None
    media_caption: str | None = None
    media_filename: str | None = None  # documents only
    # For location
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
    # For contacts (list of vCard dicts)
    contacts_vcard: list[dict[str, Any]] | None = None
    # For interactive
    interactive_payload: dict[str, Any] | None = None
    # For templates
    template_name: str | None = None
    template_language: str = "en_US"
    template_components: list[dict[str, Any]] | None = None
    # For reactions
    reaction_emoji: str | None = None
    reaction_message_id: str | None = None  # the message to react to
    # Reply context
    reply_to_message_id: str | None = None  # for threading


class WhatsAppWebhookEvent(BaseModel):
    """A normalized webhook event from the WhatsApp Cloud API."""

    event_type: str  # "message" | "status"
    message: WhatsAppIncomingMessage | None = None
    status: WhatsAppMessageStatus | None = None
    message_id: str | None = None  # for status events
    recipient_phone: str | None = None  # for status events
    timestamp: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
