"""wax.observability — runtime observability.

The dedicated metrics subsystem was removed in the open-world architecture
reset (per directive §87). What remains:
- structured logs (wax.runtime.logging)
- audit events (wax.observability.audit) — append-only operational ledger

No Prometheus-style metrics, no /metrics endpoint, no per-instance
aggregates. Structured logs + audit events are sufficient for the
open-world runtime.
"""

from wax.observability.audit import AuditEvent, record_audit_event

__all__ = ["AuditEvent", "record_audit_event"]
