"""Private-network (LAN) mode: explicit launch, mandatory sign-in, and a private-only boundary."""

import json
import os
import re
import socket
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

import main
import web.app as web_app
import web.lan as web_lan
import web.security as web_security
from backup.fingerprint import database_state
from inventory.store import InventoryStore
from storage_config import INVENTORY_DB_FILENAME
from web.app import app
from web.passwords import hash_password

PASSWORD = "correct horse battery staple"
SECRET = "Zt4vQ9pLr2Xw8Kd3Nf6Hj1Ms5Bc7Ga0Ey-Ui_Oo4Pp2Rr6Tt8"
SERVER = "192.168.1.20"
PHONE = "192.168.1.35"
LAN_ORIGIN = f"http://{SERVER}:8000"
STORAGE_ENV = ("FLIPPER_INVENTORY_DB", "FLIPPER_DATA_DIR", "FLIPPER_ATTACHMENT_ROOT")
PHONE_PAGES = ("/", "/deals", "/inventory", "/inventory/Q0001", "/sales", "/insights", "/settings")


@pytest.fixture(scope="module")
def password_hash():
    return hash_password(PASSWORD, n=2**14)


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch):
    for name in STORAGE_ENV:
        monkeypatch.delenv(name, raising=False)


def _phone(peer=PHONE, host=SERVER, **kwargs) -> TestClient:
    origin = f"http://{host}:8000"
    headers = {"Origin": origin, **kwargs.pop("headers", {})}
    return TestClient(
        app,
        base_url=origin,
        client=(peer, 50123),
        headers=headers,
        follow_redirects=False,
        **kwargs,
    )


def _sign_in(client, password=PASSWORD, **data):
    return client.post("/login", data={"password": password, **data})


def _session_cookie(response):
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        if web_security.LOCAL_SESSION_COOKIE in cookie:
            return header, cookie[web_security.LOCAL_SESSION_COOKIE]
    return None, None


@pytest.fixture
def lan(monkeypatch, tmp_path, password_hash):
    database = tmp_path / "lan.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    monkeypatch.setenv(web_security.MODE_ENV, web_security.LAN_MODE)
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, SECRET)
    store = InventoryStore(database)
    store.add(
        title="Synthetic ThinkPad with an extremely long descriptive title " + "x" * 60,
        source="synthetic estate sale",
        acquired_at="2026-08-01",
        acquisition_cost="1234567.89",
    )
    return SimpleNamespace(store=store, database=database, client=_phone())


# Configuration


def test_lan_mode_reuses_owner_sign_in_with_plain_http_cookies(password_hash):
    config = web_security.resolve_web_security(
        {
            web_security.MODE_ENV: "LAN",
            web_security.PASSWORD_HASH_ENV: password_hash,
            web_security.SESSION_SECRET_ENV: SECRET,
        }
    )
    assert config.lan and not config.hosted and config.auth_required
    assert not config.secure_cookies  # LAN is plain HTTP; Secure cookies would never be sent.
    assert config.session_cookie == web_security.LOCAL_SESSION_COOKIE
    assert config.public_origin is None
    assert SECRET not in repr(config) and password_hash not in repr(config)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({web_security.PASSWORD_HASH_ENV: None}, "lan requires FLIPPER_PASSWORD_HASH"),
        ({web_security.SESSION_SECRET_ENV: None}, "lan requires FLIPPER_SESSION_SECRET"),
        ({web_security.PASSWORD_HASH_ENV: " "}, "lan requires FLIPPER_PASSWORD_HASH"),
        ({web_security.PASSWORD_HASH_ENV: PASSWORD}, "FLIPPER_PASSWORD_HASH is invalid"),
        ({web_security.SESSION_SECRET_ENV: "short"}, "FLIPPER_SESSION_SECRET must"),
        ({web_security.PUBLIC_ORIGIN_ENV: "https://flipper.example"}, "only valid with"),
    ],
)
def test_lan_mode_never_starts_without_valid_sign_in(password_hash, overrides, message):
    environ = {
        web_security.MODE_ENV: web_security.LAN_MODE,
        web_security.PASSWORD_HASH_ENV: password_hash,
        web_security.SESSION_SECRET_ENV: SECRET,
    }
    environ.update(overrides)
    environ = {key: value for key, value in environ.items() if value is not None}
    with pytest.raises(web_security.WebSecurityConfigurationError, match=message) as raised:
        web_security.resolve_web_security(environ)
    assert PASSWORD not in str(raised.value) and SECRET not in str(raised.value)


