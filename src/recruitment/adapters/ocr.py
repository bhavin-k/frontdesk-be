import boto3
import pymupdf
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Extraction, Page


class TextractOCR:
    def __init__(self, region: str) -> None:
        self.region = region

    def extract(self, content: bytes) -> Extraction:
        try:
            client = boto3.client(
                "textract",
                region_name=self.region,
                config=Config(connect_timeout=10, read_timeout=45, retries={"max_attempts": 0}),
            )
            pages = []
            confidences: list[float] = []
            # Render each page locally; no S3 staging or additional file datastore is needed.
            with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
                for i, page in enumerate(document):
                    image = page.get_pixmap(dpi=150).tobytes("png")
                    if len(image) > 5 * 1024 * 1024:
                        image = page.get_pixmap(dpi=96).tobytes("png")
                    if len(image) > 5 * 1024 * 1024:
                        raise ProviderError("OCR_PAGE_TOO_LARGE")
                    result = client.detect_document_text(Document={"Bytes": image})
                    lines = [block for block in result["Blocks"] if block["BlockType"] == "LINE"]
                    pages.append(Page(number=i + 1, text="\n".join(b["Text"] for b in lines)))
                    confidences.extend(float(b.get("Confidence", 0)) / 100 for b in lines)
            confidence = min(confidences) if confidences else 0
            return Extraction(pages=pages, method="textract", confidence=confidence)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise ProviderError(
                "OCR_UNAVAILABLE",
                retryable=code
                in {
                    "ThrottlingException",
                    "ProvisionedThroughputExceededException",
                    "InternalServerError",
                },
            ) from exc
        except BotoCoreError as exc:
            raise ProviderError("OCR_UNAVAILABLE", retryable=True) from exc
