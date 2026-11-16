"""Bounded provider requests with fixed origins and sanitized failures."""

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from recruitment.core.errors import ProviderError


def provider_request(
    origin: str,
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    binary: bool = False,
    limit: int = 16 * 1024 * 1024,
) -> tuple[int, Any]:
    url = path if path.startswith("https://") else f"{origin}/{path.lstrip('/')}"
    target, allowed = urlsplit(url), urlsplit(origin)
    if (
        target.scheme != "https"
        or target.netloc != allowed.netloc
        or target.username
        or target.fragment
    ):
        raise ProviderError("INVALID_PROVIDER_URL")
    if not target.path.startswith(allowed.path.rstrip("/") + "/"):
        raise ProviderError("INVALID_PROVIDER_URL")
    try:
        with (
            httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client,
            client.stream(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                json=payload,
                params=params,
            ) as response,
        ):
            data = bytearray()
            for part in response.iter_bytes(65536):
                data.extend(part)
                if len(data) > limit:
                    raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
            status = response.status_code
        if status == 401:
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
        if status == 429 or status >= 500:
            raise ProviderError("MAILBOX_UNAVAILABLE", retryable=True)
        if status in (404, 410):
            return status, {}
        if status >= 300:
            raise ProviderError("MAILBOX_REQUEST_FAILED")
        return status, (bytes(data) if binary else json.loads(data) if data else {})
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise ProviderError("MAILBOX_UNAVAILABLE", retryable=True) from exc
