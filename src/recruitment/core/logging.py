"""Keep third-party HTTP logs from leaking OAuth cursor or token-bearing URLs."""

import logging


def configure_provider_logging() -> None:
    for name in ("httpx", "httpcore", "google.auth", "google_auth_oauthlib", "msal", "oauthlib"):
        logging.getLogger(name).setLevel(logging.WARNING)
