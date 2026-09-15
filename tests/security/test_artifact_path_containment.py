"""Artifact path containment — security tests (P0-containment).

`artifact.capture` and `artifact.retrieve` previously joined
`Path(resource.uri) / path` WITHOUT the central containment utility: a
model-supplied path like `../other_principal/secret` escaped the
workspace. These tests pin the fix — every model-supplied path is
resolved through `resolve_workspace_path`, and a hostile path stored in
a (corrupt) record is refused on retrieve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.provisioning import ProvisioningService
from wax.runtime.services import RuntimeServices
from wax.state.artifact_models import ArtifactRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.workspace_models import (
    WorkspaceSnapshotRecord,  # noqa: F401 — registers with Base.metadata
)

pytestmark = pytest.mark.security


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


async def _provision_workspace(services, principal_id: str) -> tuple[str, Path]:
    async with db_session() as s:
        provisioning = ProvisioningService(services.settings)
        resource = await provisioning.provision_scratch_dir(
            s, principal_id=principal_id, ttl_seconds=3600, execution_id=None
        )
        await s.commit()
        return resource.id, Path(resource.uri)


class TestArtifactCaptureContainment:
    async def test_capture_rejects_traversal_path(self, fresh_db, services):
        """`../` must NOT let the model hash arbitrary host files."""
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        (workspace_path / "legit.txt").write_text("fine")

        # A sibling file OUTSIDE the workspace, next to it on the host.
        outside_file = workspace_path.parent / "outside_secret.txt"
        outside_file.write_text("host-side secret")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "../outside_secret.txt",
                        "filename": "stolen.txt",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "containment" in (result.error or "").lower()
        # No artifact record may exist for the escape attempt.
        async with db_session() as s:
            artifacts = (await s.execute(select(ArtifactRecord))).scalars().all()
        assert artifacts == []

    async def test_capture_rejects_deep_traversal(self, fresh_db, services):
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
                        "path": "sub/../../../etc/passwd",
                        "filename": "passwd",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "traversal" in (result.error or "")

    async def test_capture_rejects_symlink_escape(self, fresh_db, services):
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)

        outside = (workspace_path.parent / "link_target.txt").resolve()
        outside.write_text("outside content")
        link = workspace_path / "innocent"
        link.symlink_to(outside)  # absolute target pointing OUTSIDE the workspace

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "innocent",
                        "filename": "innocent",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        # The resolved path escapes the workspace root — refused before
        # a single byte is read.
        assert "escapes the workspace" in (result.error or "")


class TestArtifactRetrieveContainment:
    async def test_retrieve_refuses_hostile_stored_path(self, fresh_db, services):
        """Even if a corrupt record holds an escaping path, retrieve must
        not read outside the workspace (defence in depth)."""
        principal_id = await _create_principal()
        workspace_id, workspace_path = await _provision_workspace(services, principal_id)
        (workspace_path / "ok.txt").write_text("real content")

        async with db_session() as s:
            invoker = services.invoker(s)
            capture = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="artifact.capture",
                    principal_id=principal_id,
                    inputs={
                        "workspace_id": workspace_id,
                        "path": "ok.txt",
                        "filename": "ok.txt",
                    },
                )
            )
            await s.commit()
            artifact_id = capture.outputs["artifact_id"]

        # Simulate a corrupt/hostile stored path (as if an old vulnerable
        # capture had recorded one).
        outside_file = workspace_path.parent / "outside_secret.txt"
        outside_file.write_text("host-side secret")
        async with db_session() as s:
            artifact = await s.get(ArtifactRecord, artifact_id)
            artifact.path = "../outside_secret.txt"
            await s.commit()

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

        assert result.outcome == "success"  # reported honestly, not crashed
        assert result.outputs["integrity_verified"] is False
        assert result.outputs["containment_violation"] is True


class TestWorkspaceAcquireContainment:
    async def test_acquire_rejects_traversing_filename_before_download(self, fresh_db, services):
        """The containment check must fire BEFORE any network I/O."""
        principal_id = await _create_principal()
        workspace_id, _ = await _provision_workspace(services, principal_id)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.acquire",
                    principal_id=principal_id,
                    inputs={
                        "workspace_resource_id": workspace_id,
                        # Allowlisted host + well-formed hash: the request
                        # reaches the destination-containment check with no
                        # download having happened.
                        "url": "https://github.com/some/repo",
                        "sha256": "a" * 64,
                        "filename": "..",
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "escapes the workspace" in (result.error or "")
