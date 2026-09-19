"""RuntimeServices — the process-wide service container.

This is the construction point for process singletons. It builds them
ONCE at startup and hands them to the bridge and the work runner.

The new architecture is dramatically smaller than the old one. There is:
- No capability registry
- No authority broker
- No resource accountant
- No isolation runtime
- No environment planner
- No connector runtime
- No credential vault
- No blob store
- No provisioning service
- No cost protector / dollar budget

Just: intelligence, delivery, work runner, terminal config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wax.core.config import WaxSettings
from wax.runtime.delivery import DeliveryRouter
from wax.runtime.logging import get_logger

if TYPE_CHECKING:
    from wax.intelligence.service import IntelligenceService
    from wax.runtime.work.reentry import ReentryCallback

log = get_logger(__name__)


@dataclass
class RuntimeServices:
    """Process-wide singletons shared by every runtime subsystem.

    ``intelligence`` is a REQUIRED dependency. Every component that
    needs intelligence (the bridge, memory consolidation, maintenance)
    MUST receive it explicitly — never via ``hasattr``/``getattr``
    probing. If intelligence is unavailable, the runtime fails closed
    at startup rather than silently passing ``None`` to a downstream
    caller that will crash with ``'NoneType' object has no attribute
    'complete'``.
    """

    settings: WaxSettings
    delivery: DeliveryRouter
    intelligence: IntelligenceService | None = None
    # Mutable slot for the background work runner, set by the lifespan.
    work_runner: Any | None = field(default=None)
    # Durable intelligence re-entry callback. Set ONCE by the composition
    # root so the work handler can wake the intelligence WITHOUT importing
    # the bridge.
    reentry_callback: ReentryCallback | None = field(default=None)
    # Execution recovery callback (Part 22). Set ONCE by the composition
    # root so the work runner can resume interrupted executions WITHOUT
    # importing the bridge. Called with an execution_id; returns the final
    # response text or None.
    resume_callback: Any | None = field(default=None)
    # Inbound message processing callback. Set ONCE by the composition
    # root so the work runner's `inbound_handler` can process a freshly-
    # accepted message end-to-end WITHOUT importing the bridge. Called
    # with kwargs (work_id, principal_id, interface_kind, ...); returns
    # an object with .outcome, .execution_id, .delivery_id, .response_text.
    process_inbound_callback: Any | None = field(default=None)

    @classmethod
    def build(
        cls,
        settings: WaxSettings | None,
        *,
        intelligence: IntelligenceService | None = None,
    ) -> RuntimeServices:
        """Construct all singletons. Pure in-memory — safe to call in tests.

        ``intelligence`` is optional here ONLY because some unit tests
        build services without an intelligence. Production composition
        root (create_app) ALWAYS passes intelligence. Components that
        require intelligence (consolidation, bridge) MUST verify it is
        not None at their construction site and raise a clear error if
        it is missing.
        """
        if settings is None:
            settings = WaxSettings.model_validate({})

        services = cls(
            settings=settings,
            delivery=DeliveryRouter(),
            intelligence=intelligence,
        )
        log.info(
            "runtime.services.built",
            delivery_interfaces=services.delivery.registered_interfaces(),
            intelligence_wired=intelligence is not None,
        )
        return services

    def require_intelligence(self) -> IntelligenceService:
        """Return the wired IntelligenceService or raise a clear error.

        Use this from any component that genuinely requires intelligence.
        Never use ``hasattr(self, '_intelligence')`` or
        ``getattr(self, 'intelligence', None)`` — those silently produce
        ``None`` and cause ``'NoneType' object has no attribute 'complete'``
        downstream.
        """
        if self.intelligence is None:
            raise RuntimeError(
                "RuntimeServices.intelligence is not wired. "
                "The composition root (create_app) must pass intelligence "
                "to RuntimeServices.build(). Components that require "
                "intelligence (bridge, memory consolidation) cannot "
                "operate without it."
            )
        return self.intelligence

    def register_delivery(
        self,
        interface_name: str,
        sender: Any,
        policy: Any | None = None,
    ) -> None:
        """Register an interface sender with the delivery router."""
        from wax.runtime.delivery import DeliveryPolicy

        self.delivery.register(interface_name, sender, policy=policy or DeliveryPolicy())


def services_from_app(app: Any) -> RuntimeServices | None:
    """Read the container off a FastAPI app instance, if present."""
    return getattr(app.state, "services", None)
