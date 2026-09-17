"""Terminal executor — the universal environment interface for intelligence.

The terminal is NOT a capability. It is NOT a registered tool. It is the
environment interface between intelligence and the world.

Design principles (per the open-world architecture directive):
- Full environment inheritance: the subprocess sees the same env vars as
  the WAX process (LLM credentials, DB URL, API keys, etc.). This is a
  full-trust architecture; the intelligence is trusted to operate the
  environment.
- Full network access: no host allowlist, no SSRF guard. The network is
  part of the environment.
- Full filesystem access: no path containment. The filesystem is the
  environment.
- Operational limits (timeout, output size) are transport plumbing, NOT
  authority decisions. A 60-second timeout means "this foreground process
  has a lifecycle boundary", not "you are forbidden from doing this".
- Persistent working directory: each execution gets a working dir that
  survives across terminal rounds within the same execution.
- Detached processes: the AI can start long-running services (a server,
  a worker, a tunnel) that continue after the foreground command returns.

The executor does NOT know about:
- capabilities, permissions, approvals, authority
- isolation backends, sandboxes, namespaces
- network restrictions, path containment
- resource budgets, cost protection

It only knows: execute, observe, return.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from wax.runtime.logging import get_logger

log = get_logger(__name__)


@dataclass
class TerminalResult:
    """Observation returned from a terminal execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    truncated: bool
    working_dir: str
    started_at: datetime
    ended_at: datetime
    detached: bool = False  # True if this was a background process start
    detached_pid: int | None = None  # PID of a detached process (if any)

    def to_observation(self) -> str:
        """Render the result as a compact observation string for the model.

        The model sees: exit status, stdout, stderr, duration, and whether
        the output was truncated or timed out. It does NOT see internal
        implementation details (PIDs, lease IDs, etc.) unless it started a
        detached process.
        """
        parts: list[str] = []
        if self.detached:
            parts.append(f"[detached process started, pid={self.detached_pid}]")
            return "\n".join(parts)

        status = "TIMEOUT" if self.timed_out else f"exit={self.exit_code}"
        parts.append(f"[terminal] {status} duration={self.duration_seconds:.2f}s")
        if self.truncated:
            parts.append("[terminal] output was truncated")
        if self.stdout:
            parts.append("--- stdout ---")
            parts.append(self.stdout)
        if self.stderr:
            parts.append("--- stderr ---")
            parts.append(self.stderr)
        if not self.stdout and not self.stderr and not self.timed_out:
            parts.append("(no output)")
        return "\n".join(parts)


