"""wax.media — Media Intelligence Pipeline.

When a user sends an image, audio, document, or video via WhatsApp, the
runtime extracts structured text from it BEFORE the AI sees it.

Why: LLMs cannot directly see binary media (in our text-only contract).
Even multimodal LLMs need URLs or base64 — and we never want to feed raw
untrusted bytes to a model without first sanitizing them.

Architecture:
- MediaPipeline: orchestrates extraction
- MediaExtractor: abstract contract for a media-type-specific extractor
- extractors.TesseractImageExtractor: REAL image OCR via the system
  tesseract binary (self-gating: hosts without it get an honest
  image_ocr_not_configured result, never a faked success)
- extractors.PdfDocumentExtractor: REAL PDF text-layer extraction via
  pypdf (optional extra; scanned PDFs honestly report no_text_layer)
- extractors.StubAudioExtractor: HONEST stub — offline transcription
  requires a large model download (deployment decision)

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
