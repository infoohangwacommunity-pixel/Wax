"""Behavioral architecture tests (spec §37-39).

These tests prove the architecture through behavior, not just imports.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.executor import TerminalExecutor
from wax.runtime.services import RuntimeServices
from wax.security.secret_redaction import redact_env_vars, redact_secrets
from wax.state.credential_store import decrypt_secret, encrypt_secret
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings: Any):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["terminal_working_dir_root"] = "/tmp/wax-test-workspaces"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_settings
    await dispose_engine()


@pytest.fixture
def services(test_settings: Any):
    return RuntimeServices.build(test_settings)


@pytest.fixture
def bridge(services: Any):
    mock = MockLLMProvider()
    intel = IntelligenceService(mock)
    return RuntimeBridge(intelligence=intel, services=services)


def _request(*, message_id: str = "msg-1", text: str = "Hello WAX", phone: str = "+2349138153604"):
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id=phone,
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )


class TestOpenWorldTerminal:
    """Test 1: Terminal operates without capability registration."""

    async def test_terminal_executes_without_registration(self, tmp_path) -> None:
        """A terminal command executes without any capability registry."""
        executor = TerminalExecutor(working_dir=tmp_path, timeout_seconds=5.0)
        result = await executor.execute("echo 'hello world'")
        assert result.exit_code == 0
        assert "hello world" in result.stdout


class TestDetachedProcessSurvival:
    """Test 2: Detached process survives intelligence turn end."""

    async def test_detached_process_survives(self, tmp_path) -> None:
        """A detached process should remain alive after the executor finishes."""
        executor = TerminalExecutor(working_dir=tmp_path, timeout_seconds=5.0)
        result = await executor.execute_detached("sleep 30")
        assert result.detached is True
        assert result.detached_pid is not None

        # The process should still be alive
        import os

        try:
            os.kill(result.detached_pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False
        assert alive, "Detached process was killed — it should survive"

        # Clean up
        await executor.cleanup_detached()


class TestPhoneNormalization:
    """Test 19: Identity — same phone resolves to same principal."""

    async def test_same_phone_same_principal(self, fresh_db, bridge) -> None:
        """+234... and 234... should resolve to the same principal."""
        async with db_session() as session:
            r1 = await bridge.process(session, _request(message_id="msg-1", phone="+2349138153604"))
            await session.commit()
        async with db_session() as session:
            r2 = await bridge.process(session, _request(message_id="msg-2", phone="2349138153604"))
            await session.commit()

        assert r1.principal_id == r2.principal_id, "Same phone should resolve to same principal"


class TestMemoryPersistence:
    """Test 10: Memory actually persists and influences later context."""

    async def test_memory_persists_across_conversations(self, fresh_db, services) -> None:
        """Store a memory, start a new conversation, verify it's retrieved."""
        from wax.memory.contracts import MemoryCreate, MemoryKind
        from wax.memory.repository import MemoryRepository

        # Store a memory
        async with db_session() as session:
            repo = MemoryRepository(session)
            await repo.create(
                MemoryCreate(
                    principal_id="01TESTPRINCIPAL000000000",
                    kind=MemoryKind.FACT,
                    content={"text": "User's name is Kennedy"},
                    provenance="user_statement",
                    confidence=1.0,
                    importance=0.9,
                    observed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        # Retrieve it
        async with db_session() as session:
            repo = MemoryRepository(session)
            results = await repo.search_relevant(
                principal_id="01TESTPRINCIPAL000000000",
                query="what is the user's name",
                limit=5,
            )
            assert len(results) > 0
            record, _score = results[0]
            content = (
                record.content
                if isinstance(record.content, dict)
                else {"text": str(record.content)}
            )
            assert "Kennedy" in content.get("text", "")


class TestMemorySupersession:
    """Test 11: Stale memory is superseded by current memory."""

    async def test_supersession(self, fresh_db) -> None:
        """Old fact should be superseded by new fact."""
        from wax.memory.contracts import MemoryCreate, MemoryKind
        from wax.memory.repository import MemoryRepository

        async with db_session() as session:
            repo = MemoryRepository(session)
            old = await repo.create(
                MemoryCreate(
                    principal_id="01TESTPRINCIPAL000000000",
                    kind=MemoryKind.FACT,
                    content={"text": "User lives in Lagos"},
                    provenance="user_statement",
                    confidence=1.0,
                    importance=0.8,
                    observed_at=datetime.now(UTC),
                )
            )
            new = await repo.create(
                MemoryCreate(
                    principal_id="01TESTPRINCIPAL000000000",
                    kind=MemoryKind.FACT,
                    content={"text": "User moved to Abuja"},
                    provenance="user_statement",
                    confidence=1.0,
                    importance=0.9,
                    observed_at=datetime.now(UTC),
                )
            )
            # Supersede the old memory
            await repo.supersede(old.id, new.id)
            await session.commit()

        # Verify old is superseded
        async with db_session() as session:
            repo = MemoryRepository(session)
            old_record = await repo.get(old.id)
            assert old_record.status == "superseded"
            assert old_record.superseded_by == new.id


class TestSecretRedaction:
    """Test 15: Secrets are redacted from terminal output."""

    def test_api_key_redacted(self) -> None:
        """API keys should be redacted from terminal output."""
        output = "The API key is sk-abc123def456ghi789jkl012mno345pqr678"
        redacted = redact_secrets(output)
        assert "sk-abc123" not in redacted
        assert "[REDACTED]" in redacted

    def test_github_token_redacted(self) -> None:
        output = "Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"
        redacted = redact_secrets(output)
        assert "ghp_1234567890" not in redacted
        assert "[REDACTED]" in redacted

    def test_jwt_redacted(self) -> None:
        output = "Authorization: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        redacted = redact_secrets(output)
        assert "eyJhbGci" not in redacted
        assert "[REDACTED]" in redacted

    def test_db_url_redacted(self) -> None:
        output = "DATABASE_URL=postgresql://user:secretpass@localhost:5432/db"
        redacted = redact_secrets(output)
        assert "secretpass" not in redacted
        assert "[REDACTED]" in redacted

    def test_env_vars_filtered(self) -> None:
        """Sensitive env vars should be removed from terminal subprocess env."""
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/wax",
            "WAX_SECRET_KEY": "super-secret-key",
            "WAX_DATABASE_URL": "postgresql://user:pass@host/db",
            "WAX_LLM_API_KEY": "sk-abc123",
        }
        filtered = redact_env_vars(env)
        assert "PATH" in filtered
        assert "HOME" in filtered
        assert "WAX_SECRET_KEY" not in filtered
        assert "WAX_DATABASE_URL" not in filtered
        assert "WAX_LLM_API_KEY" not in filtered


class TestEncryptedSecretStorage:
    """Test 15b: Secrets are encrypted at rest."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """Encrypt a secret, decrypt it, verify roundtrip."""
        secret_key = "test-secret-key-not-for-production-xxxxxxxxxxxxxxxx"
        plaintext = "ghp_my_super_secret_github_token_12345"

        encrypted, nonce = encrypt_secret(plaintext, secret_key)
        assert encrypted != plaintext
        assert nonce

        decrypted = decrypt_secret(encrypted, nonce, secret_key)
        assert decrypted == plaintext

    def test_encrypted_data_not_plaintext(self) -> None:
        """Encrypted data should not contain the plaintext."""
        secret_key = "test-secret-key-not-for-production-xxxxxxxxxxxxxxxx"
        plaintext = "sk-my-openai-api-key-12345"

        encrypted, nonce = encrypt_secret(plaintext, secret_key)
        assert plaintext not in encrypted
        assert plaintext not in nonce


class TestDuplicateWebhook:
    """Test 12: Duplicate webhook delivery is deduplicated."""

    async def test_duplicate_returns_duplicate_status(self, fresh_db, bridge) -> None:
        """Same message ID twice should return DUPLICATE the second time."""
        from wax.runtime.bridge.contracts import RuntimeResponseStatus

        async with db_session() as session:
            r1 = await bridge.process(session, _request(message_id="msg-dup-test"))
            await session.commit()
        async with db_session() as session:
            r2 = await bridge.process(session, _request(message_id="msg-dup-test"))
            await session.commit()

        assert r1.status == RuntimeResponseStatus.SUCCESS
        assert r2.status == RuntimeResponseStatus.DUPLICATE


class TestAIIdentity:
    """Test: AI identifies as WAX, not ChatGPT."""

    def test_system_prompt_says_wax(self) -> None:
        """System prompt should say 'Your name is WAX' not ChatGPT."""
        from wax.runtime.bridge.service import BASE_SYSTEM_PROMPT

        assert "WAX" in BASE_SYSTEM_PROMPT
        assert "NOT ChatGPT" in BASE_SYSTEM_PROMPT
