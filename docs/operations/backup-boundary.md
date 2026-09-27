# Backup and restore

Flipper keeps one authoritative SQLite database. A backup is a **recovery artifact** for that
database. It is not a second Flipper, it does not synchronize anything, and it is never an
alternate source of truth. At any moment exactly one writable Flipper database is authoritative.
If you run two writable copies, their Q/S/C numbers and accounting facts diverge, and no tool can
merge them back without guessing. Flipper never guesses.

## What is authoritative

A complete Flipper backup captures one database and the attachment files it references:

| Artifact | Contents |
| --- | --- |
| `flipper_inventory.db` | All durable business state (below) |
| attachment directory | Exact bytes of every file named by `inventory_attachments` |

The database holds, and a backup therefore preserves exactly:

- `schema_migrations`: schema version and migration history.
- The Q-, S-, and C-number counters (`inventory_id_sequence`, `sale_id_sequence`,
  `sale_cost_id_sequence`). Human-facing numbers are never reused.
- Inventory and lifecycle: `inventory_items`, including notes, unknown (NULL) versus known-zero
  acquisition facts, marketplace linkage, and eBay item IDs.
- Sales and accounting: `sales` (external order/line identities and revenue components),
  `sale_costs` (manual and eBay Finances components and their external identities), and
  `sale_reconciliation_confirmations`.
- History: `valuation_snapshots`, `research_snapshots` (stored JSON payloads and frozen Deal
  Scores), and `sourcing_travel`.
- `inventory_attachments` metadata.

Decision vs. Outcome is derived from explicit links between these tables, so it has no separate
storage. Working research and manual opportunities live only in process memory and are never
backed up. The analyzer caches (`data/flipper_seen.db`, `data/part_prices.db`) are disposable and
are not part of the boundary.

## What a backup never contains

- eBay seller credentials: the OS keyring entry, or `FLIPPER_DATA_DIR/credentials/` with the file
  credential backend.
- `.env`, OAuth access or refresh tokens, authorization codes, and any session or authentication
  secret.
- Files in the attachment directory that the database does not reference. They are listed on the
  console and counted in the manifest, and they are never copied, repaired, or deleted.
- Any other file under `FLIPPER_DATA_DIR` or the repository.

Backups copy only the database and files the database names. A credential file therefore cannot be
swept in, even when it sits next to the database or when the attachment root is misconfigured as
the data directory. Tests enforce this.

### Why credentials are separate

A data backup travels: it gets copied to other disks and later off-host. A refresh token that
travels with it would grant seller-account access wherever it lands. Data and secrets have
different lifetimes and different protection needs, so they get separate recovery paths. To
re-establish credentials after a recovery, configure the `EBAY_SELLER_*` settings on the new host
(and `FLIPPER_CREDENTIAL_BACKEND=file` with `FLIPPER_DATA_DIR` for a server). Then run
`python main.py ebay connect` there to authorize again. Reauthorizing is safer than restoring a
token you cannot protect in transit.

## Backup format (version 1)

```text
flipper-backup-YYYYMMDD-HHMMSS/
    manifest.json
    flipper_inventory.db
    attachments/
        <stored attachment filenames>
```

`manifest.json` is verification metadata, not a second database:

| Field | Meaning |
| --- | --- |
| `format`, `format_version` | `flipper-backup`, `1` |
| `created_at` | UTC capture time |
| `flipper_schema_version`, `schema_migrations` | Captured schema version and full migration list |
| `database` | Filename, SHA-256, and size of the database file |
| `logical_fingerprint` | Algorithm name and SHA-256 (see below) |
| `table_row_counts` | Row count of every table |
| `counters` | Last allocated `q_number`, `s_number`, `c_number` (`null` if the schema predates one) |
| `attachments` | Count, total bytes, and per-file attachment ID, relative path, size, SHA-256 |
| `credentials_included` | Always `false` |
| `excluded` | Human-readable list of deliberate exclusions |
| `source` | SQLite library version, supported schema, capture attempts, unreferenced-file count |
| `manifest_sha256` | Checksum over the rest of the manifest |

The manifest contains no absolute paths, hostnames, secrets, or buyer data. Its checksum catches
accidental edits and corruption. It is not a signature, so a deliberate forger could recompute it.
That is also why verification re-derives every fact from the files themselves.

## Creating a backup

```bash
python main.py backup create --output-dir /path/outside/the/data/directory
```

The command resolves storage the same way as every other command (`--database`,
`FLIPPER_INVENTORY_DB`, `FLIPPER_DATA_DIR`, attachment settings). It then:

1. Refuses an output inside the database directory or the attachment directory. A backup should
   never capture itself, and one disk failure should not destroy both copies.
2. Opens the source **read-only** and takes a consistent snapshot through SQLite's online backup
   API in a single step. The snapshot is exactly one committed state. Concurrent readers are
   unaffected. A concurrent writer waits briefly for the read lock, and an uncommitted write is
   not captured. The source is never migrated or modified, and its rollback-journal mode is left
   alone.
3. Reads the attachment list **from the snapshot** and stream-copies exactly those files, hashing
   them as it goes. Flipper writes an attachment file before committing its metadata and moves a
   file away before deleting its metadata. A referenced file that is missing therefore usually
   means a removal is in progress, so capture retries up to three times. A file that stays
   missing, or whose size differs from its metadata, fails the backup.
4. Runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on the copy and computes the
   manifest.
5. Assembles everything in a hidden `.<name>.*.partial` directory in the output directory, writes
   the manifest last, independently verifies the result, and only then atomically renames it to
   its final name. Any failure removes the staging directory, so no half-written backup ever
   looks complete.

An existing backup directory is never overwritten. Use `--name` to choose a different name.

