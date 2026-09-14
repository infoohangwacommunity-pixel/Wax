"""Integration tests for Phase I (Isolation)."""

from __future__ import annotations

import pytest

from wax.isolation.contracts import (
    IsolationKind,
    IsolationRequest,
)
from wax.isolation.service import IsolationService
from wax.isolation.subprocess_boundary import SubprocessBoundary


class TestSubprocessBoundary:
    """The simplest real isolation: separate process, restricted env, timeout."""

    @pytest.fixture
    def boundary(self) -> SubprocessBoundary:
        return SubprocessBoundary(default_timeout=5.0)

    async def test_execute_simple_python(self, boundary: SubprocessBoundary) -> None:
        result = await boundary.execute(
            IsolationRequest(
                code="print('hello from subprocess')",
                language="python",
                timeout_seconds=2.0,
            )
        )
        assert result.exit_code == 0
        assert "hello from subprocess" in result.stdout
        assert not result.timed_out

    async def test_execute_shell(self, boundary: SubprocessBoundary) -> None:
        result = await boundary.execute(
            IsolationRequest(
                code="echo 'shell hello' && echo 'shell stderr' >&2",
                language="shell",
                timeout_seconds=2.0,
            )
        )
        assert result.exit_code == 0
        assert "shell hello" in result.stdout
        assert "shell stderr" in result.stderr

    async def test_timeout_kills_process(self, boundary: SubprocessBoundary) -> None:
        """A long-running subprocess must be killed when timeout fires."""
        result = await boundary.execute(
            IsolationRequest(
                code="import time; time.sleep(10); print('should never see this')",
                language="python",
                timeout_seconds=0.5,
            )
        )
        assert result.timed_out
        assert result.exit_code == 124
        assert "should never see this" not in result.stdout

    async def test_exit_code_propagates(self, boundary: SubprocessBoundary) -> None:
        result = await boundary.execute(
            IsolationRequest(
                code="import sys; sys.exit(42)",
                language="python",
                timeout_seconds=2.0,
            )
        )
        assert result.exit_code == 42

    async def test_stderr_captured(self, boundary: SubprocessBoundary) -> None:
        result = await boundary.execute(
            IsolationRequest(
                code="import sys; print('to stderr', file=sys.stderr)",
                language="python",
                timeout_seconds=2.0,
            )
        )
        assert "to stderr" in result.stderr

    async def test_output_truncation(self, boundary: SubprocessBoundary) -> None:
        """Output exceeding max_output_bytes is truncated."""
        result = await boundary.execute(
            IsolationRequest(
                code="print('A' * 10000)",
                language="python",
                timeout_seconds=2.0,
                max_output_bytes=100,
            )
        )
        assert result.truncated
        assert len(result.stdout) <= 100

    async def test_env_does_not_inherit_secrets(self, boundary: SubprocessBoundary) -> None:
        """The subprocess env must NOT include parent-process secrets."""
        import os

        # Set a fake secret on the parent env
        os.environ["WAX_TEST_SECRET"] = "super-secret-value"
        try:
            result = await boundary.execute(
                IsolationRequest(
                    code="import os; print(os.environ.get('WAX_TEST_SECRET', 'NOT_SET'))",
                    language="python",
                    timeout_seconds=2.0,
                )
            )
            assert "NOT_SET" in result.stdout
            assert "super-secret-value" not in result.stdout
        finally:
            os.environ.pop("WAX_TEST_SECRET", None)

    async def test_env_passes_explicit_vars(self, boundary: SubprocessBoundary) -> None:
        """Explicit env vars (filtered for secrets) are passed through."""
        result = await boundary.execute(
            IsolationRequest(
                code="import os; print(os.environ.get('MY_VAR', 'NOT_SET'))",
                language="python",
                timeout_seconds=2.0,
                env={"MY_VAR": "my_value"},
            )
        )
        assert "my_value" in result.stdout

    async def test_env_filters_secret_keys(self, boundary: SubprocessBoundary) -> None:
        """Keys that look like secrets are silently dropped."""
        result = await boundary.execute(
            IsolationRequest(
                code="import os; print(os.environ.get('API_KEY', 'NOT_SET'))",
                language="python",
                timeout_seconds=2.0,
                env={"API_KEY": "should-be-filtered"},
            )
        )
        assert "NOT_SET" in result.stdout
        assert "should-be-filtered" not in result.stdout


class TestIsolationService:
    async def test_default_is_subprocess(self) -> None:
        svc = IsolationService()
        assert svc.boundary.kind == IsolationKind.SUBPROCESS

    async def test_for_kind_subprocess(self) -> None:
        svc = IsolationService.for_kind(IsolationKind.SUBPROCESS)
        assert svc.boundary.kind == IsolationKind.SUBPROCESS

    async def test_for_kind_noop_rejected(self) -> None:
        from wax.core.exceptions import WaxConfigurationError

        with pytest.raises(WaxConfigurationError, match="tests only"):
            IsolationService.for_kind(IsolationKind.NOOP)

    async def test_for_kind_container_not_yet_implemented(self) -> None:
        from wax.core.exceptions import WaxConfigurationError

        with pytest.raises(WaxConfigurationError, match="not yet implemented"):
            IsolationService.for_kind(IsolationKind.CONTAINER)


class TestIsolationRequest:
    def test_defaults(self) -> None:
        req = IsolationRequest(code="print('hi')")
        assert req.language == "python"
        assert req.timeout_seconds == 10.0
        assert req.max_output_bytes == 1_000_000
        assert req.env is None
