# Phone access on your home network

LAN mode lets you use Flipper from your phone while Flipper keeps running on your own computer
with its normal SQLite database and attachments. The phone is only a browser. Nothing is deployed,
nothing is copied to the phone, and no cloud service is involved.

```text
Phone browser ──(home Wi-Fi, plain HTTP)──▶ your computer: python main.py web lan
                                              → Flipper sign-in (web/security.py)
                                              → the same web app and database you use locally
```

!!! warning "Trusted private networks only"
    LAN mode serves plain HTTP. Use it only on a network you trust, such as your own home Wi-Fi.
    Do not use it on public or shared Wi-Fi. Never expose it with router port forwarding, UPnP,
    a VPS, or any other Internet exposure. For Internet access, see
    [Hosted deployment](../operations/deployment.md), which uses HTTPS and hosted mode.

## Quick start

### 1. One-time setup: an owner password

LAN mode always requires sign-in. It reuses Flipper's owner-password system; see
[Web security](../operations/web-security.md). Generate the hash:

```bash
python main.py auth hash-password
```

Type a long passphrase twice (it is not echoed). The command prints one line starting with
`flipper-scrypt:v1:`, which is a hash, not your password. Add it to the uncommitted `.env` file in
the repository (it is listed in `.gitignore`):

```text
FLIPPER_PASSWORD_HASH=flipper-scrypt:v1:...
```

Optional: to stay signed in across Flipper restarts, also add a session secret:

```bash
python main.py auth session-secret
```

```text
FLIPPER_SESSION_SECRET=<the printed value>
```

Without `FLIPPER_SESSION_SECRET`, LAN mode generates a random secret in memory each time it
starts. It is never printed or written anywhere, and stopping Flipper signs the phone out. That is
the default because it needs no stored secret.

!!! note
    With `FLIPPER_PASSWORD_HASH` in `.env`, the ordinary local server (`uvicorn web.app:app`) also
    asks for the password once per browser. It still accepts only this computer. To keep local
    development password-free, set the hash in the terminal session that starts LAN mode instead
    of in `.env`.

### 2. Start Flipper in LAN mode

```bash
python main.py web lan
```

Flipper checks its configuration and prints where to connect:

```text
Flipper LAN mode
  Local computer: http://127.0.0.1:8000
  Phone:          http://192.168.1.23:8000
  Database:       C:\Users\you\flipper\data\flipper_inventory.db
  Sign-in:        required (owner password)
  Sessions:       end when Flipper stops (no FLIPPER_SESSION_SECRET configured)

Keep both devices on the same trusted private network.
LAN mode uses plain HTTP and is not intended for public Wi-Fi, router port forwarding,
or any Internet exposure. Requests from public addresses are refused.
If Windows Firewall asks, allow access on Private networks only.
Press Ctrl+C to stop.
```

