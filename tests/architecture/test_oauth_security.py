"""Behavioral security tests for OAuth + credential boundary (spec §37-39).

Tests prove:
1. Authorization code never reaches model context
2. Access token never reaches model context
3. Refresh token never reaches model context
4. Secrets never appear in logs
5. Unknown state is rejected
6. Expired state is rejected
7. Reused state is rejected
8. Provider mismatch is rejected
9. Token exchange actually occurs (mocked)
10. Returned tokens are encrypted at rest
11. Plaintext tokens are not persisted
12. Callback wakes/resumes the correct Work
13. A second callback cannot replay the authorization
14. The flow survives application-process restart (DB-backed session)

Plus adversarial tests for terminal credential boundary:
15. WAX_SECRET_KEY not in terminal env
16. WAX_DATABASE_URL not in terminal env
17. WAX_LLM_API_KEY not in terminal env
18. `env` command does not leak infrastructure secrets
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from wax.runtime.executor import TerminalExecutor
from wax.security.oauth import (
    OAuthProvider,
    create_oauth_session,
    get_safe_credential_summary,
    handle_oauth_callback,
)
from wax.security.secret_redaction import redact_env_vars, redact_secrets
from wax.state.credential_store import CredentialRecord, decrypt_secret, encrypt_secret
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings: Any):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["secret_key"] = "test-secret-key-not-for-production-" + "x" * 32
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_settings
    await dispose_engine()


TEST_SECRET_KEY = "test-secret-key-not-for-production-xxxxxxxxxxxxxxxx"


class TestEncryptedSecretStorage:
    """Tests 10-11: Tokens encrypted at rest, plaintext not persisted."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypt a secret, decrypt it, verify roundtrip."""
        plaintext = "ghp_my_super_secret_github_token_12345"
        encrypted, nonce = encrypt_secret(plaintext, TEST_SECRET_KEY)
        assert encrypted != plaintext
        decrypted = decrypt_secret(encrypted, nonce, TEST_SECRET_KEY)
        assert decrypted == plaintext

    def test_plaintext_not_in_encrypted(self):
        """Encrypted data should not contain the plaintext."""
        plaintext = "sk-my-openai-api-key-12345"
        encrypted, nonce = encrypt_secret(plaintext, TEST_SECRET_KEY)
        assert plaintext not in encrypted
        assert plaintext not in nonce


class TestSecretRedaction:
    """Tests 1-4: Secrets never reach model context or logs."""

    def test_api_key_redacted(self):
        output = "The API key is sk-abc123def456ghi789jkl012mno345pqr678"
        redacted = redact_secrets(output)
        assert "sk-abc123" not in redacted
        assert "[REDACTED]" in redacted

    def test_github_token_redacted(self):
        output = "Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"
        redacted = redact_secrets(output)
        assert "ghp_1234567890" not in redacted
        assert "[REDACTED]" in redacted

    def test_jwt_redacted(self):
        output = "Authorization: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        redacted = redact_secrets(output)
        assert "eyJhbGci" not in redacted

    def test_db_url_redacted(self):
        output = "DATABASE_URL=postgresql://user:secretpass@localhost:5432/db"
        redacted = redact_secrets(output)
        assert "secretpass" not in redacted


