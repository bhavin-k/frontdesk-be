import io
import socket
import struct
import zipfile

import olefile
from docx import Document
from PIL import Image

from recruitment.adapters.pdf_safety import validate_pdf
from recruitment.core.errors import AppError, ProviderError

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC = "application/msword"
JPEG = "image/jpeg"
PNG = "image/png"
IMAGE_FORMATS = {JPEG: "JPEG", PNG: "PNG"}
WORD_TYPES = {DOC, DOCX}
DOCUMENT_TYPES = {PDF, *WORD_TYPES}
CV_TYPES = DOCUMENT_TYPES | IMAGE_FORMATS.keys()
MAX_IMAGE_PIXELS = 16_000_000
MAX_BYTES = 10 * 1024 * 1024


def validate_file(content: bytes, mime_type: str, max_bytes: int = MAX_BYTES) -> None:
    if not content or len(content) > max_bytes:
        raise AppError("FILE_SIZE", "Upload a nonempty file no larger than 10 MB.", 413)
    if mime_type not in CV_TYPES:
        raise AppError("UNSUPPORTED_FILE", "Upload a PDF, DOCX, DOC, JPEG or PNG file.", 415)
    try:
        if mime_type in IMAGE_FORMATS:
            with Image.open(io.BytesIO(content), formats=[IMAGE_FORMATS[mime_type]]) as image:
                if (
                    image.width * image.height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError
                image.verify()
            with Image.open(io.BytesIO(content), formats=[IMAGE_FORMATS[mime_type]]) as image:
                image.load()
        elif mime_type == PDF:
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
