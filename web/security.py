"""Flipper's single-user web security boundary.

Every HTTP request to the combined web app passes through :func:`security_middleware` before
routing, in this order:

1. Resolve security configuration; invalid configuration fails closed (503).
2. Validate the ``Host`` header against the trusted-host allow-list (400).
3. Without authentication (local mode, no password configured), serve loopback clients only (403).
   LAN mode serves only clients on loopback or private-network addresses (403).
4. Classify the route. Only the explicit allow-lists below are reachable without a session:
   the eBay account-deletion endpoint, the login page, and the login page's static assets.
   Everything else, including routes added later, is private.
5. For every non-public request that is not GET/HEAD, require a same-origin ``Origin`` (or, when
   absent, ``Referer``) matching the canonical origin (403). This includes login and logout.
6. For private routes, require a valid signed session: GET/HEAD redirect to login, anything else
   is rejected with 401 before any route or domain code runs.
7. Add security headers; private and authentication responses are never stored by caches.

Configuration comes from the environment (and the repository ``.env`` for local development):

- ``FLIPPER_WEB_SECURITY_MODE``: ``local`` (default), ``lan`` (set by ``python main.py web lan``),
  or ``hosted``.
- ``FLIPPER_PASSWORD_HASH``: encoded owner-password hash (``python main.py auth hash-password``).
- ``FLIPPER_SESSION_SECRET``: session signing secret (``python main.py auth session-secret``).
- ``FLIPPER_PUBLIC_ORIGIN``: canonical ``https://`` origin; hosted mode only.
- ``FLIPPER_ALLOWED_HOSTS``: optional extra comma-separated host names.
"""

from __future__ import annotations

import ipaddress
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from ebay.compliance import ACCOUNT_DELETION_PATH
from web.passwords import PasswordHash, PasswordHashError, parse_password_hash
from web.sessions import Session, SessionCodec

MODE_ENV = "FLIPPER_WEB_SECURITY_MODE"
PASSWORD_HASH_ENV = "FLIPPER_PASSWORD_HASH"
SESSION_SECRET_ENV = "FLIPPER_SESSION_SECRET"
PUBLIC_ORIGIN_ENV = "FLIPPER_PUBLIC_ORIGIN"
ALLOWED_HOSTS_ENV = "FLIPPER_ALLOWED_HOSTS"
SECURITY_ENV_NAMES = (
    MODE_ENV,
    PASSWORD_HASH_ENV,
    SESSION_SECRET_ENV,
    PUBLIC_ORIGIN_ENV,
    ALLOWED_HOSTS_ENV,
)

LOCAL_MODE = "local"
LAN_MODE = "lan"
HOSTED_MODE = "hosted"
SECURITY_MODES = (LOCAL_MODE, LAN_MODE, HOSTED_MODE)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Networks LAN mode serves: loopback, RFC 1918 private IPv4, IPv4/IPv6 link-local, and IPv6 unique
# local addresses. Public addresses, carrier-grade NAT/overlay ranges (100.64.0.0/10), and
# everything else are refused, so a forwarded router port does not reach the books.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

MIN_SESSION_SECRET_LENGTH = 43  # 32 random bytes as unpadded base64url
MAX_SESSION_SECRET_LENGTH = 512
MIN_SESSION_SECRET_DISTINCT = 16

LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
LOCAL_SESSION_COOKIE = "flipper_session"
# The __Host- prefix makes browsers require Secure, Path=/, and no Domain attribute.
HOSTED_SESSION_COOKIE = "__Host-flipper_session"

SAFE_METHODS = frozenset({"GET", "HEAD"})

