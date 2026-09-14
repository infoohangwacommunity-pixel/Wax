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

        # RuntimeServices container (Phase G — the TODO that marked the edge
        # of the wired system in the forensic audit is now real wiring).
        # Built once in create_app(); reused here.
        services = getattr(app.state, "services", None)
        if services is None:
            from wax.runtime.services import RuntimeServices

            services = RuntimeServices.build(settings)
            app.state.services = services

        # Seed built-in roles (idempotent) so authorization has truth to
        # enforce from the first message onward.
        from wax.authority.seed import seed_builtin_roles
        from wax.state.engine import db_session

        async with db_session() as session:
            await seed_builtin_roles(session)
            await session.commit()

        # Initialize RuntimeBridge (Phase R) with the service container
        from wax.runtime.bridge.service import RuntimeBridge

        bridge = RuntimeBridge(intelligence=intel, services=services)
        app.state.runtime_bridge = bridge

        # ADR-0034: register the bridge's re-entry callback on
        # RuntimeServices. The durable-work `intelligence_handler` calls
        # this callback to wake the intelligence on a runtime fact
        # (time or signal) — WITHOUT importing the bridge. The
        # composition root (this lifespan) is the SOLE place where
        # the bridge and the work handler are wired together; the
        # architecture boundary tests prohibit either from importing
        # the other.
        services.reentry_callback = bridge.run_reentry
        log.info("runtime.reentry_callback_registered")

        # Initialize WhatsApp client if credentials are present (Phase Q)
        if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
            from wax.interfaces.whatsapp.client import WhatsAppClient

            wa_client = WhatsAppClient(
                access_token=settings.whatsapp_access_token,
                phone_number_id=settings.whatsapp_phone_number_id,
                # CV-17 fix: no "unset" sentinel fallback. An HMAC key of
                # "unset" makes webhook signatures forgeable (identity
                # seeding under misconfiguration). The client fail-fasts
                # on an empty secret; misconfiguration must stop startup,
                # not silently weaken the identity boundary.
                app_secret=settings.whatsapp_app_secret,
                verify_token=settings.whatsapp_verify_token,
            )
            app.state.whatsapp_client = wa_client
            lifecycle.on_shutdown("whatsapp.close", wa_client.close())
            # Register the WhatsApp sender with the runtime's delivery router
            # (Phase W: interfaces attach to the runtime, never own it).
            # The 24-hour customer-service window is META's policy, so it is
            # DECLARED HERE — at the WhatsApp adapter wiring point — not in
            # the runtime capability layer. The runtime enforces whatever
            # the attached interface declares, generically.
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

        # (The old "TODO Phase G" comment — the audit's marker of where the
        # wired system ended — is obsolete: RuntimeServices built the
        # capability registry in create_app, roles are seeded above, and the
        # registry is reachable through services.invoker(session).)

        # Phase R/V: the background work runtime — durable work that
        # survives restarts. In-process asyncio worker (single-process
        # deployment today; the lease design admits replicas later).
        # ADR-0034: TWO handlers are registered — "capability" (the
        # universal wake-and-invoke path) and "intelligence" (the
        # durable re-entry path that wakes the LLM + tool-call loop).
        from wax.runtime.work import WorkRunner, capability_handler
        from wax.runtime.work.handlers import intelligence_handler

        work_runner = WorkRunner(
            services,
            poll_interval_seconds=settings.work_poll_interval_seconds,
        )
        work_runner.register_handler("capability", capability_handler)
        work_runner.register_handler("intelligence", intelligence_handler)
        services.work_runner = work_runner
        recovered = await work_runner.recover_orphans()
        if recovered["failed_executions"]:
            log.warning("work.startup_recovery", **recovered)
        work_runner.start()
        lifecycle.on_shutdown("work_runner", work_runner.stop())

        # Phase S: the provisioning TTL reaper (resources never outlive
        # their TTL unless intentionally promoted).
        import asyncio

        from wax.runtime.provisioning import maintenance_loop, stop_maintenance

        provisioning_task = asyncio.create_task(
            maintenance_loop(settings, interval_seconds=60.0),
            name="wax-provisioning-reaper",
        )
        lifecycle.on_shutdown("provisioning_reaper", stop_maintenance(provisioning_task))

        # Memory lifecycle: expired memories are forgotten by the runtime
        # (retention is a mechanism, never an AI chore).
        from wax.memory.lifecycle import memory_maintenance_loop
        from wax.memory.lifecycle import stop_maintenance as stop_memory_maintenance

        memory_task = asyncio.create_task(
            memory_maintenance_loop(settings, interval_seconds=300.0),
            name="wax-memory-reaper",
        )
        lifecycle.on_shutdown("memory_reaper", stop_memory_maintenance(memory_task))

        # Runtime maintenance: approval expiry + signal-ledger retention
        # + delivery retries (lifecycle hygiene the runtime owns;
        # ADR-0013/0015/0021). The delivery sweep needs the LIVE services
        # container — the interface senders are registered on it.
        from wax.runtime.maintenance import maintenance_loop
        from wax.runtime.maintenance import stop_maintenance as stop_runtime_maintenance

        maintenance_task = asyncio.create_task(
            maintenance_loop(
                settings, interval_seconds=300.0, services=app.state.services
            ),
            name="wax-maintenance",
        )
        lifecycle.on_shutdown(
            "runtime_maintenance", stop_runtime_maintenance(maintenance_task)
        )

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
            Returns the response TEXT on success; the adapter performs the
            actual send (its send branch is live code, as its contract
            documents — the audit found it dead because this callback used
            to send and return None).
            """
            from datetime import datetime

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
                received_at=message.timestamp or datetime.now(UTC),
            )

            # Approval decisions are authority events, not conversation:
            # if the human's message matches the generic decision grammar
            # ('/approve <id>' / '/deny <id>'), it is routed to the human
            # authority path and never reaches the intelligence. Any
            # interface adapter can adopt the same one-line pattern.
            from wax.runtime.bridge.contracts import RuntimeResponse as _RR

            decision_response: _RR | None = None
            async with db_session() as session:
                decision_response = await bridge.match_approval_command(session, request)
            if decision_response is not None:
                return decision_response.text

            async with db_session() as session:
                response = await bridge.process(session, request)

            if response.status.value == "success" and response.text:
                return response.text
            if response.status.value == "duplicate":
                log.info(
                    "whatsapp.duplicate_message.skipped",
                    message_id=message.message_id,
                )
            else:
                # Non-success statuses are logged, never echoed to the user
                # (deliberate: avoid noisy auto-replies during incidents).
                log.warning(
                    "whatsapp.message.not_replied",
                    message_id=message.message_id,
                    status=response.status.value,
                    error=response.error,
                )
            return None

        async def _on_send_failure(message, response_text: str, error: Exception) -> None:
            """A completed reply that could not be delivered becomes
            RECOVERABLE STATE, not a graveyard row (ADR-0021, mission
            §55): a delivery record (pending, one attempt spent) the
            maintenance loop retries until delivered, exhausted, or past
            the deliverability horizon. The execution result is separate
            from the delivery result — the reply text is preserved
            verbatim on the record.
            """
            from sqlalchemy import select

            from wax.runtime.delivery_queue import DeliveryQueue
            from wax.state.engine import db_session
            from wax.state.identity_models import PrincipalCredential

            svc = getattr(app.state, "services", None)
            if svc is not None:
                svc.metrics.send_failure("whatsapp")
            try:
                async with db_session() as session:
                    # Resolve the principal from the verified credential
                    # (the recipient is always an existing principal's
                    # interface identity — the conversation just happened).
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
                        retry_backoff_seconds=float(
                            settings.delivery_retry_backoff_seconds
                        ),
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
                    # One attempt already happened (the adapter's send) —
                    # record it so the backoff chain starts honestly.
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
                    if svc is not None:
                        svc.metrics.delivery_retrying("whatsapp")
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
