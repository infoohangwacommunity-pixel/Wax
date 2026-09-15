"""Content-addressed workspace snapshots — integration tests (P0-Workspace).

Previously `workspace.snapshot` captured only METADATA (path, sha256,
size) and `workspace.restore` could copy real bytes only while the
SOURCE workspace still existed. Once provisioning released the source
(backing directory deleted), restore silently produced EMPTY files while
reporting itself complete. These tests pin the fix: bytes are persisted
into the content-addressed blob store at capture time, so restore works
forever after — and integrity is verified on every path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.blob_store import BlobStoreError, ContentAddressedBlobStore
from wax.runtime.provisioning import ProvisioningService
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.provisioning_models import ProvisionedResourceRecord

pytestmark = pytest.mark.integration


@pytest.fixture
def test_settings(tmp_path, settings_for_testing_factory):
    return settings_for_testing_factory(snapshot_blob_root=str(tmp_path / "blobs"))


@pytest.fixture
def settings_for_testing_factory():
    from wax.core.config import settings_for_testing as _factory

    return _factory


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


async def _snapshot(services, principal_id: str, workspace_id: str) -> dict:
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
    return result.outputs


async def _restore(services, principal_id: str, snapshot_id: str, target_id: str) -> dict:
    async with db_session() as s:
        invoker = services.invoker(s)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="workspace.restore",
                principal_id=principal_id,
                inputs={"snapshot_id": snapshot_id, "target_workspace_id": target_id},
            )
        )
        await s.commit()
    assert result.outcome == "success", result.error
    return result.outputs


async def _release_workspace(workspace_id: str) -> None:
    """Simulate GC: release the resource AND delete its backing dir."""
    async with db_session() as s:
        resource = await s.get(ProvisionedResourceRecord, workspace_id)
        resource.status = "released"
        uri = resource.uri
        await s.commit()
    shutil.rmtree(uri, ignore_errors=True)


class TestRestoreAfterSourceGone:
    async def test_restore_works_after_source_workspace_deleted(self, fresh_db, services):
        """THE regression this module exists for: snapshot → source GC →
        restore must produce REAL bytes, not empty files."""
        principal_id = await _create_principal()
        content = b"persistent work product v1"
        ws_a, ws_a_path = await _provision_workspace(services, principal_id)
        (ws_a_path / "out.bin").write_bytes(content)
        (ws_a_path / "sub").mkdir()
        (ws_a_path / "sub" / "nested.txt").write_text("nested content")

        snap = await _snapshot(services, principal_id, ws_a)

        # The source workspace is released and its directory removed —
        # exactly what the provisioning reaper does at TTL expiry.
        await _release_workspace(ws_a)

        ws_b, ws_b_path = await _provision_workspace(services, principal_id)
        result = await _restore(services, principal_id, snap["snapshot_id"], ws_b)

        assert result["complete"] is True
        assert result["skipped_files"] == 0
        assert result["from_blob"] == 2
        assert result["from_source_workspace"] == 0
        assert (ws_b_path / "out.bin").read_bytes() == content
        assert (ws_b_path / "sub" / "nested.txt").read_text() == "nested content"

    async def test_empty_file_regression_is_gone(self, fresh_db, services):
        """The old behavior: source gone → touch() → empty file with
        complete=True. Verify that cannot happen silently anymore."""
        principal_id = await _create_principal()
        ws_a, ws_a_path = await _provision_workspace(services, principal_id)
        (ws_a_path / "data.txt").write_text("not-empty")
        snap = await _snapshot(services, principal_id, ws_a)
        await _release_workspace(ws_a)

        ws_b, ws_b_path = await _provision_workspace(services, principal_id)
        result = await _restore(services, principal_id, snap["snapshot_id"], ws_b)

        restored = (ws_b_path / "data.txt").read_text()
        if not result["complete"]:
            # An incomplete restore must be REPORTED as incomplete.
            assert result["skipped_files"] > 0
        assert restored == "not-empty"


class TestBlobDeduplication:
    async def test_identical_content_stored_once(self, fresh_db, services):
        store = services.blob_store
        assert store is not None
        principal_id = await _create_principal()

        ws_a, ws_a_path = await _provision_workspace(services, principal_id)
        (ws_a_path / "same.txt").write_text("identical bytes")
        snap_a = await _snapshot(services, principal_id, ws_a)

        ws_b, ws_b_path = await _provision_workspace(services, principal_id)
        (ws_b_path / "same.txt").write_text("identical bytes")
        snap_b = await _snapshot(services, principal_id, ws_b)

        assert snap_a["snapshot_id"] != snap_b["snapshot_id"]
        # Two snapshots, one physical blob.
        assert store.count_blobs() == 1

    async def test_snapshot_is_idempotent_with_blobs(self, fresh_db, services):
        store = services.blob_store
        principal_id = await _create_principal()
        ws, ws_path = await _provision_workspace(services, principal_id)
        (ws_path / "f.txt").write_text("content")

        snap1 = await _snapshot(services, principal_id, ws)
        snap2 = await _snapshot(services, principal_id, ws)

        assert snap1["snapshot_id"] == snap2["snapshot_id"]
        assert snap2["idempotent"] is True
        assert store.count_blobs() == 1


class TestRestoreIntegrity:
    async def test_restore_prefers_blob_over_tampered_source(self, fresh_db, services):
        """The blob is the source of truth; a tampered source workspace
        must not poison the restore."""
        principal_id = await _create_principal()
        ws_a, ws_a_path = await _provision_workspace(services, principal_id)
        (ws_a_path / "file.txt").write_text("original")
        snap = await _snapshot(services, principal_id, ws_a)

        # Tamper with the source AFTER the snapshot.
        (ws_a_path / "file.txt").write_text("tampered!!")

        ws_b, ws_b_path = await _provision_workspace(services, principal_id)
        result = await _restore(services, principal_id, snap["snapshot_id"], ws_b)

        assert result["from_blob"] == 1
        assert (ws_b_path / "file.txt").read_text() == "original"

    async def test_restore_without_blob_store_uses_source_and_verifies(self, fresh_db, services):
        """Legacy path (no blob store wired): source copy still verified
        against the recorded digest."""
        services.blob_store = None  # simulate a legacy container
        principal_id = await _create_principal()
        ws_a, ws_a_path = await _provision_workspace(services, principal_id)
        (ws_a_path / "file.txt").write_text("legacy content")
        snap = await _snapshot(services, principal_id, ws_a)

        ws_b, ws_b_path = await _provision_workspace(services, principal_id)
        result = await _restore(services, principal_id, snap["snapshot_id"], ws_b)

        assert result["complete"] is True
        assert result["from_source_workspace"] == 1
        assert (ws_b_path / "file.txt").read_text() == "legacy content"


class TestBlobStoreUnit:
    async def test_put_bytes_roundtrip_and_dedup(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "b")
        d1 = store.put_bytes(b"hello")
        d2 = store.put_bytes(b"hello")
        d3 = store.put_bytes(b"world")
        assert d1 == d2
        assert d1 != d3
        assert store.has(d1)
        assert store.stat_blob(d1) == 5
        assert store.count_blobs() == 2

    async def test_copy_to_verifies_digest(self, tmp_path):
        import hashlib

        store = ContentAddressedBlobStore(tmp_path / "b")
        content = b"verify me"
        digest = store.put_bytes(content)
        dest = tmp_path / "dest" / "out.txt"
        size = store.copy_to(digest, dest)
        assert size == len(content)
        assert dest.read_bytes() == content
        assert hashlib.sha256(content).hexdigest() == digest

    async def test_copy_to_detects_tampered_blob(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "b")
        digest = store.put_bytes(b"trusted content")
        blob_path = store.path_for(digest)
        blob_path.write_bytes(b"tampered content!")
        with pytest.raises(BlobStoreError):
            store.copy_to(digest, tmp_path / "out.bin")

    async def test_copy_to_missing_blob_raises(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "b")
        with pytest.raises(BlobStoreError):
            store.copy_to("0" * 64, tmp_path / "out.bin")

    async def test_invalid_digest_rejected(self, tmp_path):
        store = ContentAddressedBlobStore(tmp_path / "b")
        with pytest.raises(BlobStoreError):
            store.path_for("../not-a-digest")
