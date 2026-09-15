"""workspace.acquire — artifact acquisition tests (supply-chain safety).

The environment mechanism for "I need X to do this work". Security is the
point, so these tests are adversarial:

- integrity: hash mismatch refuses the artifact and leaves nothing behind
- allowlist: non-allowlisted hosts and disabled acquisition refuse honestly
- network boundary: SSRF targets (loopback, RFC1918, metadata) are blocked
- resource limits: oversize artifacts are refused
- cache: hits verified by hash; corrupted entries are discarded
- isolation: acquisition requires an ACTIVE, OWNED scratch workspace
- provenance: audit events record what was acquired from where
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from wax.authority.seed import ensure_principal_role, seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.capabilities.workspace_acquire import AcquisitionError, ArtifactAcquirer
from wax.runtime.provisioning import ProvisioningService
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

TEST_PRINCIPAL = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ALLOWED_HOST = "files.pythonhosted.org"
CONTENT = b"fake-package-content-for-wax-acquisition-tests"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
async def fresh_db(test_settings, tmp_path):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    # Isolated provisioning root per test: artifacts land in a real
    # directory, but the content-addressed cache never leaks between tests.
    test_settings.__dict__["provisioning_root"] = str(tmp_path / "wax-resources")
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await ensure_principal_role(session, TEST_PRINCIPAL)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


def _acquirer(services: RuntimeServices, body: bytes = CONTENT) -> ArtifactAcquirer:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    services.acquisition_transport = httpx.MockTransport(handler)
    return ArtifactAcquirer(services)


async def _workspace(
    services: RuntimeServices, *, owner: str = TEST_PRINCIPAL, ttl: int = 900
) -> str:
    async with db_session() as session:
        record = await ProvisioningService(services.settings).provision_scratch_dir(
            session, principal_id=owner, ttl_seconds=max(ttl, 10)
        )
        if ttl <= 0:
            # Force expiry (provisioning refuses negative TTLs by design).
            record.expires_at = datetime.now(UTC) - timedelta(seconds=10)
        await session.commit()
        return record.id


class TestIntegrity:
    async def test_matching_hash_is_acquired(self, fresh_db, services) -> None:
        from wax.runtime.provisioning import ProvisioningService

        async with db_session() as session:
            record = await ProvisioningService(services.settings).provision_scratch_dir(
                session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
            )
            await session.commit()
        acquirer = _acquirer(services)
        result = await acquirer.acquire(
            workspace_path=record.uri,
            url=f"https://{ALLOWED_HOST}/pkg-1.0.tar.gz",
            sha256=CONTENT_SHA,
            filename="pkg-1.0.tar.gz",
        )
        assert result["cache_hit"] is False
        assert result["sha256"] == CONTENT_SHA
        assert result["bytes"] == len(CONTENT)
        with open(Path(record.uri) / result["path"], "rb") as handle:
            assert handle.read() == CONTENT

    async def test_hash_mismatch_refuses_and_leaves_nothing(self, fresh_db, services) -> None:
        from wax.runtime.provisioning import ProvisioningService

        async with db_session() as session:
            record = await ProvisioningService(services.settings).provision_scratch_dir(
                session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
            )
            await session.commit()
        wrong_sha = hashlib.sha256(b"totally different content").hexdigest()
        acquirer = _acquirer(services)
        with pytest.raises(AcquisitionError, match="integrity"):
            await acquirer.acquire(
                workspace_path=record.uri,
                url=f"https://{ALLOWED_HOST}/pkg-1.0.tar.gz",
                sha256=wrong_sha,
                filename="pkg-1.0.tar.gz",
            )

        workspace = Path(record.uri)
        assert not (workspace / "pkg-1.0.tar.gz").exists(), "no artifact is kept"
        assert not any(workspace.glob(".tmp-*")), "no temp residue"


class TestSourcePolicy:
    async def test_non_allowlisted_host_is_refused(self, fresh_db, services) -> None:
        acquirer = _acquirer(services)
        with pytest.raises(AcquisitionError, match="allowlisted"):
            await acquirer.acquire(
                workspace_path="/tmp/whatever",
                url="https://evil.example.com/pkg.tar.gz",
                sha256=CONTENT_SHA,
                filename="pkg.tar.gz",
            )

    async def test_empty_allowlist_disables_acquisition(self, fresh_db, services) -> None:
        services.settings.__dict__["acquisition_allowed_hosts"] = ""
        acquirer = _acquirer(services)
        with pytest.raises(AcquisitionError, match="disabled"):
            await acquirer.acquire(
                workspace_path="/tmp/whatever",
                url=f"https://{ALLOWED_HOST}/pkg.tar.gz",
                sha256=CONTENT_SHA,
                filename="pkg.tar.gz",
            )

    async def test_hash_is_mandatory_and_well_formed(self, fresh_db, services) -> None:
        acquirer = _acquirer(services)
        for bad in ("", "abc123", "Z" * 64):
            with pytest.raises(AcquisitionError, match="sha256"):
                await acquirer.acquire(
                    workspace_path="/tmp/whatever",
                    url=f"https://{ALLOWED_HOST}/pkg.tar.gz",
                    sha256=bad,
                    filename="pkg.tar.gz",
                )


class TestNetworkBoundary:
    async def test_metadata_endpoint_refused_before_any_fetch(self, fresh_db, services) -> None:
        """Cloud-metadata targets are not allowlisted sources; the refusal
        happens before any bytes move (defense layer 1)."""
        acquirer = _acquirer(services)
        with pytest.raises(AcquisitionError, match="allowlisted"):
            await acquirer.acquire(
                workspace_path="/tmp/whatever",
                url="http://169.254.169.254/latest/meta-data/",
                sha256=CONTENT_SHA,
                filename="metadata.txt",
            )

    async def test_redirect_to_private_is_blocked_by_boundary(self, fresh_db, services) -> None:
        """A public allowlisted host that redirects to loopback dies at the
        egress boundary (defense layer 2 — per-hop re-validation)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == ALLOWED_HOST:
                return httpx.Response(302, headers={"location": "http://127.0.0.1:9/x"})
            return httpx.Response(200, content=b"no")

        services.acquisition_transport = httpx.MockTransport(handler)
        acquirer = ArtifactAcquirer(services)
        with pytest.raises(AcquisitionError, match="non-public address"):
            await acquirer.acquire(
                workspace_path="/tmp/whatever",
                url=f"https://{ALLOWED_HOST}/redirect",
                sha256=CONTENT_SHA,
                filename="pkg.bin",
            )


