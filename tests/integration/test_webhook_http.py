"""HTTP-level tests for the WhatsApp webhook endpoints.

These tests exist because the forensic audit (Section 19.1) proved that the
binding layer — the ONLY layer that was broken — had zero coverage:

  Defect 1 (POST binding): the handler declared its body and signature as
  plain parameters, which FastAPI bound as QUERY parameters. A genuine Meta
  POST was rejected with HTTP 422 before any WAX code ran.

  Defect 2 (GET verification): the handler bound `mode`, `token` and
  `challenge` while Meta sends `hub.mode`, `hub.verify_token` and
  `hub.challenge`, so webhook registration always failed with 403.

Every test here drives the real ASGI app over real HTTP semantics
(httpx ASGITransport), with Meta's exact wire shapes: hub.* query names,
a JSON body, and an X-Hub-Signature-256 header computed with HMAC-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from wax.interfaces.whatsapp.client import WhatsAppClient
from wax.state.engine import db_session

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "wax-test-verify-token"
PHONE_NUMBER_ID = "1234567890"
SENDER_PHONE = "2348012345678"
SENDER_NAME = "David"


def meta_text_payload(message_id: str, text: str) -> dict[str, Any]:
    """A realistic Meta webhook payload for an inbound text message."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": PHONE_NUMBER_ID,
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "2348000000000",
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": SENDER_NAME},
                                    "wa_id": SENDER_PHONE,
                                }
                            ],
                            "messages": [
                                {
                                    "from": SENDER_PHONE,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    """Compute Meta's X-Hub-Signature-256 header value for a body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_whatsapp_client(sent_log: list[httpx.Request]) -> WhatsAppClient:
    """Build a WhatsAppClient whose HTTP traffic is captured in sent_log."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent_log.append(request)
        return httpx.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.out"}]}
        )

    client = WhatsAppClient(
        access_token="test-access-token",
        phone_number_id=PHONE_NUMBER_ID,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
    )
    # Swap the underlying httpx client for one backed by a mock transport.
    client._client = httpx.AsyncClient(  # noqa: SLF001 — test seam
        base_url=WhatsAppClient.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture
async def wa_app(app: Any) -> Any:
    """The standard test app with a mock-backed WhatsApp client installed."""
    app.state.whatsapp_client = make_whatsapp_client(sent_log=[])  # placeholder; replaced per-test
    return app


async def _count(session_table: str) -> int:
    from sqlalchemy import text

    async with db_session() as session:
        result = await session.execute(text(f"SELECT COUNT(*) FROM {session_table}"))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Defect 2 — GET webhook verification with Meta's hub.* parameter names
# ---------------------------------------------------------------------------


class TestWebhookVerification:
    async def test_verification_with_meta_parameter_names_succeeds(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """Meta sends hub.mode / hub.verify_token / hub.challenge — must echo the challenge."""
        app.state.whatsapp_client = make_whatsapp_client([])
        response = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "CHALLENGE_12345",
            },
        )
        assert response.status_code == 200, response.text
        assert response.text == "CHALLENGE_12345"

    async def test_verification_with_wrong_token_is_rejected(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        app.state.whatsapp_client = make_whatsapp_client([])
        response = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "CHALLENGE_12345",
            },
        )
        assert response.status_code == 403

    async def test_verification_without_client_returns_503(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        app.state.whatsapp_client = None
        response = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "CHALLENGE_12345",
            },
        )
        assert response.status_code == 503

    async def test_openapi_schema_declares_hub_aliased_query_params(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """The generated OpenAPI schema must bind Meta's exact names, in=query."""
        schema = (await client.get("/openapi.json")).json()
        get_op = schema["paths"]["/webhooks/whatsapp"]["get"]
        param_names = {(p.get("name"), p.get("in")) for p in get_op.get("parameters", [])}
        assert ("hub.mode", "query") in param_names, param_names
        assert ("hub.verify_token", "query") in param_names
        assert ("hub.challenge", "query") in param_names


# ---------------------------------------------------------------------------
# Defect 1 — POST webhook with JSON body + signature header
# ---------------------------------------------------------------------------


class TestWebhookPostBinding:
    async def test_signed_meta_post_is_accepted_and_processed(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """A genuine Meta-shaped POST (JSON body + signature header) must reach WAX code."""
        sent_log: list[httpx.Request] = []
        app.state.whatsapp_client = make_whatsapp_client(sent_log)

        payload = meta_text_payload("wamid.HTTPTEST1", "Hello WAX")
        body = json.dumps(payload).encode()
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(body),
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        assert data["events_processed"] == 1

        # The pipeline ran: identity, idempotency, objective, execution, memory.
        assert await _count("principals") == 1
        assert await _count("principal_credentials") == 1
        assert await _count("processed_messages") == 1
        assert await _count("objectives") == 1
        assert await _count("executions") == 1
        assert await _count("memory_records") == 1

        # The reply was delivered through the interface client.
        assert len(sent_log) == 1
        sent_payload = json.loads(sent_log[0].content)
        assert sent_payload["type"] == "text"
        assert (
            "Hello WAX" in sent_payload["text"]["body"] or "[MOCK" in sent_payload["text"]["body"]
        )

    async def test_post_with_invalid_signature_is_not_processed(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        sent_log: list[httpx.Request] = []
        app.state.whatsapp_client = make_whatsapp_client(sent_log)

        body = json.dumps(meta_text_payload("wamid.HTTPTEST2", "Hello")).encode()
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "invalid_signature"
        assert sent_log == []
        assert await _count("processed_messages") == 0

    async def test_post_with_missing_signature_header_is_not_processed(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        app.state.whatsapp_client = make_whatsapp_client([])
        body = json.dumps(meta_text_payload("wamid.HTTPTEST3", "Hello")).encode()
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "invalid_signature"

    async def test_post_with_garbage_body_and_valid_signature_reports_invalid_json(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        app.state.whatsapp_client = make_whatsapp_client([])
        body = b"this is not json"
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(body),
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "invalid_json"

    async def test_redelivered_message_is_deduplicated(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """Meta redelivers on timeout — the same message ID must not re-execute."""
        sent_log: list[httpx.Request] = []
        app.state.whatsapp_client = make_whatsapp_client(sent_log)

        body = json.dumps(meta_text_payload("wamid.HTTPTEST4", "Once only")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign(body),
        }
        first = await client.post("/webhooks/whatsapp", content=body, headers=headers)
        assert first.status_code == 200
        second = await client.post("/webhooks/whatsapp", content=body, headers=headers)
        assert second.status_code == 200

        assert await _count("processed_messages") == 1
        assert await _count("executions") == 1
        # Only ONE outbound reply even though the webhook fired twice.
        assert len(sent_log) == 1

    async def test_openapi_schema_binds_signature_as_header_and_no_query_params(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """Regression guard: signature must be a header param; nothing may be a query param.

        (The raw body is read via `Request.body()`, which FastAPI intentionally
        does not describe as a schema requestBody — the wire-level behavior is
        proven by the signed-POST tests above.)
        """
        schema = (await client.get("/openapi.json")).json()
        post_op = schema["paths"]["/webhooks/whatsapp"]["post"]

        param_locs = {(p.get("name"), p.get("in")) for p in post_op.get("parameters", [])}
        assert ("X-Hub-Signature-256", "header") in param_locs, param_locs

        # No parameter may be declared as a query param for the POST op —
        # the original defect bound the body/signature as query params.
        query_params = [p for p in post_op.get("parameters", []) if p.get("in") == "query"]
        assert query_params == [], f"POST must not bind query params, found: {query_params}"
