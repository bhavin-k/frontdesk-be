"""Bounded retries for grading and experience requests."""

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from openai import RateLimitError

from recruitment.core.errors import ProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int | None


def retry_after_seconds(headers: Mapping[str, str], fallback: float) -> float | None:
    if "retry-after-ms" in headers:
        try:
            return float(headers["retry-after-ms"]) / 1000
        except ValueError:
            return None
    value = headers.get("retry-after")
    if value is None:
        return fallback
    try:
        return float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def request_with_rate_limit_retries[T](request: Callable[[], T]) -> T:
    for attempt in range(3):
        try:
            return request()
        except ProviderError as exc:
            if not isinstance(exc.__cause__, RateLimitError):
                raise
            headers = exc.__cause__.response.headers
            delay = retry_after_seconds(headers, fallback=2**attempt)
            if delay is None or not math.isfinite(delay) or delay < 0:
                raise
            if delay > 60 or attempt == 2:
                exc.retry_after_seconds = min(delay, 86_400)
                raise
            logger.info("grading.rate_limit_retry attempt=%s delay_seconds=%s", attempt + 1, delay)
            time.sleep(min(60, delay + 0.25))
    raise ProviderError("AI_UNAVAILABLE", retryable=True)
