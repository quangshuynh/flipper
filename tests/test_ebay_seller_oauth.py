from urllib.parse import parse_qs, urlsplit

import pytest

from ebay.seller_oauth import (
    FINANCES_SCOPE,
    FULFILLMENT_READONLY_SCOPE,
    SELLER_SCOPES,
    SellerNotConnectedError,
    SellerOAuthClient,
    SellerOAuthConfig,
    SellerOAuthError,
)


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class Store:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        del self.values[(service, username)]


def client(responses=(), clock=lambda: 100):
    config = SellerOAuthConfig("production", "client", "secret", "the-runame")
    return SellerOAuthClient(
        config, session=Session(responses), credential_store=Store(), clock=clock
    )


def test_authorization_url_has_exact_scope_and_state():
    oauth = client()
    parsed = urlsplit(oauth.authorization_url("state-value"))
    query = parse_qs(parsed.query)
    assert parsed.netloc == "auth.ebay.com"
    assert query == {
        "client_id": ["client"],
        "redirect_uri": ["the-runame"],
        "response_type": ["code"],
        "scope": [" ".join(SELLER_SCOPES)],
        "state": ["state-value"],
    }


def test_redirect_state_is_verified():
    assert client().parse_redirect("https://example.test/?code=abc&state=good", "good") == "abc"
    with pytest.raises(SellerOAuthError, match="state"):
        client().parse_redirect("https://example.test/?code=secret-code&state=bad", "good")


def test_code_exchange_stores_only_refresh_and_reuses_access_token():
    oauth = client(
        [Response(body={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})]
    )
    oauth.exchange_code("one-time-code")
    assert list(oauth._store.values.values()) == ["refresh"]
    assert oauth.access_token() == "access"
    assert len(oauth._session.calls) == 1


def test_refreshes_near_expiry_and_uses_required_scope():
    now = [100.0]
    oauth = client(
        [
            Response(body={"access_token": "first", "expires_in": 120}),
            Response(body={"access_token": "second", "expires_in": 120}),
        ],
        clock=lambda: now[0],
    )
    oauth._store.set_password(oauth._service, "client", "refresh-secret")
    assert oauth.access_token() == "first"
    assert oauth.access_token() == "first"
    now[0] = 161
    assert oauth.access_token() == "second"
    assert oauth._session.calls[-1][1]["data"]["scope"] == " ".join(SELLER_SCOPES)
    assert set(SELLER_SCOPES) == {FULFILLMENT_READONLY_SCOPE, FINANCES_SCOPE}


def test_disconnected_and_revoked_refresh_are_safe():
    oauth = client()
    with pytest.raises(SellerNotConnectedError):
        oauth.access_token()
    oauth = client(
        [Response(400, {"error": "invalid_grant", "error_description": "refresh-secret"})]
    )
    oauth._store.set_password(oauth._service, "client", "refresh-secret")
    with pytest.raises(SellerOAuthError) as error:
        oauth.access_token()
    assert "refresh-secret" not in str(error.value)


def test_old_refresh_token_scope_failure_requests_reconnect():
    oauth = client([Response(400, {"error": "invalid_scope"})])
    oauth._store.set_password(oauth._service, "client", "old-refresh-secret")
    with pytest.raises(SellerOAuthError, match="reconnect"):
        oauth.access_token()


def test_failed_exchange_does_not_leak_code_or_secret():
    oauth = client([Response(400, {"error": "bad", "error_description": "secret"})])
    with pytest.raises(SellerOAuthError) as error:
        oauth.exchange_code("authorization-secret")
    message = str(error.value)
    assert "authorization-secret" not in message
    assert "secret" not in message
