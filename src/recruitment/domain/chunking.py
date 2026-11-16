import bisect
from dataclasses import dataclass
from typing import Any

from recruitment.domain.contracts import Page
from recruitment.domain.tokenization import tokenizer


@dataclass(frozen=True)
class Chunk:
    text: str
    char_start: int
    char_end: int
    token_start: int
    token_end: int
    source_spans: list[dict[str, Any]]


def chunk_pages(pages: list[Page], size: int = 400, overlap: int = 64) -> list[Chunk]:
    if not 300 <= size <= 500 or not 50 <= overlap <= 80 or overlap >= size:
        raise ValueError("Use 300–500 tokens and 50–80 tokens of overlap.")
    encoding = tokenizer()
    text = "\n".join(page.text for page in pages)
    tokens = encoding.encode(text, disallowed_special=())
    char_bytes = [0]
    for char in text:
        char_bytes.append(char_bytes[-1] + len(char.encode("utf-8")))
    token_bytes = [0]
    for token in tokens:
        token_bytes.append(token_bytes[-1] + len(encoding.decode_single_token_bytes(token)))
    page_offsets: list[tuple[int | None, int, int]] = []
    cursor = 0
    for page in pages:
        page_offsets.append((page.number, cursor, cursor + len(page.text)))
        cursor += len(page.text) + 1
    result = []
    start = 0
    while start < len(tokens):
        end = min(start + size, len(tokens))
        char_start = max(0, bisect.bisect_right(char_bytes, token_bytes[start]) - 1)
        char_end = bisect.bisect_left(char_bytes, token_bytes[end])
        spans = [
            {
                "page": page,
                "page_char_start": max(char_start, begin) - begin,
                "page_char_end": min(char_end, finish) - begin,
                "chunk_char_start": max(char_start, begin) - char_start,
                "chunk_char_end": min(char_end, finish) - char_start,
            }
            for page, begin, finish in page_offsets
            if begin < char_end and finish > char_start
        ]
        result.append(Chunk(text[char_start:char_end], char_start, char_end, start, end, spans))
        if end == len(tokens):
            break
        start = end - overlap
    return result
