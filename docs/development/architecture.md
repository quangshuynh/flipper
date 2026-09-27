# Architecture

Flipper is one Python application with a CLI coordinator (`main.py`) and a server-rendered FastAPI /
Jinja web interface. Domain modules own parsing, pricing, inventory, eBay normalization and
reconciliation, sales economics, and reports. Web routes reuse those services; templates contain no
SQL or accounting formulas. CSS is static and requires no Node build.

SQLite under `inventory/` is authoritative and versioned by ordered migrations. `storage_config.py`
resolves writable locations for both the CLI and web app. The web app migrates its database once
during startup and fails to start on error; request stores then skip the redundant schema check,
while each SQLite operation still opens and closes its own connection. Independent CLI stores keep
checking migrations on every operation. Attachments are
controlled local files. External eBay data is normalized in memory and only supported explicit
imports create minimized records. The dashboard's local pages do not depend on live eBay; the eBay
Listings route deliberately isolates its live read.

`web/security.py` is the web app's single security boundary: one middleware that validates
configuration and trusted hosts, requires same-origin proof for every non-`GET` request, requires
an owner session for every route outside an explicit public allow-list, and adds security headers.
Routes never check authentication themselves. `web/passwords.py` (scrypt owner-password hash) and
`web/sessions.py` (stateless signed session cookie) have no database state. See
[Web security](../operations/web-security.md). `web/lan.py` is the explicit `python main.py web lan`
launcher: it validates LAN configuration, resolves storage exactly as the web app does, and runs
one Uvicorn process in the `lan` security mode ([Phone access](../guides/phone-access.md)).

`backup/` owns full backups of the authoritative boundary: a consistent SQLite snapshot through
the online backup API, the attachment files that snapshot references, a verification manifest,
restore into a new directory, and the `flipper-logical-v1` fingerprint used to prove equivalence.
It reads the source database read-only and never goes through `InventoryStore`, so a backup never
migrates or creates a database. There is no web backup surface.

Keep optional AI, market-data, Discord, and marketplace paths independently fallible. Preserve exact
money, stable identity, transactionality, privacy boundaries, and existing valuation/scoring when
working on unrelated features.

The ephemeral `deals/` domain layers category/source normalization, `DealOpportunity`, exact expected
economics, evidence/risk, and dimension-preserving comparison. The legacy PC parser and estimator
adapt into that domain but retain their existing behavior. Deal analysis does not touch the
inventory schema or realized-accounting services.

Working comparable research remains ephemeral. A bounded process-memory store keys records by a
random browser cookie and source opportunity identity. An explicit save copies the evaluated
aggregate into a schema-v11 durable research snapshot: queryable header fields plus a bounded,
validated versioned JSON payload. Historical views use captured calculations and never depend on the
working object or a live marketplace response. An optional inventory foreign key links history to a
Q-number without making inventory depend on research or turning estimates into accounting facts.
