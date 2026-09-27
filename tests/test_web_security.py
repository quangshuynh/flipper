import re
from datetime import datetime, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

import web.app as web_app
import web.security as web_security
from backup.fingerprint import database_state
from ebay.compliance import ACCOUNT_DELETION_PATH, create_challenge_response
from inventory.store import InventoryStore
from tests.web_client import local_client
from web.app import app
from web.passwords import hash_password
from web.security import (
    Access,
    LoginThrottle,
    WebSecurityConfigurationError,
    classify,
    normalize_origin,
    resolve_web_security,
    safe_next_path,
)

ORIGIN = "https://flipper.example.test"
HOST = "flipper.example.test"
PASSWORD = "correct horse battery staple"
SECRET = "Zt4vQ9pLr2Xw8Kd3Nf6Hj1Ms5Bc7Ga0Ey-Ui_Oo4Pp2Rr6Tt8"
OTHER_SECRET = "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk-Ll_Mm1Nn2Oo3Pp4"

# Every registered route, classified. A new route fails this table until it is classified here,
# and the middleware treats it as private in the meantime.
PUBLIC = "public compliance"
AUTHENTICATION = "authentication"
READ = "private read"
MUTATION = "private mutation"
STATIC = "static mount"
EXPECTED_ROUTES = {
    ("GET", ACCOUNT_DELETION_PATH): PUBLIC,
    ("POST", ACCOUNT_DELETION_PATH): PUBLIC,
    ("MOUNT", "/static"): STATIC,
    ("GET", "/login"): AUTHENTICATION,
    ("POST", "/login"): AUTHENTICATION,
    ("POST", "/logout"): MUTATION,
    ("GET", "/"): READ,
    ("GET", "/inventory"): READ,
    ("GET", "/inventory/{inventory_id}"): READ,
    ("POST", "/inventory/{inventory_id}/sourcing-travel"): MUTATION,
    ("POST", "/inventory/{inventory_id}/sourcing-travel/clear"): MUTATION,
    ("GET", "/inventory/{inventory_id}/attachments/{attachment_id}"): READ,
    ("POST", "/inventory/{inventory_id}/notes"): MUTATION,
    ("GET", "/sales"): READ,
    ("GET", "/sales/{sale_id}"): READ,
    ("POST", "/sales/{sale_id}/accounting"): MUTATION,
    ("GET", "/analytics"): READ,
    ("GET", "/insights"): READ,
    ("GET", "/analyze"): READ,
    ("GET", "/deals"): READ,
    ("GET", "/deals/opportunities/new"): READ,
    ("POST", "/deals/opportunities"): MUTATION,
    ("GET", "/deals/opportunities/{opportunity_id}"): READ,
    ("GET", "/deals/ebay/{item_id}"): READ,
    ("GET", "/deals/compare"): READ,
    ("GET", "/deals/history"): READ,
    ("GET", "/deals/history/{snapshot_id}"): READ,
    ("POST", "/deals/ebay/{item_id}/snapshot"): MUTATION,
    ("POST", "/deals/opportunities/{opportunity_id}/snapshot"): MUTATION,
    ("POST", "/deals/history/{snapshot_id}/link"): MUTATION,
    ("POST", "/deals/ebay/{item_id}/comparables"): MUTATION,
    ("POST", "/deals/ebay/{item_id}/comparables/{comparable_id}/edit"): MUTATION,
    ("POST", "/deals/ebay/{item_id}/comparables/{comparable_id}/remove"): MUTATION,
    ("POST", "/deals/ebay/{item_id}/acquire"): MUTATION,
    ("POST", "/deals/opportunities/{opportunity_id}/comparables"): MUTATION,
    ("POST", "/deals/opportunities/{opportunity_id}/comparables/{comparable_id}/edit"): MUTATION,
    ("POST", "/deals/opportunities/{opportunity_id}/comparables/{comparable_id}/remove"): MUTATION,
    ("POST", "/deals/opportunities/{opportunity_id}/acquire"): MUTATION,
    ("GET", "/settings"): READ,
    ("GET", "/ebay/listings"): READ,
    ("GET", "/ebay/orders"): READ,
    ("POST", "/ebay/orders/import"): MUTATION,
    ("POST", "/ebay/listings/sync"): MUTATION,
    ("POST", "/ebay/listings/import"): MUTATION,
}
SAMPLE_PARAMETERS = {"inventory_id": "Q0001", "sale_id": "S000001", "snapshot_id": "R000001"}
# Plausible bodies so a missing boundary would actually reach and exercise domain code.
SAMPLE_FORMS = {
    "/inventory/{inventory_id}/notes": {"notes": "attacker note"},
    "/inventory/{inventory_id}/sourcing-travel": {"round_trip_miles": "10", "fuel_cost": "5"},
    "/sales/{sale_id}/accounting": {"category": "fees", "action": "record", "amount": "9.99"},
    "/deals/history/{snapshot_id}/link": {"inventory_id": "Q0001"},
    "/deals/ebay/{item_id}/acquire": {
        "acquisition_cost": "1.00",
        "acquired_at": "2026-09-01",
        "acquisition_source": "attacker",
    },
    "/ebay/listings/import": {"item_id": "1", "sku": "Q0009", "acquisition_cost": "1"},
    "/ebay/orders/import": {"order_id": "o", "line_item_id": "l"},
    "/deals/opportunities": {"title": "Injected", "price": "1", "currency": "USD"},
}


