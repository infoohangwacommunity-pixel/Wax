"""Tests for Phase Q (WhatsApp interface)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from wax.interfaces.whatsapp.adapter import WhatsAppAdapter
from wax.interfaces.whatsapp.client import WhatsAppClient
from wax.interfaces.whatsapp.contracts import (
    WhatsAppIncomingMessage,
    WhatsAppMessageType,
    WhatsAppOutgoingMessage,
)

# Test credentials — do NOT use these in production.
TEST_APP_SECRET = "test-app-secret-xxxxx"
TEST_ACCESS_TOKEN = "test-access-token-xxxxx"
TEST_PHONE_NUMBER_ID = "123456789"
TEST_VERIFY_TOKEN = "wax-webhook-verify-token"


@pytest.fixture
def whatsapp_client() -> WhatsAppClient:
    return WhatsAppClient(
        access_token=TEST_ACCESS_TOKEN,
        phone_number_id=TEST_PHONE_NUMBER_ID,
        app_secret=TEST_APP_SECRET,
        verify_token=TEST_VERIFY_TOKEN,
    )


@pytest.fixture
def adapter(whatsapp_client: WhatsAppClient) -> WhatsAppAdapter:
    return WhatsAppAdapter(client=whatsapp_client)


def _sign(body: bytes, secret: str) -> str:
    """Compute the X-Hub-Signature-256 header value."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _build_text_message_webhook(
    from_phone: str = "+2348000000000",
    text: str = "hello from WhatsApp",
    message_id: str = "wamid.test123",
    contact_name: str = "Test User",
) -> dict[str, Any]:
    """Build a minimal valid WhatsApp webhook payload containing one text message."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "12345",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "+2348000000001"},
                            "contacts": [
                                {
                                    "profile": {"name": contact_name},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


class TestWhatsAppClient:
    def test_constructs_with_valid_credentials(self) -> None:
        client = WhatsAppClient(
            access_token=TEST_ACCESS_TOKEN,
            phone_number_id=TEST_PHONE_NUMBER_ID,
            app_secret=TEST_APP_SECRET,
            verify_token=TEST_VERIFY_TOKEN,
        )
        assert client.get_verify_token() == TEST_VERIFY_TOKEN

    def test_rejects_missing_credentials(self) -> None:
        from wax.core.exceptions import WaxConfigurationError

        with pytest.raises(WaxConfigurationError):
            WhatsAppClient(access_token="", phone_number_id="x", app_secret="y", verify_token="z")
        with pytest.raises(WaxConfigurationError):
            WhatsAppClient(access_token="x", phone_number_id="", app_secret="y", verify_token="z")

    def test_verify_webhook_signature_valid(self, whatsapp_client: WhatsAppClient) -> None:
        body = b'{"hello":"world"}'
        sig = _sign(body, TEST_APP_SECRET)
        assert whatsapp_client.verify_webhook_signature(body, sig)

    def test_verify_webhook_signature_invalid(self, whatsapp_client: WhatsAppClient) -> None:
        body = b'{"hello":"world"}'
        sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"
        assert not whatsapp_client.verify_webhook_signature(body, sig)

    def test_verify_webhook_signature_missing_header(self, whatsapp_client: WhatsAppClient) -> None:
        assert not whatsapp_client.verify_webhook_signature(b"{}", "")

    def test_verify_webhook_signature_wrong_prefix(self, whatsapp_client: WhatsAppClient) -> None:
        body = b"{}"
        assert not whatsapp_client.verify_webhook_signature(body, "sha1=abc")


class TestWhatsAppAdapterVerification:
    def test_verify_webhook_token_success(self, adapter: WhatsAppAdapter) -> None:
        challenge = adapter.verify_webhook_token("subscribe", TEST_VERIFY_TOKEN, "CHALLENGE_123")
        assert challenge == "CHALLENGE_123"

    def test_verify_webhook_token_wrong_token(self, adapter: WhatsAppAdapter) -> None:
        challenge = adapter.verify_webhook_token("subscribe", "wrong-token", "CHALLENGE")
        assert challenge is None

    def test_verify_webhook_token_wrong_mode(self, adapter: WhatsAppAdapter) -> None:
        challenge = adapter.verify_webhook_token("unsubscribe", TEST_VERIFY_TOKEN, "C")
        assert challenge is None


class TestWhatsAppAdapterWebhook:
    async def test_invalid_signature_rejected(self, adapter: WhatsAppAdapter) -> None:
        body = b'{"entry":[]}'
        result = await adapter.handle_webhook(
            raw_body=body,
            signature_header="sha256=invalid",
            runtime_callback=None,
        )
        assert result["status"] == "invalid_signature"

    async def test_valid_webhook_processes_message(self, adapter: WhatsAppAdapter) -> None:
        payload = _build_text_message_webhook(text="hello")
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)

        callback = AsyncMock(return_value="response text")
        # Patch the client's send_text method to avoid real HTTP
        adapter._client.send_text = AsyncMock(return_value={"messages": [{"id": "out1"}]})

        result = await adapter.handle_webhook(
            raw_body=body,
            signature_header=sig,
            runtime_callback=callback,
        )

        assert result["status"] == "ok"
        assert result["events_processed"] == 1
        callback.assert_awaited_once()
        # The callback should have been called with a normalized message
        incoming = callback.await_args.args[0]
        assert incoming.from_phone == "+2348000000000"
        assert incoming.text_body == "hello"
        assert incoming.type == WhatsAppMessageType.TEXT
        # The adapter should have sent the response back
        adapter._client.send_text.assert_awaited_once()

    async def test_text_message_normalized_correctly(self, adapter: WhatsAppAdapter) -> None:
        payload = _build_text_message_webhook(
            from_phone="+2348999999999", text="Hello WAX", contact_name="Ada"
        )
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)

        captured: list[WhatsAppIncomingMessage] = []

        async def capture(msg: WhatsAppIncomingMessage) -> str:
            captured.append(msg)
            return ""

        await adapter.handle_webhook(
            raw_body=body,
            signature_header=sig,
            runtime_callback=capture,
        )

        assert len(captured) == 1
        m = captured[0]
        assert m.from_phone == "+2348999999999"
        assert m.from_name == "Ada"
        assert m.type == WhatsAppMessageType.TEXT
        assert m.text_body == "Hello WAX"
        assert m.message_id == "wamid.test123"

    async def test_image_message_normalized(self, adapter: WhatsAppAdapter) -> None:
        """Image messages should capture media_id, mime_type, and caption."""
        payload = _build_text_message_webhook()
        # Replace the text message with an image message
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        msg["type"] = "image"
        msg["image"] = {"id": "media_abc", "mime_type": "image/jpeg", "caption": "Look at this"}
        msg.pop("text", None)

        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)

        captured: list[WhatsAppIncomingMessage] = []

        async def capture(msg: WhatsAppIncomingMessage) -> str:
            captured.append(msg)
            return ""

        await adapter.handle_webhook(
            raw_body=body,
            signature_header=sig,
            runtime_callback=capture,
        )

        assert len(captured) == 1
        m = captured[0]
        assert m.type == WhatsAppMessageType.IMAGE
        assert m.media_id == "media_abc"
        assert m.media_mime_type == "image/jpeg"
        assert m.media_caption == "Look at this"
        # effective_text should include the caption + a marker for the media
        # (Phase T will replace the marker with extracted text from OCR/etc.)
        assert "Look at this" in m.effective_text
        assert "media_abc" in m.effective_text

    async def test_status_event_processed(self, adapter: WhatsAppAdapter) -> None:
        """Webhooks also deliver message status (sent/delivered/read)."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "statuses": [
                                    {
                                        "id": "wamid.out1",
                                        "status": "delivered",
                                        "recipient_id": "+2348000000000",
                                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)

        result = await adapter.handle_webhook(
            raw_body=body,
            signature_header=sig,
            runtime_callback=None,
        )
        assert result["status"] == "ok"
        assert result["events_processed"] == 1

    async def test_callback_exception_does_not_fail_webhook(self, adapter: WhatsAppAdapter) -> None:
        """If the runtime callback raises, the webhook still returns 200.

        Meta retries on non-2xx; we want to avoid duplicate processing.
        The exception is logged but not propagated.
        """
        payload = _build_text_message_webhook()
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)

        async def failing_callback(msg: WhatsAppIncomingMessage) -> str:
            raise RuntimeError("intentional callback failure")

        # The send_text call should not happen because the callback failed
        adapter._client.send_text = AsyncMock()

        result = await adapter.handle_webhook(
            raw_body=body,
            signature_header=sig,
            runtime_callback=failing_callback,
        )
        assert result["status"] == "ok"
        adapter._client.send_text.assert_not_awaited()


class TestWhatsAppOutgoingContract:
    def test_text_message_payload(self) -> None:
        msg = WhatsAppOutgoingMessage(
            to_phone="+2348000000000",
            type=WhatsAppMessageType.TEXT,
            text_body="hello there",
        )
        assert msg.to_phone == "+2348000000000"
        assert msg.text_body == "hello there"
        assert msg.type == WhatsAppMessageType.TEXT

    def test_template_defaults(self) -> None:
        msg = WhatsAppOutgoingMessage(
            to_phone="+2348000000000",
            type=WhatsAppMessageType.TEMPLATE,
            template_name="welcome_message",
        )
        assert msg.template_language == "en_US"
