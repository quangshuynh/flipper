"""Read-only security smoke test for a running hosted Flipper.

Usage::

    python deploy/smoke.py https://flipper-app.onrender.com
    python deploy/smoke.py https://flipper.example.test --connect http://127.0.0.1:10000

The owner password is read from ``FLIPPER_SMOKE_PASSWORD`` or prompted for without echo. It is
never printed, and neither is the session cookie. The smoke signs in, reads pages, and signs out.
It submits no domain form, so it changes no inventory, sales, research, or eBay state. The only
side effect is one deliberately failed sign-in, which counts toward login throttling.

``--connect`` sends requests to another address while presenting the public origin's Host, for a
local container without TLS. The ``__Host-`` cookie is then carried by hand, as a browser would
over HTTPS.
"""

from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import httpx

COOKIE = "__Host-flipper_session"
PRIVATE_PAGES = (
    "/",
    "/deals",
    "/deals/history",
    "/inventory",
    "/sales",
    "/insights",
    "/analytics",
    "/analyze",
    "/settings",
)
FRAMEWORK_PAGES = ("/docs", "/redoc", "/openapi.json")
DELETION_PATH = "/api/ebay/account-deletion"


class SmokeFailure(AssertionError):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _security_headers(response: httpx.Response, *, html: bool) -> None:
    headers = response.headers
    _check(headers.get("x-frame-options") == "DENY", "X-Frame-Options must be DENY")
    _check(headers.get("x-content-type-options") == "nosniff", "nosniff is missing")
    _check(headers.get("referrer-policy") == "same-origin", "Referrer-Policy is wrong")
    _check("max-age=" in headers.get("strict-transport-security", ""), "HSTS is missing")
    if html:
        csp = headers.get("content-security-policy", "")
        _check("default-src 'none'" in csp, "CSP is missing default-src 'none'")
        _check("frame-ancestors 'none'" in csp, "CSP is missing frame-ancestors 'none'")


def _session_cookie(response: httpx.Response) -> str:
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        if COOKIE not in cookie:
            continue
        morsel = cookie[COOKIE]
        lowered = header.lower()
        _check("secure" in lowered, "session cookie must be Secure")
        _check("httponly" in lowered, "session cookie must be HttpOnly")
        _check(morsel["samesite"].lower() == "lax", "session cookie must be SameSite=Lax")
        _check(morsel["path"] == "/", "session cookie must have Path=/")
        _check(not morsel["domain"], "session cookie must not set Domain")
        return morsel.value
    raise SmokeFailure("sign-in did not set the session cookie")


def run_smoke(client: httpx.Client, origin: str, password: str, report=print) -> None:
    """Run every check through ``client``; raise SmokeFailure on the first failed expectation."""
    same_origin = {"Origin": origin}

    login = client.get("/login")
    _check(login.status_code == 200, f"GET /login returned {login.status_code}")
    _security_headers(login, html=True)
    _check(login.headers.get("cache-control") == "no-store", "login page must be no-store")
    report("ok  login page, security headers, no-store")

    css = client.get("/static/app.css")
    _check(css.status_code == 200, "login stylesheet is not public")
    report("ok  public login assets")

    for path in PRIVATE_PAGES:
        response = client.get(path)
        _check(response.status_code == 303, f"GET {path} without a session was not redirected")
        _check(response.headers["location"].startswith("/login"), f"{path} did not go to login")
    unauthenticated = client.post("/logout", headers=same_origin)
    _check(unauthenticated.status_code == 401, "mutation without a session was not rejected")
    report("ok  private pages require sign-in; unauthenticated mutation rejected")

    challenge = client.get(DELETION_PATH, params={"challenge_code": "flipper-smoke"})
    _check(
        challenge.status_code in {200, 503},
        f"eBay deletion challenge returned {challenge.status_code}; it must stay public",
    )
    report(f"ok  eBay deletion endpoint is public (status {challenge.status_code})")

    cross = client.post(
        "/login", data={"password": password}, headers={"Origin": "https://attacker.example"}
    )
    _check(cross.status_code == 403, "cross-origin sign-in was not rejected")
    missing = client.post("/login", data={"password": password})
    _check(missing.status_code == 403, "sign-in without Origin or Referer was not rejected")
    report("ok  cross-origin and origin-less sign-in rejected")

    wrong = client.post("/login", data={"password": "not the owner password"}, headers=same_origin)
    _check(wrong.status_code == 401, f"wrong password returned {wrong.status_code}")
    _check("Incorrect password." in wrong.text, "wrong-password message is not generic")
    _check(COOKIE not in wrong.headers.get("set-cookie", ""), "wrong password set a cookie")
    report("ok  wrong password rejected generically")

    signed_in = client.post("/login", data={"password": password}, headers=same_origin)
    _check(signed_in.status_code == 303, f"correct sign-in returned {signed_in.status_code}")
    cookie = {"Cookie": f"{COOKIE}={_session_cookie(signed_in)}"}
    client.cookies.clear()  # Send the session explicitly, even where a jar would withhold it.
    report("ok  sign-in; cookie is __Host-, Secure, HttpOnly, SameSite=Lax")

    for path in PRIVATE_PAGES:
        response = client.get(path, headers=cookie)
        _check(response.status_code == 200, f"GET {path} returned {response.status_code}")
        _security_headers(response, html=True)
        _check(response.headers.get("cache-control") == "no-store", f"{path} is not no-store")
    report(f"ok  {len(PRIVATE_PAGES)} private pages render with CSP and no-store")

    for path in FRAMEWORK_PAGES:
        response = client.get(path, headers=cookie)
        _check(response.status_code == 404, f"{path} is exposed")
    report("ok  framework docs and schema are not served")

    no_origin = client.post("/logout", headers=cookie)
    _check(no_origin.status_code == 403, "signed-in mutation without Origin was not rejected")
    foreign = client.post("/logout", headers={**cookie, "Origin": "https://attacker.example"})
    _check(foreign.status_code == 403, "signed-in cross-origin mutation was not rejected")
    report("ok  CSRF: signed-in mutations need the canonical Origin")

    logout = client.post("/logout", headers={**cookie, **same_origin})
    _check(logout.status_code == 303, f"sign-out returned {logout.status_code}")
    cleared = logout.headers.get("set-cookie", "")
    _check(COOKIE in cleared and "max-age=0" in cleared.lower(), "sign-out did not clear cookie")
    client.cookies.clear()
    after = client.get("/")
    _check(after.status_code == 303, "private page is reachable after sign-out")
    report("ok  sign-out clears the cookie; private pages require sign-in again")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("origin", help="public https origin, e.g. https://flipper-app.onrender.com")
    parser.add_argument("--connect", help="send requests here instead (local container testing)")
    args = parser.parse_args(argv)
    origin = args.origin.rstrip("/")
    host = urlsplit(origin).netloc
    password = os.environ.get("FLIPPER_SMOKE_PASSWORD") or getpass("Owner password: ")
    base_url = (args.connect or origin).rstrip("/")
    headers = {"Host": host} if args.connect else {}
    with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as client:
        try:
            run_smoke(client, origin, password)
        except SmokeFailure as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
    print("Security smoke passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