## Verifying a backup

```bash
python main.py backup verify /path/to/flipper-backup-20260927-120000
```

Verification needs only the backup directory, so it works on a copy moved to another machine. It
never modifies the backup, because the database is opened read-only and immutable. It checks:

- the manifest is a recognized format, its checksum matches, and its structure is valid;
- the database exists and its size and SHA-256 match;
- SQLite `integrity_check` and `foreign_key_check` pass;
- the schema version and `schema_migrations` match the manifest and are contiguous;
- every table's row count, the Q/S/C counters, and the logical fingerprint match;
- the manifest's attachment list equals the database's attachment records, and every file exists
  with the recorded size and SHA-256;
- nothing else is present. Any extra file in the backup or its `attachments/` directory makes the
  backup invalid. A backup is a closed artifact, so remove stray files such as `.DS_Store` before
  verifying.

| Exit code | Result |
| --- | --- |
| 0 | `valid` |
| 1 | `invalid` (corrupt, tampered, incomplete, or not a backup) |
| 3 | `unsupported_format`: written by a newer backup format; use a newer Flipper |
| 4 | `newer_schema`: the database schema is newer than this Flipper understands |

## Logical fingerprint

Two SQLite files can hold identical state with different page layouts, so a file hash cannot prove
equivalence. `flipper-logical-v1` is a SHA-256 over a canonical stream containing:

1. every schema object in `sqlite_master` (tables, indexes, triggers, and their SQL), ordered by
   type and name, excluding only SQLite's internal `sqlite_stat*` statistics tables;
2. for every table in name order, its sorted column names, then every row. Each value carries its
   SQLite storage class (`null`, `integer`, `real`, `text`, `blob`) and its exact stored value.
   Text and blobs are exact bytes, integers are exact decimal digits, and reals are exact hex
   floats. Rows are ordered by storage class and binary value of every column, so insertion order
   and rowid layout never matter;
3. each table's row count.

The fingerprint therefore covers every table, including migration history, counters, historical
snapshots, external marketplace identities, and reconciliation confirmations. Nothing is
normalized. NULL differs from zero, `8307` at scale 2 differs from `83070` at scale 3, a timestamp
one second apart differs, and a research payload is compared as its **stored JSON text** rather
than re-parsed. Equal fingerprints mean the two databases store the same facts byte for byte. The
fingerprint does not cover attachment bytes; the backup manifest's per-file SHA-256 values do.

To fingerprint any database read-only, including a live one:

```bash
python main.py backup fingerprint [path/to/flipper_inventory.db]
```

## Restoring

```bash
python main.py backup restore /path/to/backup --destination /path/to/new-data-dir
```

Restore creates a **new** directory laid out like `FLIPPER_DATA_DIR`
(`flipper_inventory.db` and `attachments/`). It:

- refuses a destination that already exists (even an empty directory), that is inside or contains
  the backup, that would become the configured active database path, or that overlaps the active
  attachment directory;
- verifies the backup first and restores nothing unless it is `valid`;
- copies the database and attachments into a hidden staging directory beside the destination and
  checks every copied byte against the manifest;
- proves the staged copy is equivalent (integrity, foreign keys, schema, row counts, counters,
  logical fingerprint, attachment hashes) and only then atomically renames it into place.

Restore never overwrites or retires an existing database, changes configuration, points Flipper at
the result, migrates it, or starts a server. In-place replacement and a `--force` option are
deliberately not provided. To use a restored directory, set `FLIPPER_DATA_DIR` (or
`FLIPPER_INVENTORY_DB`) yourself. Only do that when it is meant to become the single authoritative
copy.

## Verifying a restored instance

```bash
python main.py backup verify-restore /path/to/backup /path/to/new-data-dir
```

This re-verifies the backup, then proves that the restored directory holds the same logical
fingerprint, row counts, counters, schema, and attachment bytes, with no extra attachment files.
The restored database is opened read-only, so the check is safe even after Flipper has started on
it. Starting Flipper on an up-to-date database does not change its fingerprint. Any later data
change, such as a new item or attachment, makes the instance no longer equivalent. That is the
expected result.

## Disaster recovery

1. Stop using the damaged instance for writes.
2. Choose the most recent backup that passes `backup verify`.
3. `backup restore` it into a new directory, then run `backup verify-restore`.
4. Point Flipper at the restored directory, start it, and review the dashboard and reports.
5. Re-establish credentials separately (see above).
6. Keep the damaged copy read-only until you no longer need it for investigation. Never keep
   writing to both copies.

## Future hosted cutover

Hosted Flipper does not exist yet. The intended cutover procedure reuses these commands:

1. Stop writes to the local instance.
2. `backup create`, then `backup verify` the result.
3. Transfer the backup directory to the host. Any copy method works because verification
   re-checks every byte.
4. On the host: `backup verify`, then `backup restore` into the persistent-disk data directory,
   then `backup verify-restore`.
5. Compare `backup fingerprint` of the local source with the restored database. They must be
   equal.
6. Start hosted Flipper with `FLIPPER_DATA_DIR` pointing at the restored directory, reconnect
   eBay there, and inspect reports.
7. Only then retire the local writable database. From that point, local development uses
   synthetic data or restored copies that never write back.

## Where backups should live

Persistent-disk snapshots from a hosting provider are one recovery layer. They share the
provider's failure domain, so Flipper backups should also be copied **off-host**. Off-host
upload, scheduling, and retention are **not implemented**. Copy backups off the machine yourself
until they are. For local development, any explicit directory outside the data directory works,
ideally on another disk. Backups contain business data and attachment files, so store them
privately. Runtime databases and backups are ignored by Git and must never be committed.

Test restores to a separate directory, never against the working database.
