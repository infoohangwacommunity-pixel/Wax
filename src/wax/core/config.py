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

from enum import StrEnum

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

    # --- LLM providers (Phase O — Intelligence) ---
    llm_default_provider: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    # Base URL for OpenAI-compatible endpoints (vLLM, Together, OpenRouter, ...).
    # Empty string = the provider's official endpoint. Making this configurable
    # keeps the runtime model-independent by configuration, not by code change.
    llm_base_url: str = ""
    # Base URL for the Anthropic API. Deliberately SEPARATE from
    # llm_base_url (constitutional audit fix): a shared default made a
    # mixed-vendor fallback chain silently point the Anthropic candidate
    # at an OpenAI-compatible proxy. Empty string = the official endpoint.
    anthropic_base_url: str = ""
    # Default model override (provider-specific name).
    llm_model: str = ""
    # Provider resilience (wired via intelligence.resilience.ResilientProvider).
    llm_retry_max_attempts: int = 3
    llm_breaker_failure_threshold: int = 5
    llm_breaker_recovery_seconds: float = 30.0
    # Tool-calling rounds per message. The AI may request capabilities; the
    # runtime bounds how many request/result exchanges one message may spawn.
    max_tool_rounds: int = 5

    # --- Code execution isolation ---
    # Which sandbox boundary runs code.run: "auto" (namespace sandbox when
    # the host supports unprivileged user namespaces, else subprocess —
    # LOUD fallback), "namespace" (require it), "subprocess" (legacy).
    isolation_backend: str = "auto"
    # Resource-governance rlimits for sandboxed code (anti-bomb budgets,
    # NOT the security boundary — that is the namespace itself).
    isolation_memory_limit_mb: int = 512
    isolation_max_processes: int = 64
    isolation_max_file_bytes: int = 16_000_000

    # --- Runtime mechanisms ---
    # Poll interval for the in-process background work runner (Phase V).
    work_poll_interval_seconds: float = 2.0
    # Root directory for dynamically provisioned ephemeral resources (Phase S).
    provisioning_root: str = "./wax-resources"
    # Root directory for the content-addressed blob store (P0-Workspace).
    # workspace.snapshot persists file bytes here (keyed by sha256) so
    # workspace.restore works even after the source workspace is released.
    snapshot_blob_root: str = "./wax-blobs"
    # Shared operator token for the server-rendered control plane
    # (ADR-0048). When empty: the dashboard is enabled in development (with
    # a loud warning) and REFUSES to serve in production. When set: routes
    # require this token (Authorization: Bearer, X-Control-Token, ?token=
    # or the session cookie issued after login).
    control_plane_token: str = ""
    # Maximum active provisioned resources per principal (Phase S limit).
    provisioning_max_active_per_principal: int = 5
    # Character budget for evidence assembly (~4 chars/token). The runtime
    # fills objective > conversation > memory evidence up to this budget
    # and announces truncation honestly (ADR-0012). When the selected
    # provider advertises a context limit, the budget is derived from it
    # instead (see ADR-0015).
    context_char_budget: int = 24000
    # Reserved output tokens subtracted from a provider-advertised context
    # limit before the evidence budget is derived.
    llm_output_reserve_tokens: int = 4096

    # --- Provider failover (ADR-0024, mission §33/§34) ---
    # Comma-separated provider kinds to try IN ORDER after the default
    # provider fails (e.g. "anthropic,mock"). Empty = primary only.
    # Each candidate gets its own retry+breaker; failover happens after a
    # candidate's own retry budget exhausts. Misconfigured kinds fail at
    # boot (loud), not at 3am.
    llm_provider_fallbacks: str = ""

    # --- Human approval primitive (ADR-0013) ---
    # How long a pending approval stays decidable before it honestly
    # expires (swept by the maintenance loop).
    approval_expiry_seconds: float = 86400.0

    # --- Signal ledger retention (ADR-0015) ---
    # Signals older than this are pruned by the maintenance loop — UNLESS
    # some pending event-wake work item can still be woken by them.
    signal_retention_seconds: float = 30 * 86400.0
    # Bounded-storage bound: when the ledger exceeds this many rows, the
    # oldest prune-safe rows beyond the bound are removed.
    signal_max_ledger_rows: int = 100_000

    # --- Durable outbound delivery (ADR-0021) ---
    # A completed result whose interface send fails becomes recoverable
    # state: the maintenance loop retries with exponential backoff until
    # delivered, attempts are exhausted, or the deliverability horizon
    # passes (interface-agnostic; 24h matches the WhatsApp
    # customer-service window for template-less sends).
    delivery_retry_backoff_seconds: float = 60.0
    delivery_max_attempts: int = 5
    delivery_max_age_seconds: float = 86400.0

    # --- Capability idempotency (CV-19) ----------------------------------
    # Lease for an `executing` idempotency claim. An abandoned claim
    # (process died between claim and completion) becomes takeable again
    # after this many seconds — honest at-least-once recovery.
    capability_idempotency_claim_seconds: float = 900.0

    # --- Artifact acquisition (ADR-0015) ---
    # Comma-separated hostnames a workspace.acquire may download from.
    # Empty list = acquisition disabled (honest refusal).
    acquisition_allowed_hosts: str = "files.pythonhosted.org,github.com,objects.githubusercontent.com,raw.githubusercontent.com,registry.npmjs.org"
    # Hard cap on artifact size and download time.
    acquisition_max_bytes: int = 52_428_800  # 50 MiB
    acquisition_timeout_seconds: float = 60.0

    # --- WhatsApp (Phase Q — Interface) ---
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    # CV-17 fix: no guessable default. An empty value fail-fasts at the
    # adapter wiring point (WhatsAppClient requires a real verify token);
    # a default literal would silently accept webhook verification with a
    # publicly-known token.
    whatsapp_verify_token: str = ""

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
