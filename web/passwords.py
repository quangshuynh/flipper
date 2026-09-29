"""Owner-password hashing with the standard-library scrypt KDF.

Flipper has exactly one owner password. Only its encoded hash is configured
(``FLIPPER_PASSWORD_HASH``); the plaintext is never stored, logged, or accepted as a CLI argument.

Encoded format (no ``$``, spaces, quotes, or ``#``, so it survives shells, dotenv files, and
hosting-provider secret fields unquoted)::

    flipper-scrypt:v1:n=131072,r=8,p=1:<salt base64url>:<derived key base64url>

The version names this exact layout. Parameters are read from the encoded value, so they can be
raised later without invalidating existing hashes, but verification refuses parameters outside a
bounded range so configuration can never request an unbounded amount of memory or CPU.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass, field

SCHEME = "flipper-scrypt"
VERSION = "v1"

# OWASP's scrypt baseline: N=2^17, r=8, p=1 (128 MiB, a few hundred milliseconds).
DEFAULT_N = 2**17
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

MIN_N = 2**14
MAX_N = 2**20
MAX_R = 16
MAX_P = 4
MAX_MEMORY_BYTES = 256 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MIN_DISTINCT_CHARACTERS = 5

_ENCODED = re.compile(
    r"^(?P<scheme>[a-z0-9-]+):(?P<version>v[0-9]+):"
    r"n=(?P<n>[0-9]{1,8}),r=(?P<r>[0-9]{1,3}),p=(?P<p>[0-9]{1,3}):"
    r"(?P<salt>[A-Za-z0-9_-]+):(?P<key>[A-Za-z0-9_-]+)$"
)


class PasswordHashError(ValueError):
    """The configured hash is malformed or unsupported. Messages never include the value."""


class PasswordPolicyError(ValueError):
    """A new password is too weak to be an intentional owner password."""


@dataclass(frozen=True)
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes = field(repr=False)
    key: bytes = field(repr=False)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PasswordHashError("password hash is malformed") from exc
    if _b64encode(decoded) != value:
        raise PasswordHashError("password hash is malformed")
    return decoded


def _password_bytes(password: str) -> bytes:
    """Normalize so the same typed password verifies on every keyboard and platform."""
    return unicodedata.normalize("NFC", password).encode("utf-8")


def _memory_limit(n: int, r: int, p: int) -> int:
    # OpenSSL needs 128*r*(N+2) bytes for V plus 128*r*p for B; leave headroom for overhead.
    return 128 * r * (n + 2 + p) + 1024 * 1024


def _check_parameters(n: int, r: int, p: int) -> None:
    if n < MIN_N or n > MAX_N or n & (n - 1):
        raise PasswordHashError("password hash parameters are unsupported")
    if not 1 <= r <= MAX_R or not 1 <= p <= MAX_P or 128 * r * n > MAX_MEMORY_BYTES:
        raise PasswordHashError("password hash parameters are unsupported")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        _password_bytes(password),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_memory_limit(n, r, p),
        dklen=KEY_BYTES,
    )


def check_password_policy(password: str) -> None:
    """Reject obviously accidental input without imposing an enterprise composition policy."""
    if not password or not password.strip():
        raise PasswordPolicyError("password must not be empty")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if len(set(password)) < MIN_DISTINCT_CHARACTERS:
        raise PasswordPolicyError(
            f"password must contain at least {MIN_DISTINCT_CHARACTERS} different characters"
        )


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    """Return the encoded hash for a new owner password with a fresh random salt."""
    check_password_policy(password)
    _check_parameters(n, r, p)
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, n, r, p)
    return f"{SCHEME}:{VERSION}:n={n},r={r},p={p}:{_b64encode(salt)}:{_b64encode(key)}"


def parse_password_hash(encoded: str) -> PasswordHash:
    """Parse and bound-check an encoded hash; raise PasswordHashError without echoing it."""
    match = _ENCODED.fullmatch(encoded.strip()) if isinstance(encoded, str) else None
    if match is None:
        raise PasswordHashError("password hash is malformed")
    if match["scheme"] != SCHEME:
        raise PasswordHashError("password hash algorithm is unsupported")
    if match["version"] != VERSION:
        raise PasswordHashError("password hash version is unsupported")
    n, r, p = int(match["n"]), int(match["r"]), int(match["p"])
    _check_parameters(n, r, p)
    salt = _b64decode(match["salt"])
    key = _b64decode(match["key"])
    if not 16 <= len(salt) <= 64 or len(key) != KEY_BYTES:
        raise PasswordHashError("password hash is malformed")
    return PasswordHash(n=n, r=r, p=p, salt=salt, key=key)


def verify_password(password: str, stored: PasswordHash) -> bool:
    """Return whether ``password`` matches, comparing derived keys in constant time."""
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
        return False
    candidate = _derive(password, stored.salt, stored.n, stored.r, stored.p)
    return hmac.compare_digest(candidate, stored.key)
