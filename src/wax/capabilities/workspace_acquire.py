"""workspace.acquire — artifact acquisition into an isolated workspace.

The environment mechanism behind "I need a dependency/resource to do this
work" (mission §18, ADR-0016). A generalization of package acquisition:
the runtime retrieves an ARTIFACT from an allowed public source into a
principal-owned scratch workspace, verifying integrity before anything is
kept, caching by content address, and auditing provenance.

What this mechanism IS:
- dependency resolution at the artifact level (URL + pinned hash), the
  same primitive every package ecosystem bottoms out at
- supply-chain safety: SHA-256 integrity REQUIRED, host allowlist,
  egress through the network boundary (SSRF-guarded, per-hop
  re-validated), hard size cap, hard timeout, atomic writes
- content-addressed caching: cache key = sha256; a cache hit is verified
  by hashing the cached file before it is trusted
- isolation: artifacts land ONLY inside an active scratch_dir owned by
  the requesting principal — the runtime filesystem is never touched
- provenance: every acquisition is audited (url, host, hash, bytes,
  workspace, cache_hit)

What this mechanism is NOT:
- it never EXECUTES the artifact (no install hooks, no setup.py) — the
  artifact is data; importing/running it happens only inside code.run
  under that capability's own authority and isolation
- it is not "install-python-package" as a WAX feature — PyPI is merely
  the default allowlist; the allowlist is deployment policy
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from wax.capabilities.contracts import CapabilityDescriptor, InvocationContext
from wax.capabilities.registry import CapabilityRegistry
from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.security.network import FetchPolicy, NetworkBoundaryError, guarded_get

log = get_logger(__name__)

ACQUIRE_DESCRIPTOR = CapabilityDescriptor(
    name="workspace.acquire",
    description=(
        "Acquire an external artifact (package archive, dataset, model "
        "file) into one of your scratch workspaces. The exact SHA-256 "
        "hash of the content is REQUIRED before download — the runtime "
        "verifies it and refuses anything else (supply-chain safety). "
        "Only allowlisted public sources are reachable. The artifact is "
        "DATA: it is never executed or installed by acquisition itself."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "workspace_resource_id": {
                "type": "string",
                "description": "An active scratch.workspace resource id owned by you",
            },
            "url": {
                "type": "string",
                "description": "Direct URL of the artifact on an allowlisted host",
            },
            "sha256": {
                "type": "string",
                "description": "Expected SHA-256 hex digest of the artifact content",
            },
            "filename": {
                "type": "string",
                "description": "Name inside the workspace (default: derived from URL)",
            },
        },
        "required": ["workspace_resource_id", "url", "sha256"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "sha256": {"type": "string"},
            "bytes": {"type": "integer"},
            "cache_hit": {"type": "boolean"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=90.0,  # above the acquisition timeout
    idempotent=True,  # same inputs → same artifact; safe to retry
    is_destructive=False,
)

_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class AcquisitionError(Exception):
    """An acquisition was refused or failed. Message is caller-safe."""


def _allowed_hosts(settings: Any) -> list[str]:
    return [
        host.strip().lower()
        for host in (settings.acquisition_allowed_hosts or "").split(",")
        if host.strip()
    ]


def _derive_filename(url: str) -> str:
    name = Path(urlsplit(url).path).name
    if not name or not _FILENAME_RE.match(name):
        raise AcquisitionError(
            "cannot derive a safe filename from the URL; supply 'filename'"
        )
    return name


class ArtifactAcquirer:
    """The acquisition engine. Bound to this container (no module globals)."""

    def __init__(self, services: RuntimeServices) -> None:
        self._services = services
        root = Path(services.settings.provisioning_root)
        self._cache_dir = root / ".artifact-cache"
        self._max_bytes = int(services.settings.acquisition_max_bytes)
        self._timeout = float(services.settings.acquisition_timeout_seconds)
        self._transport = getattr(services, "acquisition_transport", None)

    # --- Verification helpers ---------------------------------------------

    @staticmethod
    def _check_host(url: str, allowed: list[str]) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            raise AcquisitionError("URL has no hostname")
        if not allowed:
            raise AcquisitionError(
                "artifact acquisition is disabled: no allowlisted sources "
                "are configured in this deployment"
            )
        if host not in allowed:
            raise AcquisitionError(
                f"host {host!r} is not an allowlisted artifact source"
            )
        return host

    @staticmethod
    def _check_hash(sha256: str) -> str:
        if not sha256 or not _SHA256_RE.match(sha256):
            raise AcquisitionError(
                "sha256 must be the 64-hex-character digest of the exact "
                "content (integrity is mandatory, not optional)"
            )
        return sha256.lower()

    def _cache_path(self, sha256: str) -> Path:
        return self._cache_dir / sha256

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_cached(self, sha256: str) -> Path | None:
        """A cache hit only counts if the cached file STILL hashes right."""
        cached = self._cache_path(sha256)
        if not cached.is_file():
            return None
        try:
            if self._hash_file(cached) == sha256:
                return cached
        except OSError:
            pass
        # Corrupted cache entry: discard it (real failure would be silent
        # supply-chain drift; deleting is the honest recovery).
        try:
            cached.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    # --- Download -----------------------------------------------------------

    async def _download_to_cache(self, url: str, sha256: str) -> Path:
        """Download through the network boundary into the cache (atomic),
        verifying the hash. Any mismatch leaves nothing behind."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self._cache_dir / f".tmp-{os.getpid()}-{sha256[:16]}"
        policy = FetchPolicy(
            max_bytes=self._max_bytes,
            total_timeout=self._timeout,
        )
        try:
            response = await guarded_get(url, policy=policy, transport=self._transport)
        except NetworkBoundaryError as e:
            self._services.metrics.artifact_rejected("network_boundary")
            raise AcquisitionError(f"download blocked by the network boundary: {e}") from e

        content = response.content
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            self._services.metrics.artifact_rejected("hash_mismatch")
            raise AcquisitionError(
                "integrity verification failed: content does not match the "
                "declared sha256 — refusing to store it"
            )
        # Atomic write: temp file + replace, so a crash never leaves a
        # half-written artifact that a later cache read could trust.
        with open(temp_path, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._cache_path(sha256))
        return self._cache_path(sha256)

    # --- Public entry ---------------------------------------------------------

    async def acquire(
        self,
        *,
        workspace_path: str,
        url: str,
        sha256: str,
        filename: str | None,
    ) -> dict[str, Any]:
        allowed = _allowed_hosts(self._services.settings)
        host = self._check_host(url, allowed)
        sha256 = self._check_hash(sha256)
        filename = filename or _derive_filename(url)
        if not _FILENAME_RE.match(filename):
            raise AcquisitionError(
                "filename must be 1-128 characters of [A-Za-z0-9._-]"
            )

        workspace = Path(workspace_path)
        # Containment: the destination is inside the verified workspace and
        # the workspace's own root is what the caller proved ownership of.
        dest = workspace / filename
        if not str(dest.resolve()).startswith(str(workspace.resolve())):
            raise AcquisitionError("destination escapes the workspace")

        cache_hit = False
        cached = self._verify_cached(sha256)
        if cached is None:
            await self._download_to_cache(url, sha256)
        else:
            cache_hit = True

        # Copy into the workspace atomically; verify what landed.
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp_dest = dest.with_name(f".tmp-{filename}")
        with open(self._cache_path(sha256), "rb") as src, open(temp_dest, "wb") as dst:
            dst.write(src.read())
        landed = self._hash_file(temp_dest)
        if landed != sha256:
            temp_dest.unlink(missing_ok=True)
            self._services.metrics.artifact_rejected("landed_hash_mismatch")
            raise AcquisitionError("integrity verification failed after write")
        os.replace(temp_dest, dest)
        size = dest.stat().st_size

        self._services.metrics.artifact_acquired(cache_hit=cache_hit)
        log.info(
            "artifact.acquired",
            host=host,
            sha256=sha256,
            bytes=size,
            cache_hit=cache_hit,
            filename=filename,
        )
        return {
            "path": str(dest),
            "sha256": sha256,
            "bytes": size,
            "cache_hit": cache_hit,
        }


