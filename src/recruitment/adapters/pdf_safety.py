"""Bounded PDF validation and page-only copies; embedded files are never opened."""

import re

import pymupdf

from recruitment.core.errors import ProviderError

MAX_PAGES = 100
MAX_OBJECTS = 100_000
MAX_PROCESSING_BYTES = 20 * 1024 * 1024
PROCESSING_VERSION = "pdf-pages-v1"
ACTIVE_CONTENT = re.compile(r"/(?:JavaScript|Launch|JS)(?=[\s/<>()\[\]]|$)")
ATTACHMENTS = re.compile(
    r"/(?:EmbeddedFile|EmbeddedFiles|Filespec|FileAttachment|EF|AF)(?=[\s/<>()\[\]]|$)"
)


def _inspect(document: pymupdf.Document) -> bool:
    object_count = document.xref_length()  # type: ignore[no-untyped-call]
    if (
        document.is_encrypted
        or not 1 <= document.page_count <= MAX_PAGES
        or object_count > MAX_OBJECTS
    ):
        raise ValueError("Unsupported PDF structure")
    attachments = bool(document.embfile_count())
    # Inspect parsed object dictionaries, including compressed object streams.
    # Never read an embedded-file stream or execute a PDF action.
    for xref in range(1, object_count):
        value = document.xref_object(xref, compressed=False)  # type: ignore[no-untyped-call]
        if ACTIVE_CONTENT.search(value):
            raise ValueError("Active PDF content")
        attachments |= bool(ATTACHMENTS.search(value))
    return attachments


def validate_pdf(content: bytes) -> None:
    if not content.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF header")
    with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
        _inspect(document)


def processing_pdf(content: bytes) -> bytes:
    """Keep ordinary PDFs unchanged; rebuild attached-file PDFs from their pages."""
    try:
        if not content.startswith(b"%PDF-") or len(content) > MAX_PROCESSING_BYTES:
            raise ValueError("Invalid PDF input")
        with pymupdf.open(stream=content, filetype="pdf") as original:  # type: ignore[no-untyped-call]
            if not _inspect(original):
                return content
            with pymupdf.open() as cleaned:  # type: ignore[no-untyped-call]
                # Drop document-level attachments/associated files, annotations,
                # links and widgets. Page text, images, fonts and geometry remain.
                cleaned.insert_pdf(original, links=False, annots=False, widgets=False)
                if cleaned.page_count != original.page_count or any(
                    cleaned[index].get_text() != original[index].get_text()
                    for index in range(original.page_count)
                ):
                    raise ValueError("PDF page content changed")
                result = bytes(cleaned.tobytes(garbage=4, deflate=True))
        if len(result) > MAX_PROCESSING_BYTES:
            raise ValueError("Processing PDF exceeds size limit")
        with pymupdf.open(stream=result, filetype="pdf") as checked:  # type: ignore[no-untyped-call]
            if _inspect(checked):
                raise ValueError("PDF attachments remain")
        return result
    except Exception as exc:
        raise ProviderError("PDF_SANITIZATION_FAILED") from exc
