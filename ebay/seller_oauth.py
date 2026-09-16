"""Authorization Code Grant and secure local token handling for one eBay seller."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import keyring
import requests
from keyring.errors import KeyringError


FULFILLMENT_READONLY_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
FINANCES_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.finances"
SELLER_SCOPES = (FULFILLMENT_READONLY_SCOPE, FINANCES_SCOPE)
REQUEST_TIMEOUT_SECONDS = 15
EXPIRY_SKEW_SECONDS = 60


class SellerOAuthError(RuntimeError):
    """A safe-to-display seller authorization failure."""


class SellerNotConnectedError(SellerOAuthError):
    """No seller refresh token is stored locally."""


class CredentialStore(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class SellerOAuthConfig:
    """Environment-specific eBay seller OAuth settings."""

    environment: str
    client_id: str
    client_secret: str
    runame: str

    @classmethod
    def from_environment(cls) -> SellerOAuthConfig:
        environment = os.getenv("EBAY_SELLER_ENV", "production").strip().lower()
        values = {
            "client_id": os.getenv("EBAY_SELLER_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("EBAY_SELLER_CLIENT_SECRET", "").strip(),
            "runame": os.getenv("EBAY_SELLER_RUNAME", "").strip(),
        }
        if environment not in {"production", "sandbox"}:
            raise SellerOAuthError("EBAY_SELLER_ENV must be 'production' or 'sandbox'")
        missing = [name for name, value in values.items() if not value]
        if missing:
            names = ", ".join(f"EBAY_SELLER_{name.upper()}" for name in missing)
            raise SellerOAuthError(f"Missing seller OAuth configuration: {names}")
        return cls(environment=environment, **values)

    @property
    def api_host(self) -> str:
        return "api.ebay.com" if self.environment == "production" else "api.sandbox.ebay.com"

    @property
    def auth_host(self) -> str:
        return "auth.ebay.com" if self.environment == "production" else "auth.sandbox.ebay.com"


class SellerOAuthClient:
    """Mint and cache User access tokens while keyring stores only the refresh token."""

    def __init__(
        self,
        config: SellerOAuthConfig,
        *,
        session: requests.Session | None = None,
        credential_store: CredentialStore = keyring,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()
        self._store = credential_store
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def _service(self) -> str:
        return f"flipper.ebay.seller.{self.config.environment}"

    @property
    def _token_url(self) -> str:
        return f"https://{self.config.api_host}/identity/v1/oauth2/token"

    def authorization_url(self, state: str) -> str:
        if not state:
            raise ValueError("OAuth state is required")
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.runame,
                "response_type": "code",
                "scope": " ".join(SELLER_SCOPES),
                "state": state,
            }
        )
        return f"https://{self.config.auth_host}/oauth2/authorize?{query}"

    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def parse_redirect(redirected_url: str, expected_state: str) -> str:
        values = parse_qs(urlsplit(redirected_url.strip()).query)
        if values.get("state", [None])[0] != expected_state:
            raise SellerOAuthError(
                "Authorization response state did not match; try connecting again"
            )
        if "error" in values:
            raise SellerOAuthError("eBay authorization was declined or failed")
        code = values.get("code", [None])[0]
        if not code:
            raise SellerOAuthError("Authorization response did not contain a code")
        return code

    def _token_request(self, data: dict[str, str], action: str) -> dict[str, Any]:
        try:
            response = self._session.post(
                self._token_url,
                auth=(self.config.client_id, self.config.client_secret),
                data=data,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SellerOAuthError(
                f"eBay {action} failed due to a network or response error"
            ) from exc
        if response.status_code >= 400:
            oauth_error = body.get("error") if isinstance(body, dict) else None
            if oauth_error in {"invalid_grant", "invalid_scope"}:
                detail = "; reconnect the eBay seller account" if action == "token refresh" else ""
                raise SellerOAuthError(f"eBay {action} was rejected{detail}")
            raise SellerOAuthError(f"eBay {action} failed (HTTP {response.status_code})")
        if not isinstance(body, dict):
            raise SellerOAuthError(f"eBay {action} returned an unexpected response")
        return body

    def exchange_code(self, code: str) -> None:
        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.runame,
            },
            "authorization-code exchange",
        )
        refresh_token = body.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise SellerOAuthError("eBay authorization response did not include a refresh token")
        try:
            self._store.set_password(self._service, self.config.client_id, refresh_token)
        except KeyringError as exc:
            raise SellerOAuthError("The OS credential store could not save authorization") from exc
        self._remember_access_token(body)

    def _remember_access_token(self, body: dict[str, Any]) -> str:
        token = body.get("access_token")
        try:
            expires_in = int(body["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SellerOAuthError("eBay token response was malformed") from exc
        if not isinstance(token, str) or not token or expires_in <= 0:
            raise SellerOAuthError("eBay token response was malformed")
        self._access_token = token
        self._expires_at = self._clock() + max(0, expires_in - EXPIRY_SKEW_SECONDS)
        return token

    def access_token(self, *, force_refresh: bool = False) -> str:
        now = self._clock()
        if not force_refresh and self._access_token and now < self._expires_at:
            return self._access_token
        try:
            refresh_token = self._store.get_password(self._service, self.config.client_id)
        except KeyringError as exc:
            raise SellerOAuthError("The OS credential store could not read authorization") from exc
        if not refresh_token:
            raise SellerNotConnectedError(
                "No eBay seller authorization is stored; run 'python main.py ebay connect'"
            )
        body = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(SELLER_SCOPES),
            },
            "token refresh",
        )
        return self._remember_access_token(body)

    def disconnect(self) -> bool:
        try:
            if not self._store.get_password(self._service, self.config.client_id):
                return False
            self._store.delete_password(self._service, self.config.client_id)
        except KeyringError as exc:
            raise SellerOAuthError(
                "The OS credential store could not remove authorization"
            ) from exc
        self._access_token = None
        self._expires_at = 0.0
        return True
