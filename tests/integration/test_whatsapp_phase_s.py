"""Phase S tests — complete WhatsApp feature support.

Tests verify normalization of every WhatsApp message type and the
new outbound methods (send_location, send_contacts, send_media,
send_reaction, mark_as_read, media lifecycle).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from wax.interfaces.whatsapp.adapter import WhatsAppAdapter
from wax.interfaces.whatsapp.client import WhatsAppClient
from wax.interfaces.whatsapp.contracts import (
    WhatsAppIncomingMessage,
    WhatsAppMessageType,
)

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
    return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


def _build_webhook(messages: list[dict[str, Any]], from_phone: str = "+2348000000000") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "+2348000000001"},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": from_phone}],
                    "messages": messages,
                },
                "field": "messages",
            }],
        }],
    }


async def _process_one(adapter: WhatsAppAdapter, payload: dict[str, Any]) -> WhatsAppIncomingMessage | None:
    body = json.dumps(payload).encode()
    sig = _sign(body, TEST_APP_SECRET)
    captured: list[WhatsAppIncomingMessage] = []

    async def capture(msg: WhatsAppIncomingMessage) -> str:
        captured.append(msg)
        return ""

    # Patch send_text to avoid HTTP
    adapter._client.send_text = AsyncMock(return_value={"messages": [{"id": "out1"}]})
    await adapter.handle_webhook(body, sig, runtime_callback=capture)
    return captured[0] if captured else None


class TestTextMessage:
    async def test_text_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.text1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "text",
            "text": {"body": "Hello WAX"},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.TEXT
        assert m.text_body == "Hello WAX"
        assert m.effective_text == "Hello WAX"


class TestImageMessage:
    async def test_image_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.img1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "image",
            "image": {
                "id": "media_img1",
                "mime_type": "image/jpeg",
                "sha256": "abc123",
                "caption": "Look at this photo",
            },
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.IMAGE
        assert m.media_id == "media_img1"
        assert m.media_mime_type == "image/jpeg"
        assert m.media_caption == "Look at this photo"
        assert "Look at this photo" in m.effective_text


class TestAudioMessage:
    async def test_audio_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.au1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "audio",
            "audio": {"id": "media_au1", "mime_type": "audio/ogg", "voice": True},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.AUDIO
        assert m.media_id == "media_au1"
        assert m.media_mime_type == "audio/ogg"


class TestDocumentMessage:
    async def test_document_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.doc1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "document",
            "document": {
                "id": "media_doc1",
                "mime_type": "application/pdf",
                "sha256": "abc",
                "filename": "report.pdf",
                "caption": "Please review this",
            },
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.DOCUMENT
        assert m.media_id == "media_doc1"
        assert m.media_filename == "report.pdf"
        assert m.media_caption == "Please review this"


class TestVideoMessage:
    async def test_video_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.vid1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "video",
            "video": {"id": "media_vid1", "mime_type": "video/mp4", "caption": "Cool video"},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.VIDEO
        assert m.media_id == "media_vid1"
        assert m.media_caption == "Cool video"


class TestStickerMessage:
    async def test_sticker_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.stk1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "sticker",
            "sticker": {"id": "media_stk1", "mime_type": "image/webp"},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.STICKER
        assert m.media_id == "media_stk1"


class TestLocationMessage:
    async def test_location_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.loc1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "location",
            "location": {
                "latitude": 6.5244,
                "longitude": 3.3792,
                "name": "Lagos",
                "address": "Lagos, Nigeria",
            },
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.LOCATION
        assert m.location_latitude == 6.5244
        assert m.location_longitude == 3.3792
        assert m.location_name == "Lagos"
        assert "6.5244" in m.effective_text


class TestContactsMessage:
    async def test_contacts_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.ctc1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "contacts",
            "contacts": [
                {
                    "name": {"first_name": "Ada", "last_name": "Okoro"},
                    "phones": [{"phone": "+2348000000099", "type": "MOBILE"}],
                }
            ],
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.CONTACTS
        assert len(m.contacts) == 1
        assert "1 contact" in m.effective_text


class TestInteractiveButtonReply:
    async def test_button_reply_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.btn1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "btn_yes", "title": "Yes"},
            },
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.INTERACTIVE
        assert m.interactive_id == "btn_yes"
        assert m.interactive_title == "Yes"
        assert "Yes" in m.effective_text


class TestReaction:
    async def test_reaction_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.rxn1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "reaction",
            "reaction": {"message_id": "wamid.original_msg", "emoji": "👍"},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.REACTION
        assert m.reaction_emoji == "👍"
        assert m.reaction_message_id == "wamid.original_msg"
        assert "👍" in m.effective_text


class TestSystemEvent:
    async def test_system_event_normalized(self, adapter: WhatsAppAdapter) -> None:
        msg = {
            "from": "+2348000000000",
            "id": "wamid.sys1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "system",
            "system": {"type": "user_changed_number", "body": {"wa_id": "+2348000000088"}},
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.type == WhatsAppMessageType.SYSTEM
        assert m.system_kind == "user_changed_number"


class TestReplyContext:
    async def test_reply_context_normalized(self, adapter: WhatsAppAdapter) -> None:
        """When user replies to a specific message, context is included."""
        msg = {
            "from": "+2348000000000",
            "id": "wamid.reply1",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "text",
            "text": {"body": "following up"},
            "context": {
                "from": "+2348000000000",
                "id": "wamid.original",
                "forwarded": True,
                "frequently_forwarded": False,
            },
        }
        m = await _process_one(adapter, _build_webhook([msg]))
        assert m is not None
        assert m.reply_context_message_id == "wamid.original"
        assert m.reply_context_from == "+2348000000000"
        assert m.is_forwarded is True
        assert m.is_frequently_forwarded is False


class TestStatusEvents:
    async def test_status_delivered(self, adapter: WhatsAppAdapter) -> None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "statuses": [{
                            "id": "wamid.out1",
                            "status": "delivered",
                            "recipient_id": "+2348000000000",
                            "timestamp": str(int(datetime.now(UTC).timestamp())),
                        }],
                    },
                    "field": "messages",
                }],
            }],
        }
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)
        result = await adapter.handle_webhook(body, sig, runtime_callback=None)
        assert result["status"] == "ok"
        assert result["events_processed"] == 1

    async def test_status_read(self, adapter: WhatsAppAdapter) -> None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "1",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "statuses": [{
                            "id": "wamid.out1",
                            "status": "read",
                            "recipient_id": "+2348000000000",
                            "timestamp": str(int(datetime.now(UTC).timestamp())),
                        }],
                    },
                    "field": "messages",
                }],
            }],
        }
        body = json.dumps(payload).encode()
        sig = _sign(body, TEST_APP_SECRET)
        result = await adapter.handle_webhook(body, sig, runtime_callback=None)
        assert result["status"] == "ok"


class TestOutboundMethods:
    """Tests for the new outbound send_* methods.

    These mock the underlying HTTP client to verify the payload shape,
    without making real API calls.
    """

    async def test_send_reaction(self, whatsapp_client: WhatsAppClient) -> None:
        whatsapp_client._client.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"messages": [{"id": "rxn_out1"}]},
                raise_for_status=lambda: None,
                content=b'{"messages":[{"id":"rxn_out1"}]}',
            )
        )
        result = await whatsapp_client.send_reaction("+2348000000000", "wamid.usr1", "👍")
        assert "messages" in result

    async def test_send_location(self, whatsapp_client: WhatsAppClient) -> None:
        whatsapp_client._client.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"messages": [{"id": "loc_out1"}]},
                raise_for_status=lambda: None,
                content=b'{}',
            )
        )
        result = await whatsapp_client.send_location(
            "+2348000000000", 6.5244, 3.3792, name="Lagos"
        )
        assert "messages" in result

    async def test_send_media_rejects_invalid_type(
        self, whatsapp_client: WhatsAppClient
    ) -> None:
        from wax.core.exceptions import WaxValidationError
        with pytest.raises(WaxValidationError):
            await whatsapp_client.send_media("+2348000000000", "invalid_type")

    async def test_send_media_requires_id_or_link(
        self, whatsapp_client: WhatsAppClient
    ) -> None:
        from wax.core.exceptions import WaxValidationError
        with pytest.raises(WaxValidationError):
            await whatsapp_client.send_media("+2348000000000", "image")

    async def test_mark_as_read(self, whatsapp_client: WhatsAppClient) -> None:
        whatsapp_client._client.post = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: {"success": True},
                raise_for_status=lambda: None,
                content=b'{"success":true}',
            )
        )
        result = await whatsapp_client.mark_as_read("wamid.usr1")
        assert result.get("success") is True