def _route_surface():
    for route in app.routes:
        if isinstance(route, Mount):
            yield ("MOUNT", route.path)
        else:
            for method in route.methods:
                yield (method, route.path)


def _concrete(path: str) -> str:
    return re.sub(r"{(\w+)}", lambda match: SAMPLE_PARAMETERS.get(match[1], "x1"), path)


def _routes(category):
    return sorted(key for key, value in EXPECTED_ROUTES.items() if value == category)


@pytest.fixture(scope="module")
def password_hash():
    return hash_password(PASSWORD, n=2**14)


def _browser(**headers) -> TestClient:
    return TestClient(
        app, base_url=ORIGIN, headers={"Origin": ORIGIN, **headers}, follow_redirects=False
    )


def _seed(store: InventoryStore):
    store.add(
        title="Recorder",
        source="estate sale",
        acquired_at="2026-08-01",
        acquisition_cost="20.00",
        notes="original note",
    )
    item = store.add(
        title="Laptop",
        source="local seller",
        acquired_at="2026-08-01",
        acquisition_cost="25.00",
        marketplace="eBay",
        marketplace_sku="SKU-1",
    )
    store.transition_status(item.inventory_id, "listed")
    store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="SKU-1",
        quantity=1,
        gross_amount=Decimal("100.00"),
        currency="USD",
        sold_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )


@pytest.fixture
def hosted(monkeypatch, tmp_path, password_hash):
    database = tmp_path / "hosted.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    monkeypatch.setenv(web_security.MODE_ENV, "hosted")
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, SECRET)
    monkeypatch.setenv(web_security.PUBLIC_ORIGIN_ENV, ORIGIN)
    store = InventoryStore(database)
    _seed(store)
    return SimpleNamespace(client=_browser(), store=store, database=database)


def _sign_in(client, password=PASSWORD, **data):
    return client.post("/login", data={"password": password, **data})


def _fingerprint(database):
    return database_state(database).fingerprint


def _set_cookie(response, name=web_security.HOSTED_SESSION_COOKIE):
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        if name in cookie:
            return header, cookie[name]
    return None, None


