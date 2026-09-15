"""MediaPipeline — orchestrates extraction across media kinds.

The pipeline:
1. Identifies the MediaKind from the mime_type
2. Looks up the appropriate extractor
3. Runs extraction with a timeout
4. Returns a MediaExtractionResult (success or failure)

Used by the runtime bridge BEFORE calling the AI. The AI receives the
extracted_text via RuntimeRequest.media[] — never raw bytes.
"""

from __future__ import annotations

import asyncio
import time

from wax.media.contracts import (
    MediaExtractionResult,
    MediaExtractor,
    MediaKind,
    MediaSource,
)
from wax.media.extractors import DEFAULT_EXTRACTORS
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class MediaPipeline:
    """Orchestrates media extraction.

    Use:
        pipeline = MediaPipeline()
        result = await pipeline.extract(MediaSource(
            media_id="...",
            mime_type="image/jpeg",
            kind=MediaKind.IMAGE,
            bytes_data=img_bytes,
        ))
        # result.extracted_text → attach to RuntimeRequest.media[]
    """

    def __init__(
        self,
        extractors: dict[MediaKind, MediaExtractor] | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._extractors = dict(extractors or DEFAULT_EXTRACTORS)
        self._default_timeout = default_timeout

    def register_extractor(self, kind: MediaKind, extractor: MediaExtractor) -> None:
        """Register or replace the extractor for a media kind."""
        self._extractors[kind] = extractor
        log.info(
            "media.extractor.registered",
            kind=kind.value,
            extractor=extractor.name,
        )

    async def extract(self, source: MediaSource) -> MediaExtractionResult:
        """Extract structured text from a media source.

        Always returns a MediaExtractionResult — never raises. On timeout
        or extractor failure, returns a failure result with the error.
        """
        start = time.perf_counter()

        extractor = self._extractors.get(source.kind)
        if extractor is None:
            return MediaExtractionResult(
                media_id=source.media_id,
                kind=source.kind,
                success=False,
                extracted_text="",
                mime_type=source.mime_type,
                extractor_name="none",
                duration_ms=(time.perf_counter() - start) * 1000,
                error=f"no_extractor_for_kind:{source.kind.value}",
            )

        try:
            result = await asyncio.wait_for(
                extractor.extract(source),
                timeout=self._default_timeout,
            )
            log.info(
                "media.extracted",
                media_id=source.media_id,
                kind=source.kind.value,
                extractor=extractor.name,
                success=result.success,
                duration_ms=round(result.duration_ms, 2),
            )
            return result
        except TimeoutError:
            log.warning(
                "media.extraction_timeout",
                media_id=source.media_id,
                kind=source.kind.value,
                extractor=extractor.name,
                timeout_s=self._default_timeout,
            )
            return MediaExtractionResult(
                media_id=source.media_id,
                kind=source.kind,
                success=False,
                extracted_text="",
                mime_type=source.mime_type,
                extractor_name=extractor.name,
                duration_ms=self._default_timeout * 1000,
                error=f"exceeded_timeout:{self._default_timeout}s",
            )
        except Exception as e:
            log.warning(
                "media.extraction_failed",
                media_id=source.media_id,
                kind=source.kind.value,
                extractor=extractor.name,
                error=str(e),
                error_type=type(e).__name__,
            )
            return MediaExtractionResult(
                media_id=source.media_id,
                kind=source.kind,
                success=False,
                extracted_text="",
                mime_type=source.mime_type,
                extractor_name=extractor.name,
                duration_ms=(time.perf_counter() - start) * 1000,
                error=f"{type(e).__name__}: {e}",
            )

    async def extract_batch(self, sources: list[MediaSource]) -> list[MediaExtractionResult]:
        """Extract multiple media sources concurrently."""
        if not sources:
            return []
        tasks = [self.extract(s) for s in sources]
        return await asyncio.gather(*tasks)


def classify_mime_type(mime_type: str) -> MediaKind:
    """Map a MIME type to a MediaKind."""
    mt = mime_type.lower()
    if mt.startswith("image/"):
        return MediaKind.IMAGE
    if mt.startswith("audio/"):
        return MediaKind.AUDIO
    if mt.startswith("video/"):
        return MediaKind.VIDEO
    if mt in (
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    ):
        return MediaKind.DOCUMENT
    return MediaKind.UNKNOWN
