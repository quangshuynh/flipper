# Web security

Flipper is a single-user personal application. The web app has one owner password and no user
accounts. It has no registration, password-reset email, roles, social login, or public pages. The
boundary is enforced by Flipper itself (`web/security.py`). It does not rely on a hosting
provider's access controls; those can be added later as defense in depth.

!!! note "Deployment"
    This page describes the boundary itself. The container, Render service, and cutover runbook are
    in [Hosted deployment](deployment.md). Phone access on a home network without hosting is in
    [Phone access](../guides/phone-access.md).

## Security modes

`FLIPPER_WEB_SECURITY_MODE` selects one of three explicit modes. Flipper never guesses the mode
from other settings.

| | `local` (default) | `lan` | `hosted` |
| --- | --- | --- | --- |
| Started by | `uvicorn web.app:app` | `python main.py web lan` | The production container |
| Sign-in | Only when a password hash and session secret are both configured | Always | Always |
| Without a password | Serves loopback clients (`127.0.0.1`, `::1`) only | Refuses to start | Refuses to start |
| Client addresses | Any, once a password is configured | Loopback and private networks only | Any (behind the host's proxy) |
| Trusted hosts | `localhost`, `127.0.0.1`, `::1`, plus `FLIPPER_ALLOWED_HOSTS` | Same, plus any private IP literal | Host of `FLIPPER_PUBLIC_ORIGIN`, plus `FLIPPER_ALLOWED_HOSTS` |
| Mutation origin | The request's own validated host | The request's own validated host | Exactly `FLIPPER_PUBLIC_ORIGIN` |
| Session cookie | `flipper_session`, not `Secure` | `flipper_session`, not `Secure` | `__Host-flipper_session`, `Secure` |
| HSTS | No | No | `max-age=31536000` |

In **local mode without a password**, `uvicorn web.app:app --reload` works as before. Every
request must come from this computer and name a loopback host. If Flipper is accidentally started
on a network interface, or behind a hosting proxy without `FLIPPER_WEB_SECURITY_MODE=hosted`, it
rejects the traffic instead of serving the books without authentication. Forgetting the mode
variable therefore fails closed.

**Local mode with a password** is for testing sign-in. Cookies are not `Secure`. Do not use this
mode for network or internet exposure.

**LAN mode** is for reaching Flipper from a phone on a trusted home network; see
[Phone access](../guides/phone-access.md). It requires the password hash and a session secret. The
`web lan` command generates a per-process secret in memory when none is configured. It rejects
`FLIPPER_PUBLIC_ORIGIN`. It serves only clients whose TCP address is loopback, RFC 1918 private,
link-local, or IPv6 unique-local, and refuses others with `403`. It also accepts any private IP
literal as the `Host`. A literal address cannot be DNS-rebound, and the phone URL survives a DHCP
change, while other host names still need `FLIPPER_ALLOWED_HOSTS`. It is plain HTTP and must not
be exposed to the Internet.

**Hosted mode** refuses to start unless all three of `FLIPPER_PASSWORD_HASH`,
`FLIPPER_SESSION_SECRET`, and `FLIPPER_PUBLIC_ORIGIN` are present and valid. The following also
stop startup, with an error that names the setting but never its value:

- setting `FLIPPER_PUBLIC_ORIGIN` without hosted mode;
- configuring only one of the password hash and the session secret;
- a malformed hash, a weak secret, a non-HTTPS origin, or an unknown mode.

If configuration becomes invalid in a running process, every request receives
`503 Flipper is not available.`

## Configuration

| Variable | Meaning |
| --- | --- |
| `FLIPPER_WEB_SECURITY_MODE` | `local` (default) or `hosted` |
| `FLIPPER_PASSWORD_HASH` | Encoded owner-password hash; never the password itself |
| `FLIPPER_SESSION_SECRET` | Random session-signing secret (at least 43 characters) |
| `FLIPPER_PUBLIC_ORIGIN` | Canonical `https://host[:port]` origin, with no path; hosted only |
| `FLIPPER_ALLOWED_HOSTS` | Optional extra comma-separated host names; no ports or wildcards |

These are process configuration only. They are not stored in SQLite, never included in backups,
and never rendered into templates, cookies, logs, or browser-visible errors. Put them in the
uncommitted `.env` locally or in the host's secret environment settings. `.env.example` contains
placeholders only.

### Generating the password hash

```bash
python main.py auth hash-password
```

The command prompts twice without echo and prints one line such as
`flipper-scrypt:v1:n=131072,r=8,p=1:<salt>:<key>`. There is no password argument, so the password
never enters shell history. The command refuses to run where the terminal would echo input. The
password must be 12 to 1024 characters with at least five different characters. This catches
accidents; it is not a composition policy. Use a long passphrase from a password manager.

Copy the printed value into `FLIPPER_PASSWORD_HASH`:

- **locally**, add `FLIPPER_PASSWORD_HASH=flipper-scrypt:v1:...` to `.env`;
- **on a host**, paste the value into its secret environment-variable setting.

The encoding contains no `$`, `#`, quotes, or spaces, so it needs no quoting in either place.

The hash uses the standard library's `hashlib.scrypt` with N=2^17, r=8, p=1 (128 MiB, OWASP's
scrypt baseline), a 16-byte random salt, and a 32-byte derived key. The encoding records the
algorithm, format version, parameters, salt, and key. Verification re-derives the key and compares
it in constant time. It accepts only a bounded parameter range, so configuration cannot request
unbounded memory. Input is Unicode-normalized (NFC), so the same passphrase verifies from any
keyboard. One verification takes a few hundred milliseconds, and verifications run one at a time.