@pytest.mark.parametrize(
    ("address", "private"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.4.5.6", True),
        ("172.16.0.9", True),
        ("172.31.255.1", True),
        ("192.168.0.2", True),
        ("169.254.10.1", True),
        ("fd12:3456::1", True),
        ("fe80::1", True),
        ("::ffff:192.168.1.5", True),
        ("172.32.0.1", False),
        (
            "100.64.0.1",
            False,
        ),  # carrier-grade NAT / overlay networks need an explicit future opt-in
        ("8.8.8.8", False),
        ("203.0.113.7", False),
        ("2001:db8::1", False),
        ("::ffff:8.8.8.8", False),
        ("0.0.0.0", False),
        ("flipper.lan", False),
        ("testclient", False),
    ],
)
def test_private_network_classification(address, private):
    assert web_security.is_private_address(address) is private


# Boundary behavior


def test_private_routes_require_sign_in_from_the_phone(lan):
    for path in PHONE_PAGES:
        response = lan.client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")
    response = lan.client.post("/inventory/Q0001/notes", data={"notes": "unauthenticated"})
    assert response.status_code == 401
    assert lan.store.get("Q0001").notes == ""


def test_phone_sign_in_navigation_and_sign_out(lan):
    assert lan.client.get("/login").status_code == 200
    assert _sign_in(lan.client, "wrong password here").status_code == 401

    response = _sign_in(lan.client, next="/inventory")
    assert response.status_code == 303 and response.headers["location"] == "/inventory"
    header, cookie = _session_cookie(response)
    lowered = header.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered
    assert "secure" not in lowered and "domain" not in lowered
    assert PASSWORD not in cookie.value and SECRET not in cookie.value

    for path in PHONE_PAGES:
        page = lan.client.get(path)
        assert page.status_code == 200, path
        assert page.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in page.headers
        assert "Sign out" in page.text

    response = lan.client.post("/inventory/Q0001/notes", data={"notes": "from the phone"})
    assert response.status_code == 303
    assert lan.store.get("Q0001").notes == "from the phone"

    response = lan.client.post("/logout")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert response.headers["clear-site-data"] == '"cache"'
    assert lan.client.get("/").status_code == 303


def test_desktop_loopback_uses_the_same_sign_in(lan):
    desktop = _phone(peer="127.0.0.1", host="127.0.0.1")
    assert desktop.get("/").status_code == 303
    assert _sign_in(desktop).status_code == 303
    assert desktop.get("/").status_code == 200


@pytest.mark.parametrize("peer", ["203.0.113.7", "8.8.8.8", "100.100.1.2", "2001:db8::5", "x"])
def test_public_peers_are_refused_even_with_a_valid_session(lan, peer):
    signed_in = lan.client
    assert _sign_in(signed_in).status_code == 303
    outsider = _phone(peer=peer)
    outsider.cookies = signed_in.cookies
    for method, path in (("GET", "/"), ("GET", "/login"), ("POST", "/login")):
        response = outsider.request(method, path, data={"password": PASSWORD})
        assert response.status_code == 403, (method, path)
        assert "no-store" in response.headers["cache-control"]
    assert len(web_security.login_throttle) == 0