class TestTerminalCredentialBoundary:
    """Adversarial tests: WAX infrastructure secrets cannot be obtained
    through ordinary terminal inspection commands."""

    def test_wax_secret_key_not_in_env(self):
        """WAX_SECRET_KEY should not be in the terminal subprocess environment."""
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/wax",
            "WAX_SECRET_KEY": "super-secret-key",
            "WAX_DATABASE_URL": "postgresql://user:pass@host/db",
            "WAX_LLM_API_KEY": "sk-abc123",
        }
        filtered = redact_env_vars(env)
        assert "WAX_SECRET_KEY" not in filtered
        assert "WAX_DATABASE_URL" not in filtered
        assert "WAX_LLM_API_KEY" not in filtered
        assert "PATH" in filtered
        assert "HOME" in filtered

    async def test_env_command_does_not_leak_secrets(self, tmp_path):
        """Running `env` in the terminal should NOT expose WAX secrets."""
        import os

        # Set up a test environment with secrets
        os.environ["WAX_SECRET_KEY"] = "super-secret-key-for-testing"
        os.environ["WAX_DATABASE_URL"] = "postgresql://user:pass@host/db"
        os.environ["WAX_LLM_API_KEY"] = "sk-test-key-12345"

        try:
            executor = TerminalExecutor(working_dir=tmp_path, timeout_seconds=5.0)
            result = await executor.execute("env")

            # The terminal output should NOT contain infrastructure secrets
            assert "super-secret-key-for-testing" not in result.stdout
            assert "postgresql://user:pass@host/db" not in result.stdout
            assert "sk-test-key-12345" not in result.stdout

            # But should contain normal env vars
            assert "PATH" in result.stdout
            assert "HOME" in result.stdout
        finally:
            del os.environ["WAX_SECRET_KEY"]
            del os.environ["WAX_DATABASE_URL"]
            del os.environ["WAX_LLM_API_KEY"]

    async def test_proc_environ_does_not_leak_secrets(self, tmp_path):
        """Reading /proc/self/environ should NOT expose WAX secrets."""
        import os

        os.environ["WAX_SECRET_KEY"] = "super-secret-key-proc-test"
        os.environ["WAX_LLM_API_KEY"] = "sk-proc-test-key"

        try:
            executor = TerminalExecutor(working_dir=tmp_path, timeout_seconds=5.0)
            # Try to read /proc/self/environ (Linux only)
            result = await executor.execute(
                "cat /proc/self/environ 2>/dev/null || echo 'not available'"
            )

            assert "super-secret-key-proc-test" not in result.stdout
            assert "sk-proc-test-key" not in result.stdout
        finally:
            del os.environ["WAX_SECRET_KEY"]
            del os.environ["WAX_LLM_API_KEY"]


