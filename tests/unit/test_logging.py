"""Unit tests for wax.runtime.logging (Phase A).

Tests verify:
- structured logging produces expected fields
- secret redaction works correctly
- console vs JSON formats are switchable
"""

from __future__ import annotations

import io
import json

import pytest
from structlog.types import EventDict

from wax.core.config import LogFormat, LogLevel, settings_for_testing
from wax.runtime.logging import _redact_sensitive, configure_logging, get_logger


def _capture_stdout() -> io.StringIO:
    buf = io.StringIO()
    return buf


class TestSecretRedaction:
    """The _redact_sensitive processor must scrub sensitive data."""

    def _redact(self, event_dict: EventDict) -> EventDict:
        return _redact_sensitive(None, "info", event_dict)

    def test_redacts_secret_key(self) -> None:
        out = self._redact({"secret_key": "abc123", "event": "test"})
        assert out["secret_key"] == "[REDACTED]"
        assert out["event"] == "test"

    def test_redacts_password(self) -> None:
        out = self._redact({"password": "p@ssw0rd"})
        assert out["password"] == "[REDACTED]"

    def test_redacts_token(self) -> None:
        out = self._redact({"auth_token": "xyz"})
        assert out["auth_token"] == "[REDACTED]"

    def test_redacts_api_key(self) -> None:
        out = self._redact({"openai_api_key": "sk-abc123"})
        assert out["openai_api_key"] == "[REDACTED]"

    def test_redacts_authorization(self) -> None:
        out = self._redact({"authorization": "Bearer foo"})
        assert out["authorization"] == "[REDACTED]"

    def test_redacts_value_substring_sk_key(self) -> None:
        # Even if the key is innocuous, a value containing "sk-" should be redacted.
        out = self._redact({"user_input": "my key is sk-12345"})
        assert out["user_input"] == "[REDACTED]"

    def test_redacts_value_substring_bearer(self) -> None:
        out = self._redact({"header_value": "Bearer abc"})
        assert out["header_value"] == "[REDACTED]"

    def test_preserves_non_sensitive_values(self) -> None:
        out = self._redact({"user_id": "u_123", "action": "read", "count": 42})
        assert out["user_id"] == "u_123"
        assert out["action"] == "read"
        assert out["count"] == 42

    def test_handles_nested_dict_cautiously(self) -> None:
        # structlog passes flat event dicts to processors by default; nested
        # data is the caller's responsibility. We test the documented contract:
        # top-level sensitive keys are redacted.
        out = self._redact({"metadata": {"password": "abc"}, "secret": "x"})
        assert out["secret"] == "[REDACTED]"
        # Nested values are not automatically scrubbed — caller responsibility.
        # (Documented limitation. A future improvement could walk nested dicts.)
        assert out["metadata"]["password"] == "abc"


class TestLoggingConfiguration:
    """configure_logging() must set up structlog correctly."""

    def test_json_format_produces_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = settings_for_testing(log_format=LogFormat.JSON, log_level=LogLevel.INFO)
        configure_logging(settings)
        log = get_logger("test")
        log.info("event_name", key="value")
        captured = capsys.readouterr()
        assert captured.out
        parsed = json.loads(captured.out.strip().split("\n")[-1])
        assert parsed["event"] == "event_name"
        assert parsed["key"] == "value"
        assert parsed["level"] == "info"
        assert "timestamp" in parsed

    def test_console_format_produces_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = settings_for_testing(log_format=LogFormat.CONSOLE, log_level=LogLevel.INFO)
        configure_logging(settings)
        log = get_logger("test")
        log.info("event_name", key="value")
        captured = capsys.readouterr()
        assert "event_name" in captured.out
        # Console output is NOT JSON.
        assert not captured.out.strip().startswith("{")

    def test_secret_redacted_in_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        settings = settings_for_testing(log_format=LogFormat.JSON, log_level=LogLevel.INFO)
        configure_logging(settings)
        log = get_logger("test")
        log.info("auth_event", api_key="sk-secret", user_id="u_123")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip().split("\n")[-1])
        assert parsed["api_key"] == "[REDACTED]"
        assert parsed["user_id"] == "u_123"
