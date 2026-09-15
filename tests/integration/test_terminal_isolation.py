"""Terminal isolation routing — integration tests (P0-Terminal).

`terminal.execute` previously spawned a RAW `asyncio.create_subprocess_shell`,
bypassing IsolationService entirely — no namespace sandbox, no rlimits,
only a scrubbed env. These tests pin the fix: terminal execution now
routes through the SAME runtime isolation decision as code.run.
"""

from __future__ import annotations

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.isolation.service import IsolationService
from wax.isolation.subprocess_boundary import SubprocessBoundary
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.environment_models import EnvironmentLeaseRecord  # noqa: F401 — registers table
from wax.state.models import Base
from wax.state.terminal_models import TerminalSessionRecord  # noqa: F401 — registers table

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


async def _open_session(services, principal_id: str) -> str:
    async with db_session() as s:
        invoker = services.invoker(s)
        env_result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="environment.request",
                principal_id=principal_id,
                inputs={"purpose": "terminal isolation test", "ttl_seconds": 3600},
            )
        )
        await s.commit()
        assert env_result.outcome == "success", env_result.error
        env_id = env_result.outputs["environment_id"]

        open_result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="terminal.session.open",
                principal_id=principal_id,
                inputs={"environment_id": env_id},
            )
        )
        await s.commit()
        assert open_result.outcome == "success", open_result.error
        return open_result.outputs["session_id"]


async def _execute(services, principal_id: str, session_id: str, **inputs) -> dict:
    async with db_session() as s:
        invoker = services.invoker(s)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="terminal.execute",
                principal_id=principal_id,
                inputs={"session_id": session_id, **inputs},
            )
        )
        await s.commit()
    assert result.outcome == "success", result.error
    return result.outputs


class TestTerminalRoutesThroughIsolationService:
    async def test_select_is_called_with_configured_backend(self, fresh_db, services, monkeypatch):
        """The runtime's isolation decision — NOT a raw subprocess — must
        produce the execution."""
        calls: list[str] = []

        @classmethod
        def spy(cls, backend):
            calls.append(backend)
            return IsolationService(SubprocessBoundary()), "subprocess"

        monkeypatch.setattr(IsolationService, "select", spy)

        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services, principal_id, session_id, command="echo hi", timeout_seconds=5
        )

        assert calls == [services.settings.isolation_backend]
        assert outputs["exit_code"] == 0
        assert "hi" in outputs["stdout"]

    async def test_result_reports_isolation_kind(self, fresh_db, services, monkeypatch):
        @classmethod
        def fake_select(cls, backend):
            return IsolationService(SubprocessBoundary()), "subprocess"

        monkeypatch.setattr(IsolationService, "select", fake_select)

        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services, principal_id, session_id, command="echo kind-check", timeout_seconds=5
        )
        assert outputs["isolation"] == "subprocess"

    async def test_unpatched_run_reports_a_real_boundary(self, fresh_db, services):
        """With no test doubles: the result names the actual boundary
        (namespace where available, subprocess otherwise) — either way it
        came from IsolationService, never from an unscoped raw spawn."""
        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services, principal_id, session_id, command="echo isolated", timeout_seconds=10
        )
        assert outputs["isolation"] in ("namespace", "subprocess")
        assert outputs["exit_code"] == 0
        assert outputs["duration_ms"] >= 0


class TestTerminalEnvStillScrubbed:
    async def test_host_env_does_not_leak_into_terminal(self, fresh_db, services, monkeypatch):
        monkeypatch.setenv("WAX_SENTINEL_XYZ", "leak-me-not")

        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services,
            principal_id,
            session_id,
            command="printenv WAX_SENTINEL_XYZ",
            timeout_seconds=5,
        )

        assert outputs["exit_code"] == 1  # printenv: variable not set
        assert "leak-me-not" not in outputs["stdout"]


class TestTerminalBehaviourPreserved:
    async def test_timeout_still_enforced_through_boundary(self, fresh_db, services):
        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services, principal_id, session_id, command="sleep 30", timeout_seconds=1
        )
        assert outputs["timed_out"] is True

    async def test_output_cap_enforced_through_boundary(self, fresh_db, services):
        principal_id = await _create_principal()
        session_id = await _open_session(services, principal_id)
        outputs = await _execute(
            services,
            principal_id,
            session_id,
            command="yes wax-cap | head -c 200000",
            timeout_seconds=10,
            max_output_bytes=2048,
        )
        assert outputs["truncated"] is True
        assert len(outputs["stdout"]) <= 2048
