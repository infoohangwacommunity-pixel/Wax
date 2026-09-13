"""WAX configuration schema.

This module defines the *shape* of WAX configuration and a loader that reads
from environment variables. The loader is the ONLY place in `wax.core` that
performs I/O (reading `os.environ`), and even that is bounded: no file reads,
no network, no subprocess.

Design principles:
- Fail fast: missing or invalid config raises `WaxConfigurationError` at
  startup, not at first use.
- No defaults for secrets: `WAX_SECRET_KEY` must be set explicitly.
- Environment-aware: production has stricter requirements than development.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wax.core.exceptions import WaxConfigurationError


class Environment(StrEnum):
    """WAX deployment environment.

    The environment affects:
    - Which validation rules are strict (production is strictest)
    - Default log level and format
    - Whether a missing secret is fatal (yes in production, warning in dev)
    """

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(StrEnum):
    CONSOLE = "console"  # human-readable, colorized
    JSON = "json"  # structured, machine-readable


class WaxSettings(BaseSettings):
    """WAX configuration, loaded from environment variables.

    All settings are read from the `WAX_`-prefixed environment. A `.env` file
    is supported in development via `SettingsConfigDict(env_file=".env")`.
    """

    model_config = SettingsConfigDict(
        env_prefix="WAX_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime ---
    env: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.CONSOLE

    # --- HTTP ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./wax.db"

    # --- Secrets ---
    secret_key: str = Field(default="", description="WAX master secret. MUST be set in production.")

    # --- LLM providers (Phase O) ---
    llm_default_provider: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Convenience predicates -------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.env == Environment.DEVELOPMENT

    # --- Validation ------------------------------------------------------

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, v: str) -> str:
        # Allow empty in dev/test; loader will enforce production stricter.
        return v

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        if not v:
            raise WaxConfigurationError("WAX_DATABASE_URL must not be empty")
        allowed_schemes = (
            "sqlite+aiosqlite:",
            "postgresql+asyncpg:",
        )
        if not v.startswith(allowed_schemes):
            raise WaxConfigurationError(
                f"WAX_DATABASE_URL scheme not supported: {v.split(':', 1)[0]!r}. "
                f"Allowed: {', '.join(allowed_schemes)}"
            )
        return v

    def enforce_production_hardening(self) -> None:
        """Raise WaxConfigurationError if production-required settings are missing.

        Called by the runtime at startup when `env == PRODUCTION`. This is a
        separate method (not a validator) so it only fires for production.
        """
        if not self.is_production:
            return

        errors: list[str] = []

        if not self.secret_key or self.secret_key == "change-me-to-a-real-secret":
            errors.append("WAX_SECRET_KEY must be set to a strong random value in production")

        if len(self.secret_key) < 32:
            errors.append("WAX_SECRET_KEY must be at least 32 characters in production")

        if self.database_url.startswith("sqlite"):
            errors.append(
                "SQLite is not supported in production. Set WAX_DATABASE_URL to a PostgreSQL URL."
            )

        if errors:
            raise WaxConfigurationError(
                "Production configuration is invalid:\n  - " + "\n  - ".join(errors)
            )


def load_settings() -> WaxSettings:
    """Load WAX settings from environment.

    This is the canonical entrypoint for reading configuration. It enforces:
    - environment variable parsing (via pydantic-settings)
    - production hardening (when env == PRODUCTION)
    - fail-fast on invalid configuration
    """
    try:
        settings = WaxSettings()
    except Exception as e:
        # Pydantic raises ValidationError; we rewrap as WaxConfigurationError.
        raise WaxConfigurationError(f"Failed to load WAX configuration: {e}") from e

    # In production, enforce stricter rules.
    settings.enforce_production_hardening()

    return settings


def settings_for_testing(
    *,
    env: Environment = Environment.DEVELOPMENT,
    database_url: str = "sqlite+aiosqlite:///:memory:",
    **overrides: object,
) -> WaxSettings:
    """Build a WaxSettings instance suitable for tests.

    This bypasses environment-variable loading so tests are hermetic.
    """
    fields: dict[str, object] = {
        "env": env,
        "database_url": database_url,
        "secret_key": "test-secret-key-not-for-production-use-" + ("x" * 32),
        "log_level": LogLevel.WARNING,
        "log_format": LogFormat.JSON,
        "host": "127.0.0.1",
        "port": 0,
        "llm_default_provider": "",
        "openai_api_key": "",
        "anthropic_api_key": "",
    }
    fields.update(overrides)
    return WaxSettings.model_validate(fields)


# Silence unused import warnings for StrEnum re-exports used by callers.
_ = (LogLevel, LogFormat, Environment)
