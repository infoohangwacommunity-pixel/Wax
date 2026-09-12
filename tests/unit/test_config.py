"""Unit tests for wax.core.config (Phase A)."""

from __future__ import annotations

import pytest

from wax.core.config import (
    Environment,
    LogFormat,
    LogLevel,
    WaxSettings,
    load_settings,
    settings_for_testing,
)
from wax.core.exceptions import WaxConfigurationError


class TestSettingsForTesting:
    """settings_for_testing() is the canonical way to build test settings."""

    def test_returns_valid_settings(self) -> None:
        s = settings_for_testing()
        assert isinstance(s, WaxSettings)
        assert s.env == Environment.DEVELOPMENT
        assert s.database_url.startswith("sqlite+aiosqlite:")

    def test_accepts_overrides(self) -> None:
        s = settings_for_testing(env=Environment.PRODUCTION, port=9999)
        assert s.env == Environment.PRODUCTION
        assert s.port == 9999


class TestEnvironmentEnum:
    def test_development_staging_production(self) -> None:
        assert Environment.DEVELOPMENT.value == "development"
        assert Environment.STAGING.value == "staging"
        assert Environment.PRODUCTION.value == "production"


class TestDatabaseUrlValidation:
    def test_sqlite_aiosqlite_allowed(self) -> None:
        s = settings_for_testing(database_url="sqlite+aiosqlite:///./test.db")
        assert s.database_url == "sqlite+aiosqlite:///./test.db"

    def test_postgres_asyncpg_allowed(self) -> None:
        s = settings_for_testing(
            database_url="postgresql+asyncpg://u:p@host:5432/wax"
        )
        assert "postgresql+asyncpg" in s.database_url

    def test_invalid_scheme_rejected(self) -> None:
        with pytest.raises(WaxConfigurationError, match="scheme not supported"):
            settings_for_testing(database_url="mysql+pymysql://u:p@host/db")

    def test_empty_rejected(self) -> None:
        with pytest.raises(WaxConfigurationError, match="must not be empty"):
            settings_for_testing(database_url="")


class TestProductionHardening:
    """Production settings must be strict."""

    def test_missing_secret_key_in_production_raises(self) -> None:
        s = settings_for_testing(
            env=Environment.PRODUCTION,
            secret_key="",
            database_url="postgresql+asyncpg://u:p@host:5432/wax",
        )
        with pytest.raises(WaxConfigurationError, match="WAX_SECRET_KEY"):
            s.enforce_production_hardening()

    def test_default_secret_key_in_production_raises(self) -> None:
        s = settings_for_testing(
            env=Environment.PRODUCTION,
            secret_key="change-me-to-a-real-secret",
            database_url="postgresql+asyncpg://u:p@host:5432/wax",
        )
        with pytest.raises(WaxConfigurationError, match="WAX_SECRET_KEY"):
            s.enforce_production_hardening()

    def test_short_secret_key_in_production_raises(self) -> None:
        s = settings_for_testing(
            env=Environment.PRODUCTION,
            secret_key="tooshort",
            database_url="postgresql+asyncpg://u:p@host:5432/wax",
        )
        with pytest.raises(WaxConfigurationError, match="at least 32 characters"):
            s.enforce_production_hardening()

    def test_sqlite_in_production_raises(self) -> None:
        s = settings_for_testing(
            env=Environment.PRODUCTION,
            secret_key="x" * 64,
            database_url="sqlite+aiosqlite:///./wax.db",
        )
        with pytest.raises(WaxConfigurationError, match="SQLite is not supported in production"):
            s.enforce_production_hardening()

    def test_development_does_not_enforce(self) -> None:
        # Same weak config but in dev — should NOT raise.
        s = settings_for_testing(
            env=Environment.DEVELOPMENT,
            secret_key="",
            database_url="sqlite+aiosqlite:///./wax.db",
        )
        s.enforce_production_hardening()  # no exception

    def test_valid_production_config_passes(self) -> None:
        s = settings_for_testing(
            env=Environment.PRODUCTION,
            secret_key="x" * 64,
            database_url="postgresql+asyncpg://u:p@host:5432/wax",
        )
        s.enforce_production_hardening()  # no exception


class TestPredicates:
    def test_is_production(self) -> None:
        assert settings_for_testing(env=Environment.PRODUCTION).is_production
        assert not settings_for_testing(env=Environment.DEVELOPMENT).is_production

    def test_is_development(self) -> None:
        assert settings_for_testing(env=Environment.DEVELOPMENT).is_development
        assert not settings_for_testing(env=Environment.PRODUCTION).is_development


class TestLoadSettings:
    """load_settings() reads from environment."""

    def test_loads_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clear any WAX_ env vars.
        for k in list(monkeypatch._setenv.keys() if hasattr(monkeypatch, "_setenv") else []):
            if k.startswith("WAX_"):
                monkeypatch.delenv(k, raising=False)

        # Use monkeypatch to set env vars directly.
        monkeypatch.setenv("WAX_ENV", "development")
        monkeypatch.setenv("WAX_DATABASE_URL", "sqlite+aiosqlite:///./test.db")
        monkeypatch.setenv("WAX_SECRET_KEY", "test-key")
        monkeypatch.setenv("WAX_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("WAX_LOG_FORMAT", "json")

        s = load_settings()
        assert s.env == Environment.DEVELOPMENT
        assert s.log_level == LogLevel.DEBUG
        assert s.log_format == LogFormat.JSON
        assert s.secret_key == "test-key"

    def test_load_settings_invalid_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WAX_DATABASE_URL", "mysql://bad")
        with pytest.raises(WaxConfigurationError):
            load_settings()
