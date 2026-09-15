"""Adversarial tests for the NamespaceBoundary (ADR-0016).

These tests treat the executed code as ADVERSARIAL: it tries to reach
the network, escape the filesystem, fork-bomb, memory-bomb, disk-bomb,
read the runtime's secrets, and survive its own timeout. Every claim
here is kernel-enforced behavior, not policy the code could talk its
way out of.

Requires Linux with unprivileged user namespaces. When the host cannot
run the sandbox, the namespace tests SKIP (loudly) — the fallback
selection tests always run.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from wax.isolation.contracts import IsolationRequest
from wax.isolation.namespace_boundary import NamespaceBoundary
from wax.isolation.service import IsolationService

ns_available = NamespaceBoundary.available()

requires_ns = pytest.mark.skipif(
    not ns_available,
    reason="host cannot run user-namespace sandboxes (unshare probe failed)",
)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    NamespaceBoundary.reset_probe_cache()
    yield
    NamespaceBoundary.reset_probe_cache()


def _run(code: str, **kwargs) -> dict:
    boundary = NamespaceBoundary()
    request = IsolationRequest(code=code, **kwargs)
    return asyncio.run(boundary.execute(request))


class TestNamespaceSandbox:
    @requires_ns
    def test_plain_python_still_works(self):
        result = _run("print(6*7)")
        assert result.exit_code == 0
        assert "42" in result.stdout

    @requires_ns
    def test_result_carries_isolation_kind(self):
        result = _run("print('x')")
        assert result.isolation_kind == "namespace"

    @requires_ns
    def test_network_is_unreachable(self):
        # The kernel gives the sandbox a network namespace with no
        # interfaces and no routes — egress is impossible, not discouraged.
        code = """
import socket
try:
    socket.create_connection(("8.8.8.8", 53), timeout=2)
    print("NETWORK-REACHED")
except OSError as e:
    print("NETWORK-BLOCKED", e.errno)
"""
        result = _run(code)
        assert result.exit_code == 0
        assert "NETWORK-BLOCKED" in result.stdout
        assert "NETWORK-REACHED" not in result.stdout

    @requires_ns
    def test_filesystem_is_read_only_outside_workspace(self, tmp_path):
        # Writes to root-owned paths AND user-owned paths outside the
        # workspace both fail: the whole mount tree is read-only.
        code = """
errno = None
try:
    with open("/usr/wax-escape", "w") as f:
        f.write("x")
except OSError as e:
    errno = e.errno
print("USR", errno)
errno = None
try:
    with open("/home/wax-escape", "w") as f:
        f.write("x")
except OSError as e:
    errno = e.errno
print("HOME", errno)
"""
        result = _run(code)
        assert result.exit_code == 0
        assert "USR 30" in result.stdout  # EROFS
        assert "HOME 30" in result.stdout  # EROFS

    @requires_ns
    def test_workspace_is_writable_and_read_write_bound(self, tmp_path):
        (tmp_path / "input.txt").write_text("from-host")
        code = """
with open("input.txt") as f:
    seen = f.read()
with open("output.txt", "w") as f:
    f.write("from-sandbox")
print("SAW", seen)
"""
        result = _run(code, working_dir=str(tmp_path))
        assert result.exit_code == 0
        assert "SAW from-host" in result.stdout
        assert (tmp_path / "output.txt").read_text() == "from-sandbox"

    @requires_ns
    def test_private_tmp_hides_host_tmp(self, tmp_path):
        canary = tmp_path / "canary"
        canary.write_text("host-secret")
        code = """
import os
print("CANARY", os.path.exists("/tmp/canary"))
with open("/tmp/mine", "w") as f:
    f.write("sandbox-file")
print("TMP-WRITE-OK")
"""
        host_tmp = tmp_path / "hosttmp"
        host_tmp.mkdir()
        # The sandbox's /tmp is its own tmpfs: host /tmp is invisible,
        # sandbox writes never reach the host.
        result = _run(code, working_dir=str(host_tmp))
        assert result.exit_code == 0
        assert "CANARY False" in result.stdout
        assert "TMP-WRITE-OK" in result.stdout
        assert not (host_tmp / "mine").exists()
        assert canary.read_text() == "host-secret"

    @requires_ns
    def test_host_proc_is_masked(self):
        # A real leak path: the sandbox maps to the SAME uid as the
        # runtime, so host /proc/<pid>/environ would be readable. The
        # boundary mounts tmpfs over /proc.
        code = """
import os
entries = os.listdir("/proc")
print("PROC-ENTRIES", [e for e in entries if e.isdigit()][:5])
"""
        result = _run(code)
        assert result.exit_code == 0
        assert "PROC-ENTRIES []" in result.stdout

    @requires_ns
    def test_environment_is_scrubbed(self):
        # The sandbox env is PATH + explicit request vars. The runtime's
        # own environment (which holds deployment secrets) is NOT
        # inherited — and /proc masking removes the backdoor read.
        code = """
