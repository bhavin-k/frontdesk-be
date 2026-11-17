"""Local, CPU-only CV text recognition using checked-in model names and local weights."""

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageOps

from recruitment.adapters.documents import IMAGE_FORMATS, MAX_IMAGE_PIXELS, PDF
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Extraction, Page

DET = "PP-OCRv5_mobile_det"
REC = "en_PP-OCRv5_mobile_rec"
MAX_SIDE = 3000
MAX_TEXT_CHARS = 200_000
METHOD = "paddleocr-3.7.0-pp-ocrv5"


class PaddleOCRExtractor:
    def __init__(self, models_dir: str) -> None:
        self.models_dir = Path(models_dir)
        self.engine: Any = None

    def _engine(self) -> Any:
        if self.engine is None:
            det = self.models_dir / f"{DET}_infer"
            rec = self.models_dir / f"{REC}_infer"
            if not all(
                (path / name).is_file()
                for path in (det, rec)
                for name in ("inference.json", "inference.pdiparams", "inference.yml")
            ):
                raise ProviderError("OCR_MODELS_NOT_CONFIGURED")
            try:
                from paddleocr import PaddleOCR

                self.engine = PaddleOCR(
                    text_detection_model_name=DET,
                    text_detection_model_dir=str(det.resolve()),
                    text_recognition_model_name=REC,
                    text_recognition_model_dir=str(rec.resolve()),
                    device="cpu",
                    enable_mkldnn=False,
                    cpu_threads=2,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
            except ImportError:
                raise ProviderError("OCR_RUNTIME_NOT_INSTALLED") from None
        return self.engine

    @staticmethod
    def _images(content: bytes, mime_type: str) -> Iterator[Image.Image]:
        if mime_type == PDF:
            with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
                for page in document:
                    scale = min(200 / 72, MAX_SIDE / max(page.rect.width, page.rect.height))
                    matrix = pymupdf.Matrix(scale, scale)  # type: ignore[no-untyped-call]
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                    yield Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        else:
            with Image.open(io.BytesIO(content), formats=[IMAGE_FORMATS[mime_type]]) as image:
                if (
                    image.width * image.height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ProviderError("INVALID_OCR_IMAGE")
                rgb = ImageOps.exif_transpose(image).convert("RGBA")
                background = Image.new("RGBA", rgb.size, "white")
                background.alpha_composite(rgb)
                rendered = background.convert("RGB")
                rendered.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                yield rendered

    def extract(self, content: bytes, mime_type: str) -> Extraction:
        try:
            engine = self._engine()
            import numpy as np

            pages: list[Page] = []
            scores: list[float] = []
            chars = 0
            for number, image in enumerate(self._images(content, mime_type), 1):
                # Paddle accepts OpenCV-style BGR image arrays.
                results = engine.predict(np.asarray(image)[:, :, ::-1].copy())
                lines = [str(line) for result in results for line in result["rec_texts"]]
                scores.extend(float(score) for result in results for score in result["rec_scores"])
                text = "\n".join(lines)
                chars += len(text) + 1
                if chars > MAX_TEXT_CHARS:
                    raise ProviderError("EXTRACTED_TEXT_TOO_LARGE")
                pages.append(Page(number=number, text=text))
            return Extraction(
                pages=pages, method=METHOD, confidence=sum(scores) / len(scores) if scores else 0
            )
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("OCR_FAILED") from None
