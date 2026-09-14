"""wax.observability — runtime observability.

Implements Directive §54, §97, §146:
- structured logs (already done in wax.runtime.logging)
- metrics (Prometheus-compatible via prometheus_client)
- tracing (OpenTelemetry-compatible, future)
- execution replay (via audit log + execution steps)

Key invariants:
- NEVER log secrets (enforced in wax.runtime.logging._redact_sensitive)
- NEVER log model chain-of-thought (Directive §97)
- Metrics are per-instance aggregates with NO principal dimension today —
  a deliberate, documented limitation, not an enforced privacy control.
  Any principal-dimensioned observability requires an explicit privacy
  design first (founder policy boundary).
"""

from wax.observability.metrics import MetricsRegistry, get_metrics

__all__ = ["MetricsRegistry", "get_metrics"]
