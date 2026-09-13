"""Isolation service — chooses the right boundary based on configuration.

For Phase I, only SubprocessBoundary is implemented (real, but minimal).
NoopBoundary is also provided but marked EXPERIMENTAL — must NEVER be
used for production AI work.

Future phases will add:
- ContainerBoundary (Docker/containerd) for stronger isolation
- MicroVMBoundary (Firecracker) for maximum isolation
- BrowserBoundary (Playwright) for web work
"""

from __future__ import annotations

from wax.core.exceptions import WaxConfigurationError
from wax.isolation.contracts import IsolationBoundary, IsolationKind
from wax.isolation.subprocess_boundary import SubprocessBoundary


class IsolationService:
    """Selects and holds the configured isolation boundary.

    The selection is a runtime decision — the AI may not choose its
    own isolation mechanism. Configuration determines the boundary.
    """

    def __init__(self, boundary: IsolationBoundary | None = None) -> None:
        self._boundary = boundary or SubprocessBoundary()

    @classmethod
    def for_kind(cls, kind: IsolationKind) -> IsolationService:
        """Build a service for a specific isolation kind."""
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

    @property
    def boundary(self) -> IsolationBoundary:
        return self._boundary

    async def close(self) -> None:
        await self._boundary.close()
