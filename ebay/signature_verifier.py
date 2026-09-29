"""Verification for eBay ECC-signed event notifications."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


PUBLIC_KEY_CACHE_TTL_SECONDS = 60 * 60
PUBLIC_KEY_CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_SECONDS = 10
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SignatureFormatError(ValueError):
    """The X-EBAY-SIGNATURE header is missing required or valid fields."""


class PublicKeyRetrievalError(RuntimeError):
    """The eBay public key could not be retrieved or parsed."""


@dataclass(frozen=True)
class PublicKeyDocument:
    """Relevant fields returned by eBay's getPublicKey method."""

    key: str
    algorithm: str
    digest: str


class EbayNotificationApiClient:
    """Retrieve notification public keys with an application OAuth token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: str = "production",
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("eBay client credentials are required")
        if environment not in {"production", "sandbox"}:
            raise ValueError("EBAY_ENV must be 'production' or 'sandbox'")

        host = "api.ebay.com" if environment == "production" else "api.sandbox.ebay.com"
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = f"https://{host}/identity/v1/oauth2/token"
        self._public_key_url = f"https://{host}/commerce/notification/v1/public_key"
        self._oauth_scope = "https://api.ebay.com/oauth/api_scope"
        self._session = session or requests.Session()
        self._clock = clock
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    @classmethod
    def from_environment(cls) -> "EbayNotificationApiClient":
        """Build a client from secret environment configuration."""
        return cls(
            client_id=os.getenv("EBAY_CLIENT_ID", ""),
            client_secret=os.getenv("EBAY_CLIENT_SECRET", ""),
            environment=os.getenv("EBAY_NOTIFICATION_ENV", "production").strip().lower(),
        )

    def _get_access_token(self) -> str:
        now = self._clock()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        try:
            response = self._session.post(
                self._token_url,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials", "scope": self._oauth_scope},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
            access_token = body["access_token"]
            expires_in = int(body.get("expires_in", 7200))
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("missing access token")
        except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
            raise PublicKeyRetrievalError("eBay application token retrieval failed") from exc

        self._access_token = access_token
        self._access_token_expires_at = now + max(0, expires_in - 60)
        return access_token

    def get_public_key(self, key_id: str) -> PublicKeyDocument:
        """Call eBay Notification API getPublicKey for a validated key ID."""
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise SignatureFormatError("invalid public key ID")

        try:
            response = self._session.get(
                f"{self._public_key_url}/{key_id}",
                headers={"Authorization": f"Bearer {self._get_access_token()}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
            return PublicKeyDocument(
                key=body["key"], algorithm=body["algorithm"], digest=body["digest"]
            )
        except PublicKeyRetrievalError:
            raise
        except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
            raise PublicKeyRetrievalError("eBay public key retrieval failed") from exc


class EbaySignatureVerifier:
    """Verify eBay notification signatures with a bounded TTL/LRU key cache."""

    def __init__(
        self,
        key_fetcher: Callable[[str], PublicKeyDocument],
        cache_ttl_seconds: int = PUBLIC_KEY_CACHE_TTL_SECONDS,
        cache_max_entries: int = PUBLIC_KEY_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_ttl_seconds <= 0 or cache_max_entries <= 0:
            raise ValueError("cache limits must be positive")
        self._key_fetcher = key_fetcher
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, ec.EllipticCurvePublicKey]] = OrderedDict()
        self._cache_lock = threading.Lock()

    @staticmethod
    def _decode_signature_header(signature_header: str) -> tuple[str, bytes]:
        try:
            decoded = base64.b64decode(signature_header, validate=True)
            envelope = json.loads(decoded.decode("ascii"))
            if not isinstance(envelope, dict):
                raise TypeError
            if envelope.get("alg", "").lower() != "ecdsa":
                raise ValueError
            if envelope.get("digest", "").upper() != "SHA1":
                raise ValueError
            key_id = envelope["kid"]
            encoded_signature = envelope["signature"]
            if not isinstance(key_id, str) or not KEY_ID_PATTERN.fullmatch(key_id):
                raise ValueError
            if not isinstance(encoded_signature, str):
                raise TypeError
            signature = base64.b64decode(encoded_signature, validate=True)
            if not signature:
                raise ValueError
        except (
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise SignatureFormatError("malformed X-EBAY-SIGNATURE header") from exc
        return key_id, signature

    @staticmethod
    def _load_public_key(document: PublicKeyDocument) -> ec.EllipticCurvePublicKey:
        if not all(
            isinstance(value, str) for value in (document.key, document.algorithm, document.digest)
        ):
            raise PublicKeyRetrievalError("eBay public key response is malformed")
        if document.algorithm.upper() != "ECDSA" or document.digest.upper() != "SHA1":
            raise PublicKeyRetrievalError("eBay public key metadata is unsupported")
        try:
            key_text = document.key.strip()
            body = key_text.replace("-----BEGIN PUBLIC KEY-----", "").replace(
                "-----END PUBLIC KEY-----", ""
            )
            der = base64.b64decode("".join(body.split()), validate=True)
            public_key = serialization.load_der_public_key(der)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise PublicKeyRetrievalError("eBay public key is malformed") from exc
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise PublicKeyRetrievalError("eBay public key is not an EC key")
        return public_key

    def _get_public_key(self, key_id: str) -> ec.EllipticCurvePublicKey:
        with self._cache_lock:
            now = self._clock()
            cached = self._cache.get(key_id)
            if cached and now < cached[0]:
                self._cache.move_to_end(key_id)
                return cached[1]
            if cached:
                del self._cache[key_id]

            public_key = self._load_public_key(self._key_fetcher(key_id))
            self._cache[key_id] = (now + self._cache_ttl_seconds, public_key)
            self._cache.move_to_end(key_id)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)
            return public_key

    def verify(self, notification: Mapping[str, Any], signature_header: str) -> bool:
        """Verify an eBay signature over its documented compact JSON representation."""
        key_id, signature = self._decode_signature_header(signature_header)
        public_key = self._get_public_key(key_id)
        payload = json.dumps(notification, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        try:
            public_key.verify(signature, payload, ec.ECDSA(hashes.SHA1()))
        except InvalidSignature:
            return False
        except ValueError as exc:
            raise SignatureFormatError("malformed ECDSA signature") from exc
        return True


def build_signature_verifier() -> EbaySignatureVerifier:
    """Build the production verifier and its network-backed key provider."""
    client = EbayNotificationApiClient.from_environment()
    return EbaySignatureVerifier(client.get_public_key)
