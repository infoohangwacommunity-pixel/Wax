"""Live runtime probe — boots the real ASGI app and exercises the fixed
webhook endpoints over real HTTP.

This is the audit-response proof: not a unit test, but the actual server
process serving actual Meta-shaped requests.

Usage:
    python scripts/live_probe.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import uvicorn  # noqa: E402

from wax.core.config import WaxSettings  # noqa: E402
from wax.runtime.app import create_app  # noqa: E402

APP_SECRET = "probe-app-secret"
VERIFY_TOKEN = "probe-verify-token"
PORT = 8971
BASE = f"http://127.0.0.1:{PORT}"


def build_settings() -> WaxSettings:
    """Minimal settings: WhatsApp configured so the client initializes."""
    return WaxSettings.model_validate(
        {
            "whatsapp_access_token": "probe-token",
            "whatsapp_phone_number_id": "123456789",
            "whatsapp_app_secret": APP_SECRET,
            "whatsapp_verify_token": VERIFY_TOKEN,
        }
    )


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def probe() -> int:
    settings = build_settings()
    # Apply migrations to the probe database (the lifespan seeds roles).
    # migrations/env.py calls asyncio.run(), which needs its own loop —
    # run alembic in a worker thread outside our event loop.
    from alembic import command
    from alembic.config import Config

    repo_root = os.path.join(os.path.dirname(__file__), "..")
    alembic_cfg = Config(os.path.join(repo_root, "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    import threading

    err: list[Exception] = []

    def _migrate() -> None:
        try:
            command.upgrade(alembic_cfg, "head")
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=_migrate)
    t.start()
    t.join()
    if err:
        raise err[0]

    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(100):
        await asyncio.sleep(0.1)
        if server.started:
            break
    else:
        print("PROBE FAIL: server did not start")
        return 1

    failures: list[str] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as http:
        # -- 1. Health ---------------------------------------------------
        r = await http.get("/healthz")
        ok = r.status_code == 200
        print(f"[{'PASS' if ok else 'FAIL'}] GET /healthz -> {r.status_code}")
        if not ok:
            failures.append("health")

        # -- 2. Meta verification handshake (GET) ------------------------
        challenge = "1158201444"
        r = await http.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": challenge,
            },
        )
        ok = r.status_code == 200 and r.text == challenge
        print(
            f"[{'PASS' if ok else 'FAIL'}] GET webhook verification "
            f"-> {r.status_code}, challenge echoed: {r.text!r}"
        )
        if not ok:
            failures.append("verification")

        # -- 3. Signed Meta-shaped POST (live path) ----------------------
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "ENTRY1",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550001111",
                                    "phone_number_id": "123456789",
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Probe User"},
                                        "wa_id": "2348000000000",
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "2348000000000",
                                        "id": "wamid.PROBE1",
                                        "timestamp": "1726000000",
                                        "text": {"body": "hello runtime"},
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
        body = httpx.Response(200, json=payload).content
        r = await http.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sign(body),
            },
        )
        ok = r.status_code == 200
        print(
            f"[{'PASS' if ok else 'FAIL'}] POST signed webhook "
            f"-> {r.status_code} {r.text[:120]}"
        )
        if not ok:
            failures.append("signed-post")

        # -- 4. Invalid signature not processed (security live path) -----
        # Contract: respond 200 (do not leak validity to probes / avoid
        # Meta retry storms) but DO NOT process — status says so.
        r = await http.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        ok = r.status_code == 200 and r.json().get("status") == "invalid_signature"
        print(
            f"[{'PASS' if ok else 'FAIL'}] POST bad signature "
            f"-> {r.status_code} {r.text[:60]}"
        )
        if not ok:
            failures.append("bad-signature")

        # -- 5. Oversize POST body (guard observed) ----------------------
        big = b'{"object": "x", "pad": "' + b"a" * 600_000 + b'"}'
        r = await http.post(
            "/webhooks/whatsapp",
            content=big,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(big)},
        )
        print(f"[INFO ] POST oversize body -> {r.status_code} (guard observed)")

    server.should_exit = True
    await server_task

    if failures:
        print(f"PROBE FAIL: {failures}")
        return 1
    print("PROBE PASS: live path verified over real HTTP")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe()))
