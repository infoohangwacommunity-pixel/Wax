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
