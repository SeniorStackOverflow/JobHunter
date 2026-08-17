from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class TokenDecryptionError(ValueError):
    """Raised when encrypted OAuth material cannot be authenticated."""


def derive_fernet_key(secret: SecretStr | str) -> bytes:
    raw = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    if len(raw) < 32:
        raise ValueError("token encryption material must contain at least 32 characters")
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


class SecretBox:
    def __init__(self, encryption_key: SecretStr | str) -> None:
        self._fernet = Fernet(derive_fernet_key(encryption_key))

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except InvalidToken as exc:
            raise TokenDecryptionError("encrypted token failed authentication") from exc
