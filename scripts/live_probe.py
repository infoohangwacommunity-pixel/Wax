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
    """Minimal settings: WhatsApp configured so the client initializes.
    Signal retention is set tiny so the live pruning proof can run in
    one pass; the approval expiry is the real default. The database is a
    FRESH file per run so probe checks (exactly-once counts, ledger
    retention) are never masked by a previous run's state."""
    import tempfile

    db_path = os.path.join(tempfile.mkdtemp(prefix="wax-probe-"), "probe.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    # migrations/env.py rebuilds WaxSettings from the environment when the
    # alembic chain runs — publish the probe's DB through the same channel
    # so the migrations and the app use ONE database.
    os.environ["WAX_DATABASE_URL"] = db_url
    return WaxSettings.model_validate(
        {
            "database_url": db_url,
            "whatsapp_access_token": "probe-token",
            "whatsapp_phone_number_id": "123456789",
            "whatsapp_app_secret": APP_SECRET,
            "whatsapp_verify_token": VERIFY_TOKEN,
            "signal_retention_seconds": 0.001,
        }
    )


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def probe() -> int:
    settings = build_settings()

    # The probe drives the MOCK provider with a deterministic script: the
    # intelligence is the variable under test's neighbor, not the subject;
    # the RUNTIME path (webhook -> identity -> gates -> approval -> work)
    # is what must be proven live. The scripted model requests a
    # destructive capability so the live approval flow can be observed.
    from wax.intelligence.adapters.mock_provider import MockLLMProvider
    from wax.intelligence.contracts import ToolCall
    from wax.intelligence.service import IntelligenceService

    wipe_call_1 = [ToolCall(id="call_1", name="test.wipe", arguments={"target": "probe-data"})]
    wipe_call_2 = [ToolCall(id="call_2", name="test.wipe", arguments={"target": "probe-data"})]
    scripted = MockLLMProvider(scripted_tool_calls=[wipe_call_1, [], wipe_call_2])
    _original_from_settings = IntelligenceService.from_settings

    def _scripted_from_settings(cfg):
        service = _original_from_settings(cfg)
        service._provider = scripted  # noqa: SLF001 — probe seam
        return service

    IntelligenceService.from_settings = staticmethod(_scripted_from_settings)

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

    # Register the destructive test capability the scripted model will
    # request (the runtime under probe decides what needs approval).
    from wax.capabilities.contracts import CapabilityDescriptor

    async def _probe_wipe(inputs, ctx=None):
        return {"wiped": inputs.get("target", "")}

    def _register_probe_capability(app):
        services = getattr(app.state, "services", None)
        if services is not None and "test.wipe" not in services.capability_registry:
            services.capability_registry.register(
                CapabilityDescriptor(
                    name="test.wipe",
                    description="Probe destructive capability",
                    required_permission="capability.invoke:built_in",
                    is_destructive=True,
                    timeout_seconds=5.0,
                ),
                _probe_wipe,
            )

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
    _register_probe_capability(app)

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

        # -- 3d. Human-approval flow (live authority boundary) ------------
        # The scripted model requested the DESTRUCTIVE test.wipe during
        # step 3. The runtime must have created a pending approval instead
        # of executing. Then the human approves through the interface
        # credential path ('/approve <id>'), and a second scripted request
        # executes exactly once.
        from wax.state.approval_models import PendingApprovalRecord

        async with db_session() as session:
            approval = (
                await session.execute(
                    select(PendingApprovalRecord)
                    .where(PendingApprovalRecord.capability_name == "test.wipe")
                    .order_by(PendingApprovalRecord.requested_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        ok = approval is not None and approval.status == "pending"
        print(
            f"[{'PASS' if ok else 'FAIL'}] approval primitive: destructive "
            f"request created pending approval "
            f"(id={approval.id if approval else None}, "
            f"status={approval.status if approval else None})"
        )
        if not ok:
            failures.append("approval-pending")

        if ok:
            # The human decides through the SAME interface credential —
            # a generic '/approve <id>' message; the runtime routes it to
            # the human authority path, never to the intelligence.
            approve_id = f"wamid.PROBEAPPROVE{ULID()}"
            approve_payload = {
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
                                            "id": approve_id,
                                            "timestamp": "1726000000",
                                            "text": {"body": f"/approve {approval.id}"},
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
            approve_body = httpx.Response(200, json=approve_payload).content
            r = await http.post(
                "/webhooks/whatsapp",
                content=approve_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sign(approve_body),
                },
            )
            await asyncio.sleep(0.2)
            async with db_session() as session:
                decided = await session.get(PendingApprovalRecord, approval.id)
            ok = r.status_code == 200 and decided.status == "approved"
            print(
                f"[{'PASS' if ok else 'FAIL'}] human approval decision over live "
                f"webhook ('/approve <id>') -> status={decided.status if decided else None}"
            )
            if not ok:
                failures.append("approval-decision")

            # The AI retries the same destructive request: the consumed
            # approval authorizes EXACTLY ONE execution.
            retry_id = f"wamid.PROBERETRY{ULID()}"
            retry_payload = {
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
                                            "id": retry_id,
                                            "timestamp": "1726000000",
                                            "text": {"body": "wipe it now"},
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
            retry_body = httpx.Response(200, json=retry_payload).content
            r = await http.post(
                "/webhooks/whatsapp",
                content=retry_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sign(retry_body),
                },
            )
            await asyncio.sleep(0.2)
            from sqlalchemy import text as _text

            from wax.state.execution_models import ExecutionStepRecord

            async with db_session() as session:
                steps = list(
                    (
                        await session.execute(
                            select(ExecutionStepRecord)
                            .where(
                                ExecutionStepRecord.capability_name == "test.wipe",
                                ExecutionStepRecord.status == "succeeded",
                            )
                        )
                    ).scalars()
                )
            ok = r.status_code == 200 and len(steps) == 1
            print(
                f"[{'PASS' if ok else 'FAIL'}] approved destructive action ran "
                f"exactly once over the live path (succeeded_wipe_steps={len(steps)})"
            )
            if not ok:
                failures.append("approval-execution")

        # -- 3e. Signal-ledger retention (live maintenance) ----------------
        # Retention is set to ~0 in probe settings: one maintenance pass
        # must prune every signal no pending waiter still needs.
        from wax.runtime.maintenance import run_maintenance_pass

        results = await run_maintenance_pass(settings)
        async with db_session() as session:
            remaining = len(
                (
                    await session.execute(select(RuntimeSignalRecord))
                ).scalars().all()
            )
        ok = remaining == 0 and results["signals_pruned"] > 0
        print(
            f"[{'PASS' if ok else 'FAIL'}] signal-ledger retention pruned "
            f"live (pruned={results['signals_pruned']}, remaining={remaining})"
        )
        if not ok:
            failures.append("ledger-retention")

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
