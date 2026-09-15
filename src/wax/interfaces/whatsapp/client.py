"""WhatsApp Cloud API client.

Wraps the WhatsApp Business Cloud API. Handles:
- Sending messages (text, media, templates, interactive)
- Webhook signature verification (HMAC-SHA256)
- Idempotency (WAMID-based dedup at the adapter layer)

Authentication: Meta's app secret + access token + phone number ID + verify token.
The verify token is set by us; Meta echoes it back during webhook setup.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from wax.core.exceptions import WaxConfigurationError, WaxValidationError
from wax.interfaces.whatsapp.contracts import WhatsAppOutgoingMessage
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class WhatsAppClient:
    """HTTP client for the WhatsApp Cloud API.

    Auth model:
    - app_secret: used to verify webhook signatures (HMAC-SHA256)
    - access_token: bearer token for sending messages
    - phone_number_id: WhatsApp Business phone number ID
    - verify_token: arbitrary string we set; Meta echoes during webhook setup
    """

    BASE_URL = "https://graph.facebook.com/v20.0"

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        app_secret: str,
        verify_token: str,
    ) -> None:
        if not access_token:
            raise WaxConfigurationError("WhatsApp access_token is required")
        if not phone_number_id:
            raise WaxConfigurationError("WhatsApp phone_number_id is required")
        if not app_secret:
            raise WaxConfigurationError("WhatsApp app_secret is required")
        if not verify_token:
            raise WaxConfigurationError("WhatsApp verify_token is required")

        self._phone_number_id = phone_number_id
        self._app_secret = app_secret
        self._verify_token = verify_token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def send_text(self, to_phone: str, text: str) -> dict[str, Any]:
        """Send a text message."""
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "text",
                "text": {"body": text},
            }
        )

    WHATSAPP_TEXT_LIMIT = 4096

    async def send_long_text(self, to_phone: str, text: str) -> list[dict[str, Any]]:
        """Send text of ANY length by chunking to WhatsApp's 4096-char limit.

        This is interface intelligence (Phase W): the delivery constraint
        lives HERE — in the interface that owns it — not in the runtime.
        Chunk boundaries prefer paragraph/line breaks; multi-part replies
        carry explicit "(part i/n)" markers so the conversation reads
        coherently. Single-shot messages under the limit are sent verbatim
        with zero transformation.
        """
        limit = self.WHATSAPP_TEXT_LIMIT - 32  # room for part markers
        if len(text) <= self.WHATSAPP_TEXT_LIMIT:
            return [await self.send_text(to_phone, text)]

        chunks = self._chunk_text(text, limit)
        results: list[dict[str, Any]] = []
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            marked = f"({i}/{total})\n\n{chunk}" if total > 1 else chunk
            results.append(await self.send_text(to_phone, marked))
        return results

    @staticmethod
    def _chunk_text(text: str, limit: int) -> list[str]:
        """Split into <=limit chunks, preferring paragraph then line breaks."""
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            window = remaining[: limit + 1]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
            if cut < limit // 2:  # no reasonable boundary — hard split
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
        return [c for c in chunks if c]

    async def send_message(self, msg: WhatsAppOutgoingMessage) -> dict[str, Any]:
        """Send a structured message."""
        payload = self._build_payload(msg)
        return await self._send(payload)

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        language: str = "en_US",
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a pre-approved template message."""
        template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
        if components:
            template["components"] = components
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "template",
                "template": template,
            }
        )

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Send a message with up to 3 buttons.

        Each button: {"type": "reply", "reply": {"id": "btn_1", "title": "Yes"}}
        """
        if len(buttons) > 3:
            raise WaxValidationError("WhatsApp allows max 3 buttons per message")
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text},
                    "action": {"buttons": buttons},
                },
            }
        )

    async def send_interactive_list(
        self,
        to_phone: str,
        body_text: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send a message with a list of options (max 10 per section)."""
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body_text},
                    "action": {
                        "button": button_text,
                        "sections": sections,
                    },
                },
            }
        )

    async def send_reaction(self, to_phone: str, message_id: str, emoji: str) -> dict[str, Any]:
        """React to a user's message with an emoji."""
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            }
        )

    async def send_location(
        self,
        to_phone: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Send a location pin to the user."""
        location: dict[str, Any] = {
            "latitude": str(latitude),
            "longitude": str(longitude),
        }
        if name:
            location["name"] = name
        if address:
            location["address"] = address
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "location",
                "location": location,
            }
        )

    async def send_contacts(self, to_phone: str, contacts: list[dict[str, Any]]) -> dict[str, Any]:
        """Send one or more contacts (vCard format).

        Each contact dict must match WhatsApp's contacts schema (see
        https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages).
        """
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "contacts",
                "contacts": contacts,
            }
        )

    async def send_media(
        self,
        to_phone: str,
        media_type: str,
        *,
        media_id: str | None = None,
        media_link: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Send an image, video, audio, document, or sticker.

        Either media_id (uploaded via upload_media) or media_link (URL
        that Meta fetches) is required.

        Args:
            media_type: one of image, video, audio, document, sticker
            media_id: media_id returned by upload_media
            media_link: publicly-accessible URL Meta fetches
            caption: optional (image, video, document only)
            filename: optional (documents only)
            reply_to_message_id: optional reply context
        """
        if media_type not in ("image", "video", "audio", "document", "sticker"):
            raise WaxValidationError(f"Unsupported media type: {media_type!r}")
        if not media_id and not media_link:
            raise WaxValidationError("Either media_id or media_link is required")
        media_obj: dict[str, Any] = {}
        if media_id:
            media_obj["id"] = media_id
        if media_link:
            media_obj["link"] = media_link
        if caption and media_type in ("image", "video", "document"):
            media_obj["caption"] = caption
        if filename and media_type == "document":
            media_obj["filename"] = filename

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": media_type,
            media_type: media_obj,
        }
        if reply_to_message_id:
            payload["context"] = {"message_id": reply_to_message_id}
        return await self._send(payload)

    async def mark_as_read(self, message_id: str) -> dict[str, Any]:
        """Mark an incoming message as read (sends read receipt to user).

        Also requires a typing indicator to be sent before this for the
        user to see the read receipt immediately.
        """
        url = f"/{self._phone_number_id}/messages"
        response = await self._client.post(
            url,
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            },
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    # NOTE (reconciliation-5): a previous send_typing_indicator method
    # was REMOVED here. It was dead code (no caller) that posted a
    # fabricated payload ("type": "reaction" + "typing" field) and
    # commented itself as a placeholder. WAX does not ship fake
    # implementations; if typing indicators are wanted, implement and
    # live-verify against the real Meta Cloud API contract first.
    # -------------------------------------------------------------------
    # Media lifecycle (Phase S)
    # -------------------------------------------------------------------

    async def upload_media(
        self,
        *,
        mime_type: str,
        filename: str,
        file_bytes: bytes,
    ) -> str:
        """Upload binary media to Meta for sending later.

        Returns the media_id to use with send_media(media_id=...).

        Args:
            mime_type: e.g. "image/jpeg", "audio/ogg", "application/pdf"
            filename: the filename to associate
            file_bytes: the raw binary content
        """
        url = f"/{self._phone_number_id}/media"
        # Note: this requires multipart/form-data, not JSON
        files = {
            "file": (filename, file_bytes, mime_type),
        }
        # Strip JSON content-type header for multipart upload
        headers = {"Authorization": f"Bearer {self._access_token_for_uploads}"}
        response = await self._client.post(
            url,
            data={
                "messaging_product": "whatsapp",
                "type": mime_type.split("/")[0],  # image, video, audio, document
            },
            files=files,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        media_id = data.get("id")
        if not media_id:
            raise WaxValidationError(f"Media upload did not return an id: {data}")
        log.info(
            "whatsapp.media.uploaded",
            media_id=media_id,
            mime_type=mime_type,
            filename=filename,
            size_bytes=len(file_bytes),
        )
        return media_id

    async def download_media(self, media_id: str) -> bytes:
        """Download media binary by its media_id.

        Returns the raw bytes. Phase T (Media Intelligence Pipeline) uses
        this to fetch images/audio/documents for OCR/transcription.
        """
        # Step 1: get the temporary media URL
        url = f"/{media_id}"
        response = await self._client.get(url)
        response.raise_for_status()
        data = response.json()
        media_url = data.get("url")
        if not media_url:
            raise WaxValidationError(f"Media {media_id} did not return a URL")

        # Step 2: download the binary
        async with self._client.stream("GET", media_url) as download_response:
            download_response.raise_for_status()
            chunks: list[bytes] = []
            async for chunk in download_response.aiter_bytes():
                chunks.append(chunk)
            return b"".join(chunks)

    async def delete_media(self, media_id: str) -> dict[str, Any]:
        """Revoke a previously uploaded media_id."""
        url = f"/{media_id}"
        response = await self._client.delete(url)
        response.raise_for_status()
        return response.json() if response.content else {}

    @property
    def _access_token_for_uploads(self) -> str:
        """Extract the access token from the client's headers.

        The Authorization header was set at construction time as
        'Bearer <token>'. We extract it for multipart uploads where
        we override headers.
        """
        auth = self._client.headers.get("Authorization", "")
        return auth.removeprefix("Bearer ").strip()

    def verify_webhook_signature(self, payload: bytes, signature_header: str) -> bool:
        """Verify the X-Hub-Signature-256 header.

        Meta computes HMAC-SHA256 of the raw request body using the app
        secret, prefixed with 'sha256='. We recompute and compare.
        """
        if not signature_header:
            return False
        if not signature_header.startswith("sha256="):
            return False
        expected = signature_header.removeprefix("sha256=")

        computed = hmac.new(
            self._app_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        # Use compare_digest to avoid timing attacks
        return hmac.compare_digest(expected, computed)

    def get_verify_token(self) -> str:
        """Return the verify token (used during webhook setup with Meta)."""
        return self._verify_token

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"/{self._phone_number_id}/messages"
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        log.info(
            "whatsapp.message.sent",
            to=payload.get("to"),
            type=payload.get("type"),
            message_id=data.get("messages", [{}])[0].get("id"),
        )
        return data

    def _build_payload(self, msg: WhatsAppOutgoingMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": msg.to_phone,
            "type": msg.type.value,
        }
        if msg.type.value == "text":
            payload["text"] = {"body": msg.text_body or ""}
        elif msg.type.value == "template":
            template: dict[str, Any] = {
                "name": msg.template_name,
                "language": {"code": msg.template_language},
            }
            if msg.template_components:
                template["components"] = msg.template_components
            payload["template"] = template
        return payload
