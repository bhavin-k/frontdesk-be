"""Prepare already scanned originals for extraction and external OCR."""

from recruitment.adapters.pdf_safety import processing_pdf
from recruitment.ports.providers import MalwareScanner


def prepare_pdf(content: bytes, scanner: MalwareScanner) -> bytes:
    prepared = processing_pdf(content)
    if prepared is not content:
        scanner.scan(prepared)
    return prepared