class TestResourceLimits:
    async def test_oversize_artifact_is_refused(self, fresh_db, services) -> None:
        services.settings.__dict__["acquisition_max_bytes"] = 10
        acquirer = _acquirer(services, body=b"x" * 64)
        with pytest.raises(AcquisitionError):
            await acquirer.acquire(
                workspace_path="/tmp/whatever",
                url=f"https://{ALLOWED_HOST}/big.bin",
                sha256=hashlib.sha256(b"x" * 64).hexdigest(),
                filename="big.bin",
            )


class TestCaching:
    async def test_cache_hit_avoids_second_download(self, fresh_db, services) -> None:
        from wax.runtime.provisioning import ProvisioningService

        async with db_session() as session:
            record = await ProvisioningService(services.settings).provision_scratch_dir(
                session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
            )
            await session.commit()

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=CONTENT)

        services.acquisition_transport = httpx.MockTransport(handler)
        acquirer = ArtifactAcquirer(services)

        first = await acquirer.acquire(
            workspace_path=record.uri,
            url=f"https://{ALLOWED_HOST}/pkg.tar.gz",
            sha256=CONTENT_SHA,
            filename="pkg.tar.gz",
        )
        assert first["cache_hit"] is False
        assert calls["n"] == 1

        second = await acquirer.acquire(
            workspace_path=record.uri,
            url=f"https://{ALLOWED_HOST}/pkg.tar.gz",
            sha256=CONTENT_SHA,
            filename="pkg2.tar.gz",
        )
        assert second["cache_hit"] is True
        assert calls["n"] == 1, "no second network call"

    async def test_corrupted_cache_entry_is_discarded(self, fresh_db, services) -> None:
        from wax.runtime.provisioning import ProvisioningService

        async with db_session() as session:
            record = await ProvisioningService(services.settings).provision_scratch_dir(
                session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
            )
            await session.commit()

        acquirer = _acquirer(services)
        # Poison the cache with WRONG content under the right name.
        cache_dir = acquirer._cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        poisoned = cache_dir / CONTENT_SHA
        poisoned.write_bytes(b"poisoned bytes")

        result = await acquirer.acquire(
            workspace_path=record.uri,
            url=f"https://{ALLOWED_HOST}/pkg.tar.gz",
            sha256=CONTENT_SHA,
            filename="pkg.tar.gz",
        )
        assert result["cache_hit"] is False, "poisoned entry not trusted"
        with open(Path(record.uri) / result["path"], "rb") as handle:
            assert handle.read() == CONTENT
        assert not poisoned.exists() or poisoned.read_bytes() == CONTENT


class TestWorkspaceIsolation:
    async def test_capability_requires_owned_active_workspace(self, fresh_db, services) -> None:
        """Through the full gate chain: another principal's workspace, an
        expired one, or a missing one all refuse."""
        other_workspace = await _workspace(services, owner="01OTHERPRINCIPAL0000000")
        expired = await _workspace(services, ttl=-1)

        async with db_session() as session:
            invoker = services.invoker(session)

            async def _try(resource_id: str):
                return await invoker.invoke(
                    CapabilityInvocationRequest(
                        capability_name="workspace.acquire",
                        principal_id=TEST_PRINCIPAL,
                        inputs={
                            "workspace_resource_id": resource_id,
                            "url": f"https://{ALLOWED_HOST}/pkg.tar.gz",
                            "sha256": CONTENT_SHA,
                        },
                    )
                )

            for resource_id in (other_workspace, expired, "01NOSUCHRESOURCE0000000"):
                result = await _try(resource_id)
                assert result.outcome != "success", resource_id


class TestProvenance:
    async def test_acquisition_is_audited(self, fresh_db, services) -> None:
        """Through the full capability path: what was acquired, from where,
        into which workspace — durable provenance, not vibes."""
        from sqlalchemy import select

        from wax.runtime.provisioning import ProvisioningService
        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            record = await ProvisioningService(services.settings).provision_scratch_dir(
                session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
            )
            await session.commit()

        # The transport mock must be attached BEFORE the capability builds
        # its acquirer (the real PyPI host would serve different bytes).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=CONTENT)

        services.acquisition_transport = httpx.MockTransport(handler)

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="workspace.acquire",
                    principal_id=TEST_PRINCIPAL,
                    inputs={
                        "workspace_resource_id": record.id,
                        "url": f"https://{ALLOWED_HOST}/pkg-1.0.tar.gz",
                        "sha256": CONTENT_SHA,
                        "filename": "pkg-1.0.tar.gz",
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success"

        async with db_session() as session:
            events = list(
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.event_kind == "artifact.acquired")
                    )
                ).scalars()
            )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["sha256"] == CONTENT_SHA
        assert payload["url_host"] == ALLOWED_HOST
        assert payload["bytes"] == len(CONTENT)
        assert payload["cache_hit"] is False
        _ = datetime.now(UTC)