# Explicit public allow-lists. Anything not listed here requires an owner session.
PUBLIC_ROUTES = frozenset(
    {
        ("GET", ACCOUNT_DELETION_PATH),  # eBay endpoint-ownership challenge
        ("POST", ACCOUNT_DELETION_PATH),  # eBay signed deletion notification
    }
)
AUTHENTICATION_ROUTES = frozenset({("GET", LOGIN_PATH), ("POST", LOGIN_PATH)})
# The web app manifest is public because browsers fetch it without cookies. It names Flipper and
# its icon only; it holds no data, and no service worker is registered.
PUBLIC_STATIC_ASSETS = frozenset(
    {
        "/static/app.css",
        "/static/operational.css",
        "/static/flipper-logo2.png",
        "/static/manifest.webmanifest",
    }
)

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; manifest-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
HSTS_VALUE = "max-age=31536000"
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class WebSecurityConfigurationError(ValueError):
    """Security configuration is invalid. Messages name settings only, never their values."""


@dataclass(frozen=True)
class WebSecurityConfig:
    mode: str
    allowed_hosts: frozenset[str]
    public_origin: str | None = None
    password_hash: PasswordHash | None = field(default=None, repr=False)
    password_hash_value: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)

    @property
    def hosted(self) -> bool:
        return self.mode == HOSTED_MODE

    @property
    def lan(self) -> bool:
        return self.mode == LAN_MODE

    @property
    def auth_required(self) -> bool:
        return self.password_hash is not None

    @property
    def secure_cookies(self) -> bool:
        return self.hosted

    @property
    def session_cookie(self) -> str:
        return HOSTED_SESSION_COOKIE if self.hosted else LOCAL_SESSION_COOKIE

    def allows_host(self, host: str | None) -> bool:
        """Whether a normalized ``Host`` name is trusted.

        LAN mode also accepts any private-network IP literal, so the phone URL keeps working when
        DHCP gives this computer a new address. A literal address cannot be DNS-rebound, and names
        other than the loopback names still need ``FLIPPER_ALLOWED_HOSTS``.
        """
        if host is None:
            return False
        if host in self.allowed_hosts:
            return True
        return self.lan and is_private_address(host)

    def session_codec(self) -> SessionCodec:
        assert self.session_secret is not None and self.password_hash_value is not None
        return SessionCodec(self.session_secret, self.password_hash_value, clock=wall_clock)


def wall_clock() -> float:
    """Session clock; tests replace this module attribute instead of sleeping."""
    return time.time()


def _value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    return value or None


