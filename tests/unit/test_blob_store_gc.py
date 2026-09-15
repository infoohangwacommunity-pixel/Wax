"""ContentAddressedBlobStore — stats + reference-driven garbage collection.

The blob store deliberately never deletes anything on its own: GC is an
explicit sweep over digests the caller proves live (the union of file
digests across workspace snapshot records). These tests pin the safety
properties: only sha256-shaped files are candidates, live digests are
untouched, empty shards are pruned, and the counters are honest.
"""

from __future__ import annotations

import pytest

from wax.runtime.blob_store import BlobStoreError, ContentAddressedBlobStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return ContentAddressedBlobStore(tmp_path / "root")


class TestStats:
    def test_empty_store_reports_zeroes(self, store):
        assert store.stats() == {"objects": 0, "bytes": 0}

    def test_stats_counts_objects_and_bytes(self, store):
        store.put_bytes(b"hello")  # 5 bytes
        store.put_bytes(b"world!!!!")  # 9 bytes
        store.put_bytes(b"hello")  # dedup — no new object
        stats = store.stats()
        assert stats == {"objects": 2, "bytes": 14}

    def test_stats_ignores_temp_and_foreign_files(self, store, tmp_path):
        store.put_bytes(b"counted")
        # A temp file from an interrupted write + a foreign file.
        shard = store.root / "blobs" / "ab"
        shard.mkdir(parents=True, exist_ok=True)
        (shard / ".tmp-blob-1234").write_bytes(b"ignored")
        (shard / "notadigest").write_bytes(b"ignored")
        stats = store.stats()
        assert stats["objects"] == 1
        assert stats["bytes"] == 7


class TestGarbageCollection:
    def test_removes_only_unreferenced_blobs(self, store):
        live = store.put_bytes(b"live data")
        stale = store.put_bytes(b"stale data")
        result = store.collect_garbage({live})
        assert result == {"removed": 1, "reclaimed_bytes": len(b"stale data")}
        assert store.has(live)
        assert not store.has(stale)

    def test_no_live_digests_deletes_everything(self, store):
        store.put_bytes(b"a")
        store.put_bytes(b"b")
        result = store.collect_garbage(set())
        assert result == {"removed": 2, "reclaimed_bytes": 2}
        assert store.stats() == {"objects": 0, "bytes": 0}

    def test_live_set_is_never_deleted_even_if_missing(self, store):
        live = store.put_bytes(b"live")
        # A caller may pass digests whose blob vanished (integrity problem,
        # not GC's business): GC must not crash and must not delete others.
        other = store.put_bytes(b"other")
        result = store.collect_garbage({live, "f" * 64})
        assert result == {"removed": 1, "reclaimed_bytes": len(b"other")}
        assert store.has(live)
        assert not store.has(other)

    def test_empty_shard_dirs_are_pruned(self, store):
        store.put_bytes(b"only blob")
        shard = store.root / "blobs" / (store.put_bytes(b"x")[:2])
        assert shard.is_dir()
        store.collect_garbage(set())
        assert not shard.exists()

    def test_foreign_files_are_never_touched(self, store):
        digest = store.put_bytes(b"keep me")
        shard_dir = store.root / "blobs" / digest[:2]
        stranger = shard_dir / ("z" * 64)  # sha256-shaped length but not hex
        stranger.write_bytes(b"foreign")
        result = store.collect_garbage(set())
        # The foreign file name isn't valid hex → not a GC candidate.
        assert result["removed"] == 1
        assert stranger.exists()

    def test_roundtrip_after_gc_restores_nothing_for_collected(self, store, tmp_path):
        stale = store.put_bytes(b"gone")
        store.collect_garbage(set())
        with pytest.raises(BlobStoreError, match="blob not found"):
            store.copy_to(stale, tmp_path / "out.bin")
