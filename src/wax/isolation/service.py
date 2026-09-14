"""Isolation service — chooses the right boundary based on configuration.

Boundaries, strongest first:
- NamespaceBoundary — unprivileged user-namespace sandbox: no network,
  read-only filesystem, masked /proc+/sys, rlimits, process-group kill.
  Kernel-enforced; the appropriate default for untrusted code on Linux.
- SubprocessBoundary — separate process, scrubbed env, timeout. Isolates
  against ACCIDENTS, not adversaries (documented honestly since Phase I).
- ContainerBoundary (future — Docker/containerd), MicroVMBoundary
  (future — Firecracker), BrowserBoundary (future — Playwright).

Selection policy (`isolation_backend` setting):
- "auto"      — NamespaceBoundary when the host can run it, otherwise
                SubprocessBoundary. The fallback is LOUD: a warning log
                and a metric, because silently degrading the sandbox
                grade is exactly the kind of quiet policy change WAX
                forbids.
- "namespace" — require the sandbox; unavailable → configuration error.
- "subprocess"— force the legacy boundary (weakest; deployments that
                choose this accept the documented limits).

The selection is a runtime decision — the AI may not choose its own
isolation mechanism.
"""

from __future__ import annotations

from wax.core.exceptions import WaxConfigurationError
from wax.isolation.contracts import IsolationBoundary, IsolationKind
from wax.isolation.namespace_boundary import NamespaceBoundary
from wax.isolation.subprocess_boundary import SubprocessBoundary
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class IsolationService:
    """Selects and holds the configured isolation boundary."""

    def __init__(self, boundary: IsolationBoundary | None = None) -> None:
        self._boundary = boundary or SubprocessBoundary()

    @classmethod
    def for_kind(cls, kind: IsolationKind) -> IsolationService:
        """Build a service for a specific isolation kind."""
        if kind == IsolationKind.NAMESPACE:
            return cls(NamespaceBoundary())
        if kind == IsolationKind.SUBPROCESS:
            return cls(SubprocessBoundary())
        if kind == IsolationKind.NOOP:
            # NoopBoundary is defined in tests; intentionally not importable
            # from production code.
            raise WaxConfigurationError(
                "NoopBoundary is for tests only — do not use in production"
            )
        if kind in (IsolationKind.CONTAINER, IsolationKind.MICROVM, IsolationKind.BROWSER):
            raise WaxConfigurationError(
                f"{kind.value} isolation not yet implemented (future phase)"
            )
        raise WaxConfigurationError(f"Unknown isolation kind: {kind!r}")

    @classmethod
    def select(cls, backend: str) -> tuple[IsolationService, str]:
        """Resolve the `isolation_backend` setting to (service, actual).

        Returns the ACTUAL boundary kind in use, so callers can record
        it in audit evidence. `auto` degrades loudly, never silently.
        """
        normalized = (backend or "auto").strip().lower()
        if normalized == "namespace":
            service = cls.for_kind(IsolationKind.NAMESPACE)
            if not NamespaceBoundary.available():
                raise WaxConfigurationError(
                    "isolation_backend=namespace but this host cannot run "
                    "user-namespace sandboxes (probe failed)"
                )
            return service, IsolationKind.NAMESPACE.value
        if normalized == "subprocess":
            return cls.for_kind(IsolationKind.SUBPROCESS), IsolationKind.SUBPROCESS.value
        if normalized == "auto":
            if NamespaceBoundary.available():
                return cls.for_kind(IsolationKind.NAMESPACE), IsolationKind.NAMESPACE.value
            log.warning(
                "isolation.auto.fallback",
                detail=(
                    "namespace sandbox unavailable; falling back to "
                    "SubprocessBoundary (accident-isolation only)"
                ),
            )
            try:
                from wax.observability.runtime_metrics import get_runtime_metrics

                get_runtime_metrics().isolation_fallback()
            except Exception:  # pragma: no cover - metrics must not break selection
                pass
            return cls.for_kind(IsolationKind.SUBPROCESS), IsolationKind.SUBPROCESS.value
        raise WaxConfigurationError(f"Unknown isolation_backend: {backend!r}")

    @property
    def boundary(self) -> IsolationBoundary:
        return self._boundary

    async def close(self) -> None:
        await self._boundary.close()