def is_private_address(value: str) -> bool:
    """Whether ``value`` is an IP literal inside :data:`PRIVATE_NETWORKS`."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in PRIVATE_NETWORKS)


def _normalize_host(host: str) -> str | None:
    """Return a lowercase host name (IPv6 without brackets), or None if it is not a bare host."""
    host = host.strip().lower().rstrip(".")
    if not host or any(char in host for char in "/\\@?#%*, \t"):
        return None
    try:
        parsed = urlsplit(f"//{host}")
        parsed.port  # noqa: B018 - validates any port
    except ValueError:
        return None
    return parsed.hostname or None


def normalize_origin(value: str | None) -> str | None:
    """Return ``scheme://host[:port]`` for an http(s) URL, or None when unusable.

    Default ports are dropped so ``https://a:443`` equals ``https://a``. Credentials,
    non-http(s) schemes, and malformed ports make the value unusable.
    """
    if not value or len(value) > 4096 or not value.isascii():
        return None
    if any(ord(char) < 0x21 or char == "\\" for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.username is not None or parsed.password:
        return None
    host = parsed.hostname
    if not host:
        return None
    if port is not None and port == {"http": 80, "https": 443}[scheme]:
        port = None
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{rendered_host}" + (f":{port}" if port is not None else "")


def _parse_public_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise WebSecurityConfigurationError(f"{PUBLIC_ORIGIN_ENV} is not a valid URL") from exc
    origin = normalize_origin(value)
    if (
        origin is None
        or parsed.scheme.lower() != "https"
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in value
    ):
        raise WebSecurityConfigurationError(
            f"{PUBLIC_ORIGIN_ENV} must be an https:// origin with no path, query, or credentials"
        )
    return origin


def _parse_allowed_hosts(value: str | None) -> frozenset[str]:
    hosts = set()
    for entry in (value or "").split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        literal = entry[1:-1] if entry.startswith("[") and entry.endswith("]") else entry
        try:
            hosts.add(str(ipaddress.ip_address(literal)))
            continue
        except ValueError:
            pass
        if not _HOSTNAME.fullmatch(entry):
            raise WebSecurityConfigurationError(
                f"{ALLOWED_HOSTS_ENV} must be comma-separated host names without ports or wildcards"
            )
        hosts.add(entry)
    return frozenset(hosts)


def _check_session_secret(secret: str) -> None:
    if (
        not MIN_SESSION_SECRET_LENGTH <= len(secret) <= MAX_SESSION_SECRET_LENGTH
        or not secret.isascii()
        or not secret.isprintable()
        or any(char.isspace() for char in secret)
        or len(set(secret)) < MIN_SESSION_SECRET_DISTINCT
    ):
        raise WebSecurityConfigurationError(
            f"{SESSION_SECRET_ENV} must be a random value of at least "
            f"{MIN_SESSION_SECRET_LENGTH} characters; generate one with "
            "`python main.py auth session-secret`"
        )


def resolve_web_security(environ: Mapping[str, str] | None = None) -> WebSecurityConfig:
    """Resolve and validate web security settings, failing closed on anything unsafe."""
    environ = os.environ if environ is None else environ
    mode = (_value(environ, MODE_ENV) or LOCAL_MODE).lower()
    if mode not in SECURITY_MODES:
        raise WebSecurityConfigurationError(
            f"{MODE_ENV} must be '{LOCAL_MODE}', '{LAN_MODE}', or '{HOSTED_MODE}'"
        )
    hash_value = _value(environ, PASSWORD_HASH_ENV)
    secret = _value(environ, SESSION_SECRET_ENV)
    origin_value = _value(environ, PUBLIC_ORIGIN_ENV)
    extra_hosts = _parse_allowed_hosts(_value(environ, ALLOWED_HOSTS_ENV))

    if mode == HOSTED_MODE:
        missing = [
            name
            for name, value in (
                (PASSWORD_HASH_ENV, hash_value),
                (SESSION_SECRET_ENV, secret),
                (PUBLIC_ORIGIN_ENV, origin_value),
            )
            if value is None
        ]
        if missing:
            raise WebSecurityConfigurationError(f"{MODE_ENV}=hosted requires {', '.join(missing)}")
    elif mode == LAN_MODE:
        # LAN exposure is never unauthenticated.
        if origin_value is not None:
            raise WebSecurityConfigurationError(
                f"{PUBLIC_ORIGIN_ENV} is only valid with {MODE_ENV}=hosted"
            )
        missing = [
            name
            for name, value in ((PASSWORD_HASH_ENV, hash_value), (SESSION_SECRET_ENV, secret))
            if value is None
        ]
        if missing:
            raise WebSecurityConfigurationError(f"{MODE_ENV}=lan requires {', '.join(missing)}")
    else:
        if origin_value is not None:
            raise WebSecurityConfigurationError(
                f"{PUBLIC_ORIGIN_ENV} is only valid with {MODE_ENV}=hosted"
            )
        if (hash_value is None) != (secret is None):
            raise WebSecurityConfigurationError(
                f"{PASSWORD_HASH_ENV} and {SESSION_SECRET_ENV} must be configured together"
            )

    password_hash = None
    if hash_value is not None:
        try:
            password_hash = parse_password_hash(hash_value)
        except PasswordHashError as exc:
            raise WebSecurityConfigurationError(f"{PASSWORD_HASH_ENV} is invalid: {exc}") from exc
    if secret is not None:
        _check_session_secret(secret)

    public_origin = _parse_public_origin(origin_value) if origin_value is not None else None
    if public_origin is not None:
        allowed = frozenset({urlsplit(public_origin).hostname}) | extra_hosts
    else:
        allowed = LOOPBACK_HOSTS | extra_hosts
    return WebSecurityConfig(
        mode=mode,
        allowed_hosts=allowed,
        public_origin=public_origin,
        password_hash=password_hash,
        password_hash_value=hash_value,
        session_secret=secret,
    )


_config_lock = threading.Lock()
_config_cache: tuple[tuple[str | None, ...], WebSecurityConfig] | None = None


def current_config() -> WebSecurityConfig:
    """Resolve from the process environment, reusing the result until a setting changes."""
    global _config_cache
    key = tuple(os.environ.get(name) for name in SECURITY_ENV_NAMES)
    cached = _config_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    config = resolve_web_security(os.environ)
    with _config_lock:
        _config_cache = (key, config)
    return config


# Route policy


class Access(Enum):
    PUBLIC = "public"
    AUTHENTICATION = "authentication"
    STATIC = "static"
    PRIVATE = "private"


def classify(method: str, path: str) -> Access:
    """Deny by default: only exact allow-listed method/path pairs are reachable without login."""
    method = method.upper()
    if (method, path) in PUBLIC_ROUTES:
        return Access.PUBLIC
    if (method, path) in AUTHENTICATION_ROUTES:
        return Access.AUTHENTICATION
    if method in SAFE_METHODS and path in PUBLIC_STATIC_ASSETS:
        return Access.STATIC
    return Access.PRIVATE


def request_host(request: Request) -> str | None:
    return _normalize_host(request.headers.get("host", ""))


def expected_origin(request: Request, config: WebSecurityConfig) -> str | None:
    """The one origin allowed to submit mutations.

    Hosted mode uses the configured canonical origin and never trusts Host, scheme, or forwarded
    headers. Local and LAN modes derive it from the request, whose Host has already been restricted
    to the loopback/explicit allow-list (plus private IP literals in LAN mode), which also defeats
    DNS-rebinding host names.
    """
    if config.public_origin is not None:
        return config.public_origin
    return normalize_origin(f"{request.url.scheme}://{request.headers.get('host', '')}")


def same_origin_request(request: Request, config: WebSecurityConfig) -> bool:
    """Require proof that a state-changing request came from Flipper's own pages."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() != "same-origin":
        return False
    expected = expected_origin(request, config)
    if expected is None:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        return normalize_origin(origin) == expected
    referer = request.headers.get("referer")
    if referer:
        return normalize_origin(referer) == expected
    return False