def register_workspace_acquire_capability(
    registry: CapabilityRegistry, services: RuntimeServices
) -> None:
    """Register workspace.acquire bound to this container."""

    async def workspace_acquire_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from sqlalchemy import select
        from ulid import ULID

        from wax.observability.audit import record_audit_event
        from wax.state.artifact_models import ArtifactRecord
        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord

        workspace_resource_id = inputs.get("workspace_resource_id")
        url = inputs.get("url")
        sha256 = inputs.get("sha256")
        filename = inputs.get("filename")
        if not workspace_resource_id or not isinstance(workspace_resource_id, str):
            raise ValueError("workspace_resource_id is required")
        if not url or not isinstance(url, str):
            raise ValueError("url is required")

        # Ownership: the workspace must be an ACTIVE scratch_dir owned by
        # the requesting principal and not yet expired.
        now = datetime.now(UTC)
        async with db_session() as session:
            result = await session.execute(
                select(ProvisionedResourceRecord).where(
                    ProvisionedResourceRecord.id == workspace_resource_id,
                    ProvisionedResourceRecord.status == "active",
                )
            )
            resource = result.scalar_one_or_none()
            if (
                resource is None
                or resource.principal_id != ctx.principal_id
                or resource.kind != "scratch_dir"
            ):
                raise ValueError(
                    "workspace_resource_id is not an active scratch_dir "
                    "owned by the requesting principal"
                )
            expires = resource.expires_at
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires <= now:
                    raise ValueError("workspace has expired; provision a new one")
            workspace_path = resource.uri

        acquirer = ArtifactAcquirer(services)
        try:
            result = await acquirer.acquire(
                workspace_path=workspace_path,
                url=url,
                sha256=sha256 if isinstance(sha256, str) else "",
                filename=filename if isinstance(filename, str) else None,
            )
        except AcquisitionError as e:
            raise ValueError(str(e)) from e

        async with db_session() as session:
            # First-class artifact record (ADR-0023, mission §56): the
            # acquisition boundary is where integrity is computed, so it
            # is where artifact state is born — owner, integrity, size,
            # workspace, provenance, and a TTL that mirrors the
            # workspace's (the file cannot outlive its workspace).
            artifact = ArtifactRecord(
                id=str(ULID()),
                principal_id=ctx.principal_id,
                workspace_resource_id=workspace_resource_id,
                filename=Path(result["path"]).name,
                path=str(Path(result["path"]).relative_to(Path(workspace_path))),
                sha256=result["sha256"],
                size_bytes=result["bytes"],
                source="workspace.acquire",
                execution_id=ctx.execution_id,
                metadata_json={
                    "url_host": urlsplit(url).hostname or "",
                    "cache_hit": result["cache_hit"],
                },
                expires_at=expires,
            )
            session.add(artifact)
            await record_audit_event(
                session,
                actor_principal_id=ctx.principal_id,
                actor_kind="ai",
                event_kind="artifact.acquired",
                outcome="success",
                payload={
                    "url_host": urlsplit(url).hostname or "",
                    "sha256": result["sha256"],
                    "bytes": result["bytes"],
                    "cache_hit": result["cache_hit"],
                    "workspace_resource_id": workspace_resource_id,
                    "filename": Path(result["path"]).name,
                },
            )
            await session.commit()

        return {**result, "artifact_id": artifact.id}

    registry.register(ACQUIRE_DESCRIPTOR, workspace_acquire_impl)
