"""Explicit private-network serving so a phone on the same trusted Wi-Fi can use Flipper.

``python main.py web lan`` is the only command that exposes the web app beyond this computer.
Ordinary development (``uvicorn web.app:app``) stays on loopback and the container stays in hosted
mode. LAN mode reuses the single-user security boundary in :mod:`web.security`: sign-in with the
configured owner-password hash is mandatory, clients must have private-network addresses, and
same-origin checks, cookies, throttling, and headers are unchanged. It serves plain HTTP and is
intended for a trusted home network only, never public Wi-Fi or router port forwarding.

Nothing here touches the network beyond binding the server socket. Finding the phone URL asks the
operating system which local address routes outward (a UDP ``connect`` sends no packets) and reads
this computer's own host-name addresses; it never scans or contacts other devices, and it never
changes firewall, router, or interface settings.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from storage_config import StorageConfig, StorageConfigurationError, resolve_storage
from web import security as web_security

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8000
MIN_PORT = 1024
MAX_PORT = 65535
ALL_INTERFACES = "0.0.0.0"
SHUTDOWN_GRACE_SECONDS = 5
MAX_FALLBACK_ADDRESSES = 3
# TEST-NET-1 (RFC 5737). Connecting a UDP socket only selects the outbound route; nothing is sent.
_ROUTE_PROBE_TARGET = ("192.0.2.1", 9)


class LanStartupError(RuntimeError):
    """LAN mode cannot start. Messages name settings and addresses, never secret values."""


@dataclass(frozen=True)
class LanPlan:
    bind: str
    port: int
    local_url: str
    phone_urls: tuple[str, ...]
    storage: StorageConfig
    ephemeral_sessions: bool
    environment: Mapping[str, str]


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"port must be a number from {MIN_PORT} to {MAX_PORT}")
    return port


def parse_bind(value: str) -> str:
    """Accept all IPv4 interfaces or one private, non-loopback IPv4 address of this computer."""
    if value == ALL_INTERFACES:
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        address = None
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or address.is_loopback
        or not web_security.is_private_address(str(address))
    ):
        raise ValueError(
            f"bind address must be {ALL_INTERFACES} or a private IPv4 address such as 192.168.1.20"
        )
    return str(address)


def _is_phone_address(value: str) -> bool:
    """A private IPv4 address a phone could type: not loopback and not link-local."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        isinstance(address, ipaddress.IPv4Address)
        and not address.is_loopback
        and not address.is_link_local
        and web_security.is_private_address(value)
    )


def route_address() -> str | None:
    """This computer's address on the default route, or None. Sends no network traffic."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE_TARGET)
            return probe.getsockname()[0]
    except OSError:
        return None


def host_addresses() -> list[str]:
    """IPv4 addresses the operating system associates with this computer's host name."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def phone_addresses(
    bind: str,
    *,
    routed: Callable[[], str | None] | None = None,
    hosted: Callable[[], Iterable[str]] | None = None,
) -> tuple[str, ...]:
    """Addresses to show for the phone URL, most likely first.

    A specific bind address is the only answer. Otherwise the private address on the default route
    is almost always the Wi-Fi or Ethernet address the phone shares. Only when that is unavailable
    are a few other private host-name addresses listed; virtual adapters may appear there, so the
    guide explains how to confirm the right one.
    """
    if bind != ALL_INTERFACES:
        return (bind,)
    primary = (routed or route_address)()
    if primary is not None and _is_phone_address(primary):
        return (primary,)
    candidates = [address for address in (hosted or host_addresses)() if _is_phone_address(address)]
    return tuple(candidates[:MAX_FALLBACK_ADDRESSES])


