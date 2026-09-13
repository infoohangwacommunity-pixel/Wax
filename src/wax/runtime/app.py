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

from fastapi import FastAPI
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

        # TODO Phase O: initialize LLM provider adapters here
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

        # TODO Phase O: check at least one LLM provider is configured
        # TODO Phase G: check capability registry is populated

        all_ok = all(v == "ok" for v in checks.values())
        status_code = 200 if all_ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if all_ok else "not_ready",
                "checks": checks,
                "version": __version__,
            },
        )

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
        mode: str | None = None,
        token: str | None = None,
        challenge: str | None = None,
    ) -> JSONResponse:
        """Meta's webhook verification endpoint.

        Meta sends a GET request with hub.mode, hub.verify_token, hub.challenge.
        If the token matches, we echo back the challenge.
        """
        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return JSONResponse(status_code=503, content={"error": "whatsapp_not_configured"})

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)
        result = adapter.verify_webhook_token(mode, token, challenge)
        if result is None:
            return JSONResponse(status_code=403, content={"error": "verification_failed"})
        # Meta expects the challenge echoed back as plain text body, not JSON
        from fastapi import Response

        return Response(content=result, media_type="text/plain")

    @app.post("/webhooks/whatsapp", tags=["webhook"])
    async def whatsapp_webhook(
        request_body: bytes,
        x_hub_signature_256: str = "",
    ) -> dict[str, Any]:
        """Receive WhatsApp webhook events.

        Security: signature verified via HMAC-SHA256 of the raw body using
        the WhatsApp app secret.
        """
        whatsapp_client = getattr(app.state, "whatsapp_client", None)
        if whatsapp_client is None:
            return {"status": "whatsapp_not_configured"}

        from wax.interfaces.whatsapp.adapter import WhatsAppAdapter

        adapter = WhatsAppAdapter(client=whatsapp_client)

        async def _runtime_callback(message):  # type: ignore[no-untyped-def]
            """Called for each incoming WhatsApp message.

            For now, just log. Phase R (Runtime Composition) will wire this
            to Objective + Intelligence + Execution.
            """
            log.info(
                "whatsapp.runtime_callback.invoked",
                from_phone=message.from_phone,
                message_type=message.type.value,
                text=message.effective_text[:200],
            )
            # Placeholder: echo back what we received
            return f"WAX received your {message.type.value} message. (Runtime composition pending Phase R.)"

        return await adapter.handle_webhook(
            raw_body=request_body,
            signature_header=x_hub_signature_256,
            runtime_callback=_runtime_callback,
        )

    return app
