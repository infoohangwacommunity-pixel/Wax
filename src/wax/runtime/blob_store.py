"""Content-addressed blob store (P0-Workspace).

A workspace snapshot used to capture only METADATA (path, sha256, size) —
the bytes lived in the source workspace. When provisioning released the
source workspace, `workspace.restore` could no longer copy real bytes and
silently produced EMPTY files. That is dishonest persistence: a snapshot
must be restorable on its own, forever.

This module fixes that. On capture, every file's bytes are written into a
local content-addressed store:

    <root>/<digest[0:2]>/<digest>

- Content address = SHA-256 of the bytes (the same digest already recorded
  in the snapshot's file entries — no second hashing scheme).
- Writes are atomic (temp file + rename) and deduplicated: storing the
  same digest twice is a no-op, so re-snapshots of unchanged workspaces
  cost nothing.
- Reads verify the digest after copy: a corrupted/tampered blob is never
  restored silently.

Blob GC is deliberately NOT implemented here: snapshots may reference a
blob for an unbounded time (restore-after-expiry is the entire point), so
deletion requires a reference-counted sweep over live snapshots. Until
that sweep exists, blobs accumulate — bounded storage is traded for
correct persistence, and the trade is documented (the runtime refuses
quiet data loss).

This is runtime infrastructure (filesystem I/O): it lives in wax.runtime,
NOT wax.core (INV-09).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from wax.runtime.logging import get_logger

log = get_logger(__name__)

# 4 MiB streaming chunk — snapshots may contain files larger than RAM budgets.
_CHUNK_SIZE = 4 * 1024 * 1024


class BlobStoreError(RuntimeError):
    """Raised when a blob operation fails integrity verification."""


class ContentAddressedBlobStore:
    """Local content-addressed store keyed by SHA-256."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._blobs_dir = self._root / "blobs"
        self._blobs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------
    # Core paths
    # ------------------------------------------------------------------

    def path_for(self, digest: str) -> Path:
        """The shard path for a digest: <root>/<aa>/<digest>."""
        if not _is_sha256(digest):
            raise BlobStoreError(f"not a valid sha256 digest: {digest!r}")
        return self._blobs_dir / digest[:2] / digest

    def has(self, digest: str) -> bool:
        """Whether the blob exists on disk."""
        try:
            return self.path_for(digest).is_file()
        except BlobStoreError:
            return False

    # ------------------------------------------------------------------
    # Writes (atomic + deduplicated)
    # ------------------------------------------------------------------

    def put_bytes(self, content: bytes) -> str:
        """Store bytes; returns the sha256 digest. Deduplicated by digest."""
        digest = hashlib.sha256(content).hexdigest()
        target = self.path_for(digest)
        if target.is_file():
            return digest  # content-addressed dedup
        self._atomic_write(target, lambda tmp: _write_all(tmp, content))
        return digest

    def put_file(self, source: Path) -> tuple[str, int]:
        """Store a file's bytes; returns (sha256, size).

        Streams in chunks so a large workspace file never has to fit in
        memory. Deduplicated: if the digest already exists, the source is
        hashed and skipped.
        """
        source = Path(source)
        # Hash first (streaming), then decide whether a write is needed.
        digest = hashlib.sha256()
        size = 0
        with open(source, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        hexdigest = digest.hexdigest()

        target = self.path_for(hexdigest)
        if target.is_file():
            return hexdigest, size

        def _copy(tmp: Path) -> None:
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())

        self._atomic_write(target, _copy)
        return hexdigest, size

    def _atomic_write(self, target: Path, writer) -> None:
        """Write via a temp file in the same directory, then rename."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-blob-", dir=str(target.parent))
        tmp = Path(tmp_name)
        os.close(fd)
        try:
            writer(tmp)
            # Content integrity gate: what landed must hash to the address
            # it is stored under. A silent corruption here would poison
            # every future restore.
            landed = _sha256_file(tmp)
            if landed != target.name:
                tmp.unlink(missing_ok=True)
                raise BlobStoreError(
                    f"blob integrity check failed after write: "
                    f"stored under {target.name}, content hashes to {landed}"
                )
            os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # Reads (digest-verified)
    # ------------------------------------------------------------------

    def copy_to(self, digest: str, destination: Path) -> int:
        """Copy a blob to a destination file; returns bytes copied.

        Raises BlobStoreError if the blob does not exist or its bytes no
        longer hash to the digest (corruption/tamper is NEVER restored
        silently — the runtime refuses quiet data loss).
        """
        source = self.path_for(digest)
        if not source.is_file():
            raise BlobStoreError(f"blob not found: {digest}")
        actual = _sha256_file(source)
        if actual != digest:
            raise BlobStoreError(
                f"blob integrity check failed on read: {digest} hashes to {actual}"
            )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".tmp-restore-", dir=str(destination.parent))
        tmp = Path(tmp_name)
        os.close(tmp_fd)
        try:
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                while True:
                    chunk = src.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, destination)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return size

    def stat_blob(self, digest: str) -> int | None:
        """The stored blob's size, or None when absent."""
        p = self.path_for(digest)
        if not p.is_file():
            return None
        return p.stat().st_size

    def count_blobs(self) -> int:
        """Number of distinct blobs currently stored (observability/tests)."""
        count = 0
        for shard in self._blobs_dir.iterdir():
            if shard.is_dir():
                count += sum(1 for p in shard.iterdir() if p.is_file())
        return count


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _write_all(path: Path, content: bytes) -> None:
    with open(path, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
