# Hosted deployment

This is the runbook for the one private, hosted Flipper instance that you reach from a phone over
HTTPS. It covers the container, the Render service, the persistent disk, secrets, verification,
the one-time real-data cutover, eBay reconnection, backups, and rollback.

```text
iPhone / browser
  → HTTPS (Render load balancer terminates TLS, redirects HTTP to HTTPS)
  → Flipper single-user sign-in (web/security.py)
  → FastAPI/Jinja app, one Uvicorn process, one container instance
  → /var/data (Render persistent disk)
       flipper/flipper_inventory.db     the one authoritative SQLite database
       flipper/attachments/             attachment files
       flipper/credentials/ebay-seller.json   file-backed eBay seller token
       backups/                         Flipper backups (same disk: not disaster recovery)
```

There is exactly one writable authoritative database. There is no Postgres, no replication, and no
synchronization with a local copy.

## Services

| Service | What it runs | Status |
| --- | --- | --- |
| Existing eBay compliance service (`flipper-zyui.onrender.com`) | Standalone `ebay.compliance:app`, registered with eBay as the account-deletion endpoint | Unchanged. Not part of the Blueprint. |
| `flipper-app` (this runbook) | The full private Flipper web app from the `Dockerfile` | New |

The two coexist. The compliance service keeps answering eBay's challenge and signed notifications
at the URL eBay already knows, so moving it would require changing the eBay developer account and
re-verifying the endpoint for no benefit. The new service also contains the public
`/api/ebay/account-deletion` route. Without `EBAY_ACCOUNT_DELETION_*` settings it answers
`503`, which is harmless because eBay does not call it. Consolidating onto `flipper-app` later
requires an explicit, approved eBay developer-console change and is not planned.

## Why Render

Checked against Render's documentation on 2026-09-27:

- Paid web services can attach a [persistent disk](https://render.com/docs/disks). Only the mount
  path persists; the rest of the filesystem is ephemeral.
- A service with a disk cannot scale beyond one instance, and deploys stop the old instance before
  starting the new one (no overlap, so there is never a second writer; a few seconds of downtime per
  deploy).
- Docker runtime, custom start command, environment secrets, HTTPS with automatic HTTP→HTTPS
  redirect, and outbound HTTPS all work.
- [SSH](https://render.com/docs/ssh) (paid services) and SFTP-based `scp -s` transfer files to the
  disk.
- Disks get automatic daily snapshots, kept for at least seven days.
- The free plan cannot attach a disk and sleeps when idle, so it is not suitable.

### Cost

| Item | Price |
| --- | --- |
| Starter web service (always on, one instance) | about USD 7/month |
| 1 GB persistent disk | USD 0.25/GB/month |
| Daily disk snapshots | included |

Expected baseline: about **USD 7.25/month**, plus the existing compliance service's plan. There is
no autoscaling and no replica. Confirm the current figures on Render's pricing page when creating
the service, and grow the disk only when needed (disks can grow but not shrink).

## Container

`Dockerfile` builds a `python:3.13-slim-bookworm` image (the version CI tests) with the pinned
`requirements.txt`. The build context is an allow-list (`.dockerignore`): application packages,
`main.py`, `requirements.txt`, the two deploy scripts, and the sample `data/listings.json`.
`.env`, databases, attachments, credentials, backups, `context.md`, `.claude/`, tests, docs, and
`.git` can never enter the image. CI checks the built image for them.

Image defaults fail closed:

| Setting | Default in the image |
| --- | --- |
| `FLIPPER_WEB_SECURITY_MODE` | `hosted`: no start without password hash, session secret, and https origin |
| `FLIPPER_DATA_DIR` | `/var/data/flipper` |
| `FLIPPER_CREDENTIAL_BACKEND` | `file` |
| `PORT` | `10000` (Render's default) |

**Entrypoint** (`deploy/entrypoint.sh`) runs before the app:

1. Refuses to start (exit 65) unless `FLIPPER_DATA_DIR`, or its parent, is a mounted volume other
   than `/`. The database can never silently land on ephemeral storage.
2. As root, creates the data directory and gives the whole disk to the unprivileged `flipper`
   user (uid 10001), because Render mounts disks owned by root.
3. Sets `umask 077`, so the database, attachments, credential file, and backups are owner-only.
4. Drops to `flipper` with `setpriv` and `exec`s the start command, which becomes PID 1 and
   receives Render's `SIGTERM` directly.

**Start command:**

```bash
uvicorn web.app:app --host 0.0.0.0 --port "$PORT" \
  --no-proxy-headers --no-server-header --timeout-graceful-shutdown 20
```

- **One process, one worker.** SQLite has one writer, login throttling is per-process, and a
  single-user app needs no throughput scaling.
- Startup (FastAPI lifespan) validates security settings and storage, then runs migrations once.
  Any failure exits the process, so Render marks the deploy failed and keeps nothing half-started.
  Requests never initialize the schema.
- **Forwarded headers are not trusted.** Render documents no fixed proxy address range, so there
  is no safe `--forwarded-allow-ips` value. Flipper's security never needs them: the CSRF origin
  comes from `FLIPPER_PUBLIC_ORIGIN`, cookies are `Secure` by mode, and redirects are relative.
  The only effect is that login throttling sees the proxy's address, which makes it global; see
  [Web security](web-security.md#login-throttling).
- Render sends `SIGTERM` and waits up to its default 30-second shutdown delay. Uvicorn finishes
  in-flight requests for up to 20 seconds and exits. Every write is one SQLite transaction, so an
  interrupted request leaves no partial write.

**CLI inside the container.** `flipper <command>` runs `python main.py <command>` as the `flipper`
user with the same environment, so files it creates stay owned by the app user:

```bash
flipper backup fingerprint
flipper backup create --output-dir /var/data/backups
```

## Render service

`render.yaml` is a Blueprint for `flipper-app` only: Docker runtime, `starter` plan, one instance,
a 1 GB disk named `flipper-data` mounted at `/var/data`, health check `GET /login`, and automatic
deploys of `main` only after CI passes (`autoDeployTrigger: checksPass`). Pull requests never
deploy.

### Environment

| Variable | Value | Kind |
| --- | --- | --- |
| `FLIPPER_WEB_SECURITY_MODE` | `hosted` | Blueprint |
| `FLIPPER_DATA_DIR` | `/var/data/flipper` | Blueprint |
| `FLIPPER_CREDENTIAL_BACKEND` | `file` | Blueprint |
| `FLIPPER_PASSWORD_HASH` | output of `python main.py auth hash-password` | Secret, entered in Render |
| `FLIPPER_SESSION_SECRET` | output of `python main.py auth session-secret` | Secret, entered in Render |
| `FLIPPER_PUBLIC_ORIGIN` | `https://<service>.onrender.com` (or your custom domain) | Entered in Render |
| `FLIPPER_ALLOWED_HOSTS` | Only if you add a second host name | Optional |
| `EBAY_SELLER_ENV` | `production` | Blueprint |
| `EBAY_SELLER_CLIENT_ID`, `EBAY_SELLER_CLIENT_SECRET`, `EBAY_SELLER_RUNAME` | Same values as local `.env` | Secret, entered in Render |

Discovery (`EBAY_DISCOVERY_*`) and other optional integrations may be added the same way. Nothing
secret is committed: `sync: false` makes Render prompt for each value when the Blueprint is first
applied, and the values live only in Render's environment settings.

Generate the two web secrets on your own computer. Neither command takes the secret as an argument,
so nothing enters shell history:

```bash
python main.py auth hash-password
python main.py auth session-secret
```

Paste each printed value straight into Render's field. Do not save it in a file, chat, or ticket.
Keep the password itself only in your password manager.

### Public origin and trusted host

`FLIPPER_PUBLIC_ORIGIN` must be the exact https origin you type on the phone. Its host becomes the
only trusted `Host`, and it is the only `Origin` accepted for sign-in and every form. Render's
health checks use the `onrender.com` host (or the verified custom domain), so they pass the
trusted-host check.

The `onrender.com` name is known only once the service exists. If it is not free, Render appends a
suffix. So create the service first, then set `FLIPPER_PUBLIC_ORIGIN` to the URL Render shows.
With a custom domain, set the origin to that domain and add the `onrender.com` host to
`FLIPPER_ALLOWED_HOSTS` only if you still want to use it.

### Health check

`GET /login` is already public, reads no data, and reveals nothing beyond a sign-in form. The app
starts listening only after security validation and migrations succeed, so a passing check means
the process is up and its storage is ready. No additional route was added, so the deny-by-default
route classification is unchanged.

## First deployment (synthetic data)

1. Merge the deployment PR so `main` contains the `Dockerfile` and `render.yaml`.
2. In Render: **New → Blueprint**, select this repository, and review that only `flipper-app` will
   be created. Enter the secrets. For `FLIPPER_PUBLIC_ORIGIN`, if you do not know the final URL
   yet, enter `https://flipper-app.onrender.com` and correct it after creation (step 4).
3. Wait for the deploy. Starting hosted mode with an empty disk creates a new, empty schema-v14
   database at `/var/data/flipper/flipper_inventory.db`.
4. If the actual URL differs, update `FLIPPER_PUBLIC_ORIGIN` (Render redeploys).
5. From your computer, run the read-only security smoke:

    ```bash
    python deploy/smoke.py https://<service>.onrender.com
    ```

    It prompts for the owner password without echo. It checks security headers, HSTS, CSP,
    anti-framing, `no-store`, redirects to sign-in, generic wrong-password handling, the
    `__Host-` Secure/HttpOnly/SameSite cookie, CSRF origin checks, the public eBay route, hidden
    framework docs, and sign-out. It changes no data. One deliberate wrong password counts toward
    throttling.

6. Also confirm `http://<service>.onrender.com` redirects to `https://`, and that an unknown host
   does not reach Flipper.
7. Create synthetic records to prove persistence. In the Render Shell (or SSH):

    ```bash
    flipper inventory add --title "SYNTHETIC persistence probe" --source "synthetic" \
      --acquired-at 2026-01-01 --cost 1.00
    flipper backup fingerprint
    ```

8. **Manual Deploy → Restart service**, then **Manual Deploy → Deploy latest commit**. After each,
   `flipper backup fingerprint` must print the same fingerprint, and the item must appear in the
   web UI.
9. Rehearse backup and restore on the disk (never onto the active directory):

    ```bash
    flipper backup create --output-dir /var/data/backups --name flipper-backup-rehearsal
    flipper backup verify /var/data/backups/flipper-backup-rehearsal
    flipper backup restore /var/data/backups/flipper-backup-rehearsal \
      --destination /var/data/restore-rehearsal
    flipper backup verify-restore /var/data/backups/flipper-backup-rehearsal \
      /var/data/restore-rehearsal
    ```

    Confirm the output reports credentials excluded, and remove the rehearsal directories after
    reviewing them: `rm -r /var/data/restore-rehearsal /var/data/backups/flipper-backup-rehearsal`.

10. Check the phone layout at about 390×844 (see [Phone access](#phone-access)).

Do not continue to real data until all of these pass.

## Real-data cutover

The cutover moves the one authoritative database from your computer to the disk. It needs your
explicit approval at the time, because it uploads real inventory and accounting data.

### Freeze the local database

From now until cutover completes, make **no** local Flipper changes: no inventory edits, sales,
costs, eBay imports or reconciliation, research snapshots, or notes. Stop any local `uvicorn`.
Nothing synchronizes later changes, so any local write after the backup would be lost.

### Record and back up the source

On your computer, from the repository with the normal local settings:

```bash
python main.py backup fingerprint
python main.py backup create --output-dir <a directory outside data/>
python main.py backup verify <that directory>/flipper-backup-YYYYMMDD-HHMMSS
```

Record the database size and SHA-256, schema version, fingerprint, Q/S/C counters, and attachment
count and bytes from the output and manifest. These are metadata only and contain no records. Stop
if verification is not `valid`.

### Transfer

Copy the verified backup directory to the disk over Render SSH (SFTP), never through Git, an issue,
a PR, a chat, or a public file host:

```bash
scp -s -r <backup directory> <service-ssh-address>:/var/data/incoming/
```

The SSH address is on the service's **Connect → SSH** menu; add your SSH public key in Render
first. If SSH is unavailable, Render documents `magic-wormhole` as an alternative. It is
end-to-end encrypted, but it must be installed in the shell session first.

### Restore beside the running synthetic instance, then switch

The app is running on the synthetic `/var/data/flipper`. Restore never touches it; it creates a
new directory:

```bash
flipper backup verify /var/data/incoming/flipper-backup-YYYYMMDD-HHMMSS
flipper backup restore /var/data/incoming/flipper-backup-YYYYMMDD-HHMMSS \
  --destination /var/data/flipper-live
flipper backup verify-restore /var/data/incoming/flipper-backup-YYYYMMDD-HHMMSS \
  /var/data/flipper-live
flipper backup fingerprint /var/data/flipper-live/flipper_inventory.db
```

The fingerprint must equal the source fingerprint recorded on your computer, and
`verify-restore` must report equivalence (integrity, foreign keys, schema v14, migration history,
row counts, Q/S/C counters, fingerprint, attachment hashes). If anything differs, **stop**. Do not
edit records to make them match.

Then set `FLIPPER_DATA_DIR=/var/data/flipper-live` in Render's environment. Render stops the old
instance before it starts the new one, so the database is never replaced underneath a running
server. The entrypoint gives the new directory to the app user, and startup finds schema v14
already current, so no data changes.

After the restart, run the same `verify-restore` and `fingerprint` commands again. The fingerprint
must be unchanged.

### Read-only verification

Signed in over HTTPS, before changing anything, open Dashboard, Deals, Research History, Inventory
and one item, Sales and one sale, Insights, and Settings. Confirm the expected records exist, that
unknown and zero still display differently, that provisional versus realized economics are labeled
as before, and that attachments, saved snapshots, frozen Deal Scores, and eBay item and order
identifiers are intact.

A controlled test mutation is optional. Only edit a note on a record that is itself a harmless
test record, restart, and confirm it persisted. Never touch money, sales, acquisition cost,
reconciliation, or deletions for this. If no such record exists, skip it.

### Authority transition

Once equivalence, read-only verification, and persistence pass, the hosted database is the one
authoritative writable Flipper.

!!! warning "Do not continue using the old local database for writes after cutover."
    The local database becomes a retained, frozen recovery copy. Do not delete it, and do not
    write to it. Local development and tests use synthetic databases, for example
    `FLIPPER_INVENTORY_DB=/tmp/flipper-dev.db`.

After the transition, delete the synthetic data directory and the uploaded copy on the disk:
`rm -r /var/data/flipper /var/data/incoming`.

### Create the first hosted backup

```bash
flipper backup create --output-dir /var/data/backups
flipper backup verify /var/data/backups/flipper-backup-YYYYMMDD-HHMMSS
```

Then copy it **off the host** (`scp -s -r <address>:/var/data/backups/<name> <local dir>`) and
verify the copy locally with `python main.py backup verify`.

## eBay seller connection

Credentials are never in backups, so the hosted instance must authorize eBay itself. Do this only
after the deployment is stable.

1. Confirm `EBAY_SELLER_*` are set and `FLIPPER_CREDENTIAL_BACKEND=file`. The token will be written
   to `$FLIPPER_DATA_DIR/credentials/ebay-seller.json` (owner-only `0600`) on the disk.
2. In an SSH session (it needs a terminal for the hidden paste prompt):

    ```bash
    flipper ebay connect
    ```

    Open the printed authorization URL on your computer, approve, and paste the full URL eBay
    redirects to. The redirect goes to the RuName's configured accept page, not to Flipper, so the
    hosted origin needs no eBay developer-console change. Scopes are unchanged.

3. Check read-only only: the **Settings** page should show Connected, then open the
   **eBay Listings** and **eBay Orders** pages (or run `flipper ebay orders`). Do not sync,
   import, or reconcile as part of deployment verification.

Do not copy the local keyring token to the server. Reauthorizing is the supported path.

## Restart and redeploy behavior

| Action | Effect on data |
| --- | --- |
| Restart service | None. Same disk, same database. |
| Deploy a new commit | Old instance stops, new image starts on the same disk. Migrations run only if a newer schema ships. |
| Change an environment variable | Redeploy as above |
| Failed deploy (startup error, bad secret) | New instance never serves; the disk is unchanged |

A few seconds of downtime per deploy is expected with a disk.

## Backups and disaster recovery

Render's daily disk snapshots and backups in `/var/data/backups` share the same provider and disk.
They are **not** disaster recovery on their own. After cutover and after meaningful changes, create
a Flipper backup, copy it off the host, and verify the copy. Automatic scheduled off-host backup is
future work.

Do not restore a Render disk snapshot as a routine recovery tool. It rolls back the whole disk,
including the credential file, and loses every later write. Prefer `backup restore` into a new
directory plus a `FLIPPER_DATA_DIR` switch, as in the cutover.

## Rollback

| When | What to do |
| --- | --- |
| Before the authority transition | Discard or repair the hosted service. The frozen local database stays authoritative; resume using it only if nothing was written to the hosted copy you care about. |
| Right after cutover, before any hosted write | Suspend `flipper-app`. Verify the local database still has the recorded source fingerprint, then resume using it locally. |
| After the hosted instance has accepted new writes | **Do not** switch back to the old local copy; that silently loses those writes. Recover from the hosted data or the latest verified hosted backup (restore into a new directory, then switch `FLIPPER_DATA_DIR`). |

A bad application release is rolled back by redeploying the previous commit from Render. Data on
the disk is unaffected, unless the release ran a new migration. Migrations are forward-only, and
the app refuses a database newer than its code, so restore a pre-release backup in that case.

## Logs and privacy

Render keeps Uvicorn's access log: method, path, query string, and status. Sign-in, sign-out, and
every mutation are `POST` forms, so passwords, accounting values, and notes never appear in logs.
The server header is suppressed. Flipper never logs secrets, cookies, CSRF data, or tokens.

Deal detail and comparison pages carry temporary research assumptions (tax, resale, fees, travel)
in the query string, so those numbers appear in the access log. They are research inputs, not
accounting records or credentials. Keep the Render log view private to your account.

## Phone access

Open the `FLIPPER_PUBLIC_ORIGIN` URL in Safari and sign in. "Add to Home Screen" works as a
bookmark. Offline use and app-style installation are not part of this deployment.

Checklist for a real iPhone:

1. Open the HTTPS URL; the lock icon shows a valid certificate.
2. Sign in; tapping the password field does not zoom the page.
3. Visit Dashboard, Deals, Research History, Inventory, eBay, Sales, Insights, and Settings from
   the navigation.
4. Open one inventory item and one sale.
5. Scroll a dense table sideways; the page itself does not scroll sideways.
6. Sign out, then open a private page URL; it asks you to sign in again.