@dataclass
class TerminalExecutor:
    """The terminal executor.

    Constructed once per execution with a working directory. The working
    directory persists across all terminal rounds within that execution,
    so the AI can create files in round 1, run tests in round 2, modify
    files in round 3, etc.

    The executor inherits the full process environment. The intelligence
    can read env vars (LLM keys, DB URLs, etc.) and use them in its
    commands. This is the trust model: the intelligence is trusted to
    operate the environment.
    """

    working_dir: Path
    timeout_seconds: float = 60.0
    output_max_chars: int = 50_000
    # Inherit the full process environment by default. The intelligence
    # can read LLM credentials, DB URLs, API keys, etc. This is deliberate:
    # the trust model is "the intelligence operates the environment".
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.env:
            # Inherit the full parent environment.
            self.env = dict(os.environ)
        self.working_dir = Path(self.working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        # Track detached processes started during this execution so we can
        # report them and (optionally) clean them up when the execution ends.
        self._detached: list[asyncio.subprocess.Process] = []

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> TerminalResult:
        """Execute a foreground command and return the observation.

        The command runs in a shell (bash -c) so the AI can use pipes,
        redirects, &&, ||, etc. The working directory is the executor's
        working_dir unless overridden.

        Operational limits (timeout, output truncation) are transport
        plumbing — they prevent a single command from destroying the
        execution (e.g. dumping 100MB into context, hanging forever).
        They are NOT authority decisions.
        """
        effective_timeout = timeout if timeout is not None else self.timeout_seconds
        effective_cwd = Path(cwd) if cwd else self.working_dir
        effective_env = dict(self.env)
        if env_overrides:
            effective_env.update(env_overrides)

        started_at = datetime.now(UTC)
        start = asyncio.get_event_loop().time()

        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(effective_cwd),
                env=effective_env,
                # Start a new process group so we can kill the whole tree
                # on timeout (avoids orphaned children).
                start_new_session=True,
            )
        except FileNotFoundError:
            return TerminalResult(
                exit_code=127,
                stdout="",
                stderr="bash not found",
                duration_seconds=0.0,
                timed_out=False,
                truncated=False,
                working_dir=str(effective_cwd),
                started_at=started_at,
                ended_at=datetime.now(UTC),
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except TimeoutError:
            timed_out = True
            await self._kill_process_tree(proc)
            try:
                stdout_bytes, stderr_bytes = await proc.communicate()
            except Exception:
                stdout_bytes, stderr_bytes = b"", b""

        ended_at = datetime.now(UTC)
        duration = asyncio.get_event_loop().time() - start

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        truncated = False

        if len(stdout) > self.output_max_chars:
            stdout = stdout[: self.output_max_chars] + "\n... [stdout truncated]"
            truncated = True
        if len(stderr) > self.output_max_chars:
            stderr = stderr[: self.output_max_chars] + "\n... [stderr truncated]"
            truncated = True

        return TerminalResult(
            exit_code=proc.returncode if proc.returncode is not None else (-1 if timed_out else 0),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            truncated=truncated,
            working_dir=str(effective_cwd),
            started_at=started_at,
            ended_at=ended_at,
        )

    async def execute_detached(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> TerminalResult:
        """Start a detached (background) process and return immediately.

        The AI can use this to start servers, workers, tunnels, long-running
        services — anything that should continue running after the foreground
        command returns. The process inherits the full environment and
        continues in the executor's process group.

        Returns a TerminalResult with detached=True and the PID. The AI can
        later inspect the process through normal terminal commands (ps, kill,
        curl localhost, etc.).
        """
        effective_cwd = Path(cwd) if cwd else self.working_dir
        effective_env = dict(self.env)
        if env_overrides:
            effective_env.update(env_overrides)

        started_at = datetime.now(UTC)
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(effective_cwd),
                env=effective_env,
                start_new_session=True,  # detach from the controlling terminal
            )
        except FileNotFoundError:
            return TerminalResult(
                exit_code=127,
                stdout="",
                stderr="bash not found",
                duration_seconds=0.0,
                timed_out=False,
                truncated=False,
                working_dir=str(effective_cwd),
                started_at=started_at,
                ended_at=datetime.now(UTC),
                detached=True,
                detached_pid=None,
            )

        self._detached.append(proc)
        ended_at = datetime.now(UTC)

        return TerminalResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            truncated=False,
            working_dir=str(effective_cwd),
            started_at=started_at,
            ended_at=ended_at,
            detached=True,
            detached_pid=proc.pid,
        )

    async def cleanup_detached(self) -> None:
        """Signal all detached processes started during this execution to stop.

        Called when the execution ends. Uses SIGTERM (graceful) then SIGKILL
        after 5 seconds if the process hasn't exited. This is operational
        hygiene, not an authority decision — a leaked server process would
        consume resources forever otherwise.
        """
        for proc in self._detached:
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        # Give them 5 seconds to exit gracefully.
        for _ in range(50):
            if all(p.returncode is not None for p in self._detached):
                break
            await asyncio.sleep(0.1)
        # Force-kill anything still alive.
        for proc in self._detached:
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        self._detached.clear()

    async def _kill_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Kill a foreground process and any children it spawned."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def create_execution_workspace(root: str | Path, execution_id: str | None = None) -> Path:
    """Create a fresh working directory for an execution.

    The directory is created under `root` (configured at startup) and
    named with a ULID or provided execution_id. The intelligence operates
    here for the duration of the execution; the directory persists across
    terminal rounds.
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    eid = execution_id or str(uuid.uuid4())
    workspace = root_path / eid
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def cleanup_execution_workspace(workspace: str | Path) -> None:
    """Remove an execution's working directory.

    Called after the execution completes (and after detached processes are
    cleaned up). The intelligence's files are ephemeral unless it explicitly
    persists them elsewhere (e.g. git push, copy to a persistent path).
    """
    try:
        shutil.rmtree(str(workspace), ignore_errors=True)
    except Exception as e:  # never let cleanup failure break the execution
        log.warning("terminal.workspace_cleanup_failed", workspace=str(workspace), error=str(e))