@pytest.mark.parametrize(
    ("host", "status"),
    [
        (SERVER, 303),
        ("10.0.0.44", 303),  # DHCP handed out a new address: no restart needed
        ("127.0.0.1", 303),
        ("localhost", 303),
        ("203.0.113.9", 400),
        ("100.101.102.103", 400),
        ("attacker.example", 400),  # DNS-rebinding name resolving to the LAN address
        ("flipper.lan", 400),
        ("192.168.1.20.nip.io", 400),
    ],
)
def test_lan_trusted_hosts(lan, host, status):
    assert lan.client.get("/", headers={"Host": f"{host}:8000"}).status_code == status


def test_lan_host_names_still_need_explicit_configuration(lan, monkeypatch):
    monkeypatch.setenv(web_security.ALLOWED_HOSTS_ENV, "flipper-pc.local")
    assert lan.client.get("/", headers={"Host": "flipper-pc.local:8000"}).status_code == 303


@pytest.mark.parametrize(
    ("headers", "accepted"),
    [
        ({}, True),
        ({"Origin": "http://192.168.1.99:8000"}, False),  # another device's origin
        ({"Origin": f"http://{SERVER}:8001"}, False),
        ({"Origin": f"https://{SERVER}:8000"}, False),
        ({"Origin": "http://attacker.example"}, False),
        ({"Origin": "null"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Origin": None, "Referer": f"{LAN_ORIGIN}/inventory/Q0001"}, True),
        ({"Origin": None, "Referer": "http://attacker.example/"}, False),
        ({"Origin": None}, False),
    ],
)
def test_lan_mutations_require_same_origin(lan, headers, accepted):
    client = lan.client
    assert _sign_in(client).status_code == 303
    if headers.get("Origin", "") is None:
        del client.headers["Origin"]
        headers = {key: value for key, value in headers.items() if value is not None}
    response = client.post("/inventory/Q0001/notes", data={"notes": "changed"}, headers=headers)
    assert response.status_code == (303 if accepted else 403)
    assert (lan.store.get("Q0001").notes == "changed") is accepted


def test_cross_site_login_is_rejected(lan):
    response = lan.client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": "http://attacker.example"}
    )
    assert response.status_code == 403
    assert _session_cookie(response) == (None, None)


def test_lan_login_throttling_is_per_phone_address(lan):
    for _ in range(web_security.LoginThrottle.FREE_FAILURES + 1):
        _sign_in(lan.client, "wrong password here")
    throttled = _sign_in(lan.client)
    assert throttled.status_code == 429 and "Retry-After" in throttled.headers
    other_phone = _phone(peer="192.168.1.36")
    assert _sign_in(other_phone).status_code == 303


def test_lan_security_headers(lan):
    assert _sign_in(lan.client).status_code == 303
    headers = lan.client.get("/").headers
    assert headers["content-security-policy"] == web_security.CONTENT_SECURITY_POLICY
    assert "script-src" not in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "strict-transport-security" not in headers


# Other modes are unchanged


def test_local_mode_does_not_gain_the_lan_host_relaxation(monkeypatch, tmp_path, password_hash):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "local.db"))
    client = _phone()
    assert client.get("/").status_code == 400  # no password: loopback only
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, SECRET)
    assert client.get("/").status_code == 400  # a LAN IP still needs FLIPPER_ALLOWED_HOSTS
    no_password_peer = TestClient(app, base_url="http://localhost", client=(PHONE, 1))
    monkeypatch.delenv(web_security.PASSWORD_HASH_ENV)
    monkeypatch.delenv(web_security.SESSION_SECRET_ENV)
    assert no_password_peer.get("/").status_code == 403