### Generating the session secret

```bash
python main.py auth session-secret
```

This prints a new random 64-character value; copy it into `FLIPPER_SESSION_SECRET`. Flipper never
writes it anywhere. Use a different secret for each deployment.

## Sessions and cookies

A session cookie is `v1.<payload>.<HMAC-SHA256 signature>`. The payload holds only a random
session ID, the sign-in time, and the last-seen time. It never contains the password, its hash,
eBay credentials, or accounting data. The signature is verified in constant time before the payload
is parsed. Tampered, malformed, re-versioned, or future-dated cookies are treated as signed out.

- **Absolute lifetime: 30 days.** Sign in again at least monthly, even when active.
- **Idle lifetime: 7 days.** A week without use signs the browser out.
- The last-seen time is refreshed at most hourly, so browsing does not rewrite the cookie on every
  request. `Max-Age` always equals the earlier of the two expiries.

These values suit one owner using a personal phone. Monthly and weekly re-authentication is cheap,
and logout plus secret rotation provide revocation.

Cookie attributes are `HttpOnly`, `SameSite=Lax`, and `Path=/`. Hosted mode adds `Secure` and the
`__Host-` name prefix, which makes browsers refuse the cookie over HTTP, from a `Domain`
attribute, or on another path. `Lax` keeps ordinary links into Flipper signed in, while browsers
still withhold the cookie from cross-site form posts.

There is no server-side session table. The signing key is derived from the session secret **and**
the password hash:

- rotating `FLIPPER_SESSION_SECRET` signs out every browser;
- changing the owner password (a new hash) also signs out every browser.

## Sign-in and sign-out

Unauthenticated `GET` requests to a private page redirect to `/login`, preserving the requested
local path. The return path must be a same-origin path. Absolute URLs, `//host`, backslashes,
control characters, and encoded variants such as `/%2F%2Fhost` all fall back to `/`, so there is
no open redirect.

A wrong password returns the same generic `Incorrect password.` message every time. The page never
reveals whether configuration exists or how close a guess was.

**Sign out** is a `POST` form in the sidebar; `GET /logout` does nothing. Signing out requires a
valid session and a same-origin request. It deletes the cookie, sends `Clear-Site-Data: "cache"`,
and redirects to `/login`.

Because sessions are stateless, a cookie value copied off the device before sign-out stays valid
until it expires. To revoke every session immediately, rotate `FLIPPER_SESSION_SECRET`.

## Route policy

Access is deny-by-default. Only three kinds of route bypass sign-in, all from explicit allow-lists
in `web/security.py`:

| Route | Why it is public |
| --- | --- |
| `GET /api/ebay/account-deletion` | eBay endpoint-ownership challenge; answered with the SHA-256 challenge response |
| `POST /api/ebay/account-deletion` | eBay deletion notification; still requires a valid `X-EBAY-SIGNATURE` |
| `GET`/`POST /login` | Sign-in (the `POST` still requires same-origin) |
| `/static/app.css`, `/static/operational.css`, `/static/flipper-logo2.png` | Login page styling and logo |
| `/static/manifest.webmanifest` | Home-screen metadata (name, icon, colors); browsers fetch it without cookies |

Every other route is private, including routes added later: dashboard, Deals, Research History,
inventory and attachments, sales and accounting, Insights, Analytics, Analyze, Settings, eBay
Listings and Sales, and every mutation. Unauthenticated private `GET`s redirect to login.
Unauthenticated mutations get `401 Sign in required. No changes were made.` before any route or
domain code runs; they are never redirected. The tests keep a classification table of every
registered route and fail when a new route is not classified. FastAPI's generated `/docs`,
`/redoc`, and `/openapi.json` are not served by the private app.

The eBay account-deletion endpoint keeps its own verification. The trusted-host check and headers
still apply to it, but it needs no `Origin` and no session. The separate `ebay.compliance:app`
entry point is unchanged.

## CSRF and origin protection

Every request other than `GET`/`HEAD` passes a central check before routing, except the eBay
notification. This includes login and logout.

1. If `Sec-Fetch-Site` is present, it must be `same-origin`.
2. If `Origin` is present, it must equal the expected origin exactly: scheme, host, and port
   (default ports normalized). `null`, credentials, lookalike suffix/prefix hosts, subdomains,
   `http` versus `https`, and other ports are rejected.
3. Otherwise `Referer` must parse to the expected origin.
4. With neither header, the request is rejected.

