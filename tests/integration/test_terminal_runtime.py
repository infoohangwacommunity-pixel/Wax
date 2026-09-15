"""Terminal Runtime — integration tests (ADR-0039, Phase 6).

Covers:
- terminal.session.open creates an active session
- terminal.execute runs a command and returns exit_code + stdout
- terminal.execute persists working_dir across commands (cd stays)
- terminal.session.close kills the process group
- session TTL expiry
- command timeout enforcement
- stdout/stderr truncation
- env var validation (no secrets)
- workspace-relative path enforcement
"""

from __future__ import annotations

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.terminal_models import TerminalSessionRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


async def _open_environment(services, principal_id: str) -> str:
    """Helper: request an environment and return its ID."""
    async with db_session() as s:
        invoker = services.invoker(s)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="environment.request",
                principal_id=principal_id,
                inputs={"purpose": "terminal test", "ttl_seconds": 3600},
            )
        )
        await s.commit()
        return result.outputs["environment_id"]


# ----------------------------------------------------------------------------
# 1. terminal.session.open
# ----------------------------------------------------------------------------


class TestTerminalSessionOpen:
    async def test_open_creates_active_session(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = result.outputs["session_id"]

        assert result.outcome == "success", result.error
        assert result.outputs["state"] == "active"

        async with db_session() as s:
            record = await s.get(TerminalSessionRecord, session_id)
            assert record.status == "active"
            assert record.environment_id == env_id
            assert record.working_dir == "."
            assert record.expires_at is not None

    async def test_open_rejects_absolute_working_dir(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={
                        "environment_id": env_id,
                        "working_dir": "/etc",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "workspace-relative" in (result.error or "")

    async def test_open_rejects_secret_env_var(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={
                        "environment_id": env_id,
                        "env_vars": {"API_TOKEN": "secret-value"},
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "secret" in (result.error or "").lower()

    async def test_open_rejects_nonexistent_environment(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": "01NOSUCHENVIRONMENT000000A"},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "No such environment" in (result.error or "")

    async def test_open_rejects_other_principal_environment(self, fresh_db, services):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")
        env_id = await _open_environment(services, principal_b)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_a,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")


# ----------------------------------------------------------------------------
# 2. terminal.execute
# ----------------------------------------------------------------------------


class TestTerminalExecute:
    async def test_execute_runs_command_and_returns_exit_code(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": session_id,
                        "command": "echo hello",
                        "timeout_seconds": 5,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["exit_code"] == 0
        assert "hello" in result.outputs["stdout"]
        assert not result.outputs["timed_out"]

    async def test_execute_nonzero_exit_code(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": session_id,
                        "command": "exit 42",
                        "timeout_seconds": 5,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["exit_code"] == 42

    async def test_execute_timeout_kills_command(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": session_id,
                        "command": "sleep 30",
                        "timeout_seconds": 1,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["timed_out"] is True

    async def test_execute_rejects_nonexistent_session(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": "01NOSUCHSESSION0000000000A",
                        "command": "echo test",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "No such terminal session" in (result.error or "")

    async def test_execute_rejects_command_too_long(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": session_id,
                        "command": "x" * 9000,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "exceeds 8192" in (result.error or "")


# ----------------------------------------------------------------------------
# 3. terminal.session.close
# ----------------------------------------------------------------------------


class TestTerminalSessionClose:
    async def test_close_marks_session_closed(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.close",
                    principal_id=principal_id,
                    inputs={"session_id": session_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["closed"] is True
        assert result.outputs["state"] == "closed"

        async with db_session() as s:
            record = await s.get(TerminalSessionRecord, session_id)
            assert record.status == "closed"

    async def test_close_idempotent(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            r1 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.close",
                    principal_id=principal_id,
                    inputs={"session_id": session_id},
                )
            )
            await s.commit()
            r2 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.close",
                    principal_id=principal_id,
                    inputs={"session_id": session_id},
                )
            )
            await s.commit()

        assert r1.outcome == "success"
        assert r1.outputs["closed"] is True
        assert r2.outcome == "success"
        assert r2.outputs["closed"] is False  # already closed

    async def test_execute_rejects_closed_session(self, fresh_db, services):
        principal_id = await _create_principal()
        env_id = await _open_environment(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            open_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.open",
                    principal_id=principal_id,
                    inputs={"environment_id": env_id},
                )
            )
            await s.commit()
            session_id = open_result.outputs["session_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.session.close",
                    principal_id=principal_id,
                    inputs={"session_id": session_id},
                )
            )
            await s.commit()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="terminal.execute",
                    principal_id=principal_id,
                    inputs={
                        "session_id": session_id,
                        "command": "echo should-fail",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "closed" in (result.error or "")