Check that **Database** is the database you normally use. It is resolved exactly as the web app
resolves it (see [Storage](#storage)).

The first time, Windows may ask whether Python may communicate on networks. Allow **Private
networks** only. Flipper never changes firewall, router, or network settings itself.

### 3. Connect the phone

1. Join the phone to the **same Wi-Fi network** as the computer. Do not use a guest network.
2. Open the **Phone** URL in Safari or Chrome, for example `http://192.168.1.23:8000`.
3. Sign in with your owner password.
4. Use **Sign out** in the menu when you are done on a shared device.

### 4. Stop the server

Press **Ctrl+C** in the terminal. The phone then cannot reach Flipper until you start it again.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--port PORT` | `8000` | TCP port, from 1024 to 65535 |
| `--bind ADDRESS` | `0.0.0.0` (all IPv4 interfaces) | Listen on one private IPv4 address of this computer only, such as your Wi-Fi address |

```bash
python main.py web lan --port 8123
python main.py web lan --bind 192.168.1.23
```

The ordinary development command is unchanged. `uvicorn web.app:app --reload` listens on
`127.0.0.1` only, and without a password it answers only this computer. The production container
is unchanged and always runs in hosted mode. Only `python main.py web lan` exposes Flipper to the
network.

## Finding the computer's address

Flipper shows the private address on the computer's default route, which is almost always the
Wi-Fi or Ethernet address your phone can reach. It discovers this without sending any network
traffic and never scans other devices. If it cannot determine the address, it says so. To find it
yourself:

1. Open **Command Prompt** or **PowerShell** and run `ipconfig`.
2. Find the adapter you actually use, such as **Wireless LAN adapter Wi-Fi** or
   **Ethernet adapter Ethernet**.
3. Use its **IPv4 Address**, usually `192.168.x.x`, `10.x.x.x`, or `172.16–31.x.x`.

Ignore virtual adapters such as `vEthernet (WSL)`, Hyper-V, VirtualBox, Docker, and VPN adapters.
A phone cannot reach those addresses.

## Add to the home screen

Flipper includes a small web app manifest (name, icon, theme color, standalone display), so it can
appear as a home-screen icon.

- **iPhone (Safari):** Share → **Add to Home Screen**.
- **Android (Chrome):** ⋮ menu → **Add to Home screen**.

Over plain HTTP, the result is a home-screen shortcut. Browsers reserve full app installation for
HTTPS sites, so behavior varies by browser. On iPhone, a home-screen app keeps its own cookies, so
you sign in once inside it.

There is deliberately **no service worker** and no offline mode. Every Flipper page is sent with
`Cache-Control: no-store`, so inventory and accounting pages are not stored on the phone for
offline use. The shortcut simply opens Flipper while the computer is running on the same network.

## How LAN mode protects Flipper

LAN mode is a third security mode, `lan`, alongside `local` and `hosted`. The command sets it for
its own process only. It reuses the existing boundary and adds two private-network rules.

| Protection | LAN mode behavior |
| --- | --- |
| Authentication | Always required. Refuses to start without a valid `FLIPPER_PASSWORD_HASH`. Every page except sign-in requires a signed session. |
| Client addresses | Only loopback, private (`10/8`, `172.16/12`, `192.168/16`), link-local, and IPv6 unique-local peers are served. Every other address receives `403`, including public addresses and `100.64.0.0/10`. |
| Trusted hosts | `localhost`, `127.0.0.1`, `::1`, any **private IP address** (so a DHCP change needs no restart), and names in `FLIPPER_ALLOWED_HOSTS`. Other host names get `400`, which blocks DNS rebinding. |
| CSRF | Unchanged: every non-`GET` request, including sign-in and sign-out, must carry a same-origin `Origin` or `Referer`, and `Sec-Fetch-Site` must be `same-origin` when present. |
| Cookies | `flipper_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and not `Secure`, because LAN HTTP is plaintext. 30-day absolute and 7-day idle lifetime. |
| Session secret | `FLIPPER_SESSION_SECRET` if configured; otherwise a random per-process secret held in memory only. |
| Login throttling | Unchanged exponential backoff, keyed by the phone's own address. Forwarded headers are never trusted in LAN mode. |
| Headers | Same strict CSP, `no-store`, `X-Frame-Options: DENY`, and related headers. There is no HSTS, because LAN mode is HTTP. |
| Server | One Uvicorn process, no reload, no `Server` header, no proxy headers. |
| Startup errors | Missing or invalid hash, weak secret, hosted configuration, a public origin, bad storage settings, or a busy port stop startup with a message that names the setting, never its value. |

What LAN mode cannot protect against is someone on the same network capturing plain-HTTP traffic,
which includes the password at sign-in and the session cookie. That is why it is for a trusted home
network with WPA2/WPA3 encryption. If a router port were forwarded by mistake, public clients would
still be refused with `403`, but do not rely on that. Never forward the port.

## Storage

LAN mode uses the same storage resolution as the web app, so it cannot create a surprise second
database:

1. `FLIPPER_INVENTORY_DB`, if set;
2. otherwise `FLIPPER_DATA_DIR/flipper_inventory.db`;
3. otherwise `data/flipper_inventory.db` in the repository.

Attachments follow `FLIPPER_ATTACHMENT_ROOT`, or sit beside the database. See
[Configuration](../reference/configuration.md#storage-locations). The command has no database flag,
and the startup summary prints the resolved path before serving. Planning startup never creates or
changes the database. As in normal web use, the server initializes the selected database once when
it starts.

## eBay

LAN mode does not change eBay behavior. Seller OAuth (`python main.py ebay connect`) is a CLI flow
that uses eBay's RuName redirect and a pasted URL, with no web callback, so the phone's address does
not matter. Credential storage, production/sandbox selection, and read-only Fulfillment/Finances
access are unchanged. eBay pages opened on the phone make the same read-only calls they make on the
computer. The account-deletion endpoint stays reachable in LAN mode only from the private network.
eBay's registered endpoint is still the separate compliance service described in
[Hosted deployment](../operations/deployment.md).

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Phone cannot connect | The terminal still shows Flipper running. Both devices are on the same Wi-Fi. The URL uses `http://`, not `https://`, and includes the port, such as `:8000`. |
| Windows Firewall prompt | Choose **Private networks** and Allow. If you dismissed it, open *Windows Security → Firewall & network protection → Allow an app through firewall* and allow Python for **Private** only. Also make sure your Wi-Fi network profile is **Private**, not Public. |
| Wrong IP | Run `ipconfig` and use the Wi-Fi/Ethernet adapter's IPv4 address, not a virtual adapter's. Or start with `--bind <that address>` so the printed URL is exactly it. |
| Devices on different networks | Phones on mobile data or on a different Wi-Fi band/SSID behind another router cannot reach the computer. Turn Wi-Fi on and join the same network. |
| Guest Wi-Fi / client isolation | Guest networks and "AP/client isolation" block device-to-device traffic. Use the main network. |
| `Port 8000 is not available` | Another program, possibly a running `uvicorn web.app:app`, uses the port. Stop it, or run `python main.py web lan --port 8123`. |
| `LAN mode requires sign-in` | Set `FLIPPER_PASSWORD_HASH` (step 1). |
| `Incorrect password.` | Retype carefully. After five failures, sign-in waits grow up to five minutes. Forgot it? Run `auth hash-password` again, replace the hash, and restart. |
| `Too many sign-in attempts` | Wait for the time shown and try again. Restarting Flipper also clears it. |
| `Invalid host header.` | You used a host name. Use the IP address, or add the name (for example `my-pc.local`) to `FLIPPER_ALLOWED_HOSTS`. |
| Signed out after a restart | Expected without `FLIPPER_SESSION_SECRET`. Set one to keep sessions. |
| Stops working after a while | The computer went to sleep. Keep it awake while you use Flipper (*Settings → System → Power*), or wake it. |
| URL changed | DHCP gave the computer a new address. Use the new **Phone** URL from the startup summary. A DHCP reservation in your router keeps the address stable. |
| `FLIPPER_WEB_SECURITY_MODE=hosted is configured` | Hosted settings in your environment conflict with LAN mode. Remove them for this terminal session. |

## Future: private access away from home (Tailscale)

Tailscale is **not** required and not used by LAN mode. This section records how the same
architecture could support it later.

- Tailscale devices talk over a private overlay network using `100.64.0.0/10` IPv4 addresses and
  `fd7a:115c:a1e0::/48` IPv6 addresses, with names such as `my-pc.tailnet.ts.net`.
- LAN mode deliberately refuses `100.64.0.0/10` today, both as a client address and as a host,
  because that range is also used by carrier-grade NAT. Allowing a tailnet should be an explicit
  future opt-in, not a default. LAN mode binds IPv4 only, so tailnet IPv6 traffic does not reach it.
- The cleanest future path is `tailscale serve`, which provides HTTPS on the tailnet name and
  proxies to Flipper on `127.0.0.1`. The client address would then be loopback, which LAN rules
  already allow. The browser's origin would be `https://my-pc.tailnet.ts.net`, so Flipper would
  need a small explicit addition: a canonical HTTPS origin for that name, similar to hosted mode's
  `FLIPPER_PUBLIC_ORIGIN`, plus `Secure` cookies. Nothing in LAN mode prevents adding this later.
- **Tailscale Funnel** publishes a service to the public Internet and must never be used with
  Flipper's LAN mode.

## Design notes: network audit before LAN mode

These findings were recorded before LAN mode was added:

- There was no `web` CLI command. `uvicorn web.app:app --reload` binds `127.0.0.1:8000`. Only the
  container binds `0.0.0.0`, and it forces hosted mode.
- Local mode without a password already failed closed on a network interface. Non-loopback peers
  got `403` and non-loopback hosts got `400`.
- Local mode with a password could already reach a LAN through `FLIPPER_ALLOWED_HOSTS`, but it
  accepted any client address, needed manual Uvicorn flags, and broke when DHCP changed the IP.
- Same-origin checks derive the expected origin from the validated `Host` outside hosted mode, so a
  LAN origin works without weakening them. Non-`Secure` cookies are required for plain-HTTP LAN
  access. HTTPS is not required on a trusted LAN, but the password and cookie travel unencrypted on
  that network.
- The strict CSP (`default-src 'none'`) would block a web app manifest, so `manifest-src 'self'` was
  added.
- Seller OAuth has no web callback, so the serving address cannot affect it.
