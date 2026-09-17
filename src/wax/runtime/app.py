"""WAX FastAPI application factory.

Stripped down for the open-world terminal architecture:
- No control plane / dashboard routes
- No /metrics endpoint
- No deleted-subsystem wiring

Routes: /healthz, /readyz, /migrations/status, /config/validate, /,
        /webhooks/whatsapp (GET + POST)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from typing import Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from wax import __version__
from wax.core.config import WaxSettings, load_settings
from wax.runtime.delivery import DeliveryPolicy
from wax.runtime.lifecycle import LifecycleManager
from wax.runtime.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(settings: WaxSettings | None = None) -> FastAPI:
    """Build and return the WAX FastAPI application."""
    if settings is None:
        settings = load_settings()

    configure_logging(settings)
    lifecycle = LifecycleManager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "wax.runtime.starting",
            version=__version__,
            env=settings.env.value,
            host=settings.host,
            port=settings.port,
        )

        lifecycle.install_signal_handlers()

        # Database engine
        from wax.state.engine import dispose_engine, init_engine

        init_engine(settings)
        lifecycle.on_shutdown("state.engine", dispose_engine())

        # Intelligence service
        from wax.intelligence.service import IntelligenceService

        intel = IntelligenceService.from_settings(settings)
        app.state.intelligence = intel
        lifecycle.on_shutdown("intelligence.close", intel.close())

        # RuntimeServices container
        from wax.runtime.services import RuntimeServices

        services = RuntimeServices.build(settings)
        app.state.services = services

        # RuntimeBridge
        from wax.runtime.bridge.service import RuntimeBridge

        bridge = RuntimeBridge(intelligence=intel, services=services)
        app.state.runtime_bridge = bridge
        services.reentry_callback = bridge.run_reentry
        services.resume_callback = bridge.resume_execution
        log.info("runtime.reentry_callback_registered")

        # WhatsApp client (optional)
        if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
            from wax.interfaces.whatsapp.client import WhatsAppClient

            wa_client = WhatsAppClient(
                access_token=settings.whatsapp_access_token,
                phone_number_id=settings.whatsapp_phone_number_id,
                app_secret=settings.whatsapp_app_secret,
                verify_token=settings.whatsapp_verify_token,
            )
            app.state.whatsapp_client = wa_client
            lifecycle.on_shutdown("whatsapp.close", wa_client.close())
            services.delivery.register(
                "whatsapp",
                wa_client.send_long_text,
                policy=DeliveryPolicy(
                    inbound_freshness_window=timedelta(hours=24),
                    freshness_note=(
                        "the 24-hour customer service window has closed and "
                        "Meta requires an approved template message; no "
                        "template is registered on this deployment. Ask the "
                        "user to message WAX first."
                    ),
                ),
            )
            log.info(
                "whatsapp.client.initialized", phone_number_id=settings.whatsapp_phone_number_id
            )
        else:
            app.state.whatsapp_client = None
            log.warning("whatsapp.client.not_configured")

        # Work runner (durable work + reentry)
        from wax.runtime.work import WorkRunner
        from wax.runtime.work.handlers import intelligence_handler

        work_runner = WorkRunner(
            services,
            poll_interval_seconds=settings.work_poll_interval_seconds,
        )
        work_runner.register_handler("intelligence", intelligence_handler)
        services.work_runner = work_runner
        recovered = await work_runner.recover_orphans()
        if recovered.get("failed_executions"):
            log.warning("work.startup_recovery", **recovered)
        work_runner.start()
        lifecycle.on_shutdown("work_runner", work_runner.stop())

        # Memory maintenance loop
        import asyncio

        from wax.memory.lifecycle import memory_maintenance_loop
        from wax.memory.lifecycle import stop_maintenance as stop_memory_maintenance

        memory_task = asyncio.create_task(
            memory_maintenance_loop(settings, interval_seconds=300.0),
            name="wax-memory-reaper",
        )
        lifecycle.on_shutdown("memory_reaper", stop_memory_maintenance(memory_task))

        # Runtime maintenance (delivery retries, conversation archival, audit retention)
        from wax.runtime.maintenance import maintenance_loop
        from wax.runtime.maintenance import stop_maintenance as stop_runtime_maintenance

        maintenance_task = asyncio.create_task(
            maintenance_loop(settings, interval_seconds=300.0, services=services),
            name="wax-maintenance",
        )
        lifecycle.on_shutdown("runtime_maintenance", stop_runtime_maintenance(maintenance_task))

        try:
            yield
        finally:
            log.info("wax.runtime.stopping")
            await lifecycle.run_shutdown()

    app = FastAPI(
        title="WAX Runtime",
        description=(
            "WAX is an AI runtime that provides intelligence with memory, a "
            "terminal environment, persistent execution, durable reentry, and "
            "communication. The intelligence determines how to accomplish work; "
            "the runtime provides the mechanisms."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    app.state.settings = settings
    app.state.lifecycle = lifecycle

    # --- Health endpoints ---

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            from sqlalchemy import text

            from wax.state.engine import db_session

            async with db_session() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"fail: {type(e).__name__}"

        intel = getattr(app.state, "intelligence", None)
        checks["intelligence"] = "ok" if intel is not None else "fail: not_initialized"

        whatsapp = getattr(app.state, "whatsapp_client", None)
        if settings.whatsapp_access_token:
            checks["whatsapp"] = "ok" if whatsapp is not None else "fail: not_initialized"
        else:
            checks["whatsapp"] = "not_configured"

        all_ok = all(v == "ok" or v == "not_configured" for v in checks.values())
        return JSONResponse(
            status_code=200 if all_ok else 503,
            content={
                "status": "ok" if all_ok else "not_ready",
                "checks": checks,
                "version": __version__,
            },
        )

    @app.get("/migrations/status", tags=["operations"])
    async def migrations_status() -> JSONResponse:
        try:
            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory
            from sqlalchemy import text

            from wax.state.engine import db_session

            async with db_session() as session:
                result = await session.execute(text("SELECT version_num FROM alembic_version"))
                row = result.fetchone()
                current = row[0] if row else None

            cfg = AlembicConfig("alembic.ini")
            script_dir = ScriptDirectory.from_config(cfg)
            head = script_dir.get_current_head()

            return JSONResponse(
                content={
                    "current_revision": current,
                    "head_revision": head,
                    "up_to_date": current == head,
                }
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"{type(e).__name__}: {e}"},
            )

    @app.get("/config/validate", tags=["operations"])
    async def validate_config() -> JSONResponse:
        config_summary = {
            "env": settings.env.value,
            "host": settings.host,
            "port": settings.port,
            "log_level": settings.log_level.value,
            "log_format": settings.log_format.value,
            "database_url_scheme": settings.database_url.split("://", 1)[0],
            "llm_provider": settings.llm_default_provider or "(mock)",
            "whatsapp_configured": bool(settings.whatsapp_access_token),
            "secret_key_set": bool(settings.secret_key)
            and settings.secret_key != "change-me-to-a-real-secret",
            "terminal_timeout_seconds": settings.terminal_timeout_seconds,
            "terminal_max_rounds": settings.terminal_max_rounds,
        }
        try:
            settings.enforce_production_hardening()
            config_summary["production_ready"] = settings.is_production
            config_summary["validation_passed"] = True
        except Exception as e:
            config_summary["production_ready"] = False
            config_summary["validation_passed"] = False
            config_summary["validation_error"] = str(e)
        return JSONResponse(content=config_summary)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, Any]:
        return {
            "name": "WAX",
            "version": __version__,
            "description": "AI runtime — intelligence + terminal + memory + execution.",
            "docs": "/docs" if settings.is_development else None,
        }

    # --- WhatsApp webhook endpoints ---

    @app.get("/webhooks/whatsapp", tags=["webhook"])
    async def whatsapp_verify(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> Response:
        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return JSONResponse(status_code=503, content={"error": "whatsapp_not_configured"})

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)
        result = adapter.verify_webhook_token(hub_mode, hub_verify_token, hub_challenge)
        if result is None:
            return JSONResponse(status_code=403, content={"error": "verification_failed"})
        return Response(content=result, media_type="text/plain")

    @app.post("/webhooks/whatsapp", tags=["webhook"])
    async def whatsapp_webhook(
        request: Request,
        x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
    ) -> dict[str, Any]:
        request_body = await request.body()

        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return {"status": "whatsapp_not_configured"}

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)

        async def _runtime_callback(message):  # type: ignore[no-untyped-def]
            from datetime import datetime

            from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
            from wax.state.engine import db_session

            bridge = getattr(app.state, "runtime_bridge", None)
            if bridge is None:
                log.error("whatsapp.bridge_not_configured")
                return None

            runtime_request = RuntimeRequest(
                interface_message_id=message.message_id,
                interface_kind=InterfaceKind.WHATSAPP,
                sender_interface_id=message.from_phone,
                sender_display_name=message.from_name,
                text=message.effective_text,
                received_at=message.timestamp or datetime.now(UTC),
            )

            async with db_session() as session:
                response = await bridge.process(session, runtime_request)

            if response.status.value == "success" and response.text:
                return response.text
            if response.status.value == "duplicate":
                log.info(
                    "whatsapp.duplicate_message.skipped",
                    message_id=message.message_id,
                )
            else:
                log.warning(
                    "whatsapp.message.not_replied",
                    message_id=message.message_id,
                    status=response.status.value,
                    error=response.error,
                )
            return None

        async def _on_send_failure(message, response_text: str, error: Exception) -> None:
            from sqlalchemy import select

            from wax.runtime.delivery_queue import DeliveryQueue
            from wax.state.engine import db_session
            from wax.state.identity_models import PrincipalCredential

            svc = getattr(app.state, "services", None)
            try:
                async with db_session() as session:
                    credential = (
                        await session.execute(
                            select(PrincipalCredential).where(
                                PrincipalCredential.kind == "whatsapp_phone",
                                PrincipalCredential.value == message.from_phone,
                            )
                        )
                    ).scalar_one_or_none()
                    if credential is None:
                        log.critical(
                            "whatsapp.send_failure.unresolvable_principal",
                            message_id=message.message_id,
                            to=message.from_phone,
                        )
                        return

                    queue = DeliveryQueue(
                        session,
                        svc,
                        retry_backoff_seconds=float(settings.delivery_retry_backoff_seconds),
                        max_age_seconds=float(settings.delivery_max_age_seconds),
                    )
                    record = await queue.enqueue(
                        principal_id=credential.principal_id,
                        interface_kind="whatsapp",
                        recipient_id=message.from_phone,
                        text=response_text,
                        source="bridge_reply",
                        max_attempts=int(settings.delivery_max_attempts),
                    )
                    record.attempts = 1
                    record.last_error = f"{type(error).__name__}: {error}"[:2000]
                    if record.attempts >= record.max_attempts:
                        record.status = "failed"
                    else:
                        from datetime import UTC, datetime, timedelta

                        record.next_attempt_at = datetime.now(UTC) + timedelta(
                            seconds=float(settings.delivery_retry_backoff_seconds)
                        )
                    await session.commit()
            except Exception as delivery_error:
                log.critical(
                    "whatsapp.delivery_record_write_failed",
                    error=str(delivery_error),
                    message_id=message.message_id,
                )

        return await adapter.handle_webhook(
            raw_body=request_body,
            signature_header=x_hub_signature_256,
            runtime_callback=_runtime_callback,
            on_send_failure=_on_send_failure,
        )

    return app
