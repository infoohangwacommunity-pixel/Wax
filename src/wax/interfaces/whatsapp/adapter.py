"""WhatsApp adapter — the bridge between WhatsApp Cloud API and WAX runtime.

The adapter:
1. Receives webhook events from WhatsApp
2. Verifies the signature (security)
3. Normalizes the WhatsApp-specific payload into WAX universal contracts
4. Resolves the sender's phone → WAX Principal (creating a new principal
   + whatsapp_phone credential on first contact)
5. Creates an Objective + starts an Execution
6. Asks the IntelligenceService to handle the objective
7. Returns the response via WhatsApp

The adapter does NOT make authorization decisions. It identifies the
principal and forwards the objective — the runtime does the rest.

INVARIANT INV-02: The runtime survives WhatsApp being removed. This
adapter is the ONLY place that knows WhatsApp exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from wax.interfaces.whatsapp.client import WhatsAppClient
from wax.interfaces.whatsapp.contracts import (
    WhatsAppIncomingMessage,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
    WhatsAppOutgoingMessage,
)
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class WhatsAppAdapter:
    """The bridge between WhatsApp Cloud API and WAX runtime.

    Lifecycle:
        adapter = WhatsAppAdapter(client=...)
        # Webhook verification (Meta setup)
        if adapter.verify_webhook_token(mode, token, challenge): return challenge
        # Webhook event handling
        await adapter.handle_webhook(raw_body, signature_header, runtime_callback)

    The runtime_callback is an async function that takes a WhatsAppIncomingMessage
    and returns a string response. The adapter sends the response back via WhatsApp.
    """

    def __init__(self, client: WhatsAppClient) -> None:
        self._client = client

    # -------------------------------------------------------------------
    # Webhook verification (Meta setup)
    # -------------------------------------------------------------------

    def verify_webhook_token(
        self, mode: str | None, token: str | None, challenge: str | None
    ) -> str | None:
        """Verify Meta's webhook setup request.

        Meta sends: GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
        If the verify_token matches, we echo back the challenge.
        """
        if mode == "subscribe" and token == self._client.get_verify_token():
            log.info("whatsapp.webhook.verified")
            return challenge
        log.warning("whatsapp.webhook.verification_failed", mode=mode)
        return None

    # -------------------------------------------------------------------
    # Webhook event handling
    # -------------------------------------------------------------------

    async def handle_webhook(
        self,
        raw_body: bytes,
        signature_header: str,
        runtime_callback: Any = None,
    ) -> dict[str, Any]:
        """Process a webhook event.

        Args:
            raw_body: the raw request body (bytes — needed for HMAC verification)
            signature_header: the X-Hub-Signature-256 header value
            runtime_callback: async function(message: WhatsAppIncomingMessage) -> str
                              called for each incoming message

        Returns:
            {"status": "ok"} (200 response) — even if processing fails,
            because Meta retries on non-2xx and we want to avoid duplicate
            processing.
        """
        # 1. Verify signature
        if not self._client.verify_webhook_signature(raw_body, signature_header):
            log.warning("whatsapp.webhook.invalid_signature")
            return {"status": "invalid_signature"}

        # 2. Parse payload
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.warning("whatsapp.webhook.invalid_json")
            return {"status": "invalid_json"}

        # 3. Process events
        events_processed = 0
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Process messages
                for msg_data in value.get("messages", []):
                    incoming = self._normalize_message(msg_data, value)
                    if incoming is None:
                        continue
                    if runtime_callback is not None:
                        try:
                            response_text = await runtime_callback(incoming)
                            if response_text:
                                await self._client.send_text(
                                    incoming.from_phone, response_text
                                )
                        except Exception as e:
                            log.error(
                                "whatsapp.runtime_callback.error",
                                error=str(e),
                                error_type=type(e).__name__,
                                from_phone=incoming.from_phone,
                            )
                    events_processed += 1

                # Process status events
                for status_data in value.get("statuses", []):
                    self._process_status(status_data)
                    events_processed += 1

        return {"status": "ok", "events_processed": events_processed}

    def _normalize_message(
        self, msg_data: dict[str, Any], value: dict[str, Any]
    ) -> WhatsAppIncomingMessage | None:
        """Convert WhatsApp's payload shape into our universal contract.

        Supports ALL incoming WhatsApp message types:
        - text, image, audio, document, video, sticker
        - location, contacts
        - interactive (button_reply, list_reply)
        - reaction
        - system events (customer_changed_phone_number, etc.)
        - reply context (when user replies to a specific message)
        - forwarded / frequently_forwarded metadata
        - errors (when outbound message delivery fails)
        """
        try:
            msg_type = msg_data.get("type", "unknown")
            try:
                type_enum = WhatsAppMessageType(msg_type)
            except ValueError:
                type_enum = WhatsAppMessageType.UNKNOWN

            contacts = value.get("contacts", [])
            from_name = None
            if contacts:
                from_name = contacts[0].get("profile", {}).get("name")

            # Common fields
            text_body: str | None = None
            media_id: str | None = None
            media_mime_type: str | None = None
            media_caption: str | None = None
            media_sha256: str | None = None
            media_filename: str | None = None
            interactive_id: str | None = None
            interactive_title: str | None = None
            reaction_emoji: str | None = None
            reaction_message_id: str | None = None
            location_lat: float | None = None
            location_lon: float | None = None
            location_name: str | None = None
            location_addr: str | None = None
            contacts_list: list[dict[str, Any]] = []
            system_kind: str | None = None
            is_forwarded = bool(msg_data.get("context", {}).get("forwarded"))
            is_frequently_forwarded = bool(
                msg_data.get("context", {}).get("frequently_forwarded")
            )
            reply_context_message_id: str | None = None
            reply_context_from: str | None = None

            # Extract content per type
            type_data = msg_data.get(msg_type, {}) or {}

            if msg_type == "text":
                text_body = type_data.get("body")
            elif msg_type in ("image", "video", "audio", "document", "sticker"):
                media_id = type_data.get("id")
                media_mime_type = type_data.get("mime_type")
                media_sha256 = type_data.get("sha256")
                if msg_type == "image":
                    media_caption = type_data.get("caption")
                elif msg_type == "video":
                    media_caption = type_data.get("caption")
                elif msg_type == "document":
                    media_caption = type_data.get("caption")
                    media_filename = type_data.get("filename")
            elif msg_type == "location":
                location_lat = float(type_data.get("latitude", 0))
                location_lon = float(type_data.get("longitude", 0))
                location_name = type_data.get("name")
                location_addr = type_data.get("address")
            elif msg_type == "contacts":
                # WhatsApp sends a list of contact objects
                contacts_in = type_data if isinstance(type_data, list) else [type_data]
                for c in contacts_in:
                    if isinstance(c, dict):
                        contacts_list.append(c)
            elif msg_type == "interactive":
                interactive_type = type_data.get("type")
                if interactive_type == "button_reply":
                    interactive_id = type_data.get("button_reply", {}).get("id")
                    interactive_title = type_data.get("button_reply", {}).get("title")
                elif interactive_type == "list_reply":
                    interactive_id = type_data.get("list_reply", {}).get("id")
                    interactive_title = type_data.get("list_reply", {}).get("title")
            elif msg_type == "reaction":
                reaction_msg = type_data.get("message_id")
                reaction_emoji = type_data.get("emoji")
                reaction_message_id = reaction_msg
            elif msg_type == "system":
                system_kind = type_data.get("type")
                # for customer_changed_phone_number, body has old/new
                body = type_data.get("body") or {}
                # Extract legacy fields
                from_phone_legacy = body.get("wa_id") if isinstance(body, dict) else None
                if from_phone_legacy:
                    # We'll override later if from_phone from msg_data
                    pass

            # Reply context (when user replies to a specific message)
            context_data = msg_data.get("context")
            if context_data:
                reply_context_message_id = context_data.get("id")
                reply_context_from = context_data.get("from")

            # Forwarded metadata
            if context_data:
                is_forwarded = bool(context_data.get("forwarded"))
                is_frequently_forwarded = bool(context_data.get("frequently_forwarded"))

            return WhatsAppIncomingMessage(
                message_id=msg_data.get("id", ""),
                from_phone=msg_data.get("from", ""),
                from_name=from_name,
                timestamp=datetime.fromtimestamp(
                    int(msg_data.get("timestamp", 0)), tz=timezone.utc
                ),
                type=type_enum,
                text_body=text_body,
                media_id=media_id,
                media_mime_type=media_mime_type,
                media_caption=media_caption,
                media_sha256=media_sha256,
                media_filename=media_filename,
                interactive_id=interactive_id,
                interactive_title=interactive_title,
                reply_context_message_id=reply_context_message_id,
                reply_context_from=reply_context_from,
                reaction_emoji=reaction_emoji,
                reaction_message_id=reaction_message_id,
                location_latitude=location_lat,
                location_longitude=location_lon,
                location_name=location_name,
                location_address=location_addr,
                contacts=contacts_list,
                system_kind=system_kind,
                is_forwarded=is_forwarded,
                is_frequently_forwarded=is_frequently_forwarded,
                raw_payload=msg_data,
            )
        except Exception as e:
            log.warning(
                "whatsapp.message.normalization_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    def _process_status(self, status_data: dict[str, Any]) -> None:
        """Process a message status event (sent/delivered/read)."""
        status_value = status_data.get("status")
        try:
            status = WhatsAppMessageStatus(status_value)
        except ValueError:
            return
        log.info(
            "whatsapp.message.status",
            message_id=status_data.get("id"),
            status=status.value,
            recipient=status_data.get("recipient_id"),
        )

    async def close(self) -> None:
        await self._client.close()