In hosted mode, the expected origin is `FLIPPER_PUBLIC_ORIGIN`. It never comes from `Host`,
`X-Forwarded-Host`, `X-Forwarded-Proto`, or `Forwarded`. In local mode it is derived from the
request, whose `Host` has already been restricted to the allow-list; that also blocks DNS-rebinding
host names.

Synchronizer tokens were considered and not added. Modern browsers send `Origin` on every form
`POST`, a request without one is rejected rather than allowed, and the session cookie is
`SameSite=Lax`. Tokens would add protection only against attackers who can already run script on
Flipper's own origin, where tokens do not help.

## Trusted hosts

Every request, including the eBay endpoint, must carry a `Host` in the allow-list, or it receives
`400 Invalid host header.` Host names are matched case-insensitively without ports. Ports are
enforced by the origin check. Add extra names, such as a platform health-check host or a LAN name,
with `FLIPPER_ALLOWED_HOSTS`. No provider host name is hard-coded. LAN mode additionally accepts
private IP literals, never names.

## Security headers and caching

| Header | Value | Applied to |
| --- | --- | --- |
| `Content-Security-Policy` | `default-src 'none'; style-src 'self'; img-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` | HTML responses |
| `X-Content-Type-Options` | `nosniff` | All |
| `X-Frame-Options` | `DENY` | All |
| `Referrer-Policy` | `same-origin` | All |
| `Cross-Origin-Opener-Policy` | `same-origin` | All |
| `Permissions-Policy` | `camera=(), geolocation=(), microphone=()` | All |
| `Strict-Transport-Security` | `max-age=31536000` | All, hosted mode only |
| `Cache-Control` | `no-store` | Everything except the public login assets |

The CSP is strict because the templates contain no scripts, inline styles, or third-party
resources, and a test asserts that for representative pages. Asset URLs are rendered as paths, so
they stay same-origin behind an HTTPS proxy.

`no-store` stops private and accounting pages, attachments, redirects, and rejections from being
written to browser or intermediary caches. Browsers generally keep `no-store` pages out of the
back/forward cache and evict them when cookies change at sign-out. HTTP cannot control what a
browser still shows in an already-open tab or its app switcher. Sign out and close the tab on a
shared device.

## Login throttling

Failed sign-ins use bounded, in-process exponential backoff, suited to the one-process
architecture (no Redis or database):

- Five failures are free. After that, the wait doubles from 1 second up to a 5-minute cap.
- While throttled, attempts receive a generic `429` with `Retry-After`. They are refused before
  password verification and do not extend the wait.
- A successful sign-in clears the client's record, and records idle for an hour are forgotten.
- At most 1,024 client records are kept. Malformed requests create none.

The client key is the address the ASGI server reports. Flipper never parses `X-Forwarded-For`
itself. Behind a proxy that is not configured as trusted, all clients share the proxy's address,
so throttling becomes global. That is the conservative failure: an attacker could delay the owner
for up to five minutes after stopping, but can never lock the owner out permanently. Only one
scrypt verification runs at a time, which bounds CPU and memory under a flood of guesses. A long
random passphrase remains the primary defense.

## Reverse proxy expectations

The host terminates HTTPS at its proxy and forwards to one Uvicorn process. The production image
([Hosted deployment](deployment.md)) runs:

```bash
uvicorn web.app:app --host 0.0.0.0 --port "$PORT" \
  --no-proxy-headers --no-server-header --timeout-graceful-shutdown 20
```

- Flipper's security decisions do not depend on forwarded headers. Origin checks use
  `FLIPPER_PUBLIC_ORIGIN`, cookies are `Secure` by mode, and HSTS is sent by mode.
- Forwarded headers would only make the reported client address (used for throttling) accurate,
  and that is safe only with `--forwarded-allow-ips` set to the proxy's own addresses. Render
  publishes no fixed proxy range, and `*` would let any client spoof its address. The image
  therefore trusts no forwarded headers, and throttling is global.
- The proxy must pass through the original `Host`, and should redirect plain HTTP to HTTPS.
- Run one process (no multiple workers), because throttling state is per-process.

## Rotation and recovery

| Situation | Action |
| --- | --- |
| Suspected stolen cookie or lost device | Generate and set a new `FLIPPER_SESSION_SECRET`, then restart |
| Forgotten or compromised password | Run `auth hash-password`, set the new `FLIPPER_PASSWORD_HASH`, then restart (this also signs everyone out) |
| Restoring data to a new host | Restore the backup, then configure new security settings there |

There is no password-reset email. Recovery is done by whoever controls the host's configuration.
Losing the password loses no data, because the password protects access and does not encrypt
anything.

## Backups exclude security settings

Backups contain only the database and the attachments it references; see
[Backup and restore](backup-boundary.md). The password hash, session secret, public origin,
allowed hosts, eBay credentials, and `.env` are never part of a backup, and a test proves none of
them appear in any backup file. Authentication adds no tables or migrations, so the schema stays at
v14. Restoring a backup never restores or changes security settings.
