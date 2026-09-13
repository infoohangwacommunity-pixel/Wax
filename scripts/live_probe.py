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

import uvicorn
from ulid import ULID

from wax.core.config import WaxSettings
from wax.runtime.app import create_app

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
        except Exception as exc:
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

        # -- 1b. Readiness (DB + intelligence + interface) ----------------
        r = await http.get("/readyz")
        ready_ok = r.status_code == 200
        print(f"[{'PASS' if ready_ok else 'FAIL'}] GET /readyz -> {r.status_code} {r.text[:120]}")
        if not ready_ok:
            failures.append("readyz")

        # -- 1c. Metrics registry live (endpoint liveness; counters
        #       materialize lazily, so presence is asserted after the POST) --
        r = await http.get("/metrics")
        metrics_ok = r.status_code == 200 and '"counters"' in r.text
        print(f"[{'PASS' if metrics_ok else 'FAIL'}] GET /metrics -> {r.status_code}")
        if not metrics_ok:
            failures.append("metrics")

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
        # Unique message id per run: a re-delivered message is honestly
        # deduplicated by the runtime (idempotency), so the probe must
        # present a fresh message to exercise the live path end to end.
        probe_message_id = f"wamid.PROBE{ULID()}"
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
                                        "id": probe_message_id,
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
        print(f"[{'PASS' if ok else 'FAIL'}] POST signed webhook -> {r.status_code} {r.text[:120]}")
        if not ok:
            failures.append("signed-post")

        # -- 3b. Metrics recorded the live message ------------------------
        r = await http.get("/metrics")
        recorded = "bridge_messages_started_total" in r.text
        print(f"[{'PASS' if recorded else 'FAIL'}] /metrics shows the processed message")
        if not recorded:
            failures.append("metrics-recording")

        # -- 3c. Event ledger + durable-waiting proof (live path) ---------
        # The accepted message must have announced
        # interface.message:<principal> on the runtime signal ledger.
        # Then: schedule an event-wake work item whose watermark predates
        # that signal, run the REAL runner once, and watch it wake and run.
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from wax.runtime.work import WorkRepository, WorkRunner, capability_handler
        from wax.state.engine import db_session
        from wax.state.work_models import RuntimeSignalRecord, WorkItemRecord

        async with db_session() as session:
            sig = (
                await session.execute(
                    select(RuntimeSignalRecord)
                    .where(RuntimeSignalRecord.name.like("interface.message:%"))
                    .order_by(RuntimeSignalRecord.emitted_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        ok = sig is not None and sig.emitted_by == "bridge"
        print(
            f"[{'PASS' if ok else 'FAIL'}] event ledger recorded "
            f"interface message signal (name={sig.name if sig else None})"
        )
        if not ok:
            failures.append("event-ledger")

        if ok:
            pid = sig.name.split(":", 1)[1]
            async with db_session() as session:
                item = await WorkRepository(session).schedule(
                    kind="capability",
                    payload={
                        "capability_name": "echo",
                        "inputs": {"message": "probe-woke"},
                    },
                    wake_at=datetime.now(UTC),
                    principal_id=pid,
                    max_attempts=1,
                    wake_kind="event",
                    wake_event=sig.name,
                )
                # The wait is inserted AFTER the fact for probe purposes;
                # move the watermark behind the signal so it can fire.
                item.wake_watermark = sig.emitted_at - timedelta(seconds=5)
                await session.commit()

            services = getattr(app.state, "services", None)
            runner = WorkRunner(
                services,
                poll_interval_seconds=0.05,
                lease_seconds=120.0,
                retry_backoff_seconds=0.0,
            )
            runner.register_handler("capability", capability_handler)
            ran = await runner.run_once()
            async with db_session() as session:
                final = await session.get(WorkItemRecord, item.id)
            ok = (
                ran == 1
                and final is not None
                and final.status == "succeeded"
                and final.result == {"echo": {"message": "probe-woke"}}
            )
            print(
                f"[{'PASS' if ok else 'FAIL'}] durable waiting: event-wake work "
                f"correlated against the ledger and ran (ran={ran}, "
                f"status={final.status if final else None})"
            )
            if not ok:
                failures.append("durable-waiting")

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
        print(f"[{'PASS' if ok else 'FAIL'}] POST bad signature -> {r.status_code} {r.text[:60]}")
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
