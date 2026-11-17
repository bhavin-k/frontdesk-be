"""Private, bounded conversion. The converter cannot fetch arbitrary input URLs."""

import httpx

from recruitment.adapters.documents import PDF, validate_file
from recruitment.core.errors import AppError, ProviderError


class GotenbergConverter:
    version = "gotenberg-8.39.0"

    def __init__(self, endpoint: str, timeout: int = 60) -> None:
        self.endpoint, self.timeout = endpoint.rstrip("/"), timeout

    def convert(self, content: bytes, mime_type: str) -> bytes:
        from recruitment.adapters.documents import DOC

        filename = "cv.doc" if mime_type == DOC else "cv.docx"
        try:
            with (
                httpx.Client(
                    timeout=self.timeout, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "POST",
                    f"{self.endpoint}/forms/libreoffice/convert",
                    files={"files": (filename, content, mime_type)},
                ) as response,
            ):
                if (
                    response.status_code != 200
                    or response.headers.get("content-type", "").split(";")[0] != PDF
                ):
                    raise ProviderError("CONVERSION_FAILED", retryable=response.status_code >= 500)
                result = bytearray()
                for part in response.iter_bytes(65536):
                    result.extend(part)
                    if len(result) > 20 * 1024 * 1024:
                        raise ProviderError("CONVERTED_FILE_TOO_LARGE")
            validate_file(bytes(result), PDF, max_bytes=20 * 1024 * 1024)
            return bytes(result)
        except AppError as exc:
            raise ProviderError("INVALID_CONVERTED_DOCUMENT") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("CONVERTER_UNAVAILABLE", retryable=True) from exc


class FakeDocumentConverter:
    version = "fake-converter-v1"

    def convert(self, content: bytes, mime_type: str) -> bytes:
        import io

        import pymupdf
        from docx import Document

        from recruitment.adapters.documents import DOCX

        with pymupdf.open() as document:  # type: ignore[no-untyped-call]
            page = document.new_page()
            if mime_type == DOCX:
                source = Document(io.BytesIO(content))
                lines = [paragraph.text for paragraph in source.paragraphs]
                for table in source.tables:
                    lines.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
                page.insert_textbox(page.rect + (36, 36, -36, -36), "\n".join(lines), fontsize=10)
            return bytes(document.tobytes())