def lan_environment(environ: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """Return the security settings LAN mode adds to this process, and whether they are ephemeral.

    The owner-password hash must already be configured. Without ``FLIPPER_SESSION_SECRET`` a fresh
    random secret is generated for this process only: it is never printed or written anywhere, and
    stopping Flipper signs the phone out. Hosted configuration is refused rather than overridden.
    """
    mode = (environ.get(web_security.MODE_ENV, "").strip() or web_security.LOCAL_MODE).lower()
    if mode == web_security.HOSTED_MODE:
        raise LanStartupError(
            f"{web_security.MODE_ENV}=hosted is configured; LAN mode is for this computer's own "
            "private network. Remove the hosted settings for this process first."
        )
    if mode not in {web_security.LOCAL_MODE, web_security.LAN_MODE}:
        raise LanStartupError(
            f"{web_security.MODE_ENV} must be '{web_security.LOCAL_MODE}' or "
            f"'{web_security.LAN_MODE}' to start LAN mode"
        )
    if not environ.get(web_security.PASSWORD_HASH_ENV, "").strip():
        raise LanStartupError(
            f"LAN mode requires sign-in. Run `python main.py auth hash-password` and set "
            f"{web_security.PASSWORD_HASH_ENV} in your uncommitted .env or environment."
        )
    updates = {web_security.MODE_ENV: web_security.LAN_MODE}
    ephemeral = not environ.get(web_security.SESSION_SECRET_ENV, "").strip()
    if ephemeral:
        updates[web_security.SESSION_SECRET_ENV] = secrets.token_urlsafe(48)
    try:
        web_security.resolve_web_security({**environ, **updates})
    except web_security.WebSecurityConfigurationError as exc:
        raise LanStartupError(str(exc)) from exc
    return updates, ephemeral


def check_port_available(bind: str, port: int) -> None:
    """Fail early with a clear message when another program already holds the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: also detect narrower bindings.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name == "posix":  # Match the server, which ignores lingering TIME_WAIT sockets.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind, port))
        except OSError as exc:
            raise LanStartupError(
                f"Port {port} is not available on {bind}; another program (perhaps another "
                "Flipper) is using it. Stop it or choose another port with --port."
            ) from exc


def plan_lan(
    port: int = DEFAULT_PORT,
    bind: str = ALL_INTERFACES,
    *,
    environ: Mapping[str, str] | None = None,
    routed: Callable[[], str | None] | None = None,
    hosted: Callable[[], Iterable[str]] | None = None,
) -> LanPlan:
    """Resolve everything LAN mode needs without touching the database or the process state."""
    environ = os.environ if environ is None else environ
    updates, ephemeral = lan_environment(environ)
    try:
        # The same resolution as the web app, so LAN mode never selects a second database.
        storage = resolve_storage(environ, default_root=REPOSITORY_ROOT)
    except StorageConfigurationError as exc:
        raise LanStartupError(str(exc)) from exc
    local_host = "127.0.0.1" if bind == ALL_INTERFACES else bind
    return LanPlan(
        bind=bind,
        port=port,
        local_url=f"http://{local_host}:{port}",
        phone_urls=tuple(
            f"http://{address}:{port}"
            for address in phone_addresses(bind, routed=routed, hosted=hosted)
        ),
        storage=storage,
        ephemeral_sessions=ephemeral,
        environment=updates,
    )


def startup_banner(plan: LanPlan) -> list[str]:
    """Startup text. It contains addresses and paths only, never a hash, secret, or password."""
    lines = ["Flipper LAN mode", f"  Local computer: {plan.local_url}"]
    if plan.phone_urls:
        lines.append(f"  Phone:          {plan.phone_urls[0]}")
        lines.extend(f"                  {url} (other adapter)" for url in plan.phone_urls[1:])
    else:
        lines.append(
            "  Phone:          address not detected. Run `ipconfig` and use the IPv4 Address of "
            f"your Wi-Fi or Ethernet adapter: http://<that address>:{plan.port}"
        )
    lines.append(f"  Database:       {plan.storage.inventory_database}")
    lines.append("  Sign-in:        required (owner password)")
    if plan.ephemeral_sessions:
        lines.append(
            "  Sessions:       end when Flipper stops (no FLIPPER_SESSION_SECRET configured)"
        )
    lines += [
        "",
        "Keep both devices on the same trusted private network.",
        "LAN mode uses plain HTTP and is not intended for public Wi-Fi, router port forwarding,",
        "or any Internet exposure. Requests from public addresses are refused.",
        "If Windows Firewall asks, allow access on Private networks only.",
        "Press Ctrl+C to stop.",
    ]
    return lines


def serve(
    port: int = DEFAULT_PORT,
    bind: str = ALL_INTERFACES,
    *,
    runner: Callable[..., object] | None = None,
    output: Callable[[str], object] = print,
) -> None:
    """Validate, print the startup summary, and run one Uvicorn process until interrupted."""
    load_dotenv(REPOSITORY_ROOT / ".env")  # What the web app reads; never overrides the shell.
    plan = plan_lan(port, bind)
    check_port_available(bind, port)
    # In-process only: the generated secret, if any, is never written to .env or disk.
    os.environ.update(plan.environment)

    from web.app import app  # Imported after the LAN settings are in place.

    if runner is None:
        import uvicorn

        runner = uvicorn.run
    for line in startup_banner(plan):
        output(line)
    runner(
        app,
        host=bind,
        port=port,
        # Peer addresses decide LAN access and throttling, so forwarded headers are never trusted.
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
