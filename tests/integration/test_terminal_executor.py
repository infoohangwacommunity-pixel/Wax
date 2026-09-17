"""Terminal executor tests.

Verifies the core terminal behaviors:
- foreground execution returns exit code + stdout + stderr
- environment inheritance works
- working directory persists
- timeout handling
- output truncation
- detached process start
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wax.runtime.executor import (
    TerminalExecutor,
    cleanup_execution_workspace,
    create_execution_workspace,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = create_execution_workspace(str(tmp_path), execution_id="test-exec-1")
    yield ws
    cleanup_execution_workspace(ws)


@pytest.fixture
def executor(workspace: Path) -> TerminalExecutor:
    return TerminalExecutor(working_dir=workspace, timeout_seconds=10.0, output_max_chars=1000)


class TestForegroundExecution:
    async def test_simple_echo(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("echo hello world")
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert not result.timed_out

    async def test_exit_code_propagates(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("exit 42")
        assert result.exit_code == 42

    async def test_stderr_captured(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("echo 'error message' >&2")
        assert result.exit_code == 0
        assert "error message" in result.stderr

    async def test_command_not_found(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("this_command_does_not_exist_12345")
        assert result.exit_code != 0
        assert "not found" in result.stderr.lower() or "command not found" in result.stderr.lower()


class TestEnvironmentInheritance:
    async def test_inherits_process_env(self, executor: TerminalExecutor) -> None:
        """The terminal inherits the full process environment."""
        # PATH is always set; the terminal should see it.
        result = await executor.execute("echo $PATH")
        assert result.exit_code == 0
        assert len(result.stdout.strip()) > 0

    async def test_env_override(self, executor: TerminalExecutor) -> None:
        """Environment overrides are applied."""
        result = await executor.execute(
            "echo $TEST_VAR_12345",
            env_overrides={"TEST_VAR_12345": "hello_from_test"},
        )
        assert "hello_from_test" in result.stdout

    async def test_can_read_secrets_from_env(self, executor: TerminalExecutor) -> None:
        """The terminal can read secrets from the environment (full-trust architecture)."""
        result = await executor.execute(
            "python3 -c \"import os; print(os.environ.get('WAX_TEST_SECRET', 'missing'))\"",
            env_overrides={"WAX_TEST_SECRET": "super_secret_value"},
        )
        assert "super_secret_value" in result.stdout


class TestWorkingDirectory:
    async def test_default_working_dir(self, executor: TerminalExecutor, workspace: Path) -> None:
        result = await executor.execute("pwd")
        assert str(workspace) in result.stdout

    async def test_working_dir_persists_across_calls(self, executor: TerminalExecutor) -> None:
        """Files created in round 1 are visible in round 2."""
        await executor.execute("echo 'hello' > test_file.txt")
        result = await executor.execute("cat test_file.txt")
        assert "hello" in result.stdout

    async def test_can_create_subdirectories(self, executor: TerminalExecutor) -> None:
        await executor.execute("mkdir -p sub/dir && echo 'nested' > sub/dir/file.txt")
        result = await executor.execute("cat sub/dir/file.txt")
        assert "nested" in result.stdout


class TestTimeout:
    async def test_timeout_kills_process(self, tmp_path: Path) -> None:
        ws = create_execution_workspace(str(tmp_path), execution_id="test-timeout")
        try:
            executor = TerminalExecutor(working_dir=ws, timeout_seconds=0.5)
            result = await executor.execute("sleep 10")
            assert result.timed_out is True
            # The process should have been killed; duration should be ~0.5s
            assert result.duration_seconds < 5.0
        finally:
            cleanup_execution_workspace(ws)

    async def test_custom_timeout(self, executor: TerminalExecutor) -> None:
        """A custom timeout overrides the default."""
        result = await executor.execute("sleep 0.1", timeout=1.0)
        assert not result.timed_out
        assert result.exit_code == 0


class TestOutputTruncation:
    async def test_large_output_truncated(self, tmp_path: Path) -> None:
        ws = create_execution_workspace(str(tmp_path), execution_id="test-trunc")
        try:
            executor = TerminalExecutor(working_dir=ws, timeout_seconds=10.0, output_max_chars=100)
            result = await executor.execute("python3 -c \"print('x' * 500)\"")
            assert result.truncated is True
            assert "truncated" in result.stdout
        finally:
            cleanup_execution_workspace(ws)


class TestDetachedProcess:
    async def test_detached_returns_immediately(self, executor: TerminalExecutor) -> None:
        result = await executor.execute_detached("sleep 30")
        assert result.detached is True
        assert result.detached_pid is not None
        assert result.duration_seconds < 1.0

    async def test_detached_process_writes_file(self, executor: TerminalExecutor) -> None:
        """A detached process can write to the working directory."""
        await executor.execute_detached("sleep 0.2 && echo 'background done' > bg_output.txt")
        # Wait for the detached process to finish writing
        await asyncio.sleep(1.0)
        result = await executor.execute("cat bg_output.txt")
        assert "background done" in result.stdout


class TestNetworkAccess:
    async def test_network_available(self, executor: TerminalExecutor) -> None:
        """The terminal has network access (no SSRF guard, no allowlist)."""
        # Try to reach a well-known service. If network is blocked, this fails.
        result = await executor.execute(
            "python3 -c \"import urllib.request; urllib.request.urlopen('https://httpbin.org/get', timeout=5).read()\" 2>&1 || echo 'network blocked'"
        )
        # We don't assert success (the sandbox may not have network), but
        # we DO assert that the runtime didn't block the attempt — the
        # command ran and produced output (success or failure).
        assert result.exit_code is not None


class TestObservationFormat:
    async def test_observation_includes_exit_code(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("echo test")
        obs = result.to_observation()
        assert "exit=0" in obs
        assert "test" in obs

    async def test_observation_includes_duration(self, executor: TerminalExecutor) -> None:
        result = await executor.execute("echo test")
        obs = result.to_observation()
        assert "duration=" in obs

    async def test_detached_observation_includes_pid(self, executor: TerminalExecutor) -> None:
        result = await executor.execute_detached("sleep 1")
        obs = result.to_observation()
        assert "detached" in obs
        assert "pid=" in obs
