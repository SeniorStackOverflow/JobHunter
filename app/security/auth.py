from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hash.verify(password, encoded)
    except Exception:  # password backends use different malformed-hash exceptions
        return False


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, allowed_hashes: list[str]) -> bool:
    candidate = hash_api_key(api_key)
    return any(hmac.compare_digest(candidate, allowed) for allowed in allowed_hashes)


class SessionSigner:
    def __init__(self, secret_key: str, salt: str = "job-agent-session") -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt=salt)

    def issue(self, subject: str) -> str:
        return self._serializer.dumps({"sub": subject, "nonce": secrets.token_urlsafe(12)})

    def verify(self, token: str, max_age: int) -> str | None:
        try:
            payload = self._serializer.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return None
        subject = payload.get("sub")
        return subject if isinstance(subject, str) else None


class CsrfProtector:
    def __init__(self, secret_key: str) -> None:
        self._secret = secret_key.encode("utf-8")

    def issue(self, session_id: str) -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        value = f"{session_id}:{timestamp}:{nonce}"
        signature = hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()
        return f"{timestamp}.{nonce}.{signature}"

    def verify(self, token: str, session_id: str, max_age: int) -> bool:
        try:
            timestamp, nonce, signature = token.split(".", maxsplit=2)
            if int(time.time()) - int(timestamp) > max_age:
                return False
        except (TypeError, ValueError):
            return False
        value = f"{session_id}:{timestamp}:{nonce}"
        expected = hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
