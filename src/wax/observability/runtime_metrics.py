"""Runtime metrics emission.

The forensic audit (Section 18) found that /metrics always returned an empty
registry: the registry existed, was read by the /metrics endpoint, but no
runtime code ever recorded a metric. This module is the single facade the
live path uses to emit metrics, so metric names stay consistent and the
deployment guide's instructions (e.g. watch llm_latency_ms) describe metrics
that actually exist.

Every method is defensive: metrics MUST NEVER break message processing.
"""

from __future__ import annotations

from typing import Any

from wax.observability.metrics import get_metrics
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class RuntimeMetrics:
    """Facade over the global metrics registry for the live pipeline."""

    def __init__(self) -> None:
        self._registry = get_metrics()

    # --- Bridge pipeline -------------------------------------------------

    def message_started(self) -> None:
        self._registry.counter("bridge_messages_started_total").inc()

    def message_finished(self, status: str, interface: str) -> None:
        self._registry.counter("bridge_messages_total", status=status, interface=interface).inc()

    def observe_process_duration(self, duration_ms: float) -> None:
        self._registry.histogram("bridge_process_duration_ms").observe(duration_ms)

    def rate_limited(self, kind: str) -> None:
        # kind: "rate_limit" | "cost_cap" | "abuse_block"
        self._registry.counter("bridge_rejected_total", kind=kind).inc()

    # --- Intelligence ----------------------------------------------------

    def observe_llm_latency(self, duration_ms: float, *, provider: str, model: str) -> None:
        self._registry.histogram("llm_latency_ms", provider=provider, model=model).observe(
            duration_ms
        )

    def add_llm_tokens(self, *, provider: str, model: str, tokens_total: int) -> None:
        self._registry.counter("llm_tokens_total", provider=provider, model=model).inc(tokens_total)

    def llm_error(self, provider: str) -> None:
        self._registry.counter("llm_errors_total", provider=provider).inc()

    # --- Capabilities ----------------------------------------------------

    def capability_invoked(self, outcome: str, capability: str) -> None:
        self._registry.counter(
            "capability_invocations_total", outcome=outcome, capability=capability
        ).inc()

    # --- Delivery --------------------------------------------------------

    def send_failure(self, interface: str) -> None:
        self._registry.counter("delivery_send_failures_total", interface=interface).inc()

    def send_ok(self, interface: str) -> None:
        self._registry.counter("delivery_send_total", interface=interface).inc()

    # --- Background work (Phase V) ---------------------------------------

    def work_woken(self, outcome: str, kind: str) -> None:
        self._registry.counter("work_items_total", outcome=outcome, kind=kind).inc()

    def work_inflight(self, count: float) -> None:
        self._registry.gauge("work_items_inflight").set(count)

    # --- Provisioning (Phase S) ------------------------------------------

    def resource_provisioned(self, kind: str) -> None:
        self._registry.counter("provisioned_resources_total", kind=kind).inc()

    def resource_released(self, kind: str, reason: str) -> None:
        self._registry.counter("released_resources_total", kind=kind, reason=reason).inc()

    def snapshot(self) -> dict[str, Any]:
        return self._registry.snapshot()


_metrics: RuntimeMetrics | None = None


def get_runtime_metrics() -> RuntimeMetrics:
    """Process-wide singleton. The registry underneath is also a singleton,
    so re-creating the facade is harmless; this just avoids churn."""
    global _metrics
    if _metrics is None:
        _metrics = RuntimeMetrics()
    return _metrics