def test_hosted_mode_is_unchanged(monkeypatch, tmp_path, password_hash):
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(tmp_path / "hosted.db"))
    monkeypatch.setenv(web_security.MODE_ENV, web_security.HOSTED_MODE)
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(web_security.SESSION_SECRET_ENV, SECRET)
    monkeypatch.setenv(web_security.PUBLIC_ORIGIN_ENV, "https://flipper.example.test")
    config = web_security.current_config()
    assert config.secure_cookies and config.session_cookie == "__Host-flipper_session"
    assert not config.allows_host(SERVER) and config.allows_host("flipper.example.test")
    # The hosting proxy's address is public: hosted mode never applies the private-peer rule.
    client = TestClient(app, base_url="https://flipper.example.test", client=("203.0.113.7", 1))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["strict-transport-security"] == web_security.HSTS_VALUE
    assert client.get("/", headers={"Host": SERVER}).status_code == 400


def test_ordinary_development_server_defaults_to_loopback():
    assert uvicorn.Config(app).host == "127.0.0.1"
    assert web_security.resolve_web_security({}).mode == web_security.LOCAL_MODE


# Launch planning


def test_cli_requires_an_explicit_lan_subcommand():
    parser = main.build_parser()
    args = parser.parse_args(["web", "lan"])
    assert (args.port, args.bind) == (8000, "0.0.0.0")
    args = parser.parse_args(["web", "lan", "--port", "8123", "--bind", "192.168.1.20"])
    assert (args.port, args.bind) == (8123, "192.168.1.20")
    assert not hasattr(args, "database")  # the web app's own storage resolution applies
    for argv in (
        ["web"],
        ["web", "lan", "--port", "80"],
        ["web", "lan", "--port", "70000"],
        ["web", "lan", "--port", "http"],
        ["web", "lan", "--bind", "127.0.0.1"],
        ["web", "lan", "--bind", "8.8.8.8"],
        ["web", "lan", "--bind", "::"],
        ["web", "lan", "--bind", "localhost"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def _lan_env(password_hash, **overrides):
    environ = {web_security.PASSWORD_HASH_ENV: password_hash}
    environ.update(overrides)
    return {key: value for key, value in environ.items() if value is not None}


def test_missing_password_hash_fails_with_setup_guidance():
    with pytest.raises(web_lan.LanStartupError, match="auth hash-password"):
        web_lan.lan_environment({})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({web_security.MODE_ENV: "hosted"}, "hosted is configured"),
        ({web_security.MODE_ENV: "public"}, "must be 'local' or 'lan'"),
        ({web_security.PUBLIC_ORIGIN_ENV: "https://flipper.example"}, "only valid with"),
        ({web_security.PASSWORD_HASH_ENV: PASSWORD}, "FLIPPER_PASSWORD_HASH is invalid"),
        ({web_security.SESSION_SECRET_ENV: "not-random-enough"}, "FLIPPER_SESSION_SECRET"),
        ({web_security.ALLOWED_HOSTS_ENV: "*.lan"}, "FLIPPER_ALLOWED_HOSTS"),
    ],
)
def test_invalid_security_configuration_fails_safely(password_hash, overrides, message):
    environ = _lan_env(password_hash, **overrides)
    with pytest.raises(web_lan.LanStartupError, match=re.escape(message)) as raised:
        web_lan.lan_environment(environ)
    assert PASSWORD not in str(raised.value) and password_hash not in str(raised.value)


def test_without_a_configured_secret_sessions_are_process_ephemeral(password_hash):
    environ = _lan_env(password_hash)
    first, ephemeral = web_lan.lan_environment(environ)
    second, _ = web_lan.lan_environment(environ)
    assert ephemeral
    assert first[web_security.MODE_ENV] == "lan"
    assert first[web_security.SESSION_SECRET_ENV] != second[web_security.SESSION_SECRET_ENV]
    web_security.resolve_web_security({**environ, **first})
    assert environ == _lan_env(password_hash)  # the caller's mapping is not modified


def test_a_configured_secret_is_kept_so_sign_in_survives_restarts(password_hash):
    updates, ephemeral = web_lan.lan_environment(
        _lan_env(password_hash, FLIPPER_SESSION_SECRET=SECRET, FLIPPER_WEB_SECURITY_MODE="local")
    )
    assert not ephemeral and updates == {web_security.MODE_ENV: "lan"}


