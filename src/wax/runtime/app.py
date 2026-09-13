"""WAX FastAPI application factory.

This module builds the FastAPI app, wires up middleware, routes, lifecycle,
and the health endpoint. It is the entrypoint for the HTTP server.

Architectural rules:
- The FastAPI app is an *adapter* into WAX, not WAX itself. The runtime
  boundary lives below the app — app handlers call into `wax.runtime`
  services, which call into `wax.core` contracts.
- The HTTP surface is one of potentially many interfaces (WhatsApp, web,
  Telegram, ...). Nothing in `wax.core` may know about HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from wax import __version__
from wax.core.config import WaxSettings, load_settings
from wax.runtime.lifecycle import LifecycleManager
from wax.runtime.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(settings: WaxSettings | None = None) -> FastAPI:
    """Build and return the WAX FastAPI application.

    Args:
        settings: optional pre-loaded settings. If omitted, loads from env.
    """
    if settings is None:
        settings = load_settings()

    # Configure logging as early as possible.
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

        # Initialize database engine (Phase C)
        from wax.state.engine import dispose_engine, init_engine

        init_engine(settings)
        lifecycle.on_shutdown("state.engine", dispose_engine())

        # Initialize IntelligenceService (Phase K)
        from wax.intelligence.service import IntelligenceService

        intel = IntelligenceService.from_settings(settings)
        app.state.intelligence = intel
        lifecycle.on_shutdown("intelligence.close", intel.close())

        # Initialize RuntimeBridge (Phase R)
        from wax.runtime.bridge.service import RuntimeBridge

        bridge = RuntimeBridge(intelligence=intel)
        app.state.runtime_bridge = bridge

        # Initialize WhatsApp client if credentials are present (Phase Q)
        if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
            from wax.interfaces.whatsapp.client import WhatsAppClient

            wa_client = WhatsAppClient(
                access_token=settings.whatsapp_access_token,
                phone_number_id=settings.whatsapp_phone_number_id,
                app_secret=settings.whatsapp_app_secret or "unset",
                verify_token=settings.whatsapp_verify_token,
            )
            app.state.whatsapp_client = wa_client
            lifecycle.on_shutdown("whatsapp.close", wa_client.close())
            log.info(
                "whatsapp.client.initialized", phone_number_id=settings.whatsapp_phone_number_id
            )
        else:
            app.state.whatsapp_client = None
            log.warning("whatsapp.client.not_configured")

        # TODO Phase G: initialize capability registry here

        try:
            yield
        finally:
            log.info("wax.runtime.stopping")
            await lifecycle.run_shutdown()

    app = FastAPI(
        title="WAX Runtime",
        description=(
            "WAX is an AI-native environment/runtime in which intelligence can "
            "safely, persistently, and autonomously pursue legitimate human "
            "objectives using available capabilities and resources."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    # Stash the lifecycle + settings on app.state for handlers/tests.
    app.state.settings = settings
    app.state.lifecycle = lifecycle

    # -------------------------------------------------------------------
    # Health endpoints
    # -------------------------------------------------------------------
    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, Any]:
        """Liveness probe. Returns 200 if the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        """Readiness probe. Returns 200 only if all dependencies are ready."""
        checks: dict[str, str] = {}

        # Phase C: check DB connectivity
        try:
            from sqlalchemy import text

            from wax.state.engine import db_session

            async with db_session() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"fail: {type(e).__name__}"

        # Phase K: check LLM provider is configured
        intel = getattr(app.state, "intelligence", None)
        if intel is not None:
            checks["intelligence"] = "ok"
        else:
            checks["intelligence"] = "fail: not_initialized"

        # Phase Q: check WhatsApp client (optional — may be None in dev)
        whatsapp = getattr(app.state, "whatsapp_client", None)
        if settings.whatsapp_access_token:
            checks["whatsapp"] = "ok" if whatsapp is not None else "fail: not_initialized"
        else:
            checks["whatsapp"] = "not_configured"

        all_ok = all(v == "ok" or v == "not_configured" for v in checks.values())
        status_code = 200 if all_ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if all_ok else "not_ready",
                "checks": checks,
                "version": __version__,
            },
        )

    @app.get("/metrics", tags=["observability"])
    async def metrics() -> JSONResponse:
        """Prometheus-compatible metrics endpoint.

        Returns the global MetricsRegistry snapshot. In production, a
        Prometheus scraper polls this endpoint every 15-60 seconds.
        """
        from wax.observability.metrics import get_metrics

        snapshot = get_metrics().snapshot()
        return JSONResponse(content=snapshot)

    @app.get("/migrations/status", tags=["operations"])
    async def migrations_status() -> JSONResponse:
        """Check whether the database schema is up to date.

        Returns the current migration revision + whether the schema matches
        the latest migration. Useful for deployment validation.
        """
        try:
            from alembic.config import Config as AlembicConfig
            from alembic.runtime.migration import MigrationContext
            from alembic.script import ScriptDirectory
            from sqlalchemy import text

            from wax.state.engine import db_session

            # Get current revision from DB
            async with db_session() as session:
                result = await session.execute(text("SELECT version_num FROM alembic_version"))
                row = result.fetchone()
                current = row[0] if row else None

            # Get head revision from migrations dir
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
        """Validate the current configuration.

        Returns the configuration (with secrets redacted) + whether it
        passes production hardening checks.
        """
        from wax.core.config import WaxSettings

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
        """Root metadata endpoint."""
        return {
            "name": "WAX",
            "version": __version__,
            "description": "AI-native environment/runtime.",
            "docs": "/docs" if settings.is_development else None,
            "constitution": (
                "WAX is infrastructure, not intelligence. The AI is the intelligence; "
                "WAX is the environment."
            ),
        }

    # -------------------------------------------------------------------
    # WhatsApp webhook endpoints (Phase Q)
    # -------------------------------------------------------------------
    @app.get("/webhooks/whatsapp", tags=["webhook"])
    async def whatsapp_verify(
        hub_mode: str | None = Query(default=None, alias="hub.mode"),
        hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> Response:
        """Meta's webhook verification endpoint.

        Meta sends: GET /webhooks/whatsapp?hub.mode=subscribe
        &hub.verify_token=...&hub.challenge=...

        The parameter names MUST match Meta's wire format exactly
        (hub.mode, hub.verify_token, hub.challenge) — FastAPI binds query
        parameters by alias. If the token matches, we echo back the
        challenge as the plain-text response body.
        """
        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return JSONResponse(status_code=503, content={"error": "whatsapp_not_configured"})

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)
        result = adapter.verify_webhook_token(hub_mode, hub_verify_token, hub_challenge)
        if result is None:
            return JSONResponse(status_code=403, content={"error": "verification_failed"})
        # Meta expects the challenge echoed back as plain text body, not JSON
        return Response(content=result, media_type="text/plain")

    @app.post("/webhooks/whatsapp", tags=["webhook"])
    async def whatsapp_webhook(
        request: Request,
        x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
    ) -> dict[str, Any]:
        """Receive WhatsApp webhook events.

        Binding notes (both defects were verified live by the forensic
        audit and MUST NOT regress):
        - The raw body is read via `request.body()` — declaring a plain
          `bytes` parameter makes FastAPI treat it as a query parameter,
          which 422-rejects every genuine Meta POST before our code runs.
        - The signature arrives in the X-Hub-Signature-256 HEADER, bound
          via Header(alias=...), not as a query parameter.

        Security: signature verified via HMAC-SHA256 of the raw body using
        the WhatsApp app secret.
        """
        request_body = await request.body()

        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return {"status": "whatsapp_not_configured"}

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)

        async def _runtime_callback(message):  # type: ignore[no-untyped-def]
            """Convert the WhatsApp message → RuntimeRequest → process via bridge.

            This is the SOLE place where WhatsApp shapes become RuntimeRequest.
            """
            from datetime import datetime, timezone

            from wax.runtime.bridge.contracts import (
                InterfaceKind,
                RuntimeRequest,
            )
            from wax.state.engine import db_session

            bridge = getattr(app.state, "runtime_bridge", None)
            if bridge is None:
                log.error("whatsapp.bridge_not_configured")
                return None

            request = RuntimeRequest(
                interface_message_id=message.message_id,
                interface_kind=InterfaceKind.WHATSAPP,
                sender_interface_id=message.from_phone,
                sender_display_name=message.from_name,
                text=message.effective_text,
                received_at=message.timestamp or datetime.now(timezone.utc),
            )

            async with db_session() as session:
                response = await bridge.process(session, request)

            # Send the response back via WhatsApp (if successful and not duplicate)
            if response.status.value == "success" and response.text:
                try:
                    await whatsapp_client.send_text(message.from_phone, response.text)
                except Exception as e:
                    log.error(
                        "whatsapp.response.send_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        to=message.from_phone,
                    )
            elif response.status.value == "duplicate":
                log.info(
                    "whatsapp.duplicate_message.skipped",
                    message_id=message.message_id,
                )
            # On error, do NOT send anything back (avoid noise during outages)
            return None  # bridge handles sending; callback returns None

        return await adapter.handle_webhook(
            raw_body=request_body,
            signature_header=x_hub_signature_256,
            runtime_callback=_runtime_callback,
        )

    return app
