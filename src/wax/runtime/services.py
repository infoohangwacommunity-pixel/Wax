"""RuntimeServices — the process-wide service container.

The forensic audit (Section 33) located the boundary of the wired system at
a single comment in the lifespan: "TODO Phase G: initialize capability
registry here". Everything downstream — capabilities, authority, agency,
resource budgets, security enforcement, metrics — existed as tested code but
was never constructed by the running application.

This container is that missing construction point. It builds the process
singletons ONCE at startup and hands them to the bridge, the webhook layer,
and the background worker. Per-session collaborators (AuthorizationService,
AgencyService, CapabilityInvoker) are constructed on demand with a session.

Nothing here knows about domains, education, WhatsApp, or any use case —
these are operating-system-style mechanisms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from wax.agency.service import AgencyService
from wax.authority.service import AuthorizationService
from wax.capabilities.built_ins import register_builtins
from wax.capabilities.invoker import CapabilityInvoker
from wax.capabilities.registry import CapabilityRegistry
from wax.capabilities.runtime_capabilities import register_runtime_capabilities
from wax.core.config import WaxSettings
from wax.observability.runtime_metrics import RuntimeMetrics, get_runtime_metrics
from wax.resources.accountant import ResourceAccountant
from wax.runtime.connectors.service import ConnectorRuntime
from wax.runtime.delivery import DeliveryRouter
from wax.runtime.environment.planner import EnvironmentPlanner
from wax.runtime.logging import get_logger
from wax.runtime.vault.service import CredentialVault
from wax.security.abuse import AbuseDetector
from wax.security.cost_protection import CostProtector
from wax.security.input_sanitizer import InputSanitizer
from wax.security.rate_limiter import RateLimiter

if TYPE_CHECKING:
    # Avoid a circular import at runtime: the work package's __init__
    # imports handlers, which import services. Importing the type only
    # under TYPE_CHECKING keeps the type hint without triggering the
    # package init at module load time.
    from wax.runtime.work.reentry import ReentryCallback

log = get_logger(__name__)


@dataclass
class RuntimeServices:
    """Process-wide singletons shared by every runtime subsystem."""

    settings: WaxSettings
    metrics: RuntimeMetrics
    rate_limiter: RateLimiter
    cost_protector: CostProtector
    abuse_detector: AbuseDetector
    input_sanitizer: InputSanitizer
    resource_accountant: ResourceAccountant
    capability_registry: CapabilityRegistry
    delivery: DeliveryRouter
    # Mutable slot for the background work runner, set by the lifespan
    # (Phase V). Typed loosely to avoid an import cycle; tests may inspect.
    work_runner: Any | None = field(default=None)
    # ADR-0034: durable intelligence re-entry callback. Set ONCE by the
    # composition root (create_app) so the work handler can wake the
    # intelligence WITHOUT importing the bridge. None means the runtime
    # was built without re-entry wiring (e.g. a stripped-down test
    # container) — the intelligence_handler fails honestly in that case.
    reentry_callback: ReentryCallback | None = field(default=None)
    # ADR-0038 (Phase 5): environment planner. Resolves an
    # EnvironmentRequirement into a concrete plan + lease. The
    # intelligence calls the `environment.request` capability, which
    # delegates to this planner.
    environment_planner: EnvironmentPlanner | None = field(default=None)
    # ADR-0040 (Phase 7): credential vault. Provider-neutral secret
    # storage. The vault NEVER exposes secrets to the model — only
    # opaque connection_ids and grant handles.
    credential_vault: CredentialVault | None = field(default=None)
    # ADR-0041 (Phase 8): connector runtime. Resource-type-based
    # discovery + resolution. The intelligence discovers services
    # (GitHub, GitLab, npm, PyPI, Railway, etc.) through the
    # environment — no architectural change when a new platform appears.
    connector_runtime: ConnectorRuntime | None = field(default=None)

    @classmethod
    def build(cls, settings: WaxSettings | None) -> RuntimeServices:
        """Construct all singletons. Pure in-memory — safe to call in tests.

        `settings=None` builds a container with development defaults; it is
        used when a legacy caller (or a bare unit test) constructs a bridge
        without an application lifespan.
        """
        if settings is None:
            settings = WaxSettings.model_validate({})

        registry = CapabilityRegistry()
        register_builtins(registry)

        services = cls(
            settings=settings,
            metrics=get_runtime_metrics(),
            rate_limiter=RateLimiter(),
            cost_protector=CostProtector(),
            abuse_detector=AbuseDetector(),
            input_sanitizer=InputSanitizer(),
            resource_accountant=ResourceAccountant(),
            capability_registry=registry,
            delivery=DeliveryRouter(),
            environment_planner=EnvironmentPlanner(settings),
            credential_vault=CredentialVault(settings),
            connector_runtime=ConnectorRuntime(settings),
        )
        # Runtime mechanisms exposed to the AI as capabilities
        # (work.schedule / work.cancel / work.list / message.send) —
        # registered after construction so they close over this container.
        register_runtime_capabilities(registry, services)
        log.info(
            "runtime.services.built",
            capabilities=len(registry),
            delivery_interfaces=services.delivery.registered_interfaces(),
        )
        return services

    # --- Per-session collaborators ---------------------------------------
    # These need an AsyncSession, so they are built at use time.

    def authority(self, session: AsyncSession) -> AuthorizationService:
        return AuthorizationService(session)

    def agency(self, session: AsyncSession) -> AgencyService:
        return AgencyService(session, AuthorizationService(session))

    def invoker(self, session: AsyncSession) -> CapabilityInvoker:
        """The SOLE enforcement point for AI-requested effects (INV-04).
        CV-19: the idempotency claim lease is operator-tunable."""
        return CapabilityInvoker(
            self.capability_registry,
            AuthorizationService(session),
            idempotency_claim_seconds=self.settings.capability_idempotency_claim_seconds,
        )


def services_from_app(app: Any) -> RuntimeServices | None:
    """Read the container off a FastAPI app instance, if present."""
    return getattr(app.state, "services", None)
