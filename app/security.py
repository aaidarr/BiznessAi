from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Used to keep login timing similar when the email does not exist.
DUMMY_HASH = _ph.hash("dummy-password-for-timing")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def const_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _fernet() -> Fernet:
    s = get_settings()
    key = s.encryption_key
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(s.secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "••••"


class RateLimiter:
    """Sliding-window limiter in process memory (single instance)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # crude memory bound
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