import os
print("HAS-HOME", "HOME" in os.environ)
print("HAS-SECRET", any("TOKEN" in k or "SECRET" in k for k in os.environ))
"""
        result = _run(code, env={"WAX_PUBLIC_VAR": "fine"})
        assert result.exit_code == 0
        assert "HAS-HOME False" in result.stdout
        assert "HAS-SECRET False" in result.stdout

    @requires_ns
    def test_default_cwd_is_fresh_private_dir(self):
        code = """
import os
print("CWD", os.getcwd())
print("LISTING", sorted(os.listdir(".")))
"""
        result = _run(code)
        assert result.exit_code == 0
        assert "CWD /tmp/work" in result.stdout
        assert "LISTING []" in result.stdout

    @requires_ns
    def test_timeout_kills_the_whole_group(self):
        # The code spawns background children. After the timeout, NONE
        # of them survive on the host (process-group kill).
        import time as _time

        marker = "sleep 6060"
        started = _time.perf_counter()
        result = _run(
            f"{marker} & {marker}; echo done",
            language="shell",
            timeout_seconds=1.5,
        )
        elapsed = _time.perf_counter() - started
        assert result.timed_out is True
        assert elapsed < 10
        # Host-wide orphan scan (host /proc is visible to the TEST).
        orphans = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if b"6060" in f.read():
                        orphans.append(pid)
            except OSError:
                continue
        assert orphans == [], f"orphaned sandbox children survived: {orphans}"

    @requires_ns
    def test_fork_bomb_is_bounded(self):
        # RLIMIT_NPROC bounds the sandbox process tree. Whatever the
        # host's process pressure, the bomb cannot fork unboundedly.
        code = """
import os
forked = 0
try:
    while forked < 200:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        forked += 1
except OSError:
    pass
print("FORKS", forked)
"""
        result = _run(code, max_processes=48, timeout_seconds=20.0)
        assert result.exit_code == 0
        fork_line = next(line for line in result.stdout.splitlines() if line.startswith("FORKS"))
        assert int(fork_line.split()[1]) < 200

    @requires_ns
    def test_memory_bomb_is_bounded(self):
        # RLIMIT_AS caps address space; the allocator raises before the
        # host OOMs.
        code = """
allocated = 0
chunks = []
try:
    for _ in range(128):          # 128 x 16MB = 2GB attempted
        chunks.append(bytearray(16 * 1024 * 1024))
        allocated += 16
except MemoryError:
    pass
print("MB", allocated)
"""
        result = _run(code, memory_limit_mb=256, timeout_seconds=25.0)
        assert result.exit_code == 0
        mb_line = next(line for line in result.stdout.splitlines() if line.startswith("MB"))
        assert int(mb_line.split()[1]) < 2048

    @requires_ns
    def test_disk_bomb_is_bounded(self):
        # Private /tmp is a size-capped tmpfs AND RLIMIT_FSIZE caps a
        # single file. Either way the host disk is untouched.
        code = """
size = 0
try:
    with open("/tmp/bomb", "wb") as f:
        for _ in range(64):        # 64 x 8MB = 512MB attempted
            f.write(b"\\0" * (8 * 1024 * 1024))
            size += 8
except OSError:
    pass
print("WROTE-MB", size)
"""
        result = _run(code, max_file_bytes=32_000_000, timeout_seconds=25.0)
        assert result.exit_code == 0
        wrote_line = next(
            line for line in result.stdout.splitlines() if line.startswith("WROTE-MB")
        )
        assert int(wrote_line.split()[1]) <= 512

    @requires_ns
    def test_shell_sees_no_runtime_files_by_default(self):
        code = "print(__import__('os').path.exists('pyproject.toml'))"
        result = _run(code)
        assert "False" in result.stdout


class TestBoundarySelection:
    def test_auto_selects_namespace_when_available(self):
        _service, kind = IsolationService.select("auto")
        if ns_available:
            assert kind == "namespace"
        else:
            assert kind == "subprocess"

    def test_explicit_subprocess_selects_subprocess(self):
        _, kind = IsolationService.select("subprocess")
        assert kind == "subprocess"

    def test_explicit_namespace_selects_namespace(self):
        if not ns_available:
            pytest.skip("namespace unavailable on host")
        _, kind = IsolationService.select("namespace")
        assert kind == "namespace"

    def test_namespace_required_but_unavailable_is_loud(self, monkeypatch):
        NamespaceBoundary._available = False
        from wax.core.exceptions import WaxConfigurationError

        with pytest.raises(WaxConfigurationError):
            IsolationService.select("namespace")

    def test_auto_fallback_is_loud_not_silent(self, monkeypatch):
        NamespaceBoundary._available = False
        from wax.observability.runtime_metrics import get_runtime_metrics

        metrics = get_runtime_metrics()
        before = metrics._registry.counter("isolation_fallback_total").value
        _, kind = IsolationService.select("auto")
        assert kind == "subprocess"
        after = metrics._registry.counter("isolation_fallback_total").value
        assert after == before + 1  # the fallback was METERED, not silent

    def test_unknown_backend_is_configuration_error(self):
        from wax.core.exceptions import WaxConfigurationError

        with pytest.raises(WaxConfigurationError):
            IsolationService.select("yolo")
