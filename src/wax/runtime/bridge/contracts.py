"""Canonical contracts for the runtime bridge.

These are interface-agnostic. A WhatsApp message, a web chat message,
and a future Telegram message all become a RuntimeRequest before they
reach the runtime. The runtime never sees WhatsApp-specific shapes.

This is the contract that makes WAX interface-independent (INV-02):
the runtime only knows about RuntimeRequest, never about WhatsApp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class InterfaceKind(StrEnum):
    """Which interface a message came in on.

    Adding a new kind here is a deliberate architectural act — it means
    a new interface adapter has been integrated.
    """

    WHATSAPP = "whatsapp"
    WEB = "web"
    TELEGRAM = "telegram"
    API = "api"  # direct API call (no chat interface)


class RuntimeResponseStatus(StrEnum):
    """Outcome of processing a RuntimeRequest.

    The interface adapter maps these to platform-specific behaviors:
    - success → send response back to user
    - duplicate → silently ack the webhook (Meta retried; we already processed)
    - principal_unauthorized → send "you are not authorized" message
    - internal_error → send generic "something went wrong" message
    """

    SUCCESS = "success"
    DUPLICATE = "duplicate"  # already processed (idempotency hit)
    PRINCIPAL_UNAUTHORIZED = "principal_unauthorized"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    PENDING = "pending"  # accepted, will process async


class RuntimeRequest(BaseModel):
    """A canonical runtime request, interface-agnostic.

    The interface adapter builds this from its platform-specific shape.
    For example, the WhatsApp adapter converts a WhatsAppIncomingMessage
    into a RuntimeRequest before calling the bridge.

    Fields:
    - interface_message_id: the platform's message ID (e.g. WhatsApp wamid.*)
      Used for idempotency: if we already processed this ID, we return
      DUPLICATE without re-executing.
    - interface_kind: which interface this came from (whatsapp, web, ...)
    - sender_interface_id: the sender's identifier on that interface
      (e.g. WhatsApp phone number, web session token)
    - sender_display_name: optional human-readable name
    - text: the user's message text (already normalized by adapter)
    - media: optional media attachments (extracted text from images, audio
      transcriptions, document contents — produced by Phase T pipeline)
    - received_at: when the interface received the message (UTC)
    - raw_metadata: opaque dict for debugging (never contains secrets)
    """

    interface_message_id: str = Field(..., min_length=1)
    interface_kind: InterfaceKind
    sender_interface_id: str = Field(..., min_length=1)
    sender_display_name: str | None = None
    text: str = Field(default="", description="Normalized text content")
    media: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Media attachments (extracted content, not raw bytes)",
    )
    received_at: datetime
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_text(self) -> str:
        """The text the AI should see: message text + extracted media text.

        The runtime may further transform this (add context, etc.) but
        the bridge's job is to produce a single text blob the intelligence
        can reason over.
        """
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        for m in self.media:
            if m.get("extracted_text"):
                kind = m.get("kind", "media")
                parts.append(f"[{kind}: {m['extracted_text']}]")
        return "\n".join(parts)


class RuntimeResponse(BaseModel):
    """A canonical runtime response, interface-agnostic.

    The interface adapter translates this into its platform-specific shape
    before sending. For WhatsApp, the response.text becomes a text message;
    for web, it becomes a chat bubble; etc.
    """

    status: RuntimeResponseStatus
    text: str | None = None
    execution_id: str | None = None
    objective_id: str | None = None
    principal_id: str | None = None
    error: str | None = None
    processed_at: datetime
    duplicate_of_execution_id: str | None = None  # set when status == DUPLICATE


@dataclass
class InboundResult:
    """The outcome of processing a freshly-accepted inbound message.

    Returned by ``RuntimeBridge.process_inbound`` to the work runner's
    ``inbound_handler``. The work runner uses ``outcome`` to decide
    whether the work item succeeded or needs retry.

    Fields:
    - outcome: "success" | "duplicate" | "failed"
    - execution_id: the execution that processed this message (if any)
    - delivery_id: the DeliveryRecord ID for the outbound reply (if any)
    - response_text: the reply text the AI generated (truncated)
    - error: error message if outcome == "failed"
    """

    outcome: str
    execution_id: str | None = None
    delivery_id: str | None = None
    response_text: str = ""
    error: str | None = None
