import pytest

from recruitment.adapters.documents import DOCX, PDF, LocalExtractor, needs_ocr, validate_file
from recruitment.core.errors import AppError
from recruitment.domain.contracts import Extraction, Page
from tests.support import docx_bytes, pdf_bytes


@pytest.mark.parametrize("mime,builder", [(PDF, pdf_bytes), (DOCX, docx_bytes)])
def test_local_extraction_preserves_supported_cv_text(mime, builder) -> None:
    content = builder("Candidate Name\nPython development for three years.")
    validate_file(content, mime)
    extraction = LocalExtractor().extract(content, mime)
    assert "Python development" in extraction.pages[0].text
    assert not needs_ocr(extraction)
    if mime == DOCX:
        assert extraction.pages[0].number is None


@pytest.mark.parametrize(
    "content,mime,code",
    [
        (b"MZ executable", PDF, "INVALID_DOCUMENT"),
        (b"password encrypted", DOCX, "INVALID_DOCUMENT"),
        (b"some content", "text/plain", "UNSUPPORTED_FILE"),
        (b"x" * (10 * 1024 * 1024 + 1), PDF, "FILE_SIZE"),
    ],
)
def test_bad_uploads_are_not_accepted(content: bytes, mime: str, code: str) -> None:
    with pytest.raises(AppError) as caught:
        validate_file(content, mime)
    assert caught.value.code == code


def test_ocr_only_for_unusable_pages() -> None:
    assert needs_ocr(Extraction(pages=[Page(number=1, text="")], method="local"))
    assert not needs_ocr(
        Extraction(
            pages=[Page(number=1, text="Python backend development experience.")], method="local"
        )
    )


def test_docx_tables_remain_between_their_surrounding_paragraphs():
    import io

    from docx import Document

    from recruitment.adapters.documents import DOCX, LocalExtractor

    document = Document()
    document.add_paragraph("Before the table")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Python"
    table.cell(0, 1).text = "PostgreSQL"
    document.add_paragraph("After the table")
    buffer = io.BytesIO()
    document.save(buffer)
    text = LocalExtractor().extract(buffer.getvalue(), DOCX).pages[0].text
    assert text.index("Before") < text.index("Python") < text.index("After")
    assert "PostgreSQL" in text


def test_converter_rejects_non_pdf_and_http_errors(monkeypatch):
    import httpx
    import pytest

    from recruitment.adapters import conversion
    from recruitment.adapters.documents import DOC
    from recruitment.core.errors import ProviderError

    original = httpx.Client
    for response, code in (
        (
            httpx.Response(200, content=b"not-pdf", headers={"content-type": "application/pdf"}),
            "INVALID_CONVERTED_DOCUMENT",
        ),
        (httpx.Response(503), "CONVERSION_FAILED"),
    ):
        transport = httpx.MockTransport(lambda request, response=response: response)
        monkeypatch.setattr(
            conversion.httpx,
            "Client",
            lambda transport=transport, **kwargs: original(transport=transport, **kwargs),
        )
        with pytest.raises(ProviderError, match=code):
            conversion.GotenbergConverter("http://127.0.0.1:8300").convert(b"local fixture", DOC)
