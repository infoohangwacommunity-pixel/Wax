"""wax.media — Media Intelligence Pipeline.

When a user sends an image, audio, document, or video via WhatsApp, the
runtime extracts structured text from it BEFORE the AI sees it.

Why: LLMs cannot directly see binary media (in our text-only contract).
Even multimodal LLMs need URLs or base64 — and we never want to feed raw
untrusted bytes to a model without first sanitizing them.

Architecture:
- MediaPipeline: orchestrates extraction
- MediaExtractor: abstract contract for a media-type-specific extractor
- extractors/image_ocr: extracts text from images (Phase T placeholder —
  wires to Tesseract or cloud OCR when available)
- extractors/audio_transcription: extracts text from audio (placeholder —
  wires to Whisper or cloud transcription when available)
- extractors/document_text: extracts text from documents (PDF, DOCX, etc.)

INVARIANT: The runtime extracts. The AI interprets. The AI never receives
raw media bytes — only the extracted text/structure.
"""

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)
from wax.media.pipeline import MediaPipeline

__all__ = [
    "MediaExtractionResult",
    "MediaExtractor",
    "MediaKind",
    "MediaPipeline",
    "MediaSource",
]
