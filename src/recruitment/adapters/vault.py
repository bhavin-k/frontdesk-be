import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.ports.mailbox import CredentialVault, Sealed


class AESCredentialVault:
    def __init__(self, keys: dict[str, str], active: str) -> None:
        try:
            self.keys = {key: base64.b64decode(value, validate=True) for key, value in keys.items()}
            if active not in self.keys or any(len(key) != 32 for key in self.keys.values()):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ProviderError("INVALID_CREDENTIAL_KEY_RING") from exc
        self.active = active

    def seal(self, value: dict[str, Any], context: str) -> Sealed:
        nonce = secrets.token_bytes(12)
        return Sealed(
            self.active,
            nonce,
            AESGCM(self.keys[self.active]).encrypt(
                nonce, json.dumps(value).encode(), context.encode()
            ),
        )

    def open(self, value: Sealed, context: str) -> dict[str, Any]:
        try:
            plaintext = AESGCM(self.keys[value.key_id]).decrypt(
                value.nonce, value.ciphertext, context.encode()
            )
            result = json.loads(plaintext)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception as exc:
            raise ProviderError("CREDENTIAL_DECRYPTION_FAILED") from exc


class FakeCredentialVault:
    """Local fake data only. This format cannot hold real provider grants."""

    def seal(self, value: dict[str, Any], context: str) -> Sealed:
        if any(
            key in value
            for key in ("access_token", "refresh_token", "cache", "token", "app_password")
        ):
            raise ProviderError("REAL_CREDENTIALS_IN_FAKE_VAULT")
        return Sealed("local-fake", context.encode(), json.dumps(value).encode())

    def open(self, value: Sealed, context: str) -> dict[str, Any]:
        if value.key_id != "local-fake" or value.nonce != context.encode():
            raise ProviderError("CREDENTIAL_DECRYPTION_FAILED")
        result: dict[str, Any] = json.loads(value.ciphertext)
        return result


def credential_vault(settings: Settings) -> CredentialVault:
    if settings.credential_keys and settings.credential_keys.get_secret_value():
        return AESCredentialVault(
            json.loads(settings.credential_keys.get_secret_value()), settings.credential_active_key
        )
    if settings.mailbox_provider_mode == "fake" and settings.environment != "production":
        return FakeCredentialVault()
    raise ProviderError("CREDENTIAL_VAULT_NOT_CONFIGURED")
