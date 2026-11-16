import io
import socket
import struct
import zipfile

import olefile
import pymupdf
from docx import Document

from recruitment.adapters.pdf_safety import validate_pdf
from recruitment.core.errors import AppError, ProviderError
from recruitment.domain.contracts import Extraction, Page

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC = "application/msword"
MAX_BYTES = 10 * 1024 * 1024


def validate_file(content: bytes, mime_type: str, max_bytes: int = MAX_BYTES) -> None:
    if not content or len(content) > max_bytes:
        raise AppError("FILE_SIZE", "Upload a nonempty file no larger than 10 MB.", 413)
    if mime_type not in {PDF, DOCX, DOC}:
        raise AppError("UNSUPPORTED_FILE", "Upload a PDF, DOCX or DOC file.", 415)
    try:
        if mime_type == PDF:
            validate_pdf(content)
        elif mime_type == DOC:
            with olefile.OleFileIO(
                io.BytesIO(content), raise_defects=olefile.DEFECT_INCORRECT
            ) as archive:
                names = ["/".join(path).casefold() for path in archive.listdir()]
                if not archive.exists("WordDocument") or not any(
                    archive.exists(name) for name in ("0Table", "1Table")
                ):
                    raise ValueError
                if any(
                    "vba" in name or "encrypted" in name or "objectpool" in name for name in names
                ):
                    raise ValueError
                header = archive.openstream("WordDocument").read(12)
                if len(header) < 12 or int.from_bytes(header[10:12], "little") & 0x8100:
                    raise ValueError
        else:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError
                if any(
                    item.flag_bits & 1
                    or item.file_size > 20 * MAX_BYTES
                    or item.filename.lower().endswith(("vbaproject.bin", ".exe", ".js"))
                    for item in archive.infolist()
                ):
                    raise ValueError
                if sum(item.file_size for item in archive.infolist()) > 20 * MAX_BYTES:
                    raise ValueError
                Document(io.BytesIO(content))
    except Exception as exc:
        raise AppError(
            "INVALID_DOCUMENT", "The document is encrypted, unsafe or unreadable.", 422
        ) from exc


class LocalExtractor:
    def extract(self, content: bytes, mime_type: str) -> Extraction:
        try:
            if mime_type == PDF:
                with pymupdf.open(stream=content, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
                    pages = [Page(number=i + 1, text=page.get_text()) for i, page in enumerate(doc)]
                return Extraction(pages=pages, method="pymupdf")
            doc = Document(io.BytesIO(content))
            from docx.table import Table
            from docx.text.paragraph import Paragraph

            parts = []
            for element in doc.iter_inner_content():
                if isinstance(element, Paragraph):
                    parts.append(element.text)
                elif isinstance(element, Table):
                    parts.extend(
                        " | ".join(cell.text for cell in row.cells) for row in element.rows
                    )
            text = "\n".join(parts)
            return Extraction(pages=[Page(number=None, text=text)], method="python-docx")
        except Exception as exc:
            raise ProviderError("EXTRACTION_FAILED") from exc


class ClamAVScanner:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def scan(self, content: bytes) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=30) as connection:
                connection.sendall(b"zINSTREAM\0")
                for offset in range(0, len(content), 65536):
                    chunk = content[offset : offset + 65536]
                    connection.sendall(struct.pack("!I", len(chunk)) + chunk)
                connection.sendall(struct.pack("!I", 0))
                response = bytearray()
                while len(response) < 4096:
                    data = connection.recv(4096)
                    if not data:
                        break
                    response.extend(data)
                    if b"\0" in data:
                        break
            if b"FOUND" in response:
                raise AppError("UNSAFE_FILE", "The file did not pass the security scan.", 422)
            if b"OK" not in response:
                raise ProviderError("SCANNER_UNAVAILABLE", retryable=True)
        except OSError as exc:
            raise ProviderError("SCANNER_UNAVAILABLE", retryable=True) from exc


def needs_ocr(extraction: Extraction) -> bool:
    return not extraction.pages or any(
        sum(character.isalnum() for character in page.text) < 20 for page in extraction.pages
    )
