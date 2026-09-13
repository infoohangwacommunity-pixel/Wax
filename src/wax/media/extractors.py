"""Media extractors — honest defaults for every kind.

DEFAULT_EXTRACTORS wires the REAL extractors where the runtime can
provide them:

- IMAGE: TesseractImageExtractor (system tesseract; self-gating — on
  hosts without the binary it reports image_ocr_not_configured,
  identical to the previous stub behavior, so this is a strict upgrade)
- DOCUMENT: PdfDocumentExtractor (pypdf text layer; self-gating;
  scanned PDFs honestly report no_text_layer)
- AUDIO: StubAudioExtractor — HONEST stub. Offline transcription needs
  a large model download (a deployment decision); the stub reports
  audio_transcription_not_configured and never fakes text.

Stub classes are retained for explicit deployments/tests that want to
pin the not-configured behavior.
"""

from __future__ import annotations

import time

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)
from wax.media.real_extractors import PdfDocumentExtractor, TesseractImageExtractor


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


# Registry of default extractors. IMAGE and DOCUMENT are REAL
# (self-gating) extractors; AUDIO stays an honest stub.
DEFAULT_EXTRACTORS: dict[MediaKind, MediaExtractor] = {
    MediaKind.IMAGE: TesseractImageExtractor(),
    MediaKind.AUDIO: StubAudioExtractor(),
    MediaKind.DOCUMENT: PdfDocumentExtractor(),
}

# Real extractors live in real_extractors.py (imported here so the
# default registry above is the single wiring point).
