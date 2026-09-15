"""Subprocess isolation boundary.

The simplest non-trivial isolation mechanism. Runs code as a separate
process with:
- explicit environment (no inherited secrets)
- timeout (kill the process if it exceeds timeout_seconds)
- output cap (truncate stdout/stderr at max_output_bytes)

This is NOT a full sandbox — a malicious subprocess can still affect
the host (network calls, filesystem writes within the runtime user's
permissions). For untrusted code, use a stronger boundary (container,
microVM). SubprocessBoundary is appropriate for:
- trusted-but-unpredictable code (e.g., a Python script the user wrote)
- deterministic tools (math evaluation, simple utilities)
- tests where the code under test is not adversarial
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import UTC, datetime

from wax.isolation.contracts import (
    ExecutionResult,
    IsolationBoundary,
    IsolationKind,
    IsolationRequest,
)
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class SubprocessBoundary(IsolationBoundary):
    """Runs code as a subprocess with restricted environment + timeout."""

    def __init__(self, default_timeout: float = 10.0) -> None:
        self._default_timeout = default_timeout

    @property
    def kind(self) -> IsolationKind:
        return IsolationKind.SUBPROCESS

    async def execute(self, request: IsolationRequest) -> ExecutionResult:
        started_at = datetime.now(UTC)
        start_perf = time.perf_counter()

        # Build the command based on language
        cmd, env = self._prepare_command(request)

        # The child gets its own session so a timeout can kill the whole
        # process GROUP. proc.kill() alone orphans grandchildren spawned by
        # the code (e.g. `sleep 1000 &` in shell) — a resource leak the
        # code could weaponize.
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=request.working_dir,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return ExecutionResult(
                exit_code=127,
                stdout="",
                stderr=f"Executable not found: {e}",
                duration_ms=0.0,
                started_at=started_at,
                ended_at=datetime.now(UTC),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.timeout_seconds,
            )
            timed_out = False
            exit_code = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            # Kill the process GROUP if it times out — reaps descendants
            # spawned by the executed code, not just the direct child.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except ProcessLookupError:
                pass
            stdout_b = b""
            stderr_b = (f"timeout: process exceeded {request.timeout_seconds}s\n").encode()
            exit_code = 124  # standard timeout exit code
            timed_out = True

        # Enforce output cap
        stdout = stdout_b.decode("utf-8", errors="replace")[: request.max_output_bytes]
        stderr = stderr_b.decode("utf-8", errors="replace")[: request.max_output_bytes]
        truncated = (
            len(stdout_b) > request.max_output_bytes or len(stderr_b) > request.max_output_bytes
        )

        duration_ms = (time.perf_counter() - start_perf) * 1000
        ended_at = datetime.now(UTC)

        log.info(
            "isolation.subprocess.executed",
            language=request.language,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=round(duration_ms, 2),
            truncated=truncated,
        )

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round(duration_ms, 2),
            timed_out=timed_out,
            truncated=truncated,
            started_at=started_at,
            ended_at=ended_at,
            isolation_kind=self.kind.value,
        )

    def _prepare_command(self, request: IsolationRequest) -> tuple[list[str], dict[str, str]]:
        """Build the command + sanitized environment for the subprocess."""
        if request.language == "python":
            cmd = ["python3", "-c", request.code]
        elif request.language == "shell":
            cmd = ["sh", "-c", request.code]
        else:
            raise ValueError(f"Unsupported language: {request.language!r}")

        # Restricted env: NEVER inherit the parent's env. Only PATH + explicit.
        env: dict[str, str] = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if request.env:
            # Filter out anything that looks like a secret
            for k, v in request.env.items():
                kl = k.lower()
                if any(s in kl for s in ("secret", "token", "key", "password", "credential")):
                    continue
                env[k] = v

        return cmd, env
