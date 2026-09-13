"""Contracts for the isolation system.

An IsolationBoundary is the abstraction over any sandboxing mechanism.
Implementations may include:
- SubprocessBoundary (separate process, restricted env)
- ContainerBoundary (future — Docker/containerd)
- MicroVMBoundary (future — Firecracker, Cloud Hypervisor)
- BrowserBoundary (future — Playwright headless browser for web work)
- NoopBoundary (testing only — NO real isolation)

The contract is intentionally minimal: run code, return stdout/stderr/exit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IsolationKind(StrEnum):
    """Discriminator for the type of isolation a boundary provides."""

    SUBPROCESS = "subprocess"  # separate process, restricted env
    NOOP = "noop"  # in-process (testing only — NO real isolation)
    CONTAINER = "container"  # future — Docker/containerd
    MICROVM = "microvm"  # future — Firecracker
    BROWSER = "browser"  # future — headless browser


@dataclass
class IsolationRequest:
    """A request to execute code in an isolated environment.

    The runtime constructs this — the AI never constructs one directly.
    """

    code: str
    language: str = "python"  # python | shell | javascript (future)
    timeout_seconds: float = 10.0
    max_output_bytes: int = 1_000_000  # 1 MiB cap on stdout+stderr
    env: dict[str, str] | None = None  # additional env vars for the isolated process
    working_dir: str | None = None  # override working directory


@dataclass
class ExecutionResult:
    """The outcome of running code in an isolated environment.

    Output is truncated if it exceeds max_output_bytes.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    truncated: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None


class IsolationBoundary(ABC):
    """Abstract contract for any isolation mechanism.

    Implementations MUST enforce:
    - timeout (kill the process if it exceeds timeout_seconds)
    - output cap (truncate stdout/stderr at max_output_bytes)
    - clean env (do NOT inherit secrets — explicit env vars only)
    """

    @property
    @abstractmethod
    def kind(self) -> IsolationKind:
        """Return the discriminator for this boundary type."""

    @abstractmethod
    async def execute(self, request: IsolationRequest) -> ExecutionResult:
        """Execute the requested code in an isolated environment."""

    async def close(self) -> None:
        """Release any resources held by this boundary.

        Default implementation is a no-op. Subclasses with resources
        (e.g., container clients) should override.
        """
        return None