class Tripwire:
    """Stands in for domain entry points; any use proves the boundary was bypassed."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append("call")
        raise AssertionError("domain code reached")

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError("domain code reached")


@pytest.fixture
def tripwire(monkeypatch):
    wire = Tripwire()
    for name in (
        "_store",
        "_discovery",
        "_live_listing_results",
        "_live_order_review",
        "research_store",
        "acquire_opportunity",
        "import_ebay_sales",
        "create_manual_opportunity",
        "sync_listings",
        "import_listing",
    ):
        monkeypatch.setattr(web_app, name, wire)
    return wire


# Route classification


def test_every_registered_route_is_explicitly_classified():
    surface = set(_route_surface())
    assert surface - EXPECTED_ROUTES.keys() == set(), "classify new routes in EXPECTED_ROUTES"
    assert EXPECTED_ROUTES.keys() - surface == set()


def test_policy_matches_the_classification_table():
    expected_access = {
        PUBLIC: Access.PUBLIC,
        AUTHENTICATION: Access.AUTHENTICATION,
        READ: Access.PRIVATE,
        MUTATION: Access.PRIVATE,
    }
    for (method, path), category in EXPECTED_ROUTES.items():
        if category != STATIC:
            assert classify(method, _concrete(path)) is expected_access[category], (method, path)


def test_public_allow_list_is_exactly_the_ebay_deletion_endpoint():
    assert web_security.PUBLIC_ROUTES == {
        ("GET", ACCOUNT_DELETION_PATH),
        ("POST", ACCOUNT_DELETION_PATH),
    }
    assert web_security.AUTHENTICATION_ROUTES == {("GET", "/login"), ("POST", "/login")}
    for path in web_security.PUBLIC_STATIC_ASSETS:
        assert (web_app.ROOT / "web" / path.removeprefix("/")).is_file()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ACCOUNT_DELETION_PATH + "/"),
        ("PUT", ACCOUNT_DELETION_PATH),
        ("POST", "/static/app.css"),
        ("GET", "/static/./app.css"),
        ("GET", "/static/other.css"),
        ("GET", "/LOGIN"),
        ("GET", "/logout"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_near_misses_of_allow_listed_paths_are_private(method, path):
    assert classify(method, path) is Access.PRIVATE


def test_framework_schema_and_docs_routes_are_not_served(hosted):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert hosted.client.get(path).status_code == 303
    _sign_in(hosted.client)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert hosted.client.get(path).status_code == 404


def test_a_newly_added_route_is_private_by_default(hosted):
    @app.api_route("/__security-probe", methods=["GET", "POST"])
    def probe():
        return {"reached": True}

    try:
        assert hosted.client.get("/__security-probe").status_code == 303
        assert hosted.client.post("/__security-probe").status_code == 401
        _sign_in(hosted.client)
        assert hosted.client.get("/__security-probe").json() == {"reached": True}
    finally:
        app.router.routes[:] = [
            route for route in app.router.routes if route.path != "/__security-probe"
        ]


# Unauthenticated access


@pytest.mark.parametrize("route", _routes(READ), ids=lambda route: route[1])
def test_every_private_read_redirects_to_login(hosted, tripwire, route):
    path = _concrete(route[1])
    response = hosted.client.get(path)
    assert response.status_code == 303
    expected = "/login" if path == "/" else "/login?" + web_app.urlencode({"next": path})
    assert response.headers["location"] == expected
    assert response.headers["cache-control"] == "no-store"
    assert tripwire.calls == []


@pytest.mark.parametrize("route", _routes(MUTATION), ids=lambda route: route[1])
def test_every_private_mutation_is_rejected_before_domain_code(hosted, tripwire, route):
    before = _fingerprint(hosted.database)
    response = hosted.client.post(_concrete(route[1]), data=SAMPLE_FORMS.get(route[1], {"x": "1"}))

    assert response.status_code == 401
    assert "location" not in response.headers
    assert "No changes were made" in response.text
    assert tripwire.calls == []
    assert _fingerprint(hosted.database) == before


@pytest.mark.parametrize("route", _routes(MUTATION), ids=lambda route: route[1])
def test_signed_in_cross_origin_mutations_are_rejected_before_domain_code(
    hosted, monkeypatch, route
):
    assert _sign_in(hosted.client).status_code == 303
    wire = Tripwire()
    for name in ("_store", "_discovery", "research_store", "_live_order_review"):
        monkeypatch.setattr(web_app, name, wire)

    response = hosted.client.post(
        _concrete(route[1]),
        data=SAMPLE_FORMS.get(route[1], {"x": "1"}),
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403
    assert wire.calls == []


def test_unauthenticated_domain_mutations_leave_real_state_unchanged(hosted):
    """Representative inventory, accounting, research, and eBay actions against a real store."""
    store = hosted.store
    before = _fingerprint(hosted.database)
    attempts = [
        ("/inventory/Q0001/notes", {"notes": "attacker"}),
        ("/inventory/Q0001/sourcing-travel", {"round_trip_miles": "12", "fuel_cost": "4"}),
        ("/sales/S000001/accounting", {"category": "fees", "action": "record", "amount": "5"}),
        ("/sales/S000001/accounting", {"category": "shipping", "action": "confirm_zero"}),
        ("/deals/opportunities", {"title": "Injected", "price": "1", "currency": "USD"}),
        ("/ebay/listings/sync", {}),
        ("/ebay/orders/import", {"order_id": "order-2", "line_item_id": "line-2"}),
    ]
    for path, data in attempts:
        assert hosted.client.post(path, data=data).status_code == 401, path

    assert _fingerprint(hosted.database) == before
    assert store.get("Q0001").notes == "original note"
    assert store.list_sale_costs("S000001") == []
    assert web_app.research_store.list_opportunities(None) == ()

    # Positive control: the same request succeeds once signed in, so the check above is real.
    _sign_in(hosted.client)
    assert hosted.client.post("/inventory/Q0001/notes", data={"notes": "owner"}).status_code == 303
    assert store.get("Q0001").notes == "owner"


def test_login_and_login_assets_are_reachable_without_a_session(hosted):
    login = hosted.client.get("/login")
    assert login.status_code == 200
    assert 'type="password"' in login.text
    for path in sorted(web_security.PUBLIC_STATIC_ASSETS):
        response = hosted.client.get(path)
        assert response.status_code == 200, path
        assert "no-store" not in response.headers.get("cache-control", "")


# eBay account-deletion endpoint stays public and self-verifying


TOKEN = "test_verification_token_32_chars_minimum"
ENDPOINT = f"{ORIGIN}{ACCOUNT_DELETION_PATH}"
NOTIFICATION = {
    "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
    "notification": {"notificationId": "n-1", "data": {}},
}


def test_ebay_challenge_needs_no_login_and_keeps_its_protocol(hosted, monkeypatch):
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_TOKEN", TOKEN)
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_ENDPOINT", ENDPOINT)
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)  # eBay sends no Origin

    response = client.get(ACCOUNT_DELETION_PATH, params={"challenge_code": "abc123"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "challengeResponse": create_challenge_response("abc123", TOKEN, ENDPOINT)
    }
    assert "set-cookie" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["strict-transport-security"] == web_security.HSTS_VALUE
    assert response.headers["cache-control"] == "no-store"
    assert client.get(ACCOUNT_DELETION_PATH).status_code == 422


def test_ebay_notifications_need_no_login_but_still_require_a_valid_signature(hosted, monkeypatch):
    processed = []
    monkeypatch.setattr("ebay.compliance.process_account_deletion", processed.append)
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)

    missing = client.post(ACCOUNT_DELETION_PATH, json=NOTIFICATION)
    malformed = client.post(
        ACCOUNT_DELETION_PATH, content=b"{", headers={"content-type": "application/json"}
    )

    class Verifier:
        def __init__(self, result):
            self.result = result

        def verify(self, _notification, _signature):
            return self.result

    monkeypatch.setattr("ebay.compliance._get_signature_verifier", lambda: Verifier(False))
    invalid = client.post(
        ACCOUNT_DELETION_PATH, json=NOTIFICATION, headers={"X-EBAY-SIGNATURE": "bad"}
    )
    assert (missing.status_code, malformed.status_code, invalid.status_code) == (412, 400, 412)
    assert processed == []

    monkeypatch.setattr("ebay.compliance._get_signature_verifier", lambda: Verifier(True))
    valid = client.post(
        ACCOUNT_DELETION_PATH,
        json=NOTIFICATION,
        headers={"X-EBAY-SIGNATURE": "good", "Origin": "https://ebay.example"},
    )
    assert valid.status_code == 204
    assert processed == [NOTIFICATION]


def test_ebay_endpoint_still_requires_a_trusted_host(hosted):
    client = TestClient(app, base_url="https://attacker.example", follow_redirects=False)
    assert client.get(ACCOUNT_DELETION_PATH, params={"challenge_code": "x"}).status_code == 400


# Login, sessions, and logout


def test_login_page_is_minimal_and_reveals_nothing(hosted, password_hash):
    response = hosted.client.get("/login")
    assert "<script" not in response.text
    assert 'name="robots" content="noindex' in response.text
    for secret in (password_hash, SECRET, "FLIPPER_", "scrypt"):
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_wrong_password_fails_generically(hosted, password_hash):
    for attempt in ("wrong password value", "", PASSWORD[:-1]):
        response = _sign_in(hosted.client, attempt)
        assert response.status_code == 401
        assert "Incorrect password." in response.text
        assert _set_cookie(response) == (None, None)
        assert password_hash not in response.text and SECRET not in response.text
    assert hosted.client.get("/").status_code == 303


def test_correct_password_sets_a_hardened_session_cookie(hosted, password_hash):
    response = _sign_in(hosted.client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    header, cookie = _set_cookie(response)
    assert header is not None
    lowered = header.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
    assert "path=/" in lowered and "domain" not in lowered
    assert cookie["max-age"] == str(7 * 24 * 60 * 60)
    for secret in (PASSWORD, password_hash, SECRET):
        assert secret not in header
    page = hosted.client.get("/")
    assert page.status_code == 200
    assert 'action="/logout"' in page.text


def test_login_preserves_only_safe_local_destinations(hosted):
    redirect = hosted.client.get("/inventory?status=listed&search=Q0")
    assert (
        redirect.headers["location"] == "/login?next=%2Finventory%3Fstatus%3Dlisted%26search%3DQ0"
    )
    page = hosted.client.get(redirect.headers["location"])
    assert 'name="next" value="/inventory?status=listed&amp;search=Q0"' in page.text

    response = _sign_in(hosted.client, next="/inventory?status=listed&search=Q0")
    assert response.headers["location"] == "/inventory?status=listed&search=Q0"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example",
        "///evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "/%2F%2Fevil.example",
        "/%2f/evil.example",
        "/%252F%252Fevil.example",
        "/%5Cevil.example",
        "%2F%2Fevil.example",
        "javascript:alert(1)",
        "http:/evil.example",
        "/\tinventory",
        "/inventory\r\nSet-Cookie: x=y",
        "/login",
        "/logout",
        "",
        "inventory",
        "/" + "a" * 3000,
    ],
)
def test_unsafe_return_destinations_fall_back_to_the_dashboard(hosted, target):
    assert safe_next_path(target) == "/"
    response = _sign_in(hosted.client, next=target)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_signed_in_login_page_redirects_to_safe_destination(hosted):
    _sign_in(hosted.client)
    assert hosted.client.get("/login?next=/sales").headers["location"] == "/sales"
    assert hosted.client.get("/login?next=//evil.example").headers["location"] == "/"


def test_tampered_cookie_is_treated_as_signed_out(hosted):
    _sign_in(hosted.client)
    name = web_security.HOSTED_SESSION_COOKIE
    value = hosted.client.cookies.get(name)
    hosted.client.cookies.set(name, value[:-3] + ("AAA" if not value.endswith("AAA") else "BBB"))
    assert hosted.client.get("/").status_code == 303
    assert hosted.client.post("/inventory/Q0001/notes", data={"notes": "x"}).status_code == 401


def test_rotating_the_session_secret_signs_everyone_out(hosted, monkeypatch):
    _sign_in(hosted.client)
    assert hosted.client.get("/").status_code == 200
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, OTHER_SECRET)
    assert hosted.client.get("/").status_code == 303


def test_changing_the_password_signs_everyone_out(hosted, monkeypatch):
    _sign_in(hosted.client)
    monkeypatch.setenv(
        web_security.PASSWORD_HASH_ENV, hash_password("a different owner password", n=2**14)
    )
    assert hosted.client.get("/").status_code == 303


def test_idle_refresh_is_throttled_and_idle_expiry_signs_out(hosted, monkeypatch):
    now = [2_000_000_000.0]
    monkeypatch.setattr(web_security, "wall_clock", lambda: now[0])
    _sign_in(hosted.client)

    now[0] += 60
    assert _set_cookie(hosted.client.get("/")) == (None, None)

    now[0] += 2 * 60 * 60
    refreshed = hosted.client.get("/")
    header, cookie = _set_cookie(refreshed)
    assert refreshed.status_code == 200 and header is not None
    assert cookie["max-age"] == str(7 * 24 * 60 * 60)

    now[0] += 7 * 24 * 60 * 60
    assert hosted.client.get("/").status_code == 303


def test_absolute_expiry_signs_out_an_active_browser(hosted, monkeypatch):
    now = [2_000_000_000.0]
    monkeypatch.setattr(web_security, "wall_clock", lambda: now[0])
    _sign_in(hosted.client)
    for _ in range(5):  # stays active; never idle for more than five days
        now[0] += 5 * 24 * 60 * 60
        assert hosted.client.get("/").status_code == 200
    now[0] += 5 * 24 * 60 * 60  # 30 days after sign-in
    assert hosted.client.get("/").status_code == 303


def test_logout_clears_the_cookie_and_private_pages_require_login_again(hosted):
    _sign_in(hosted.client)
    assert hosted.client.get("/inventory").headers["cache-control"] == "no-store"

    response = hosted.client.post("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert response.headers["clear-site-data"] == '"cache"'
    assert response.headers["cache-control"] == "no-store"
    header, cookie = _set_cookie(response)
    assert cookie.value == "" and cookie["max-age"] == "0"
    assert "secure" in header.lower()
    assert hosted.client.get("/inventory").status_code == 303


def test_logout_is_not_a_get_side_effect_and_requires_same_origin(hosted):
    _sign_in(hosted.client)
    assert hosted.client.get("/logout").status_code == 405
    rejected = hosted.client.post("/logout", headers={"Origin": "https://attacker.example"})
    assert rejected.status_code == 403
    assert _set_cookie(rejected) == (None, None)
    assert hosted.client.get("/").status_code == 200


def test_logout_requires_a_session(hosted):
    assert hosted.client.post("/logout").status_code == 401


# CSRF / origin validation


@pytest.mark.parametrize(
    ("headers", "accepted"),
    [
        ({"Origin": ORIGIN}, True),
        ({"Origin": ORIGIN + ":443"}, True),
        ({"Origin": "HTTPS://FLIPPER.EXAMPLE.TEST"}, True),
        ({"Origin": "https://attacker.example"}, False),
        ({"Origin": "http://flipper.example.test"}, False),
        ({"Origin": "https://flipper.example.test.attacker.example"}, False),
        ({"Origin": "https://evilflipper.example.test"}, False),
        ({"Origin": "https://sub.flipper.example.test"}, False),
        ({"Origin": "https://flipper.example.test:8443"}, False),
        ({"Origin": "null"}, False),
        ({"Origin": ""}, False),
        ({"Origin": "https://user@flipper.example.test"}, False),
        ({"Referer": ORIGIN + "/inventory/Q0001?edit_notes=true"}, True),
        ({"Referer": "https://attacker.example/inventory/Q0001"}, False),
        ({"Referer": "https://flipper.example.test.attacker.example/"}, False),
        ({"Referer": "http://flipper.example.test/inventory"}, False),
        ({"Referer": "https://flipper.example.test:99999/"}, False),
        ({"Referer": "not a url"}, False),
        ({"Referer": "/inventory/Q0001"}, False),
        ({}, False),
        ({"Origin": "https://attacker.example", "Referer": ORIGIN + "/"}, False),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, False),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"}, False),
        ({"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}, True),
    ],
)
def test_hosted_mutation_origin_validation(hosted, headers, accepted):
    _sign_in(hosted.client)
    client = TestClient(
        app,
        base_url=ORIGIN,
        headers=headers,
        cookies=dict(hosted.client.cookies),
        follow_redirects=False,
    )
    before = hosted.store.get("Q0001").notes

    response = client.post("/inventory/Q0001/notes", data={"notes": "changed"})

    assert response.status_code == (303 if accepted else 403)
    assert (hosted.store.get("Q0001").notes != before) is accepted


def test_forwarded_and_host_headers_cannot_redefine_the_trusted_origin(hosted):
    _sign_in(hosted.client)
    for headers in (
        {"Origin": "https://attacker.example", "X-Forwarded-Host": "attacker.example"},
        {"Origin": "http://flipper.example.test", "X-Forwarded-Proto": "http"},
        {"Origin": "https://attacker.example", "Forwarded": "host=attacker.example;proto=https"},
    ):
        response = hosted.client.post(
            "/inventory/Q0001/notes", data={"notes": "x"}, headers=headers
        )
        assert response.status_code == 403
    attacker_host = hosted.client.post(
        "/inventory/Q0001/notes",
        data={"notes": "x"},
        headers={"Host": "attacker.example", "Origin": "https://attacker.example"},
    )
    assert attacker_host.status_code == 400
    assert hosted.store.get("Q0001").notes == "original note"


def test_login_is_protected_against_cross_site_login(hosted):
    for headers in ({"Origin": "https://attacker.example"}, {"Origin": "null"}):
        response = hosted.client.post("/login", data={"password": PASSWORD}, headers=headers)
        assert response.status_code == 403
        assert _set_cookie(response) == (None, None)
    no_origin = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    response = no_origin.post("/login", data={"password": PASSWORD})
    assert response.status_code == 403
    assert _set_cookie(response) == (None, None)


def test_origin_normalization():
    assert normalize_origin("https://Flipper.Example.test:443/path?q#f") == ORIGIN
    assert normalize_origin("http://[::1]:8000/x") == "http://[::1]:8000"
    for value in ("ftp://x", "https://", "https://a:b@x", "https://x:bad", "https://x\\y", None):
        assert normalize_origin(value) is None


# Trusted hosts


@pytest.mark.parametrize(
    ("host", "status"),
    [
        (HOST, 303),
        ("FLIPPER.EXAMPLE.TEST", 303),
        (HOST + ":443", 303),
        ("attacker.example", 400),
        ("flipper.example.test.attacker.example", 400),
        ("localhost", 400),
        ("", 400),
        ("flipper.example.test@attacker.example", 400),
        ("flipper.example.test:notaport", 400),
    ],
)
def test_hosted_trusted_hosts(hosted, host, status):
    assert hosted.client.get("/", headers={"Host": host}).status_code == status


def test_additional_allowed_hosts_are_configurable(hosted, monkeypatch):
    monkeypatch.setenv(web_security.ALLOWED_HOSTS_ENV, "flipper-internal.example.test, 10.0.0.5")
    for host in ("flipper-internal.example.test", "10.0.0.5", HOST):
        assert hosted.client.get("/", headers={"Host": host}).status_code == 303
    assert hosted.client.get("/", headers={"Host": "other.example"}).status_code == 400


# Security headers


def test_private_html_headers(hosted):
    _sign_in(hosted.client)
    response = hosted.client.get("/inventory")
    headers = response.headers
    assert headers["content-security-policy"] == web_security.CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["strict-transport-security"] == "max-age=31536000"
    assert headers["cache-control"] == "no-store"


def test_mutation_redirects_and_rejections_are_not_cacheable(hosted):
    rejected = hosted.client.post("/inventory/Q0001/notes", data={"notes": "x"})
    _sign_in(hosted.client)
    accepted = hosted.client.post("/inventory/Q0001/notes", data={"notes": "x"})
    for response in (rejected, accepted):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_rendered_templates_are_compatible_with_the_content_security_policy(hosted):
    _sign_in(hosted.client)
    for path in ("/", "/inventory", "/inventory/Q0001", "/sales", "/sales/S000001", "/settings"):
        text = hosted.client.get(path).text
        assert "<script" not in text and " style=" not in text and "<style" not in text, path
        assert not re.search(r'(src|href)="(https?:)?//', text.split("<main", 1)[0]), path


def test_local_mode_sends_no_hsts(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "local.db"))
    response = local_client(app).get("/")
    assert "strict-transport-security" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == web_security.CONTENT_SECURITY_POLICY


# Login throttling


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_throttle_backs_off_exponentially_with_a_cap():
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(LoginThrottle.FREE_FAILURES):
        assert throttle.retry_after("a") == 0
        throttle.record_failure("a")
    delays = []
    for _ in range(12):
        delays.append(throttle.retry_after("a"))
        clock.now += delays[-1]
        throttle.record_failure("a")
    assert delays[:4] == [1, 2, 4, 8]
    assert max(delays) == LoginThrottle.MAX_DELAY_SECONDS
    assert throttle.retry_after("other") == 0


def test_throttle_success_resets_and_idle_entries_expire():
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(8):
        throttle.record_failure("a")
        throttle.record_failure("b")
    throttle.record_success("a")
    assert throttle.retry_after("a") == 0
    assert throttle.retry_after("b") > 0
    clock.now += LoginThrottle.FORGET_AFTER_SECONDS
    assert throttle.retry_after("b") == 0
    assert len(throttle) == 0


def test_throttle_state_is_bounded():
    throttle = LoginThrottle(Clock())
    for index in range(LoginThrottle.MAX_ENTRIES * 3):
        throttle.record_failure(f"client-{index}")
    assert len(throttle) == LoginThrottle.MAX_ENTRIES


def test_login_throttling_is_generic_skips_verification_and_recovers(hosted, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(web_security, "login_throttle", LoginThrottle(clock))
    verifications = []
    real_verify = web_app._verify_owner_password

    def counted(config, password):
        verifications.append(1)
        return real_verify(config, password)

    monkeypatch.setattr(web_app, "_verify_owner_password", counted)
    for _ in range(LoginThrottle.FREE_FAILURES):
        assert _sign_in(hosted.client, "wrong password value").status_code == 401

    for attempt in ("wrong password value", PASSWORD):
        throttled = _sign_in(hosted.client, attempt)
        assert throttled.status_code == 429
        assert "Too many sign-in attempts" in throttled.text
        assert throttled.headers["retry-after"] == "1"
        assert _set_cookie(throttled) == (None, None)
    assert len(verifications) == LoginThrottle.FREE_FAILURES

    clock.now += 1
    assert _sign_in(hosted.client).status_code == 303
    assert web_security.login_throttle.retry_after("testclient") == 0


def test_malformed_login_requests_do_not_grow_throttle_state(hosted):
    for _ in range(20):
        response = hosted.client.post(
            "/login", content=b"password=x", headers={"content-type": "text/plain"}
        )
        assert response.status_code == 415
    assert len(web_security.login_throttle) == 0


# Local mode


@pytest.fixture
def local(monkeypatch, tmp_path):
    database = tmp_path / "local.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    store = InventoryStore(database)
    _seed(store)
    return SimpleNamespace(store=store, database=database)


def test_local_mode_without_password_is_loopback_only(local):
    assert local_client(app).get("/").status_code == 200
    assert "Sign out" not in local_client(app).get("/").text
    for peer in ("192.168.1.20", "203.0.113.7", "testclient"):
        client = TestClient(app, base_url="http://localhost", client=(peer, 1234))
        assert client.get("/").status_code == 403
    for host in ("flipper.example.test", "attacker.example", "192.168.1.20"):
        assert local_client(app).get("/", headers={"Host": host}).status_code == 400


def test_local_login_route_is_inert_without_a_password(local):
    client = local_client(app, follow_redirects=False)
    assert client.get("/login").headers["location"] == "/"
    assert client.post("/login", data={"password": "anything"}).headers["location"] == "/"


@pytest.mark.parametrize(
    ("headers", "accepted"),
    [
        ({}, True),  # the helper sends Origin: http://localhost
        ({"Origin": "http://localhost:8000"}, False),
        ({"Origin": "https://localhost"}, False),
        ({"Origin": "http://127.0.0.1"}, False),
        ({"Origin": "https://attacker.example"}, False),
        ({"Origin": None, "Referer": "http://localhost/inventory/Q0001"}, True),
        ({"Origin": None, "Referer": "https://attacker.example/"}, False),
        ({"Origin": None}, False),
    ],
)
def test_local_mode_still_requires_same_origin_mutations(local, headers, accepted):
    client = local_client(app, follow_redirects=False)
    if headers.get("Origin", "") is None:
        del client.headers["Origin"]
        headers = {key: value for key, value in headers.items() if value is not None}

    response = client.post("/inventory/Q0001/notes", data={"notes": "changed"}, headers=headers)

    assert response.status_code == (303 if accepted else 403)
    assert (local.store.get("Q0001").notes == "changed") is accepted


def test_local_mode_with_password_requires_login_and_allows_configured_lan_host(
    local, monkeypatch, password_hash
):
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, SECRET)
    monkeypatch.setenv(web_security.ALLOWED_HOSTS_ENV, "flipper.lan")
    client = TestClient(
        app,
        base_url="http://flipper.lan:8000",
        client=("192.168.1.20", 5555),
        headers={"Origin": "http://flipper.lan:8000"},
        follow_redirects=False,
    )
    assert client.get("/").status_code == 303
    response = _sign_in(client)
    header, _ = _set_cookie(response, web_security.LOCAL_SESSION_COOKIE)
    assert "secure" not in header.lower() and "httponly" in header.lower()
    assert client.get("/").status_code == 200
    assert "strict-transport-security" not in client.get("/").headers


# Startup and configuration


def _environment(password_hash, **overrides):
    environ = {
        web_security.MODE_ENV: "hosted",
        web_security.PASSWORD_HASH_ENV: password_hash,
        web_security.SESSION_SECRET_ENV: SECRET,
        web_security.PUBLIC_ORIGIN_ENV: ORIGIN,
    }
    environ.update(overrides)
    return {key: value for key, value in environ.items() if value is not None}


def test_default_is_local_mode_without_authentication():
    config = resolve_web_security({})
    assert config.mode == "local" and not config.auth_required and not config.secure_cookies
    assert config.allowed_hosts == {"localhost", "127.0.0.1", "::1"}
    assert config.session_cookie == "flipper_session"


def test_valid_hosted_configuration(password_hash):
    config = resolve_web_security(
        _environment(password_hash, FLIPPER_PUBLIC_ORIGIN="https://Flipper.Example.test:443/")
    )
    assert config.hosted and config.auth_required and config.secure_cookies
    assert config.public_origin == ORIGIN
    assert config.allowed_hosts == {HOST}
    assert config.session_cookie == "__Host-flipper_session"
    assert SECRET not in repr(config) and password_hash not in repr(config)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({web_security.PASSWORD_HASH_ENV: None}, "requires FLIPPER_PASSWORD_HASH"),
        ({web_security.SESSION_SECRET_ENV: None}, "requires FLIPPER_SESSION_SECRET"),
        ({web_security.PUBLIC_ORIGIN_ENV: None}, "requires FLIPPER_PUBLIC_ORIGIN"),
        ({web_security.SESSION_SECRET_ENV: "  "}, "requires FLIPPER_SESSION_SECRET"),
        ({web_security.MODE_ENV: "production"}, "must be 'local' or 'hosted'"),
        (
            {web_security.PASSWORD_HASH_ENV: "plaintext-password"},
            "FLIPPER_PASSWORD_HASH is invalid",
        ),
        ({web_security.SESSION_SECRET_ENV: "too-short-secret"}, "FLIPPER_SESSION_SECRET must"),
        ({web_security.SESSION_SECRET_ENV: "ab" * 40}, "FLIPPER_SESSION_SECRET must"),
        ({web_security.PUBLIC_ORIGIN_ENV: "http://flipper.example.test"}, "https:// origin"),
        ({web_security.PUBLIC_ORIGIN_ENV: ORIGIN + "/app"}, "https:// origin"),
        ({web_security.PUBLIC_ORIGIN_ENV: ORIGIN + "?x=1"}, "https:// origin"),
        ({web_security.PUBLIC_ORIGIN_ENV: "https://u:p@flipper.example.test"}, "https:// origin"),
        ({web_security.PUBLIC_ORIGIN_ENV: "https://*.example.test"}, "https:// origin"),
        ({web_security.PUBLIC_ORIGIN_ENV: "flipper.example.test"}, "https:// origin"),
        ({web_security.ALLOWED_HOSTS_ENV: "*.example.test"}, "FLIPPER_ALLOWED_HOSTS"),
        ({web_security.ALLOWED_HOSTS_ENV: "host.example:8000"}, "FLIPPER_ALLOWED_HOSTS"),
        ({web_security.ALLOWED_HOSTS_ENV: "https://host.example"}, "FLIPPER_ALLOWED_HOSTS"),
    ],
)
def test_invalid_hosted_configuration_fails_without_revealing_values(
    password_hash, overrides, message
):
    with pytest.raises(WebSecurityConfigurationError, match=re.escape(message)) as raised:
        resolve_web_security(_environment(password_hash, **overrides))
    for value in (password_hash, SECRET, "plaintext-password", "too-short-secret"):
        assert value not in str(raised.value)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({web_security.PASSWORD_HASH_ENV: "set"}, "must be configured together"),
        ({web_security.SESSION_SECRET_ENV: SECRET}, "must be configured together"),
        ({web_security.PUBLIC_ORIGIN_ENV: ORIGIN}, "only valid with FLIPPER_WEB_SECURITY_MODE"),
    ],
)
def test_partial_local_configuration_fails_rather_than_guessing(environ, message):
    with pytest.raises(WebSecurityConfigurationError, match=message):
        resolve_web_security(environ)


def test_hosted_startup_fails_closed_without_each_required_setting(
    monkeypatch, tmp_path, password_hash
):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "startup.db"))
    for missing in (
        web_security.PASSWORD_HASH_ENV,
        web_security.SESSION_SECRET_ENV,
        web_security.PUBLIC_ORIGIN_ENV,
    ):
        for name, value in _environment(password_hash).items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv(missing)
        with pytest.raises(WebSecurityConfigurationError, match=missing):
            with TestClient(app, base_url=ORIGIN):
                pass
    assert not (tmp_path / "startup.db").exists()


def test_invalid_configuration_without_lifespan_fails_closed_per_request(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "web.db"))
    monkeypatch.setenv(web_security.MODE_ENV, "hosted")
    response = TestClient(app, base_url=ORIGIN).get(ACCOUNT_DELETION_PATH)
    assert response.status_code == 503
    assert response.text == "Flipper is not available."
    assert response.headers["cache-control"] == "no-store"


def test_valid_hosted_configuration_starts_and_serves_login(hosted):
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 303


def test_browser_errors_are_generic(hosted, monkeypatch, password_hash):
    _sign_in(hosted.client)

    def failing(*_args, **_kwargs):
        raise ValueError(f"internal detail {hosted.database} {SECRET}")

    monkeypatch.setattr(web_app, "build_summary_report", failing)
    client = TestClient(
        app,
        base_url=ORIGIN,
        cookies=dict(hosted.client.cookies),
        raise_server_exceptions=False,
    )
    response = client.get("/")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    missing = hosted.client.get("/inventory/Q9999")
    assert missing.status_code == 404
    for text in (response.text, missing.text):
        assert SECRET not in text and password_hash not in text
        assert str(hosted.database) not in text and "Traceback" not in text
