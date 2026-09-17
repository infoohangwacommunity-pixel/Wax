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
- Persistent per-principal workspace: each principal gets a stable
  workspace at /<root>/<principal_id>/. Files survive across executions
  and conversations so the AI can build long projects (resume, assignment,
  research, coding) without rebuilding every turn.
- Execution sessions: multiple terminal calls inside one reasoning loop
  share the same working directory and process group.
- Detached processes: the AI can start long-running services (a server,
  a worker, a tunnel) that continue after the foreground command returns.
- Internet observability: network calls are recorded (started/succeeded/
  failed/duration) — observed, not censored.

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
class NetworkCall:
    """Recorded observation of a network call (NOT censored, just observed)."""

    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    protocol: str | None = None  # http, https, git, ssh, etc.
    host: str | None = None
    port: int | None = None
    succeeded: bool | None = None
    error: str | None = None


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
    detached: bool = False
    detached_pid: int | None = None
    network_calls: list[NetworkCall] = field(default_factory=list)

    def to_observation(self) -> str:
        """Render the result as a compact observation string for the model."""
        parts: list[str] = []
        if self.detached:
            parts.append(f"[detached process started, pid={self.detached_pid}]")
            return "\n".join(parts)

        status = "TIMEOUT" if self.timed_out else f"exit={self.exit_code}"
        parts.append(f"[terminal] {status} duration={self.duration_seconds:.2f}s")
        if self.truncated:
            parts.append("[terminal] output was truncated")
        if self.network_calls:
            net_summary = ", ".join(
                f"{nc.protocol}://{nc.host}"
                + (f":{nc.port}" if nc.port else "")
                + f"={'ok' if nc.succeeded else 'fail'}"
                for nc in self.network_calls
                if nc.host
            )
            if net_summary:
                parts.append(f"[network] {net_summary}")
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

    Constructed per-execution with a working directory. The working
    directory is the principal's persistent workspace — files survive
    across executions so the AI can build long projects.

    The executor inherits the full process environment. The intelligence
    can read env vars (LLM keys, DB URLs, API keys, etc.) and use them.
    """

    working_dir: Path
    timeout_seconds: float = 60.0
    output_max_chars: int = 50_000
    env: dict[str, str] = field(default_factory=dict)
    env_overrides: dict[str, str] = field(default_factory=dict)
    _network_log: list[NetworkCall] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.env:
            self.env = dict(os.environ)
        # Apply env overrides (WAX_CURRENT_PRINCIPAL_ID, etc.) so the
        # wax_runtime helper can read them from inside the terminal.
        self.env.update(self.env_overrides)
        self.working_dir = Path(self.working_dir)
        try:
            self.working_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            # Fallback to /tmp if the configured path isn't writable.
            # This happens on Railway when the volume mount isn't owned
            # by the non-root user. /tmp is always writable.
            import tempfile

            fallback = Path(tempfile.gettempdir()) / "wax-workspaces" / self.working_dir.name
            log.warning(
                "terminal.workspace_fallback",
                configured_path=str(self.working_dir),
                fallback_path=str(fallback),
            )
            self.working_dir = fallback
            self.working_dir.mkdir(parents=True, exist_ok=True)
        self._detached: list[asyncio.subprocess.Process] = []

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> TerminalResult:
        """Execute a foreground command and return the observation."""
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

        # Observe network calls from stdout (best-effort — not censored, just recorded)
        network_calls = self._extract_network_observations(stdout, stderr, started_at, ended_at)
        self._network_log.extend(network_calls)

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
            network_calls=network_calls,
        )

    async def execute_detached(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> TerminalResult:
        """Start a detached (background) process and return immediately."""
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
        """Signal all detached processes to stop. Operational hygiene only."""
        for proc in self._detached:
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        for _ in range(50):
            if all(p.returncode is not None for p in self._detached):
                break
            await asyncio.sleep(0.1)
        for proc in self._detached:
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        self._detached.clear()

    def network_log(self) -> list[NetworkCall]:
        """Return the cumulative network observation log for this execution."""
        return list(self._network_log)

    async def _kill_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Kill a foreground process and any children it spawned."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    def _extract_network_observations(
        self,
        stdout: str,
        stderr: str,
        started_at: datetime,
        ended_at: datetime,
    ) -> list[NetworkCall]:
        """Best-effort extraction of network call observations from output.

        This is NOT censorship — the AI can call any host. This is
        observability: the runtime records what happened so the audit
        ledger can show "the AI connected to github.com at 14:32".
        """
        import re

        calls: list[NetworkCall] = []
        # Match URLs in output (http://, https://, git@, ssh://)
        url_pattern = re.compile(
            r"(?:https?|git|ssh)://([a-zA-Z0-9._-]+)(?::(\d+))?|git@([a-zA-Z0-9._-]+)"
        )
        seen_hosts: set[str] = set()
        for match in url_pattern.finditer(stdout + "\n" + stderr):
            host = match.group(1) or match.group(3)
            port = match.group(2)
            if host and host not in seen_hosts:
                seen_hosts.add(host)
                calls.append(
                    NetworkCall(
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_seconds=(ended_at - started_at).total_seconds(),
                        protocol="https" if match.group(0).startswith("https") else "http",
                        host=host,
                        port=int(port) if port else None,
                        succeeded=True,  # the URL appeared in output, suggesting it was reached
                    )
                )
        return calls


# ---------------------------------------------------------------------------
# Workspace management — persistent per-principal workspaces
# ---------------------------------------------------------------------------


def principal_workspace(root: str | Path, principal_id: str) -> Path:
    """Return the persistent workspace path for a principal.

    The workspace is /<root>/<principal_id>/. Files survive across
    executions and conversations so the AI can build long projects.

    The workspace is created if it doesn't exist. Idle cleanup happens
    later (a maintenance sweep can archive workspaces not touched in N
    days, but never mid-conversation).

    If the configured root isn't writable (e.g. Railway volume permissions),
    falls back to /tmp/wax-workspaces.
    """
    root_path = Path(root)
    ws = root_path / principal_id
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        import tempfile

        fallback_root = Path(tempfile.gettempdir()) / "wax-workspaces"
        ws = fallback_root / principal_id
        log.warning(
            "terminal.workspace_root_fallback",
            configured_root=str(root_path),
            fallback_root=str(fallback_root),
        )
        ws.mkdir(parents=True, exist_ok=True)
    return ws


def execution_workspace(root: str | Path, principal_id: str, execution_id: str) -> Path:
    """Return a per-execution subdirectory inside the principal's workspace.

    The AI operates in this subdirectory during one execution. Files
    persist across terminal rounds within that execution. The principal's
    top-level workspace is also accessible (the AI can cd .. to find
    files from previous executions).
    """
    ws = principal_workspace(root, principal_id) / "executions" / execution_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def list_principal_files(root: str | Path, principal_id: str) -> list[str]:
    """List all files in a principal's workspace (top-level + executions/).

    Used by the runtime to describe the workspace state in the system
    prompt — the AI sees what files already exist before it starts.
    """
    ws = Path(root) / principal_id
    if not ws.exists():
        return []
    files: list[str] = []
    for path in sorted(ws.rglob("*")):
        if path.is_file():
            rel = path.relative_to(ws)
            files.append(str(rel))
    return files[:100]  # cap to keep the prompt small


# Backwards-compat helpers (older code used these)
def create_execution_workspace(root: str | Path, execution_id: str | None = None) -> Path:
    """Legacy helper — use principal_workspace/execution_workspace instead."""
    eid = execution_id or str(uuid.uuid4())
    ws = Path(root) / eid
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def cleanup_execution_workspace(workspace: str | Path) -> None:
    """Remove an execution's working directory (NOT the principal's workspace).

    Per-execution subdirectories under executions/ can be cleaned up
    after the execution ends. The principal's top-level workspace
    persists.
    """
    try:
        shutil.rmtree(str(workspace), ignore_errors=True)
    except Exception as e:
        log.warning("terminal.workspace_cleanup_failed", workspace=str(workspace), error=str(e))
