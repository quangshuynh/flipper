# Configuration

## eBay public discovery

The Deals workspace uses application OAuth, independently of seller authorization:

| Variable | Meaning |
| --- | --- |
| `EBAY_DISCOVERY_ENV` | `production` or `sandbox` |
| `EBAY_DISCOVERY_CLIENT_ID` | Environment-specific application client ID |
| `EBAY_DISCOVERY_CLIENT_SECRET` | Environment-specific application secret |
| `EBAY_DISCOVERY_MARKETPLACE` | Marketplace header; defaults to `EBAY_US` |

Browse production access requires eBay approval. Never expose the secret or minted Application token.

Configuration comes from environment variables and optional `.env`; never commit that file.

| Setting family | Role |
| --- | --- |
| `FLIPPER_*` storage settings | Writable data and credential locations; see below |
| `GROQ_API_KEY` | Optional Groq-compatible analysis enrichment |
| `EBAY_OAUTH_TOKEN` | Optional legacy eBay asking-price lookup |
| `DISCORD_WEBHOOK_URL` | Optional alerts |
| `EBAY_SELLER_*` | Seller OAuth client, environment, and RuName |
| account-deletion variables | eBay compliance endpoint verification |

## Storage locations

Flipper writes authoritative data to one SQLite database and an attachment directory beside it.
Blank values are treated as unset. The web app and CLI resolve locations the same way:

| Variable | Meaning |
| --- | --- |
| `FLIPPER_DATA_DIR` | Optional absolute directory supplying storage defaults |
| `FLIPPER_INVENTORY_DB` | Explicit inventory database path |
| `FLIPPER_ATTACHMENT_ROOT` | Explicit attachment directory |
| `FLIPPER_CREDENTIAL_BACKEND` | `keyring` (default) or `file` for the seller refresh token |

Precedence:

1. Inventory database: CLI `--database`, else `FLIPPER_INVENTORY_DB`, else
   `FLIPPER_DATA_DIR/flipper_inventory.db`, else `data/flipper_inventory.db`. With nothing
   configured the web app uses the repository's `data/` directory and the CLI uses `data/` in the
   working directory, exactly as before.
2. Attachments: CLI `--attachment-root`, else `FLIPPER_ATTACHMENT_ROOT`, else an `attachments`
   directory beside the selected database.
3. Credentials: `keyring` uses the operating system credential store. `file` stores the seller
   refresh token in `FLIPPER_DATA_DIR/credentials/ebay-seller.json` and requires
   `FLIPPER_DATA_DIR`.

A future persistent-disk layout can therefore be selected with one setting:

```text
$FLIPPER_DATA_DIR/
    flipper_inventory.db
    attachments/
    credentials/ebay-seller.json   (only with FLIPPER_CREDENTIAL_BACKEND=file)
```

Invalid storage settings, such as a relative `FLIPPER_DATA_DIR` or an unknown credential backend,
stop the web app at startup and make CLI storage commands exit with an error. Resolving settings
never creates, moves, or copies files. The CLI-only analyzer caches (`data/flipper_seen.db` and
`data/part_prices.db`) are disposable and are not affected by these settings.

### File credential backend

The file backend is for a single-user server whose persistent disk is already protected. It stores
only the refresh token the keyring would hold, as plaintext JSON, because a key kept on the same
host would not add protection. On POSIX systems the file is created owner read/write (`0600`), a
missing credentials directory is created owner-only, the file is replaced atomically, and it is
refused if its permissions are broader. Malformed or unreadable files fail as an unavailable
credential store without revealing contents. Keep the file out of source control, backups shared
with others, and logs. Connect with `python main.py ebay connect` on the host that owns the file.

Use the CLI help and `.env.example` if one is present for exact names. Settings displays only safe
environment/configuration/connection booleans. Secrets, refresh tokens, authorization codes, buyer
data, deletion tokens, and raw payloads must never appear in pages, logs, docs, or commits.