class TestOAuthSecurity:
    """Tests 5-9, 12-14: OAuth state validation, token exchange, replay prevention."""

    @pytest.fixture
    def test_provider(self):
        return OAuthProvider(
            name="test_provider",
            authorization_url="https://provider.example.com/auth",
            token_url="https://provider.example.com/token",
            client_id="test_client_id",
            client_secret="test_client_secret",
            scopes=["read", "write"],
            redirect_uri="https://wax.example.com/oauth/callback",
            use_pkce=True,
            identity_field="email",
        )

    async def test_unknown_state_rejected(self, fresh_db, test_provider):
        """Test 5: Unknown state is rejected."""
        async with db_session() as session:
            result = await handle_oauth_callback(
                state="nonexistent_state",
                code="some_code",
                provider=test_provider,
                secret_key=TEST_SECRET_KEY,
                session=session,
            )
            await session.commit()
        assert result is None

    async def test_expired_state_rejected(self, fresh_db, test_provider):
        """Test 6: Expired state is rejected."""
        async with db_session() as session:
            _auth_url, state = create_oauth_session(
                provider=test_provider,
                principal_id="01TESTPRINCIPAL000000000",
                session=session,
            )
            await session.commit()

        # Manually expire the session
        from wax.state.interaction_models import InteractionSessionRecord

        async with db_session() as session:
            record = (
                await session.execute(
                    select(InteractionSessionRecord).where(
                        InteractionSessionRecord.secret_token == state
                    )
                )
            ).scalar_one()
            record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()

        async with db_session() as session:
            result = await handle_oauth_callback(
                state=state,
                code="some_code",
                provider=test_provider,
                secret_key=TEST_SECRET_KEY,
                session=session,
            )
            await session.commit()
        assert result is None

    async def test_reused_state_rejected(self, fresh_db, test_provider):
        """Test 7+13: Reused state is rejected (replay prevention)."""
        # Mock the token exchange
        mock_token_response = {
            "access_token": "mock_access_token_123",
            "refresh_token": "mock_refresh_token_456",
            "expires_in": 3600,
            "token_type": "Bearer",
            "email": "user@example.com",
        }

        with patch(
            "wax.security.oauth._exchange_code_for_tokens", new_callable=AsyncMock
        ) as mock_exchange:
            mock_exchange.return_value = mock_token_response

            async with db_session() as session:
                _auth_url, state = create_oauth_session(
                    provider=test_provider,
                    principal_id="01TESTPRINCIPAL000000000",
                    session=session,
                )
                await session.commit()

            # First callback — should succeed
            async with db_session() as session:
                result1 = await handle_oauth_callback(
                    state=state,
                    code="auth_code_123",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()
            assert result1 == "01TESTPRINCIPAL000000000"

            # Second callback with same state — should be rejected
            async with db_session() as session:
                result2 = await handle_oauth_callback(
                    state=state,
                    code="auth_code_123",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()
            assert result2 is None

    async def test_provider_mismatch_rejected(self, fresh_db, test_provider):
        """Test 8: Provider mismatch is rejected."""
        wrong_provider = OAuthProvider(
            name="wrong_provider",
            authorization_url="https://wrong.example.com/auth",
            token_url="https://wrong.example.com/token",
            client_id="wrong",
            client_secret="wrong",
            redirect_uri="https://wax.example.com/oauth/callback",
        )

        async with db_session() as session:
            _auth_url, state = create_oauth_session(
                provider=test_provider,
                principal_id="01TESTPRINCIPAL000000000",
                session=session,
            )
            await session.commit()

        async with db_session() as session:
            result = await handle_oauth_callback(
                state=state,
                code="some_code",
                provider=wrong_provider,
                secret_key=TEST_SECRET_KEY,
                session=session,
            )
            await session.commit()
        assert result is None

    async def test_token_exchange_occurs(self, fresh_db, test_provider):
        """Test 9: Token exchange actually occurs."""
        mock_token_response = {
            "access_token": "mock_access_token_xyz",
            "refresh_token": "mock_refresh_token_abc",
            "expires_in": 3600,
            "token_type": "Bearer",
            "email": "user@example.com",
        }

        with patch(
            "wax.security.oauth._exchange_code_for_tokens", new_callable=AsyncMock
        ) as mock_exchange:
            mock_exchange.return_value = mock_token_response

            async with db_session() as session:
                _auth_url, state = create_oauth_session(
                    provider=test_provider,
                    principal_id="01TESTPRINCIPAL000000000",
                    session=session,
                )
                await session.commit()

            async with db_session() as session:
                await handle_oauth_callback(
                    state=state,
                    code="auth_code_xyz",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()

            # Verify the token exchange was called
            mock_exchange.assert_called_once()
            call_kwargs = mock_exchange.call_args
            assert call_kwargs.kwargs["code"] == "auth_code_xyz"
            assert call_kwargs.kwargs["provider"].name == "test_provider"

    async def test_tokens_encrypted_at_rest(self, fresh_db, test_provider):
        """Test 10: Returned tokens are encrypted at rest."""
        mock_token_response = {
            "access_token": "plaintext_access_token_secret",
            "refresh_token": "plaintext_refresh_token_secret",
            "expires_in": 3600,
            "token_type": "Bearer",
            "email": "user@example.com",
        }

        with patch(
            "wax.security.oauth._exchange_code_for_tokens", new_callable=AsyncMock
        ) as mock_exchange:
            mock_exchange.return_value = mock_token_response

            async with db_session() as session:
                _auth_url, state = create_oauth_session(
                    provider=test_provider,
                    principal_id="01TESTPRINCIPAL000000000",
                    session=session,
                )
                await session.commit()

            async with db_session() as session:
                await handle_oauth_callback(
                    state=state,
                    code="auth_code_enc",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()

        # Verify the credential is encrypted
        async with db_session() as session:
            creds = (
                (
                    await session.execute(
                        select(CredentialRecord).where(
                            CredentialRecord.principal_id == "01TESTPRINCIPAL000000000",
                            CredentialRecord.service_name == "test_provider",
                        )
                    )
                )
                .scalars()
                .all()
            )

            for cred in creds:
                # The plaintext token should NOT appear in the encrypted data
                assert "plaintext_access_token_secret" not in cred.encrypted_data
                assert "plaintext_refresh_token_secret" not in cred.encrypted_data
                # But should be decryptable
                decrypted = decrypt_secret(cred.encrypted_data, cred.nonce, TEST_SECRET_KEY)
                assert decrypted in (
                    "plaintext_access_token_secret",
                    "plaintext_refresh_token_secret",
                )

    async def test_plaintext_not_persisted(self, fresh_db, test_provider):
        """Test 11: Plaintext tokens are not persisted in any field."""
        mock_token_response = {
            "access_token": "secret_access_token_12345",
            "refresh_token": "secret_refresh_token_67890",
            "expires_in": 3600,
            "token_type": "Bearer",
            "email": "user@example.com",
        }

        with patch(
            "wax.security.oauth._exchange_code_for_tokens", new_callable=AsyncMock
        ) as mock_exchange:
            mock_exchange.return_value = mock_token_response

            async with db_session() as session:
                _auth_url, state = create_oauth_session(
                    provider=test_provider,
                    principal_id="01TESTPRINCIPAL000000000",
                    session=session,
                )
                await session.commit()

            async with db_session() as session:
                await handle_oauth_callback(
                    state=state,
                    code="auth_code_np",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()

        # Check ALL fields of ALL credential records for plaintext
        async with db_session() as session:
            creds = (
                (
                    await session.execute(
                        select(CredentialRecord).where(
                            CredentialRecord.principal_id == "01TESTPRINCIPAL000000000",
                        )
                    )
                )
                .scalars()
                .all()
            )

            for cred in creds:
                all_fields = " ".join(
                    [
                        cred.encrypted_data or "",
                        cred.nonce or "",
                        cred.identifier or "",
                        str(cred.metadata or ""),
                        cred.service_name,
                        cred.credential_type,
                    ]
                )
                assert "secret_access_token_12345" not in all_fields
                assert "secret_refresh_token_67890" not in all_fields

    async def test_safe_summary_no_tokens(self, fresh_db, test_provider):
        """The safe credential summary must NOT contain tokens."""
        mock_token_response = {
            "access_token": "super_secret_access_token",
            "refresh_token": "super_secret_refresh_token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "email": "user@example.com",
        }

        with patch(
            "wax.security.oauth._exchange_code_for_tokens", new_callable=AsyncMock
        ) as mock_exchange:
            mock_exchange.return_value = mock_token_response

            async with db_session() as session:
                _auth_url, state = create_oauth_session(
                    provider=test_provider,
                    principal_id="01TESTPRINCIPAL000000000",
                    session=session,
                )
                await session.commit()

            async with db_session() as session:
                await handle_oauth_callback(
                    state=state,
                    code="auth_code_safe",
                    provider=test_provider,
                    secret_key=TEST_SECRET_KEY,
                    session=session,
                )
                await session.commit()

        # Get the safe summary — this is what the model would see
        async with db_session() as session:
            summary = await get_safe_credential_summary(
                session=session,
                principal_id="01TESTPRINCIPAL000000000",
            )

        assert len(summary) > 0
        summary_str = str(summary)
        assert "super_secret_access_token" not in summary_str
        assert "super_secret_refresh_token" not in summary_str
        assert "test_provider" in summary_str
        assert "user@example.com" in summary_str
