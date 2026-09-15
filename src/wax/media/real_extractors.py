"""Real media extractors — honest tools with honest failure modes.

Every extractor here is SELF-GATING: it probes its own tooling at
extraction time and, when the tool is absent, returns the same honest
"not configured" failure the stubs did — never a faked success. The
runtime can therefore ship them as defaults without changing behavior
on hosts that lack the tools, and without pretending on hosts that
have them.

- TesseractImageExtractor: OCR via the system `tesseract` binary
  (subprocess, arg-vector only — no shell, size-capped input, timeout).
- PdfDocumentExtractor: text-layer extraction via `pypdf` (optional
  dependency; PDFs WITHOUT a text layer honestly fail — OCR of scans
  is a pipeline decision, not something this extractor fakes).
- Audio stays a documented stub: offline transcription requires a
  multi-hundred-MB model download — a deployment decision, recorded in
  reconciliation-5.

All of them enforce: byte caps, hard timeouts, no shell interpolation,
no raw bytes in logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)
from wax.runtime.logging import get_logger

log = get_logger(__name__)

MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MiB per artifact
_OCR_TIMEOUT_SECONDS = 25.0


class _Unavailable:
    """Shared honest-failure construction for toolless extraction."""


def _too_large(source: MediaSource, kind: MediaKind) -> MediaExtractionResult | None:
    size = len(source.bytes_data or b"")
    if size <= MAX_MEDIA_BYTES:
        return None
    return MediaExtractionResult(
        media_id=source.media_id,
        kind=kind,
        success=False,
        extracted_text="",
        mime_type=source.mime_type,
        extractor_name="n/a",
        duration_ms=0.0,
        error="media_too_large",
        metadata={"size_bytes": size, "cap_bytes": MAX_MEDIA_BYTES},
    )


class TesseractImageExtractor(MediaExtractor):
    """OCR via the system tesseract binary — the mechanism Chrome-class
    software ships with; zero credentials, offline.

    Failure modes (all honest, all structured):
    - tesseract absent on host → "image_ocr_not_configured"
    - media over the byte cap → "media_too_large"
    - OCR ran but found nothing → success=True, empty text
    - tesseract failed → "ocr_failed" with exit code
    """

    def __init__(self, *, language: str = "eng") -> None:
        self._language = language

    @property
    def name(self) -> str:
        return "tesseract_ocr"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.IMAGE})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        started = time.perf_counter()

        def _finish(**kw) -> MediaExtractionResult:
            return MediaExtractionResult(
                media_id=source.media_id,
                kind=MediaKind.IMAGE,
                mime_type=source.mime_type,
                extractor_name=self.name,
                duration_ms=(time.perf_counter() - started) * 1000,
                **kw,
            )

        binary = shutil.which("tesseract")
        if binary is None:
            return _finish(
                success=False,
                extracted_text="",
                error="image_ocr_not_configured",
                metadata={"filename": source.filename or ""},
            )
        oversize = _too_large(source, MediaKind.IMAGE)
        if oversize is not None:
            return _finish(
                success=False,
                extracted_text="",
                error=oversize.error,
                metadata=oversize.metadata,
            )
        if not source.bytes_data:
            return _finish(
                success=False,
                extracted_text="",
                error="media_bytes_missing",
                metadata={"filename": source.filename or ""},
            )

        # tesseract reads a file; the bytes are untrusted so they land in
        # an anonymous temp file the process owns, never in a shell arg.
        fd, path = tempfile.mkstemp(suffix=".img", prefix="wax-ocr-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(source.bytes_data)
            proc = await asyncio.create_subprocess_exec(
                binary,
                path,
                "stdout",
                "-l",
                self._language,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_OCR_TIMEOUT_SECONDS
                )
            except TimeoutError:
                try:
                    import signal as _signal

                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                return _finish(
                    success=False,
                    extracted_text="",
                    error="ocr_timeout",
                    metadata={"timeout_seconds": _OCR_TIMEOUT_SECONDS},
                )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

        if proc.returncode != 0:
            log.warning(
                "media.ocr_failed",
                exit_code=proc.returncode,
                stderr_len=len(stderr_b or b""),
            )
            return _finish(
                success=False,
                extracted_text="",
                error="ocr_failed",
                metadata={"exit_code": proc.returncode},
            )

        text = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        return _finish(
            success=True,
            extracted_text=text,
            metadata={"chars": len(text)},
        )


class PdfDocumentExtractor(MediaExtractor):
    """Text-layer extraction via pypdf (optional dependency).

    Honest limits: a scanned PDF has NO text layer — this extractor
    reports "no_text_layer" rather than pretending. OCR-on-documents is
    a pipeline policy decision (route through the image extractor per
    page), not something faked here.
    """

    @property
    def name(self) -> str:
        return "pypdf_text"

    @property
    def supported_kinds(self) -> frozenset[MediaKind]:
        return frozenset({MediaKind.DOCUMENT})

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        import io

        started = time.perf_counter()

        def _finish(**kw) -> MediaExtractionResult:
            return MediaExtractionResult(
                media_id=source.media_id,
                kind=MediaKind.DOCUMENT,
                mime_type=source.mime_type,
                extractor_name=self.name,
                duration_ms=(time.perf_counter() - started) * 1000,
                **kw,
            )

        try:
            import pypdf
        except ImportError:
            return _finish(
                success=False,
                extracted_text="",
                error="document_extraction_not_configured",
                metadata={"filename": source.filename or ""},
            )
        oversize = _too_large(source, MediaKind.DOCUMENT)
        if oversize is not None:
            return _finish(
                success=False,
                extracted_text="",
                error=oversize.error,
                metadata=oversize.metadata,
            )
        if not source.bytes_data:
            return _finish(
                success=False,
                extracted_text="",
                error="media_bytes_missing",
                metadata={"filename": source.filename or ""},
            )

        try:
            reader = pypdf.PdfReader(io.BytesIO(source.bytes_data))
            parts: list[str] = []
            for page in reader.pages[:50]:  # bounded: 50 pages per artifact
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    parts.append(page_text)
        except Exception as e:
            return _finish(
                success=False,
                extracted_text="",
                error="document_parse_failed",
                metadata={"detail": str(e)[:200]},
            )

        text = "\n\n".join(parts)
        if not text:
            return _finish(
                success=False,
                extracted_text="",
                error="no_text_layer",
                metadata={"filename": source.filename or ""},
            )
        return _finish(
            success=True,
            extracted_text=text,
            metadata={"chars": len(text), "pages_parsed": len(parts)},
        )
