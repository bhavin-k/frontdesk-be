import pymupdf
import pytest

from recruitment.adapters.documents import PDF, validate_file
from recruitment.adapters.pdf_safety import MAX_PROCESSING_BYTES, processing_pdf
from recruitment.core.errors import AppError, ProviderError
from tests.support import pdf_bytes, pdf_with_attachment


def test_embedded_files_are_discarded_without_opening_them(monkeypatch):
    original = pdf_with_attachment(pdf_bytes("Python development experience."))

    def forbidden(*args, **kwargs):
        raise AssertionError("Never extract the embedded attachment")

    monkeypatch.setattr(pymupdf.Document, "embfile_get", forbidden)
    validate_file(original, PDF)
    prepared = processing_pdf(original)
    assert prepared != original
    with (
        pymupdf.open(stream=original, filetype="pdf") as source,
        pymupdf.open(stream=prepared, filetype="pdf") as clean,
    ):
        assert source.embfile_count() == 1
        assert clean.embfile_count() == 0
        assert clean.page_count == source.page_count
        assert clean[0].get_text() == source[0].get_text()
        assert (
            clean[0].get_pixmap(annots=False).samples == source[0].get_pixmap(annots=False).samples
        )
        assert not any(
            "/EmbeddedFile" in clean.xref_object(i) for i in range(1, clean.xref_length())
        )


@pytest.mark.parametrize("associated", [False, True])
def test_file_annotations_and_associated_files_are_removed(associated):
    with pymupdf.open(stream=pdf_bytes("Python development experience."), filetype="pdf") as doc:
        if associated:
            doc.embfile_add("source.txt", b"hidden attachment fixture")
            stream = next(
                index
                for index in range(1, doc.xref_length())
                if doc.xref_get_key(index, "Type") == ("name", "/EmbeddedFile")
            )
            file_spec = doc.get_new_xref()
            doc.update_object(
                file_spec, f"<< /Type /Filespec /F (source.txt) /EF << /F {stream} 0 R >> >>"
            )
            doc.xref_set_key(doc.pdf_catalog(), "AF", f"[{file_spec} 0 R]")
            doc.xref_set_key(doc.pdf_catalog(), "Names", "null")
        else:
            doc[0].add_file_annot((80, 80), b"hidden attachment fixture", "source.txt")
        original = doc.tobytes()
    prepared = processing_pdf(original)
    assert prepared != original
    with pymupdf.open(stream=prepared, filetype="pdf") as clean:
        assert clean.embfile_count() == 0
        assert list(clean[0].annots() or []) == []
        assert clean.xref_get_key(clean.pdf_catalog(), "AF")[0] == "null"
        assert "Python development" in clean[0].get_text()


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("action", ["JavaScript", "Launch"])
def test_active_actions_remain_blocked_even_inside_object_streams(action, compressed):
    with pymupdf.open(stream=pdf_bytes("Python development experience."), filetype="pdf") as doc:
        xref = doc.get_new_xref()
        doc.update_object(xref, f"<< /S /{action} /JS (local test only) >>")
        doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
        content = doc.tobytes(use_objstms=int(compressed))
    with pytest.raises(AppError) as caught:
        validate_file(content, PDF)
    assert caught.value.code == "INVALID_DOCUMENT"
    with pytest.raises(ProviderError, match="PDF_SANITIZATION_FAILED"):
        processing_pdf(content)


def test_ordinary_pdf_is_not_rewritten():
    content = pdf_bytes("Python and JavaScript development experience.")
    assert processing_pdf(content) is content


@pytest.mark.parametrize("encrypted", [False, True])
def test_encrypted_and_over_page_limit_pdfs_remain_blocked(encrypted):
    with pymupdf.open() as doc:
        for _ in range(1 if encrypted else 101):
            doc.new_page()
        content = (
            doc.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="fixture-owner",
                user_pw="fixture-user",
            )
            if encrypted
            else doc.tobytes()
        )
    with pytest.raises(AppError) as caught:
        validate_file(content, PDF)
    assert caught.value.code == "INVALID_DOCUMENT"


def test_processing_input_limit_and_conversion_failure_are_safe(monkeypatch):
    with pytest.raises(ProviderError, match="PDF_SANITIZATION_FAILED"):
        processing_pdf(b"%PDF-" + b"x" * MAX_PROCESSING_BYTES)
    content = pdf_with_attachment(pdf_bytes("Python development experience."))

    def broken(*args, **kwargs):
        raise RuntimeError("fixture page-copy failure")

    monkeypatch.setattr(pymupdf.Document, "insert_pdf", broken)
    with pytest.raises(ProviderError, match="PDF_SANITIZATION_FAILED"):
        processing_pdf(content)