@pytest.mark.parametrize(
    ("storage", "expected"),
    [
        ({}, web_lan.REPOSITORY_ROOT / "data" / INVENTORY_DB_FILENAME),
        ({"FLIPPER_DATA_DIR": "{tmp}/data-dir"}, "{tmp}/data-dir/" + INVENTORY_DB_FILENAME),
        (
            {"FLIPPER_DATA_DIR": "{tmp}/data-dir", "FLIPPER_INVENTORY_DB": "{tmp}/explicit.db"},
            "{tmp}/explicit.db",
        ),
    ],
)
def test_lan_resolves_the_same_database_as_the_web_app(
    monkeypatch, tmp_path, password_hash, storage, expected
):
    storage = {name: value.format(tmp=tmp_path) for name, value in storage.items()}
    expected = Path(str(expected).format(tmp=tmp_path))
    for name, value in storage.items():
        monkeypatch.setenv(name, value)
    plan = web_lan.plan_lan(environ=_lan_env(password_hash, **storage), routed=lambda: None)
    assert plan.storage.inventory_database == expected
    assert plan.storage == web_app._storage()
    assert not (tmp_path / "data-dir").exists() and not (tmp_path / "explicit.db").exists()


def test_invalid_storage_configuration_fails_before_serving(password_hash):
    with pytest.raises(web_lan.LanStartupError, match="FLIPPER_DATA_DIR must be an absolute"):
        web_lan.plan_lan(environ=_lan_env(password_hash, FLIPPER_DATA_DIR="relative/dir"))


@pytest.mark.parametrize(
    ("bind", "routed", "hosted", "expected"),
    [
        ("0.0.0.0", "192.168.1.20", ["10.0.0.3"], ("192.168.1.20",)),
        ("0.0.0.0", "203.0.113.4", ["127.0.0.1", "10.0.0.3"], ("10.0.0.3",)),
        ("0.0.0.0", None, ["169.254.1.1", "172.20.0.4", "8.8.8.8"], ("172.20.0.4",)),
        (
            "0.0.0.0",
            None,
            [f"10.0.0.{n}" for n in range(1, 8)],
            ("10.0.0.1", "10.0.0.2", "10.0.0.3"),
        ),
        ("0.0.0.0", None, [], ()),
        ("192.168.1.50", "192.168.1.20", [], ("192.168.1.50",)),
    ],
)
def test_phone_address_selection(bind, routed, hosted, expected):
    assert web_lan.phone_addresses(bind, routed=lambda: routed, hosted=lambda: hosted) == expected


def test_route_lookup_is_offline_safe(monkeypatch):
    class NoRoute:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def connect(self, address):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(web_lan.socket, "socket", NoRoute)
    assert web_lan.route_address() is None


def test_startup_banner_is_friendly_and_contains_no_secrets(password_hash):
    plan = web_lan.plan_lan(8123, environ=_lan_env(password_hash), routed=lambda: "192.168.1.20")
    text = "\n".join(web_lan.startup_banner(plan))
    assert "Local computer: http://127.0.0.1:8123" in text
    assert "Phone:          http://192.168.1.20:8123" in text
    assert "same trusted private network" in text
    assert "not intended for public Wi-Fi, router port forwarding" in text
    assert "Private networks only" in text
    assert "end when Flipper stops" in text
    assert str(plan.storage.inventory_database) in text
    for secret in (password_hash, PASSWORD, *plan.environment.values()):
        if secret != "lan":
            assert secret not in text


def test_startup_banner_explains_how_to_find_the_address(password_hash):
    plan = web_lan.plan_lan(
        environ=_lan_env(password_hash, FLIPPER_SESSION_SECRET=SECRET),
        routed=lambda: None,
        hosted=lambda: [],
    )
    text = "\n".join(web_lan.startup_banner(plan))
    assert "ipconfig" in text and "http://<that address>:8000" in text
    assert "end when Flipper stops" not in text
    assert SECRET not in text


