"""Structured logging for WAX.

Uses `structlog` to produce JSON (production) or console (development) logs.

Design principles:
- NEVER log secrets. The runtime does not log WAX_SECRET_KEY, API keys,
  passwords, tokens, or Authorization headers.
- NEVER log model chain-of-thought. Logs record actions, inputs metadata,
  authorization decisions, outcomes — never the model's private reasoning.
- Logs are structured (key=value), not freeform strings.
- Log level is configurable; default is INFO.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from wax.core.config import LogFormat, LogLevel, WaxSettings

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Substrings that, if they appear in a log event value, must be redacted.
# Match is case-insensitive on key names.
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "connection_string",
)

# Values that should never appear in logs even if the key looks innocuous.
_SENSITIVE_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "sk-",  # OpenAI-style API keys
    "sk_",
    "Bearer ",
)


def _redact_sensitive(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Redact any value whose key matches a sensitive fragment."""
    redacted = "[REDACTED]"
    for key in list(event_dict.keys()):
        key_lower = key.lower()
        if any(frag in key_lower for frag in _SENSITIVE_KEY_FRAGMENTS):
            event_dict[key] = redacted
        else:
            value = event_dict[key]
            if isinstance(value, str) and any(sub in value for sub in _SENSITIVE_VALUE_SUBSTRINGS):
                event_dict[key] = redacted
    return event_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configure_logging(settings: WaxSettings) -> None:
    """Configure structlog and stdlib logging in one consistent way.

    Must be called once at process startup, before any logger is used.
    """
    # stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level.value,
    )

    # structlog processors (shared between renderers)
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_sensitive,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format == LogFormat.JSON:
        # Production: machine-readable JSON
        structlog.configure(
            processors=[
                *shared_processors,
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(settings.log_level)),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
            cache_logger_on_first_use=True,
        )
    else:
        # Development: human-readable console
        structlog.configure(
            processors=[
                *shared_processors,
                structlog.dev.ConsoleRenderer(colors=settings.is_development),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(settings.log_level)),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
            cache_logger_on_first_use=True,
        )


def _level_to_int(level: LogLevel) -> int:
    """Convert our LogLevel enum to stdlib logging level int."""
    return {
        LogLevel.DEBUG: logging.DEBUG,
        LogLevel.INFO: logging.INFO,
        LogLevel.WARNING: logging.WARNING,
        LogLevel.ERROR: logging.ERROR,
        LogLevel.CRITICAL: logging.CRITICAL,
    }[level]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger.

    Use:
        from wax.runtime.logging import get_logger
        log = get_logger(__name__)
        log.info("event", user_id="u_123", action="read")
    """
    return structlog.get_logger(name)
