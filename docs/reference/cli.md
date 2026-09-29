# CLI reference

Run `python main.py --help` for the current command tree and `python main.py COMMAND --help` for exact
arguments. The main areas are:

| Area | Purpose |
| --- | --- |
| default pipeline / `acquire` | Analyze exported listings; explicitly record an acquisition |
| `inventory` | Inspect and maintain local items, lifecycle, valuations, and attachments |
| `sales` | Inspect sales, costs, and reconciliation confirmations |
| `reports` | Summary, inventory, and sales reports |
| `backup` | Create, verify, and restore database + attachment backups; logical fingerprints |
| `auth` | Generate the web owner-password hash and session secret (prints only) |
| `web lan` | Serve the web app to a phone on this trusted private network (sign-in required) |
| `ebay` | Seller OAuth, read-only listing/order/Finances retrieval, reconciliation, and explicit local imports |

Commands use the local SQLite database selected by their documented option/environment default.
Always inspect `--help` before a mutation. The web application supports active-listing sync and
reviewed sale import; Finances import and advanced sale accounting remain CLI workflows.

## Backup commands

```bash
python main.py backup create --output-dir DIR [--name NAME]
python main.py backup verify BACKUP
python main.py backup restore BACKUP --destination NEW_DIR
python main.py backup verify-restore BACKUP RESTORED_DIR
python main.py backup fingerprint [DATABASE]
```

`create` and `fingerprint` read the configured database read-only and never migrate it. `restore`
only creates a new directory and never changes configuration. `verify` exits 0 when valid, 1 when
invalid, 3 for an unsupported backup format, and 4 for a newer database schema. See
[Backup & restore](../operations/backup-boundary.md).

## Auth commands

```bash
python main.py auth hash-password
python main.py auth session-secret
```

`hash-password` prompts twice without echo and prints only the encoded `FLIPPER_PASSWORD_HASH`
value; there is deliberately no password argument. `session-secret` prints a new random
`FLIPPER_SESSION_SECRET`. Neither writes files or configuration. See
[Web security](../operations/web-security.md).

## Web commands

```bash
python main.py web lan [--port PORT] [--bind ADDRESS]
```

Serves the web app over plain HTTP to devices on this computer's private network in the `lan`
security mode. It requires `FLIPPER_PASSWORD_HASH`. It generates an in-memory session secret when
`FLIPPER_SESSION_SECRET` is unset and refuses hosted configuration. It uses the web app's own
storage resolution, so there is no `--database` flag. The defaults are `0.0.0.0` and port `8000`;
`--bind` accepts one private IPv4 address. It prints the local and phone URLs and the database path,
never secrets, and exits with status 1 on invalid configuration or a busy port. See
[Phone access](../guides/phone-access.md).
