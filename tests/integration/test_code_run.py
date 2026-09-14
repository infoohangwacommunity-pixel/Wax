"""Execution environment tests (Phase U) — isolated code execution.

Audit Section 12: the SubprocessBoundary existed and was tested, but
"nothing in the live path executes arbitrary code, so nothing invokes this
boundary." Table 7, objective C ("help me write a Python program") stopped
at "Subprocess isolation exists but is unwired; code cannot be run or
tested."

These tests prove the boundary is now reachable — as an authority-gated
capability with an explicit trust boundary:
- members are DENIED by default (capability.invoke:code_run is not in the
  member role),
- privileged principals may run code in a scrubbed subprocess,
- scratch.workspace resources integrate as optional working directories,
  verified for ownership.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings, tmp_path):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["provisioning_root"] = str(tmp_path / "resources")
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


@pytest.fixture
def bridge(services) -> RuntimeBridge:
    return RuntimeBridge(intelligence=IntelligenceService(MockLLMProvider()), services=services)


def _request(message_id: str = "msg-code-1", sender: str = "+2348000000000") -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id=sender,
        sender_display_name="Tester",
        text="run this code",
        received_at=datetime.now(UTC),
    )


class TestCodeRunTrustBoundary:
    async def test_member_principal_is_denied_by_default(self, fresh_db, services, bridge) -> None:
        """THE trust boundary: ordinary users cannot execute code. The
        permission is granted only by explicit role assignment."""
        async with db_session() as session:
            response = await bridge.process(session, _request())

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=response.principal_id,  # member
                    inputs={"code": "print('hello')"},
                )
            )
            await session.commit()
        assert result.outcome == "denied"
        assert "capability.invoke:code_run" in (result.error or "")

    async def test_privileged_principal_can_run_python(self, fresh_db, services, bridge) -> None:
        """Grant the admin role → code runs in an isolated subprocess with
        scrubbed environment + timeout + output caps."""
        from ulid import ULID

        from wax.authority.seed import get_role_by_name
        from wax.state.authority_models import PrincipalRole

        async with db_session() as session:
            response = await bridge.process(session, _request())
            principal_id = response.principal_id
            admin = await get_role_by_name(session, "admin")
            session.add(PrincipalRole(id=str(ULID()), principal_id=principal_id, role_id=admin.id))
            await session.commit()

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=principal_id,
                    inputs={"code": "print(21 * 2)"},
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error
        assert result.outputs["exit_code"] == 0
        assert result.outputs["stdout"].strip() == "42"
        assert result.outputs["timed_out"] is False

    async def test_timeout_is_enforced(self, fresh_db, services, bridge) -> None:
        from ulid import ULID

        from wax.authority.seed import get_role_by_name
        from wax.state.authority_models import PrincipalRole

        async with db_session() as session:
            response = await bridge.process(session, _request())
            admin = await get_role_by_name(session, "admin")
            session.add(
                PrincipalRole(
                    id=str(ULID()),
                    principal_id=response.principal_id,
                    role_id=admin.id,
                )
            )
            await session.commit()
            principal_id = response.principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=principal_id,
                    inputs={
                        "code": "import time; time.sleep(30)",
                        "timeout_seconds": 1.0,
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success"
        assert result.outputs["timed_out"] is True
        assert result.outputs["exit_code"] == 124

    async def test_subprocess_never_inherits_secrets(
        self, fresh_db, services, bridge, monkeypatch
    ) -> None:
        """The boundary's core promise: no environment inheritance."""
        from ulid import ULID

        from wax.authority.seed import get_role_by_name
        from wax.state.authority_models import PrincipalRole

        monkeypatch.setenv("WAX_TEST_SECRET_TOKEN", "super-secret-value")

        async with db_session() as session:
            response = await bridge.process(session, _request())
            admin = await get_role_by_name(session, "admin")
            session.add(
                PrincipalRole(
                    id=str(ULID()),
                    principal_id=response.principal_id,
                    role_id=admin.id,
                )
            )
            await session.commit()
            principal_id = response.principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=principal_id,
                    inputs={
                        "code": (
                            "import os, json;"
                            "print(json.dumps({k: v for k, v in os.environ.items()"
                            " if 'SECRET' in k}))"
                        ),
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success"
        assert "super-secret-value" not in result.outputs["stdout"]

    async def test_workspace_integration_requires_ownership(
        self, fresh_db, services, bridge, test_settings, tmp_path
    ) -> None:
        """code.run x Phase S: an owned scratch dir works as cwd; someone
        else's resource is refused."""
        from pathlib import Path

        from ulid import ULID

        from wax.authority.seed import get_role_by_name
        from wax.runtime.provisioning import ProvisioningService
        from wax.state.authority_models import PrincipalRole

        async with db_session() as session:
            response = await bridge.process(session, _request())
            principal_id = response.principal_id
            admin = await get_role_by_name(session, "admin")
            session.add(PrincipalRole(id=str(ULID()), principal_id=principal_id, role_id=admin.id))
            # A scratch dir owned by this principal.
            provisioning = ProvisioningService(test_settings)
            record = await provisioning.provision_scratch_dir(
                session, principal_id=principal_id, ttl_seconds=600
            )
            # And one owned by someone else.
            stranger_dir = await provisioning.provision_scratch_dir(
                session, principal_id=str(ULID()), ttl_seconds=600
            )
            await session.commit()
            owned_id, foreign_id = record.id, stranger_dir.id

        async with db_session() as session:
            invoker = services.invoker(session)
            ok = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=principal_id,
                    inputs={
                        "code": "import os; print(os.getcwd())",
                        "workspace_resource_id": owned_id,
                    },
                )
            )
            await session.commit()
        assert ok.outcome == "success", ok.error
        assert Path(ok.outputs["stdout"].strip()) == Path(record.uri)

        async with db_session() as session:
            invoker = services.invoker(session)
            denied = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="code.run",
                    principal_id=principal_id,
                    inputs={
                        "code": "print('nope')",
                        "workspace_resource_id": foreign_id,
                    },
                )
            )
        assert denied.outcome == "failure"
        assert "owned by the requesting principal" in denied.error
