"""Namespace isolation boundary — a kernel-enforced sandbox.

This boundary wraps adversarial code in Linux user namespaces (the same
kernel primitives containers use) with NO external dependencies: no
Docker, no daemons, no root. Unprivileged user namespaces are the
mechanism Chrome and Firefox use to sandbox renderer processes.

What the kernel enforces for every execution (verified properties):

- **No network.** A fresh network namespace has no interfaces and no
  routes — egress is impossible at the socket layer, not by policy.
  SSRF, exfiltration, and C2 callbacks from adversarial code end here.
- **Read-only filesystem.** The whole mount tree is remounted
  read-only. Writes to /usr, /etc, the workspace's parent, the
  runtime's own files — all fail with EROFS at the kernel layer.
- **Private /proc and /sys.** tmpfs is mounted over them, so the
  sandbox cannot read the runtime's /proc/<pid>/environ (a real secret
  channel when the sandbox maps to the same uid as the runtime) or
  enumerate host kernel/device state.
- **Writable only where the runtime says.** The workspace (if any) is
  bind-remounted read-write at its original path — absolute paths keep
  working — and /tmp is a private, size-capped, noexec tmpfs.
- **Fresh PID/IPC/UTS namespaces.** Host processes cannot be seen or
  signalled via the namespace; IPC objects are per-namespace.
- **No privilege escalation.** The process is "root" only inside its
  own user namespace; it owns CAP_SYS_ADMIN there and nothing outside.
- **Resource limits (rlimits).** RLIMIT_AS (memory bomb), RLIMIT_CPU
  (CPU bomb), RLIMIT_NPROC (fork bomb), RLIMIT_FSIZE (disk bomb) are
  set on the sandbox process tree. These are resource GOVERNANCE —
  the security boundary is the namespace itself.

This is container-grade isolation built from container primitives.
It is NOT a microVM: kernel exploits that break out of user namespaces
would apply here too. Deployments needing stronger guarantees implement
IsolationBoundary with Docker/Firecracker — the contract is unchanged.

Availability: requires Linux + `unshare` (util-linux) + unprivileged
user namespaces enabled. `NamespaceBoundary.available()` probes this
once and caches. On unavailable hosts the isolation service falls back
to SubprocessBoundary LOUDLY (log + metric), never silently.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import time
from datetime import UTC, datetime

from wax.isolation.contracts import (
    ExecutionResult,
    IsolationKind,
    IsolationRequest,
)
from wax.isolation.subprocess_boundary import SubprocessBoundary
from wax.runtime.logging import get_logger

log = get_logger(__name__)

# The unshare(1) flag set: user+mount+pid+net+ipc+uts namespaces, forked
# so the sandbox process tree is fully inside the new namespaces.
_UNSHARE_FLAGS = [
    "--user",
    "--map-root-user",
    "--mount",
    "--pid",
    "--net",
    "--ipc",
    "--uts",
    "--fork",
]

_default_tmp_size = "256m"


class NamespaceBoundary(SubprocessBoundary):
    """Runs code inside an unprivileged user-namespace sandbox.

    Subclasses SubprocessBoundary to reuse the command building and the
    environment-scrubbing rules (never inherit secrets). Everything
    else — the sandbox assembly, rlimits, process-group kill — is the
    namespace boundary's own hardening.
    """

    _available: bool | None = None  # cached probe result (per process)

    @property
    def kind(self) -> IsolationKind:
        return IsolationKind.NAMESPACE

    @classmethod
    def available(cls) -> bool:
        """Probe (once per process) that the sandbox can actually start.

        The probe runs a real `unshare` with the full flag set — a
        privileged/seccomp-restricted host can reject user namespaces
        or individual namespace types, so existence of the binary is
        not enough.
        """
        if cls._available is not None:
            return cls._available
        cls._available = cls._probe()
        if not cls._available:
            log.warning("isolation.namespace.unavailable")
        return cls._available

    @classmethod
    def reset_probe_cache(cls) -> None:
        """Forget the cached probe result (tests, configuration changes)."""
        cls._available = None

    @classmethod
    def _probe(cls) -> bool:
        unshare = shutil.which("unshare")
        if unshare is None or os.name != "posix":
            return False
        try:
            import subprocess

            result = subprocess.run(
                [unshare, *_UNSHARE_FLAGS, "true"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    async def execute(self, request: IsolationRequest) -> ExecutionResult:
        if not self.available():
            raise RuntimeError(
                "NamespaceBoundary is unavailable on this host "
                "(unshare/user namespaces). Configure isolation_backend "
                "to fall back explicitly."
            )

        started_at = datetime.now(UTC)
        start_perf = time.perf_counter()

        cmd, env = self._prepare_command(request)
        inner = self._inner_script(request, cmd)
        argv = [shutil.which("unshare") or "unshare", *_UNSHARE_FLAGS,
                "sh", "-c", inner]

        preexec = self._rlimit_preset(request)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # The sandbox gets its own session: killing the process
                # group on timeout reaps every descendant (fork bombs,
                # background children) — no orphans survive the boundary.
                start_new_session=True,
                preexec_fn=preexec,
            )
        except (OSError, ValueError) as e:
            return ExecutionResult(
                exit_code=126,
                stdout="",
                stderr=f"sandbox start failed: {e}",
                duration_ms=0.0,
                started_at=started_at,
                ended_at=datetime.now(UTC),
                isolation_kind=self.kind.value,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.timeout_seconds,
            )
            timed_out = False
            exit_code = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            await self._kill_group(proc)
            stdout_b = b""
            stderr_b = (
                f"timeout: sandbox exceeded {request.timeout_seconds}s\n"
            ).encode()
            exit_code = 124
            timed_out = True

        stdout = stdout_b.decode("utf-8", errors="replace")[:request.max_output_bytes]
        stderr = stderr_b.decode("utf-8", errors="replace")[:request.max_output_bytes]
        truncated = (
            len(stdout_b) > request.max_output_bytes
            or len(stderr_b) > request.max_output_bytes
        )

        duration_ms = (time.perf_counter() - start_perf) * 1000
        ended_at = datetime.now(UTC)

        log.info(
            "isolation.namespace.executed",
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

    # ------------------------------------------------------------------
    # Sandbox assembly
    # ------------------------------------------------------------------

    def _inner_script(self, request: IsolationRequest, cmd: list[str]) -> str:
        """Build the inner shell script that hardens the mount tree.

        Runs INSIDE the new namespaces, as the namespace's mapped root,
        before exec'ing the user's command. Order matters:

        1. ro /            — the world becomes read-only first
        2. mask /proc /sys — host process/kernel info hidden (must come
                              AFTER ro / so the mask mount itself is
                              allowed, and BEFORE anything runs)
        3. rw workspace    — the designated workspace re-exposed rw at
                              its original path (paths keep working)
        4. private /tmp    — size-capped, noexec, nodev, nosuid
        5. cd + exec       — run the user's command
        """
        # 1. The world becomes read-only first.
        lines = [
            "set -e",
            "mount -o remount,ro,bind /",
            "mount -t tmpfs -o mode=555,nodev,nosuid,noexec tmpfs /proc",
            "mount -t tmpfs -o mode=555,nodev,nosuid,noexec tmpfs /sys",
        ]
        # 2. Writable space. Order matters when there is no workspace:
        #    /tmp must be mounted before mkdir into it.
        if request.working_dir:
            # Absolute path: the mount script runs with a different cwd,
            # and bind-mount semantics require exact path identity.
            wd = os.path.abspath(request.working_dir)
            lines += [
                f"mount --bind {wd} {wd}",
                f"mount -o remount,rw,bind {wd}",
                f"cd {wd}",
            ]
        else:
            # No workspace: the sandbox gets a fresh private cwd. The
            # default (inheriting the runtime's cwd) would expose the
            # runtime's own files and invite accidental writes.
            lines += [
                f"mount -t tmpfs -o size={_default_tmp_size},mode=1777,"
                "nodev,nosuid,noexec tmpfs /tmp",
                "mkdir -p /tmp/work",
                "cd /tmp/work",
            ]
        if request.working_dir:
            lines += [
                f"mount -t tmpfs -o size={_default_tmp_size},mode=1777,"
                "nodev,nosuid,noexec tmpfs /tmp",
            ]
        # 3. Run the user's command — nothing after it.
        lines.append("exec " + _shell_join(cmd))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Resource limits + reaping
    # ------------------------------------------------------------------

    @staticmethod
    def _rlimit_preset(request: IsolationRequest):
        """Build a preexec_fn setting the resource rlimits.

        Runs in the forked child before exec — limits apply to the
        entire sandbox tree. Returns None when all limits are disabled.
        """
        memory_bytes = request.memory_limit_mb * 1024 * 1024
        file_bytes = request.max_file_bytes
        nproc = request.max_processes
        cpu_seconds = int(request.timeout_seconds) + 1

        def _apply() -> None:  # pragma: no cover - runs in child
            if memory_bytes > 0:
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            if file_bytes > 0:
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
            if nproc > 0:
                resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            if cpu_seconds > 0:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))

        limits_active = any(
            (memory_bytes, file_bytes, nproc, cpu_seconds)
        )
        return _apply if limits_active else None

    @staticmethod
    async def _kill_group(proc: asyncio.subprocess.Process) -> None:
        """Kill the sandbox process GROUP, not just the direct child.

        `unshare --fork` + `start_new_session` put every descendant in
        one process group; killpg(9) reaps them all. A plain proc.kill()
        would orphan grandchildren (the SubprocessBoundary defect this
        boundary is designed to avoid).
        """
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - SIGKILL cannot hang
            pass


def _shell_join(cmd: list[str]) -> str:
    """Quote a command list for the inner shell (defensive quoting)."""
    import shlex

    return " ".join(shlex.quote(part) for part in cmd)
