"""Stateless signed owner sessions.

A session cookie is ``v1.<payload>.<signature>``: a base64url JSON payload and a base64url
HMAC-SHA256 over ``v1.<payload>``. The payload holds only a random session ID and two integer UTC
timestamps. It never contains the password, its hash, eBay credentials, or accounting data.

The signing key is derived from both ``FLIPPER_SESSION_SECRET`` and the configured password hash,
so rotating the secret *or* changing the owner password invalidates every existing cookie. There
is no server-side session table; logout clears the browser's cookie, and revoking a cookie that may
have been copied elsewhere requires rotating the secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

FORMAT_VERSION = "v1"
ABSOLUTE_LIFETIME_SECONDS = 30 * 24 * 60 * 60
IDLE_LIFETIME_SECONDS = 7 * 24 * 60 * 60
# Refresh last-seen at most hourly so ordinary browsing does not rewrite the cookie every request.
REFRESH_INTERVAL_SECONDS = 60 * 60
# Tolerate small clock differences between processes without accepting far-future timestamps.
CLOCK_SKEW_SECONDS = 5 * 60
MAX_COOKIE_LENGTH = 512

_KEY_CONTEXT = b"flipper-session-signing-key-v1\x00"


@dataclass(frozen=True)
class Session:
    session_id: str
    issued_at: int
    last_seen: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical encoding")
    return decoded


class SessionCodec:
    """Issue, sign, verify, and age sessions against an injectable clock."""

    def __init__(
        self,
        secret: str,
        password_hash: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = hmac.new(
            secret.encode("utf-8"),
            _KEY_CONTEXT + password_hash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        self._clock = clock

    def now(self) -> int:
        return int(self._clock())

    def issue(self) -> Session:
        now = self.now()
        return Session(secrets.token_urlsafe(16), issued_at=now, last_seen=now)

    def _signature(self, signed: str) -> bytes:
        return hmac.new(self._key, signed.encode("ascii"), hashlib.sha256).digest()

    def encode(self, session: Session) -> str:
        payload = json.dumps(
            {"sid": session.session_id, "iat": session.issued_at, "seen": session.last_seen},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signed = f"{FORMAT_VERSION}.{_b64encode(payload)}"
        return f"{signed}.{_b64encode(self._signature(signed))}"

    def decode(self, value: str | None) -> Session | None:
        """Return a currently valid session, or None for anything tampered, stale, or malformed."""
        if not value or len(value) > MAX_COOKIE_LENGTH or not value.isascii():
            return None
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != FORMAT_VERSION:
            return None
        try:
            signature = _b64decode(parts[2])
        except (binascii.Error, ValueError):
            return None
        # Authenticate before parsing anything attacker-controlled.
        if not hmac.compare_digest(signature, self._signature(f"{parts[0]}.{parts[1]}")):
            return None
        try:
            payload = json.loads(_b64decode(parts[1]))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"sid", "iat", "seen"}:
            return None
        session_id, issued_at, last_seen = payload["sid"], payload["iat"], payload["seen"]
        if not isinstance(session_id, str) or not 16 <= len(session_id) <= 64:
            return None
        if not all(type(value) is int for value in (issued_at, last_seen)):
            return None
        session = Session(session_id, issued_at, last_seen)
        return session if self._is_current(session) else None

    def _is_current(self, session: Session) -> bool:
        now = self.now()
        return (
            session.issued_at <= session.last_seen <= now + CLOCK_SKEW_SECONDS
            and now < session.issued_at + ABSOLUTE_LIFETIME_SECONDS
            and now < session.last_seen + IDLE_LIFETIME_SECONDS
        )

    def needs_refresh(self, session: Session) -> bool:
        return self.now() - session.last_seen >= REFRESH_INTERVAL_SECONDS

    def touch(self, session: Session) -> Session:
        return replace(session, last_seen=max(session.last_seen, self.now()))

    def max_age(self, session: Session) -> int:
        """Seconds until the earlier of absolute or idle expiry, for the cookie's Max-Age."""
        expires = min(
            session.issued_at + ABSOLUTE_LIFETIME_SECONDS,
            session.last_seen + IDLE_LIFETIME_SECONDS,
        )
        return max(0, expires - self.now())
