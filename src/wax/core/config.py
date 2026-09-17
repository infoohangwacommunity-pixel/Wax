"""WAX configuration schema.

Stripped down for the open-world terminal architecture. Removed:
- capability/approval/resource/provisioning settings
- isolation backend settings
- workspace/artifact/blob settings
- network allowlist settings
- control plane token
- acquisition settings

Kept: runtime, HTTP, database, secrets, LLM providers, terminal executor,
work runner, delivery, WhatsApp, logging, memory.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wax.core.exceptions import WaxConfigurationError


class Environment(StrEnum):
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
    CONSOLE = "console"
    JSON = "json"


class WaxSettings(BaseSettings):
    """WAX configuration, loaded from environment variables."""

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

    # --- LLM providers ---
    llm_default_provider: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_base_url: str = ""
    anthropic_base_url: str = ""
    llm_model: str = ""
    llm_retry_max_attempts: int = 3
    llm_breaker_failure_threshold: int = 5
    llm_breaker_recovery_seconds: float = 30.0
    llm_provider_fallbacks: str = ""
    llm_output_reserve_tokens: int = 4096

    # --- Terminal executor (the open-world environment interface) ---
    # Default foreground process timeout. This is a transport limit, NOT an
    # authority decision — it prevents a single command from hanging the
    # execution forever.
    terminal_timeout_seconds: float = 60.0
    # Maximum output size per command (stdout and stderr each). Prevents a
    # single command from dumping megabytes into the model context.
    terminal_output_max_chars: int = 50_000
    # Root directory for execution working directories. Each execution gets
    # its own subdirectory; the AI's files persist across terminal rounds
    # within that execution and are cleaned up after.
    terminal_working_dir_root: str = "./wax-workspaces"
    # Maximum terminal rounds per message. The AI can call the terminal
    # this many times before the runtime forces a final response.
    terminal_max_rounds: int = 10

    # --- Rate limiting (infrastructure protection, NOT authority) ---
    # Simple per-principal, per-hour message cap. Prevents abuse without
    # a complex subsystem. In-memory only (resets on restart).
    rate_limit_messages_per_hour: int = 30

    # --- Work runner ---
    work_poll_interval_seconds: float = 2.0

    # --- Signal ledger retention ---
    signal_retention_seconds: float = 30 * 86400.0
    signal_max_ledger_rows: int = 100_000

    # --- Audit ledger retention ---
    audit_retention_days: int = 0
    audit_max_rows: int = 0

    # --- Durable outbound delivery ---
    delivery_retry_backoff_seconds: float = 60.0
    delivery_max_attempts: int = 5
    delivery_max_age_seconds: float = 86400.0

    # --- Conversation lifecycle ---
    conversation_idle_timeout_seconds: float = 1800.0  # 30 min
    conversation_archive_after_seconds: float = 7 * 86400.0  # 7 days

    # --- WhatsApp ---
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""

    # --- Convenience predicates ---

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.env == Environment.DEVELOPMENT

    # --- Validation ---

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
        """Raise WaxConfigurationError if production-required settings are missing."""
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
    """Load WAX settings from environment."""
    try:
        settings = WaxSettings()
    except Exception as e:
        raise WaxConfigurationError(f"Failed to load WAX configuration: {e}") from e

    settings.enforce_production_hardening()
    return settings


def settings_for_testing(
    *,
    env: Environment = Environment.DEVELOPMENT,
    database_url: str = "sqlite+aiosqlite:///:memory:",
    **overrides: object,
) -> WaxSettings:
    """Build a WaxSettings instance suitable for tests."""
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
