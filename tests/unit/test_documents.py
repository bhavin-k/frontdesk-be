import pytest

from recruitment.adapters.documents import DOCX, PDF, validate_file
from recruitment.core.errors import AppError


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
