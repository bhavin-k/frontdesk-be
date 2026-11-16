import pytest

from recruitment.domain.chunking import chunk_pages, tokenizer
from recruitment.domain.contracts import Page


def test_token_windows_overlap_and_keep_source_offsets() -> None:
    pages = [
        Page(number=1, text="Python PostgreSQL developer experience. " * 180),
        Page(number=2, text="Backend API engineering and testing. " * 180),
    ]
    chunks = chunk_pages(pages)
    original = "\n".join(page.text for page in pages)
    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.text == original[chunk.char_start : chunk.char_end]
        assert 0 < chunk.token_end - chunk.token_start <= 400
        assert chunk.source_spans
    for first, second in zip(chunks, chunks[1:], strict=False):
        assert first.token_end - second.token_start == 64
    assert {span["page"] for c in chunks for span in c.source_spans} == {1, 2}


def test_unicode_windows_do_not_corrupt_source_quotes() -> None:
    text = "Résumé développement Python café 🐍. " * 300
    for chunk in chunk_pages([Page(number=None, text=text)]):
        assert "\ufffd" not in chunk.text
        assert chunk.text == text[chunk.char_start : chunk.char_end]


def test_tokenizer_is_offline_and_deterministic() -> None:
    assert tokenizer().encode("Python developer") == tokenizer().encode("Python developer")
    assert chunk_pages([]) == []


@pytest.mark.parametrize("size,overlap", [(100, 64), (600, 64), (400, 10)])
def test_invalid_windows_are_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_pages([Page(number=1, text="CV")], size, overlap)
