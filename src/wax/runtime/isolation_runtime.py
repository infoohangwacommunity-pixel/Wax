"""Runtime isolation decision holder (P0-Terminal).

The isolation grade — namespace sandbox vs subprocess, per the
`isolation_backend` setting — is a RUNTIME decision from configuration.
It is never the caller's, never the model's.

Historically only `code.run` honoured that rule; `terminal.execute`
spawned a raw `asyncio.create_subprocess_shell` with no sandbox at all,
bypassing IsolationService entirely. This module is the shared wiring
point: `RuntimeServices` holds ONE RuntimeIsolation, and every execution
path (code.run, terminal.execute) resolves its boundary through it.

Selection is LAZY and CACHED: `IsolationService.select()` probes the
host (user-namespace availability) and can raise a configuration error;
deferring that to first use keeps `RuntimeServices.build()` safe for
hermetic tests, and caching keeps the probe + the loud `auto` fallback
warning a per-process event instead of per-execution noise.
"""

from __future__ import annotations

from wax.core.config import WaxSettings
from wax.isolation.contracts import IsolationRequest
from wax.isolation.service import IsolationService
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class RuntimeIsolation:
    """Lazily-selected, cached isolation boundary for the whole runtime."""

    def __init__(self, settings: WaxSettings) -> None:
        self._settings = settings
        self._service: IsolationService | None = None
        self._kind: str | None = None

    def select(self) -> tuple[IsolationService, str]:
        """Resolve (and memoize) the configured boundary.

        Returns (service, actual_kind) where actual_kind is recorded in
        audit evidence. The `auto` backend degrades LOUDLY (log + metric)
        when the namespace sandbox is unavailable — never silently.
        """
        if self._service is None or self._kind is None:
            self._service, self._kind = IsolationService.select(self._settings.isolation_backend)
            log.info(
                "isolation.runtime_selected",
                backend=self._settings.isolation_backend,
                boundary=self._kind,
            )
        return self._service, self._kind

    @property
    def kind(self) -> str:
        """The resolved boundary kind (forces selection)."""
        _, kind = self.select()
        return kind

    async def execute(self, request: IsolationRequest):
        """Execute through the configured boundary (convenience helper)."""
        service, _ = self.select()
        return await service.boundary.execute(request)

    async def close(self) -> None:
        if self._service is not None:
            await self._service.close()
            self._service = None
            self._kind = None
