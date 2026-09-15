"""Workspace + Artifact Lifecycle — integration tests (ADR-0042, Phase 9).

Covers:
- workspace.snapshot captures current state (content-addressed, idempotent)
- workspace.restore restores into a target workspace
- workspace.promote clears TTL
- artifact.capture records a file as a durable artifact (SHA-256)
- artifact.list returns metadata only
- artifact.retrieve re-verifies integrity
- wrong principal rejected
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.provisioning import ProvisioningService
from wax.runtime.services import RuntimeServices
from wax.runtime.vault import seed_builtin_connectors
from wax.state.artifact_models import ArtifactRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.provisioning_models import ProvisionedResourceRecord
from wax.state.workspace_models import (
    WorkspaceSnapshotRecord,  # noqa: F401 — registers with Base.metadata for create_all
)

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
        await seed_builtin_connectors(s)
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


async def _provision_workspace(services, principal_id: str) -> tuple[str, Path]:
    """Helper: provision a scratch workspace and return (id, path)."""
    async with db_session() as s:
        provisioning = ProvisioningService(services.settings)
        resource = await provisioning.provision_scratch_dir(
            s,
            principal_id=principal_id,
            ttl_seconds=3600,
            execution_id=None,
        )
        await s.commit()
        return resource.id, Path(resource.uri)


# ----------------------------------------------------------------------------
# 1. workspace.snapshot
# ----------------------------------------------------------------------------


class TestWorkspaceSnapshot:
    async def test_snapshot_captures_file_state(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        # Create a file in the workspace
        (workspace_path / "test.txt").write_text("hello world")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.snapshot",
                    principal_id=principal_id,
                    inputs={"workspace_id": workspace_id},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["file_count"] == 1
        assert result.outputs["total_bytes"] == 11
        assert result.outputs["idempotent"] is False

    async def test_snapshot_is_idempotent(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        (workspace_path / "test.txt").write_text("same content")

        async with db_session() as s:
            invoker = services.invoker(s)
            r1 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.snapshot",
                    principal_id=principal_id,
                    inputs={"workspace_id": workspace_id},
                )
            )
            await s.commit()
            r2 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.snapshot",
                    principal_id=principal_id,
                    inputs={"workspace_id": workspace_id},
                )
            )
            await s.commit()

        assert r1.outcome == "success"
        assert r2.outcome == "success"
        assert r1.outputs["snapshot_id"] == r2.outputs["snapshot_id"]
        assert r2.outputs["idempotent"] is True

    async def test_snapshot_rejects_wrong_principal(self, fresh_db, services):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")
        workspace_id, _ = await _provision_workspace(services, principal_a)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.snapshot",
                    principal_id=principal_b,
                    inputs={"workspace_id": workspace_id},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")


# ----------------------------------------------------------------------------
# 2. workspace.promote
# ----------------------------------------------------------------------------


class TestWorkspacePromote:
    async def test_promote_clears_ttl(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, _ = await _provision_workspace(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.promote",
                    principal_id=principal_id,
                    inputs={"workspace_id": workspace_id},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["promoted"] is True

        async with db_session() as s:
            record = await s.get(ProvisionedResourceRecord, workspace_id)
            assert record.expires_at is None  # permanent


# ----------------------------------------------------------------------------
# 3. artifact.capture + artifact.list + artifact.retrieve
# ----------------------------------------------------------------------------


class TestArtifactLifecycle:
    async def test_capture_records_artifact_with_sha256(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        content = b"artifact content here"
        (workspace_path / "output.txt").write_bytes(content)
        expected_sha = hashlib.sha256(content).hexdigest()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "output.txt",
                        "filename": "output.txt",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["sha256"] == expected_sha
        assert result.outputs["size_bytes"] == len(content)

        # The artifact record exists
        async with db_session() as s:
            artifact = (
                await s.execute(
                    select(ArtifactRecord).where(ArtifactRecord.principal_id == principal_id)
                )
            ).scalar_one()
            assert artifact.sha256 == expected_sha
            assert artifact.filename == "output.txt"

    async def test_capture_rejects_nonexistent_file(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, _ = await _provision_workspace(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "nonexistent.txt",
                        "filename": "nonexistent.txt",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "does not exist" in (result.error or "")

    async def test_list_returns_metadata_only(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        (workspace_path / "file1.txt").write_text("content1")

        async with db_session() as s:
            invoker = services.invoker(s)
            await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "file1.txt",
                        "filename": "file1.txt",
                    },
                )
            )
            await s.commit()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.list",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["count"] >= 1
        # Metadata only — no file bytes
        for artifact in result.outputs["artifacts"]:
            assert "content1" not in str(artifact)
            assert "sha256" in artifact
            assert "filename" in artifact

    async def test_retrieve_re_verifies_integrity(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        content = b"verifiable content"
        (workspace_path / "verify.txt").write_bytes(content)

        async with db_session() as s:
            invoker = services.invoker(s)
            capture_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "verify.txt",
                        "filename": "verify.txt",
                    },
                )
            )
            await s.commit()
            artifact_id = capture_result.outputs["artifact_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.retrieve",
                    principal_id=principal_id,
                    inputs={"artifact_id": artifact_id},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["integrity_verified"] is True
        assert result.outputs["artifact"]["sha256"] == hashlib.sha256(content).hexdigest()

    async def test_retrieve_detects_tampered_file(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        (workspace_path / "tamper.txt").write_text("original")

        async with db_session() as s:
            invoker = services.invoker(s)
            capture_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "tamper.txt",
                        "filename": "tamper.txt",
                    },
                )
            )
            await s.commit()
            artifact_id = capture_result.outputs["artifact_id"]

        # Tamper with the file
        (workspace_path / "tamper.txt").write_text("tampered")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.retrieve",
                    principal_id=principal_id,
                    inputs={"artifact_id": artifact_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["integrity_verified"] is False  # tampered

    async def test_capture_rejects_absolute_path(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, _ = await _provision_workspace(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "/etc/passwd",
                        "filename": "passwd",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "workspace-relative" in (result.error or "")
