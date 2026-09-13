"""Tests for Phase T — Media Intelligence Pipeline."""

from __future__ import annotations

import asyncio
import time

import pytest

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)
from wax.media.pipeline import MediaPipeline, classify_mime_type


class _SlowExtractor(MediaExtractor):
    """Extractor that sleeps longer than the timeout — for testing timeouts."""

    @property
    def name(self) -> str:
        return "slow_extractor"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.IMAGE})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        await asyncio.sleep(5.0)
        return MediaExtractionResult(
            media_id=source.media_id,
            kind=source.kind,
            success=True,
            extracted_text="should never see this",
            mime_type=source.mime_type,
            extractor_name=self.name,
            duration_ms=5000,
        )


class _FailingExtractor(MediaExtractor):
    """Extractor that always raises."""

    @property
    def name(self) -> str:
        return "failing_extractor"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.AUDIO})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        raise RuntimeError("intentional extractor failure")


class _WorkingExtractor(MediaExtractor):
    """Extractor that returns a fixed string."""

    @property
    def name(self) -> str:
        return "working_extractor"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.DOCUMENT})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        return MediaExtractionResult(
            media_id=source.media_id,
            kind=source.kind,
            success=True,
            extracted_text="this is extracted document text",
            mime_type=source.mime_type,
            extractor_name=self.name,
            duration_ms=10.0,
        )


class TestClassifyMimeType:
    def test_image(self) -> None:
        assert classify_mime_type("image/jpeg") == MediaKind.IMAGE
        assert classify_mime_type("image/png") == MediaKind.IMAGE

    def test_audio(self) -> None:
        assert classify_mime_type("audio/ogg") == MediaKind.AUDIO
        assert classify_mime_type("audio/mpeg") == MediaKind.AUDIO

    def test_video(self) -> None:
        assert classify_mime_type("video/mp4") == MediaKind.VIDEO

    def test_document_pdf(self) -> None:
        assert classify_mime_type("application/pdf") == MediaKind.DOCUMENT

    def test_document_docx(self) -> None:
        assert classify_mime_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ) == MediaKind.DOCUMENT

    def test_unknown(self) -> None:
        assert classify_mime_type("application/x-unknown") == MediaKind.UNKNOWN


class TestMediaPipelineDefaults:
    async def test_default_pipeline_uses_real_image_extractor(self) -> None:
        """The default image extractor is REAL (tesseract, self-gating).
        On hosts with tesseract, garbage bytes fail honestly with
        ocr_failed; on hosts without it, image_ocr_not_configured."""
        import shutil as _shutil

        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="m1",
                mime_type="image/jpeg",
                kind=MediaKind.IMAGE,
                bytes_data=b"fake-image-bytes",
            )
        )
        assert result.success is False
        assert result.extractor_name == "tesseract_ocr"
        if _shutil.which("tesseract"):
            assert result.error == "ocr_failed"
        else:
            assert result.error == "image_ocr_not_configured"

    async def test_default_pipeline_self_gates_without_tesseract(self, monkeypatch) -> None:
        """No tesseract on host → the honest not-configured result."""
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: None)
        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="m1",
                mime_type="image/jpeg",
                kind=MediaKind.IMAGE,
                bytes_data=b"fake-image-bytes",
            )
        )
        assert result.success is False
        assert result.error == "image_ocr_not_configured"

    async def test_default_pipeline_uses_stub_audio_extractor(self) -> None:
        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="m2",
                mime_type="audio/ogg",
                kind=MediaKind.AUDIO,
                bytes_data=b"fake-audio-bytes",
            )
        )
        assert result.success is False
        assert result.error == "audio_transcription_not_configured"

    async def test_default_pipeline_uses_real_document_extractor(self) -> None:
        """The default document extractor is REAL (pypdf); garbage bytes
        fail honestly as a parse failure."""
        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="m3",
                mime_type="application/pdf",
                kind=MediaKind.DOCUMENT,
                bytes_data=b"fake-pdf-bytes",
            )
        )
        assert result.success is False
        assert result.extractor_name == "pypdf_text"
        assert result.error in ("document_parse_failed", "document_extraction_not_configured")

    async def test_unknown_kind_returns_no_extractor_error(self) -> None:
        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="m4",
                mime_type="application/x-unknown",
                kind=MediaKind.UNKNOWN,
            )
        )
        assert result.success is False
        assert "no_extractor_for_kind" in (result.error or "")


class TestMediaPipelineCustomExtractors:
    async def test_register_extractor_replaces_default(self) -> None:
        pipeline = MediaPipeline()
        pipeline.register_extractor(MediaKind.DOCUMENT, _WorkingExtractor())
        result = await pipeline.extract(
            MediaSource(
                media_id="m5",
                mime_type="application/pdf",
                kind=MediaKind.DOCUMENT,
                bytes_data=b"pdf",
            )
        )
        assert result.success is True
        assert result.extracted_text == "this is extracted document text"
        assert result.extractor_name == "working_extractor"

    async def test_timeout_returns_failure_result(self) -> None:
        pipeline = MediaPipeline(default_timeout=0.1)
        pipeline.register_extractor(MediaKind.IMAGE, _SlowExtractor())
        result = await pipeline.extract(
            MediaSource(
                media_id="m6",
                mime_type="image/jpeg",
                kind=MediaKind.IMAGE,
            )
        )
        assert result.success is False
        assert "exceeded_timeout" in (result.error or "")

    async def test_extractor_exception_returns_failure_result(self) -> None:
        pipeline = MediaPipeline()
        pipeline.register_extractor(MediaKind.AUDIO, _FailingExtractor())
        result = await pipeline.extract(
            MediaSource(
                media_id="m7",
                mime_type="audio/ogg",
                kind=MediaKind.AUDIO,
            )
        )
        assert result.success is False
        assert "RuntimeError" in (result.error or "")

    async def test_extract_batch_processes_concurrently(self) -> None:
        pipeline = MediaPipeline()
        pipeline.register_extractor(MediaKind.DOCUMENT, _WorkingExtractor())
        sources = [
            MediaSource(media_id=f"batch-{i}", mime_type="application/pdf", kind=MediaKind.DOCUMENT)
            for i in range(5)
        ]
        start = time.perf_counter()
        results = await pipeline.extract_batch(sources)
        duration = time.perf_counter() - start
        assert len(results) == 5
        assert all(r.success for r in results)
        # Concurrent — should be quick
        assert duration < 1.0

    async def test_extract_batch_empty_input(self) -> None:
        pipeline = MediaPipeline()
        results = await pipeline.extract_batch([])
        assert results == []


class TestMediaExtractionResultContract:
    def test_metadata_default_is_dict(self) -> None:
        r = MediaExtractionResult(
            media_id="x",
            kind=MediaKind.IMAGE,
            success=True,
            extracted_text="hi",
            mime_type="image/jpeg",
            extractor_name="test",
            duration_ms=10.0,
        )
        assert r.metadata == {}

    def test_extracted_at_set_automatically(self) -> None:
        r = MediaExtractionResult(
            media_id="x",
            kind=MediaKind.IMAGE,
            success=True,
            extracted_text="hi",
            mime_type="image/jpeg",
            extractor_name="test",
            duration_ms=10.0,
        )
        assert r.extracted_at is not None
