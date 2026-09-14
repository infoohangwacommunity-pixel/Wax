"""Environment Negotiation — integration tests (ADR-0038, Phase 5).

Covers:
- environment.request capability with a workspace requirement
- environment.request with isolation requirement (degradation: container → namespace)
- environment.request with network requirement (degradation: open → none)
- environment.request with credential requirement (recorded but not resolved)
- environment lease persisted + retrievable
- environment release (owner-fenced)
- payload validation (size limits, no secrets, invalid enum)
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.environment.contracts import (
    ENVIRONMENT_PURPOSE_MAX_CHARS,
    EnvironmentState,
    EnvironmentValidationError,
    IsolationGrade,
    NetworkPolicyKind,
    validate_environment_requirement,
)
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.environment_models import (
    EnvironmentCapabilityBindingRecord,
    EnvironmentLeaseRecord,
)
from wax.state.models import Base

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
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


# ----------------------------------------------------------------------------
# 1. Payload validation (unit-level)
# ----------------------------------------------------------------------------


class TestEnvironmentRequirementValidation:
    def test_valid_minimal_requirement(self):
        req = validate_environment_requirement({"purpose": "test"})
        assert req.purpose == "test"
        assert req.isolation == IsolationGrade.NONE

    def test_missing_purpose_raises(self):
        with pytest.raises(EnvironmentValidationError, match="purpose"):
            validate_environment_requirement({})

    def test_empty_purpose_raises(self):
        with pytest.raises(EnvironmentValidationError, match="purpose"):
            validate_environment_requirement({"purpose": "  "})

    def test_oversized_purpose_raises(self):
        with pytest.raises(EnvironmentValidationError, match="exceeds"):
            validate_environment_requirement(
                {"purpose": "x" * (ENVIRONMENT_PURPOSE_MAX_CHARS + 1)}
            )

    def test_invalid_isolation_raises(self):
        with pytest.raises(EnvironmentValidationError, match="isolation"):
            validate_environment_requirement(
                {"purpose": "test", "isolation": "quantum"}
            )

    def test_invalid_network_kind_raises(self):
        with pytest.raises(EnvironmentValidationError, match=r"network\.kind"):
            validate_environment_requirement(
                {"purpose": "test", "network": {"kind": "wormhole"}}
            )

    def test_too_many_tools_raises(self):
        with pytest.raises(EnvironmentValidationError, match="tools"):
            validate_environment_requirement(
                {"purpose": "test", "tools": [{"name": f"tool-{i}"} for i in range(21)]}
            )

    def test_too_many_credentials_raises(self):
        with pytest.raises(EnvironmentValidationError, match="credentials"):
            validate_environment_requirement(
                {
                    "purpose": "test",
                    "credentials": [
                        {"connector": "git_host"} for _ in range(11)
                    ],
                }
            )

    def test_full_requirement_validates(self):
        req = validate_environment_requirement(
            {
                "purpose": "controlled workspace for project work",
                "workspace": {"persistent": True, "disk_bytes": 500_000_000},
                "execution": {
                    "cpu_seconds": 3600,
                    "memory_bytes": 2_147_483_648,
                    "processes": 32,
                    "timeout_seconds": 1800,
                },
                "tools": [
                    {"name": "shell", "acquire_if_missing": False},
                    {"name": "package-manager", "acquire_if_missing": True},
                ],
                "credentials": [
                    {
                        "connector": "git_host",
                        "scopes": ["repository.read"],
                        "purpose": "read source code",
                    }
                ],
                "network": {"kind": "allowlisted", "allowlist": ["pypi.org"]},
                "isolation": "namespace",
            }
        )
        assert req.workspace.persistent
        assert req.workspace.disk_bytes == 500_000_000
        assert req.execution.cpu_seconds == 3600
        assert len(req.tools) == 2
        assert req.credentials[0].connector == "git_host"
        assert req.network.kind == NetworkPolicyKind.ALLOWLISTED
        assert req.isolation == IsolationGrade.NAMESPACE

    def test_none_payload_raises(self):
        with pytest.raises(EnvironmentValidationError):
            validate_environment_requirement(None)

    def test_non_dict_payload_raises(self):
        with pytest.raises(EnvironmentValidationError):
            validate_environment_requirement("not a dict")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# 2. environment.request capability (end-to-end)
# ----------------------------------------------------------------------------


class TestEnvironmentRequestCapability:
    async def test_request_with_workspace_provisions_lease(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "scratch space for code project",
                        "workspace": {"persistent": False, "disk_bytes": 100_000_000},
                        "isolation": "none",
                        "ttl_seconds": 3600,
                    },
                )
            )
            await s.commit()
            environment_id = result.outputs.get("environment_id") if result.outputs else None

        assert result.outcome == "success", result.error
        assert environment_id is not None

        # The lease record exists
        async with db_session() as s:
            record = await s.get(EnvironmentLeaseRecord, environment_id)
            assert record is not None
            assert record.status == EnvironmentState.PROVISIONED.value
            assert record.principal_id == principal_id
            assert record.workspace_resource_id is not None  # workspace was provisioned

    async def test_request_without_workspace(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "no-workspace env",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error

        async with db_session() as s:
            record = await s.get(EnvironmentLeaseRecord, result.outputs["environment_id"])
            assert record.workspace_resource_id is None

    async def test_request_with_container_isolation_degrades_to_namespace(
        self, fresh_db, services
    ):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "container env (will degrade)",
                        "isolation": "container",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        # The degraded field should mention the container → namespace downgrade
        assert any("container" in d for d in result.outputs["degraded"])

    async def test_request_with_open_network_degrades_to_none(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "open network (will degrade)",
                        "network": {"kind": "open"},
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert any("network" in d for d in result.outputs["degraded"])

    async def test_request_with_credential_requirement_records_in_plan(
        self, fresh_db, services
    ):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "env with credential requirement",
                        "credentials": [
                            {
                                "connector": "git_host",
                                "scopes": ["repository.read"],
                                "purpose": "read source",
                            }
                        ],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        # Credential handles are empty (vault not yet wired — Phase 7)
        assert result.outputs["credential_handles"] == []
        # Notes mention the vault boundary
        assert "vault" in (result.outputs["notes"] or "")

    async def test_invalid_requirement_returns_failure(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={"purpose": ""},  # empty purpose
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "purpose" in (result.error or "")


# ----------------------------------------------------------------------------
# 3. Lease release (owner-fenced)
# ----------------------------------------------------------------------------


class TestEnvironmentRelease:
    async def test_release_owner_can_release(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={"purpose": "to-release"},
                )
            )
            await s.commit()
            environment_id = result.outputs["environment_id"]

        # Release via the planner
        async with db_session() as s:
            planner = services.environment_planner
            ok = await planner.release(s, environment_id)
            await s.commit()

        assert ok
        async with db_session() as s:
            record = await s.get(EnvironmentLeaseRecord, environment_id)
            assert record.status == EnvironmentState.RELEASED.value

    async def test_release_already_released_returns_false(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={"purpose": "double-release"},
                )
            )
            await s.commit()
            environment_id = result.outputs["environment_id"]

        async with db_session() as s:
            planner = services.environment_planner
            ok1 = await planner.release(s, environment_id)
            await s.commit()
            ok2 = await planner.release(s, environment_id)
            await s.commit()

        assert ok1
        assert not ok2  # already released

    async def test_release_nonexistent_returns_false(self, fresh_db, services):
        async with db_session() as s:
            planner = services.environment_planner
            ok = await planner.release(s, "01NOSUCHENVIRONMENT000000000A")
            await s.commit()
        assert not ok


# ----------------------------------------------------------------------------
# 4. EnvironmentCapabilityBinding
# ----------------------------------------------------------------------------


class TestEnvironmentCapabilityBinding:
    async def test_binding_record_can_be_persisted(self, fresh_db, services):
        """Smoke test: the EnvironmentCapabilityBindingRecord table is
        usable. Phase 6 will register real bindings when terminal.execute
        is wired to environments."""
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="environment.request",
                    principal_id=principal_id,
                    inputs={"purpose": "binding test"},
                )
            )
            await s.commit()
            environment_id = result.outputs["environment_id"]

        # Manually insert a binding (Phase 6 will wire this automatically)
        async with db_session() as s:
            binding = EnvironmentCapabilityBindingRecord(
                id="01BINDINGTESTID000000000000A",
                environment_id=environment_id,
                capability_name="terminal.execute",
                handle="opaque-handle-1",
            )
            s.add(binding)
            await s.commit()

            # Verify it's retrievable
            retrieved = (
                await s.execute(
                    select(EnvironmentCapabilityBindingRecord).where(
                        EnvironmentCapabilityBindingRecord.environment_id == environment_id
                    )
                )
            ).scalar_one_or_none()
            assert retrieved is not None
            assert retrieved.capability_name == "terminal.execute"
            assert retrieved.handle == "opaque-handle-1"
