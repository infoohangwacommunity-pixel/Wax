"""Real extractor tests (reconciliation-5): image OCR + PDF text.

The runtime's image/document extractors were honest stubs while the
tools they need were INSTALLED on the host. These tests prove the real
mechanisms end-to-end:

- Tesseract OCR actually reads text out of a real rendered image
- pypdf actually reads the text layer out of a real PDF
- both fail HONESTLY when their tooling is absent (self-gating)
"""

from __future__ import annotations

import pytest

from wax.media.contracts import MediaKind, MediaSource
from wax.media.real_extractors import PdfDocumentExtractor, TesseractImageExtractor


def _make_png_with_text(text: str) -> bytes:
    pytest.importorskip("PIL")
    import io

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (600, 160), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except OSError:  # pragma: no cover - font layout differs on some hosts
        font = ImageFont.load_default()
    draw.text((20, 50), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_pdf_with_text(text: str) -> bytes:
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.pages[0].extract_text  # noqa: B018 — existence probe only
    # pypdf cannot DRAW text; it manipulates PDFs. Instead of faking a
    # text layer, build a minimal one-page PDF with the text in a raw
    # content stream.
    stream = (
        "BT /F1 24 Tf 72 720 Td (" + text.replace("(", " ").replace(")", " ") + ") Tj ET"
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


class TestTesseractOCR:
    @pytest.mark.skipif(
        __import__("shutil").which("tesseract") is None,
        reason="tesseract not installed on host",
    )
    async def test_ocr_reads_rendered_text(self):
        pytest.importorskip("PIL")
        png = _make_png_with_text("WAX RUNTIME 42")
        result = await TesseractImageExtractor().extract(
            MediaSource(
                media_id="ocr-1",
                mime_type="image/png",
                kind=MediaKind.IMAGE,
                bytes_data=png,
            )
        )
        assert result.success is True, f"OCR failed: {result.error}"
        normalized = " ".join(result.extracted_text.split()).upper()
        assert "WAX" in normalized
        assert "42" in normalized

    async def test_self_gates_when_tesseract_missing(self, monkeypatch):
        import shutil as shutil_mod

        monkeypatch.setattr(shutil_mod, "which", lambda name: None)
        result = await TesseractImageExtractor().extract(
            MediaSource(
                media_id="ocr-2",
                mime_type="image/png",
                kind=MediaKind.IMAGE,
                bytes_data=b"\x89PNG fake",
            )
        )
        assert result.success is False
        assert result.error == "image_ocr_not_configured"

    async def test_oversize_media_is_refused(self, monkeypatch):
        import shutil as shutil_mod

        monkeypatch.setattr(shutil_mod, "which", lambda name: "/usr/bin/tesseract")
        result = await TesseractImageExtractor().extract(
            MediaSource(
                media_id="ocr-3",
                mime_type="image/png",
                kind=MediaKind.IMAGE,
                bytes_data=b"\0" * (20 * 1024 * 1024 + 1),
            )
        )
        assert result.success is False
        assert result.error == "media_too_large"
        assert result.metadata["cap_bytes"] == 20 * 1024 * 1024


class TestPdfText:
    async def test_pdf_text_layer_extraction(self):
        pytest.importorskip("pypdf")
        pdf = _make_pdf_with_text("WAX RUNTIME 42")
        result = await PdfDocumentExtractor().extract(
            MediaSource(
                media_id="pdf-1",
                mime_type="application/pdf",
                kind=MediaKind.DOCUMENT,
                bytes_data=pdf,
            )
        )
        assert result.success is True, f"PDF extraction failed: {result.error}"
        assert "WAX RUNTIME 42" in result.extracted_text

    async def test_no_text_layer_is_honest(self):
        pytest.importorskip("pypdf")
        import io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        result = await PdfDocumentExtractor().extract(
            MediaSource(
                media_id="pdf-2",
                mime_type="application/pdf",
                kind=MediaKind.DOCUMENT,
                bytes_data=buf.getvalue(),
            )
        )
        assert result.success is False
        assert result.error == "no_text_layer"

    async def test_parse_failure_is_honest(self):
        pytest.importorskip("pypdf")
        result = await PdfDocumentExtractor().extract(
            MediaSource(
                media_id="pdf-3",
                mime_type="application/pdf",
                kind=MediaKind.DOCUMENT,
                bytes_data=b"not a pdf at all",
            )
        )
        assert result.success is False
        assert result.error == "document_parse_failed"