def test_port_conflict_is_reported_clearly():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(web_lan.LanStartupError, match=f"Port {port} is not available"):
            web_lan.check_port_available("0.0.0.0", port)
    web_lan.check_port_available("0.0.0.0", port)


@pytest.fixture
def serving_environment(monkeypatch, tmp_path, password_hash):
    """Isolate serve(): no .env loading, and every process setting it adds is undone."""
    monkeypatch.setattr(web_lan, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_lan, "route_address", lambda: "192.168.1.20")
    database = tmp_path / "synthetic.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    monkeypatch.setenv(web_security.PASSWORD_HASH_ENV, password_hash)
    for name in (web_security.MODE_ENV, web_security.SESSION_SECRET_ENV):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)  # registers removal, so settings serve() adds are undone
    return database


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_serve_binds_for_the_network_without_touching_the_database(
    serving_environment, password_hash
):
    database = serving_environment
    InventoryStore(database).add(
        title="Synthetic", source="test", acquired_at="2026-08-01", acquisition_cost="5.00"
    )
    before = (database_state(database).fingerprint, database.stat().st_mtime_ns)
    calls, output = [], []
    port = _free_port()

    def runner(application, **options):
        calls.append((application, options))
        config = web_security.current_config()
        assert config.lan and config.auth_required

    web_lan.serve(port, runner=runner, output=output.append)

    [(application, options)] = calls
    assert application is app
    assert options["host"] == "0.0.0.0" and options["port"] == port
    assert options["proxy_headers"] is False and options["server_header"] is False
    assert "workers" not in options and "reload" not in options
    text = "\n".join(output)
    assert f"http://192.168.1.20:{port}" in text
    generated_secret = os.environ[web_security.SESSION_SECRET_ENV]
    for secret in (generated_secret, password_hash, PASSWORD):
        assert secret not in text
    assert (database_state(database).fingerprint, database.stat().st_mtime_ns) == before


def test_serve_does_not_create_a_database_while_planning(serving_environment):
    web_lan.serve(_free_port(), runner=lambda *args, **kwargs: None, output=lambda line: None)
    assert not serving_environment.exists()


def test_cli_reports_startup_errors_without_serving(monkeypatch, capsys):
    monkeypatch.setattr(web_lan, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("server started"))
    assert main.main(["web", "lan"]) == 1
    captured = capsys.readouterr()
    assert "auth hash-password" in captured.err and captured.out == ""


# Home-screen metadata


def test_manifest_is_public_minimal_and_same_origin(lan):
    response = lan.client.get("/static/manifest.webmanifest")
    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")
    manifest = json.loads(response.text)
    assert manifest["name"] == manifest["short_name"] == "Flipper"
    assert manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert set(manifest) <= {
        "name",
        "short_name",
        "description",
        "id",
        "start_url",
        "scope",
        "display",
        "background_color",
        "theme_color",
        "icons",
    }
    for icon in manifest["icons"]:
        assert icon["src"] in web_security.PUBLIC_STATIC_ASSETS
        assert lan.client.get(icon["src"]).status_code == 200


def test_pages_link_the_manifest_and_icon_without_a_service_worker(lan):
    login = lan.client.get("/login").text
    assert _sign_in(lan.client).status_code == 303
    dashboard = lan.client.get("/").text
    for page in (login, dashboard):
        assert '<link rel="manifest" href="/static/manifest.webmanifest">' in page
        assert '<link rel="apple-touch-icon" href="/static/flipper-logo2.png">' in page
        assert 'name="viewport" content="width=device-width, initial-scale=1"' in page
    assert "manifest-src 'self'" in web_security.CONTENT_SECURITY_POLICY
    web_root = web_app.ROOT / "web"
    assert not list(web_root.rglob("*.js"))
    for path in web_root.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".css", ".webmanifest"}:
            assert "serviceWorker" not in path.read_text(encoding="utf-8"), path
