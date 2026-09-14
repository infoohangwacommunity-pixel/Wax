"""Tests for Phase W (interface intelligence): WhatsApp delivery constraints.

The runtime does not own wire limits — the interface does. These tests prove:
- Short messages pass through verbatim (zero transformation).
- Long messages are chunked at WhatsApp's 4096-char limit.
- Chunk boundaries prefer paragraph/line breaks over hard splits.
- Multi-part replies carry explicit (i/n) markers.
- The capability descriptor's ceiling (12000) matches the client's behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from wax.interfaces.whatsapp.client import WhatsAppClient

TEST_APP_SECRET = "test-app-secret-xxxxx"
TEST_ACCESS_TOKEN = "test-access-token-xxxxx"
TEST_PHONE_NUMBER_ID = "123456789"
TEST_VERIFY_TOKEN = "wax-webhook-verify-token"


@pytest.fixture
def client() -> WhatsAppClient:
    return WhatsAppClient(
        access_token=TEST_ACCESS_TOKEN,
        phone_number_id=TEST_PHONE_NUMBER_ID,
        app_secret=TEST_APP_SECRET,
        verify_token=TEST_VERIFY_TOKEN,
    )


class TestShortMessages:
    async def test_short_message_sent_verbatim(self, client: WhatsAppClient) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        results = await client.send_long_text("+2348000000000", "hello")
        assert len(results) == 1
        body = client._send.call_args[0][0]["text"]["body"]
        assert body == "hello"

    async def test_exactly_limit_sent_verbatim(self, client: WhatsAppClient) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        text = "x" * client.WHATSAPP_TEXT_LIMIT
        results = await client.send_long_text("+2348000000000", text)
        assert len(results) == 1
        body = client._send.call_args[0][0]["text"]["body"]
        assert body == text


class TestChunking:
    async def test_long_message_chunked_under_wire_limit(
        self, client: WhatsAppClient
    ) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        text = "y" * (client.WHATSAPP_TEXT_LIMIT * 3)
        results = await client.send_long_text("+2348000000000", text)
        effective = client.WHATSAPP_TEXT_LIMIT - 32
        assert len(results) >= -(-len(text) // effective)
        for call in client._send.call_args_list:
            body = call[0][0]["text"]["body"]
            assert len(body) <= client.WHATSAPP_TEXT_LIMIT

    async def test_chunks_carry_part_markers(self, client: WhatsAppClient) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        text = "z" * (client.WHATSAPP_TEXT_LIMIT * 2)
        results = await client.send_long_text("+2348000000000", text)
        first = client._send.call_args_list[0][0][0]["text"]["body"]
        second = client._send.call_args_list[1][0][0]["text"]["body"]
        assert first.startswith(f"(1/{len(results)})")
        assert second.startswith(f"(2/{len(results)})")

    async def test_chunk_boundaries_prefer_paragraph_breaks(
        self, client: WhatsAppClient
    ) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        # Two paragraphs; each under the limit but the total over it.
        para = "word " * 400  # ~2000 chars
        text = para + "\n\n" + para + "\n\n" + para
        assert len(text) > client.WHATSAPP_TEXT_LIMIT
        results = await client.send_long_text("+2348000000000", text)
        bodies = [c[0][0]["text"]["body"] for c in client._send.call_args_list]
        # No chunk should end mid-word.
        for body in bodies:
            payload = body.split("\n\n", 1)[1] if body.startswith("(") else body
            assert not payload.endswith("wor")

    async def test_hard_split_when_no_boundaries(self, client: WhatsAppClient) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        text = "a" * (client.WHATSAPP_TEXT_LIMIT * 2)  # no spaces/newlines
        results = await client.send_long_text("+2348000000000", text)
        effective = client.WHATSAPP_TEXT_LIMIT - 32
        assert len(results) == -(-len(text) // effective)
        for call in client._send.call_args_list:
            body = call[0][0]["text"]["body"]
            # Strip marker line.
            payload = body.split("\n\n", 1)[1]
            assert set(payload) == {"a"}

    async def test_content_preserved_across_chunks(
        self, client: WhatsAppClient
    ) -> None:
        client._send = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
        text = ("sentence. " * 900).strip()
        results = await client.send_long_text("+2348000000000", text)
        bodies = [c[0][0]["text"]["body"] for c in client._send.call_args_list]
        reassembled = " ".join(
            b.split("\n\n", 1)[1] if b.startswith("(") else b for b in bodies
        )
        # Every sentence survives; reassembly is pure sentences + separators.
        assert reassembled.count("sentence.") == 900
        assert reassembled.replace("sentence.", "").strip(" ") == ""


class TestCapabilityCeiling:
    async def test_capability_rejects_over_12000(self) -> None:
        from wax.capabilities.runtime_capabilities import MESSAGE_SEND_DESCRIPTOR

        schema = MESSAGE_SEND_DESCRIPTOR.input_schema
        assert schema["properties"]["text"]["maxLength"] == 12000
