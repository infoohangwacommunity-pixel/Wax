"""WAX process lifecycle.

Handles startup and graceful shutdown of the runtime process.

Graceful shutdown is a non-negotiable property of a production runtime
(Directive §25, §56). We MUST:
- stop accepting new requests on signal
- let in-flight requests complete (with a bounded timeout)
- release resources (DB pools, HTTP clients) in reverse acquisition order
- exit cleanly (exit code 0) when shutdown completes
- exit with non-zero code if shutdown fails or times out
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

from wax.runtime.logging import get_logger

if TYPE_CHECKING:
    from wax.core.config import WaxSettings

log = get_logger(__name__)


class LifecycleManager:
    """Coordinates graceful startup and shutdown of the WAX process.

    Components register their shutdown coroutines via `on_shutdown`. The
    manager invokes them in reverse registration order (LIFO) on signal.
    """

    def __init__(self, settings: WaxSettings) -> None:
        self._settings = settings
        self._shutdown_handlers: list[tuple[str, asyncio.CancelableCoro]] = []
        self._shutting_down = False
        self._shutdown_event = asyncio.Event()

    def on_shutdown(self, name: str, coro: asyncio.CancelableCoro) -> None:
        """Register a shutdown coroutine.

        Coroutines are awaited in reverse registration order. Each is given
        a bounded timeout (default 10s) — if it does not complete, it is
        cancelled and the manager moves on.
        """
        self._shutdown_handlers.append((name, coro))

    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def trigger_shutdown(self) -> None:
        """Signal the main loop to begin shutdown."""
        self._shutdown_event.set()

    async def wait_for_shutdown(self) -> None:
        """Block until shutdown is triggered (by signal or explicit call)."""
        await self._shutdown_event.wait()

    async def run_shutdown(self, timeout: float = 10.0) -> None:
        """Invoke all registered shutdown handlers in LIFO order."""
        self._shutting_down = True
        log.info("lifecycle.shutdown.starting", remaining=len(self._shutdown_handlers))

        while self._shutdown_handlers:
            name, coro = self._shutdown_handlers.pop()
            try:
                await asyncio.wait_for(coro, timeout=timeout)
                log.info("lifecycle.shutdown.ok", component=name)
            except TimeoutError:
                log.error(
                    "lifecycle.shutdown.timeout",
                    component=name,
                    timeout_s=timeout,
                )
                coro.close()
            except Exception as e:
                log.error(
                    "lifecycle.shutdown.error",
                    component=name,
                    error=str(e),
                    error_type=type(e).__name__,
                )

        log.info("lifecycle.shutdown.complete")

    def install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers that trigger graceful shutdown."""
        loop = asyncio.get_running_loop()

        def _handler(signum: int) -> None:
            log.info("lifecycle.signal.received", signum=signum, name=_signal_name(signum))
            self.trigger_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handler, sig.value)
            except (NotImplementedError, RuntimeError):
                # add_signal_handler is not available on Windows / some test environments.
                # Fall back to default signal handling.
                signal.signal(sig, lambda s, f: _handler(s))


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIGNAL_{signum}"
