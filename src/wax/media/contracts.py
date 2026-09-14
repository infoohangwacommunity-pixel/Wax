"""Contracts for the media intelligence pipeline.

A MediaExtractor takes raw bytes + mime type and returns structured text
extracted from the media. Different extractors handle different media
types (image OCR, audio transcription, document parsing).

The pipeline orchestrates: download → identify type → route to extractor →
return MediaExtractionResult.

The bridge then attaches the result to RuntimeRequest.media[], and the AI
sees it via effective_text() — never raw bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class MediaKind(StrEnum):
    """What kind of media this is — determines which extractor handles it."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


@dataclass
class MediaSource:
    """A reference to media that needs extraction.

    The pipeline fetches bytes from `source_url` (or uses the provided
    bytes directly), then routes to the appropriate extractor.
    """

    media_id: str  # WhatsApp media_id, or our internal ID
    mime_type: str
    kind: MediaKind
    source_url: str | None = None  # if None, bytes must be provided
    bytes_data: bytes | None = None
    filename: str | None = None
    sha256: str | None = None


@dataclass
class MediaExtractionResult:
    """The outcome of extracting structured text from media.

    `extracted_text` is what the AI sees. If extraction failed, this is
    a human-readable error string and `success` is False.
    """

    media_id: str
    kind: MediaKind
    success: bool
    extracted_text: str
    mime_type: str
    extractor_name: str
    duration_ms: float
    metadata: dict[str, str | int | float] = field(default_factory=dict)
    extracted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None


class MediaExtractor(ABC):
    """Abstract contract for a media-type-specific extractor.

    Implementations:
    - ImageOCRExtractor (Tesseract, cloud OCR)
    - AudioTranscriptionExtractor (Whisper, cloud speech-to-text)
    - DocumentTextExtractor (PDF text, DOCX text)
    - VideoFrameExtractor (extract frames, then OCR — future)

    All extractors are I/O-bound and async. They MUST:
    - enforce a timeout (the pipeline enforces a global one, but extractors
      should bail early on slow processing)
    - never trust the mime_type (verify magic bytes where possible)
    - never log the raw bytes (only metadata)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Extractor identifier (e.g. 'tesseract_ocr', 'whisper_small')."""

    @property
    @abstractmethod
    def supported_kinds(self) -> frozenset[MediaKind]:
        """Which MediaKinds this extractor handles."""

    @abstractmethod
    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        """Extract structured text from the media source."""