def _is_loopback_peer(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _is_private_peer(request: Request) -> bool:
    """The TCP peer is on a private network. LAN serving never trusts forwarded headers."""
    return request.client is not None and is_private_address(request.client.host)


def safe_next_path(value: str | None) -> str:
    """Return a same-origin local path to continue to after login, else ``/``."""
    if not value or len(value) > 2048 or not value.isascii():
        return "/"
    if any(ord(char) < 0x21 or ord(char) == 0x7F or char == "\\" for char in value):
        return "/"
    decoded = value
    for _ in range(3):  # Reject encoded tricks such as /%2F%2Fevil.example or %5C.
        decoded = unquote(decoded)
    if not value.startswith("/") or decoded.startswith("//") or "\\" in decoded:
        return "/"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or parsed.path in {LOGIN_PATH, LOGOUT_PATH}:
        return "/"
    return value


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    next_path = safe_next_path(target)
    location = LOGIN_PATH if next_path == "/" else f"{LOGIN_PATH}?{urlencode({'next': next_path})}"
    return RedirectResponse(location, status_code=303)


def set_session_cookie(
    response: Response, config: WebSecurityConfig, codec: SessionCodec, session: Session
) -> None:
    response.set_cookie(
        config.session_cookie,
        codec.encode(session),
        max_age=codec.max_age(session),
        path="/",
        secure=config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, config: WebSecurityConfig) -> None:
    response.delete_cookie(
        config.session_cookie,
        path="/",
        secure=config.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def apply_security_headers(response: Response, config: WebSecurityConfig, access: Access) -> None:
    headers = response.headers
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "same-origin"
    headers["X-Frame-Options"] = "DENY"
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    if headers.get("content-type", "").startswith("text/html"):
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if config.hosted:
        headers["Strict-Transport-Security"] = HSTS_VALUE
    if access is not Access.STATIC:
        headers["Cache-Control"] = "no-store"


def _reject(status_code: int, message: str) -> PlainTextResponse:
    return PlainTextResponse(message, status_code=status_code)


# Login throttling


class LoginThrottle:
    """Bounded in-process exponential backoff for failed login attempts.

    Keys are the server-resolved client address, which is the proxy's address unless the ASGI
    server is configured to trust that proxy's forwarding headers; Flipper never parses
    ``X-Forwarded-For`` itself. Throttled attempts are refused before password verification and do
    not extend the delay, the delay is capped, and idle entries are forgotten, so no request can
    lock the owner out permanently.
    """

    FREE_FAILURES = 5
    BASE_DELAY_SECONDS = 1.0
    MAX_DELAY_SECONDS = 300.0
    FORGET_AFTER_SECONDS = 3600.0
    MAX_ENTRIES = 1024

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _delay(self, failures: int) -> float:
        if failures < self.FREE_FAILURES:
            return 0.0
        exponent = min(failures - self.FREE_FAILURES, 16)
        return min(self.BASE_DELAY_SECONDS * 2**exponent, self.MAX_DELAY_SECONDS)

    def _purge(self, now: float) -> None:
        while self._entries:
            key, (_, last) = next(iter(self._entries.items()))
            if now - last < self.FORGET_AFTER_SECONDS:
                break
            del self._entries[key]

    def retry_after(self, key: str) -> float:
        """Seconds before ``key`` may attempt again; 0 when allowed."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            entry = self._entries.get(key)
            if entry is None:
                return 0.0
            failures, last = entry
            return max(0.0, last + self._delay(failures) - now)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            failures = self._entries.pop(key, (0, now))[0] + 1
            self._entries[key] = (failures, now)
            self._purge(now)
            while len(self._entries) > self.MAX_ENTRIES:
                self._entries.popitem(last=False)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)


login_throttle = LoginThrottle()


def throttle_key(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


# Middleware


async def security_middleware(request: Request, call_next):
    try:
        config = current_config()
    except WebSecurityConfigurationError:
        response = _reject(503, "Flipper is not available.")
        response.headers["Cache-Control"] = "no-store"
        return response
    request.state.auth_enabled = config.auth_required
    request.state.signed_in = False
    request.state.secure_cookies = config.secure_cookies

    host = request_host(request)
    method = request.method.upper()
    access = classify(method, request.url.path)
    if not config.allows_host(host):
        response = _reject(400, "Invalid host header.")
        apply_security_headers(response, config, access)
        return response
    if not config.auth_required and not _is_loopback_peer(request):
        response = _reject(403, "This Flipper instance only accepts requests from this computer.")
        apply_security_headers(response, config, access)
        return response
    if config.lan and not _is_private_peer(request):
        response = _reject(403, "This Flipper instance only accepts private-network requests.")
        apply_security_headers(response, config, access)
        return response

    if access is Access.PUBLIC:
        response = await call_next(request)
        apply_security_headers(response, config, access)
        return response

    if method not in SAFE_METHODS and not same_origin_request(request, config):
        response = _reject(403, "Cross-origin request rejected. No changes were made.")
        apply_security_headers(response, config, access)
        return response

    session = None
    codec = None
    if config.auth_required:
        codec = config.session_codec()
        session = codec.decode(request.cookies.get(config.session_cookie))
        request.state.signed_in = session is not None
        if session is None and access is Access.PRIVATE:
            if method in SAFE_METHODS:
                response = login_redirect(request)
            else:
                response = _reject(401, "Sign in required. No changes were made.")
            apply_security_headers(response, config, access)
            return response

    response = await call_next(request)
    if (
        session is not None
        and codec is not None
        and access is Access.PRIVATE
        and request.url.path != LOGOUT_PATH
        and codec.needs_refresh(session)
    ):
        set_session_cookie(response, config, codec, codec.touch(session))
    apply_security_headers(response, config, access)
    return response
