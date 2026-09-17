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

Just: intelligence, delivery, work runner, terminal config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wax.core.config import WaxSettings
from wax.runtime.delivery import DeliveryRouter
from wax.runtime.logging import get_logger

if TYPE_CHECKING:
    from wax.runtime.work.reentry import ReentryCallback

log = get_logger(__name__)


@dataclass
class RuntimeServices:
    """Process-wide singletons shared by every runtime subsystem."""

    settings: WaxSettings
    delivery: DeliveryRouter
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

    @classmethod
    def build(cls, settings: WaxSettings | None) -> RuntimeServices:
        """Construct all singletons. Pure in-memory — safe to call in tests."""
        if settings is None:
            settings = WaxSettings.model_validate({})

        services = cls(
            settings=settings,
            delivery=DeliveryRouter(),
        )
        log.info(
            "runtime.services.built",
            delivery_interfaces=services.delivery.registered_interfaces(),
        )
        return services

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
