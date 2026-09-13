"""Stub extractors for Phase T.

These are WORKING PLACEHOLDERS that:
- Return clear "extraction not yet configured" messages
- Do NOT pretend to extract real text
- Document the contract real extractors will satisfy

Real extractor implementations:
- ImageOCRExtractor: Tesseract (open source) or AWS Textract / Google
  Vision / Azure Computer Vision (cloud)
- AudioTranscriptionExtractor: OpenAI Whisper (local) or
  AWS Transcribe / Google Speech-to-Text / Azure Speech (cloud)
- DocumentTextExtractor: pypdf for PDF, python-docx for DOCX

These will be implemented when the founder provides API credentials or
when we wire local libraries (Tesseract, Whisper local model).
"""

from __future__ import annotations

import time

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)


class StubImageExtractor(MediaExtractor):
    """Stub for image OCR.

    Returns a clear message that OCR is not configured. The runtime
    records this so callers know the extraction is pending configuration.
    """

    @property
    def name(self) -> str:
        return "stub_image_ocr"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.IMAGE})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        start = time.perf_counter()
        return MediaExtractionResult(
            media_id=source.media_id,
            kind=MediaKind.IMAGE,
            success=False,
            extracted_text="",
            mime_type=source.mime_type,
            extractor_name=self.name,
            duration_ms=(time.perf_counter() - start) * 1000,
            error="image_ocr_not_configured",
            metadata={"filename": source.filename or ""},
        )


class StubAudioExtractor(MediaExtractor):
    """Stub for audio transcription."""

    @property
    def name(self) -> str:
        return "stub_audio_transcription"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.AUDIO})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        start = time.perf_counter()
        return MediaExtractionResult(
            media_id=source.media_id,
            kind=MediaKind.AUDIO,
            success=False,
            extracted_text="",
            mime_type=source.mime_type,
            extractor_name=self.name,
            duration_ms=(time.perf_counter() - start) * 1000,
            error="audio_transcription_not_configured",
            metadata={"filename": source.filename or ""},
        )


class StubDocumentExtractor(MediaExtractor):
    """Stub for document text extraction."""

    @property
    def name(self) -> str:
        return "stub_document_text"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.DOCUMENT})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        start = time.perf_counter()
        return MediaExtractionResult(
            media_id=source.media_id,
            kind=MediaKind.DOCUMENT,
            success=False,
            extracted_text="",
            mime_type=source.mime_type,
            extractor_name=self.name,
            duration_ms=(time.perf_counter() - start) * 1000,
            error="document_extraction_not_configured",
            metadata={"filename": source.filename or ""},
        )


# Registry of default extractors
DEFAULT_EXTRACTORS: dict[MediaKind, MediaExtractor] = {
    MediaKind.IMAGE: StubImageExtractor(),
    MediaKind.AUDIO: StubAudioExtractor(),
    MediaKind.DOCUMENT: StubDocumentExtractor(),
}
