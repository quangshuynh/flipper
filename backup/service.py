"""Create, verify, and restore full Flipper data backups.

A backup is a closed, portable recovery artifact for one authoritative inventory database and
the attachment files that database references. It is never a second writable Flipper instance.
Credentials and every file the database does not reference are deliberately excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from backup.fingerprint import (
    COUNTER_TABLES,
    FINGERPRINT_ALGORITHM,
    DatabaseState,
    open_read_only,
    read_state,
)
from inventory.store import CURRENT_SCHEMA_VERSION

BACKUP_FORMAT = "flipper-backup"
BACKUP_FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
DATABASE_FILENAME = "flipper_inventory.db"
ATTACHMENTS_DIRNAME = "attachments"
CHECKSUM_KEY = "manifest_sha256"
CAPTURE_ATTEMPTS = 3
_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED = (
    "eBay seller credentials and any FLIPPER_DATA_DIR/credentials directory",
    ".env files, OS keyring entries, OAuth tokens, and session or authentication secrets",
    "files in the attachment directory that the database does not reference",
    "analyzer caches and any other file outside the database and its referenced attachments",
)


class BackupError(RuntimeError):
    """A backup could not be created; nothing was left that looks complete."""


class RestoreError(RuntimeError):
    """A restore was refused or failed; no destination was created."""


class BackupStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNSUPPORTED_FORMAT = "unsupported_format"
    NEWER_SCHEMA = "newer_schema"


@dataclass(frozen=True)
class VerificationResult:
    status: BackupStatus
    problems: tuple[str, ...] = ()
    manifest: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status is BackupStatus.VALID


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: dict
    unreferenced_files: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RestoreResult:
    destination: Path
    manifest: dict


def _utc_timestamp(clock) -> datetime:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("backup clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _copy_file(source: Path, target: Path) -> tuple[int, str]:
    """Stream-copy exact bytes to a new file, hashing them on the way, then flush to disk."""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while chunk := reader.read(_CHUNK_BYTES):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return size, digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _safe_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _seal(manifest: dict) -> dict:
    """Add a checksum over the rest of the manifest so accidental edits are detected."""
    body = {key: value for key, value in manifest.items() if key != CHECKSUM_KEY}
    return {**body, CHECKSUM_KEY: hashlib.sha256(_canonical(body)).hexdigest()}


def _within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _integrity_problems(connection: sqlite3.Connection) -> list[str]:
    problems = []
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if [tuple(row) for row in rows] != [(b"ok",)]:
        problems.append(f"SQLite integrity_check reported {len(rows)} problem(s)")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        problems.append(f"SQLite foreign_key_check reported {len(violations)} violation(s)")
    return problems


def _attachment_rows(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inventory_attachments'"
    ).fetchone()
    if exists is None:
        return []
    rows = connection.execute(
        "SELECT attachment_id, stored_filename, byte_size FROM inventory_attachments "
        "ORDER BY attachment_id"
    ).fetchall()
    return [(bytes(a).decode(), bytes(s).decode(), size) for a, s, size in rows]


def _snapshot_database(source: Path, target: Path) -> None:
    """Copy a consistent snapshot through SQLite's online backup API.

    One backup step (``pages=-1``) holds a single read transaction on the source, so the copy
    reflects exactly one committed state. The source is opened read-only and is never changed;
    concurrent writers simply wait for the short read lock (their busy timeout is 30 seconds).
    """
    source_connection = open_read_only(source)
    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection, pages=-1)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    _fsync_file(target)


def create_backup(
    database: str | Path,
    output_dir: str | Path,
    *,
    attachment_root: str | Path | None = None,
    name: str | None = None,
    clock=None,
    attempts: int = CAPTURE_ATTEMPTS,
    retry_delay: float = 0.25,
) -> BackupResult:
    """Capture a verified backup directory inside ``output_dir``.

    The artifact is assembled in a hidden ``.partial`` staging directory, verified
    independently, and only then renamed to its final name. A failure removes staging, so no
    apparently complete backup is left behind.
    """
    database = Path(database)
    if not database.is_file():
        raise BackupError("source inventory database does not exist")
    root = Path(attachment_root) if attachment_root is not None else database.parent / "attachments"
    output_dir = Path(output_dir)
    resolved_output = output_dir.resolve()
    for protected, label in (
        (database.parent.resolve(), "the database directory"),
        (root.resolve(), "the attachment directory"),
    ):
        if _within(resolved_output, protected):
            raise BackupError(
                f"backup output must be outside {label} so a backup is never captured into its "
                "own source and one disk failure cannot destroy both"
            )
    created = _utc_timestamp(clock)
    name = name or f"flipper-backup-{created:%Y%m%d-%H%M%S}"
    if not _safe_name(name):
        raise BackupError("backup name must be a plain directory name")
    final = output_dir / name
    if final.exists() or final.is_symlink():
        raise BackupError("backup destination already exists; refusing to overwrite it")

    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", suffix=".partial", dir=output_dir))
    try:
        state, files, attempt = _capture(staging, database, root, attempts, retry_delay)
        referenced = {entry["path"].split("/", 1)[1] for entry in files}
        unreferenced = tuple(
            sorted(
                entry.name
                for entry in (root.iterdir() if root.is_dir() else ())
                if entry.name not in referenced
            )
        )
        database_size, database_sha256 = _hash_file(staging / DATABASE_FILENAME)
        manifest = _seal(
            {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": created.isoformat().replace("+00:00", "Z"),
                "flipper_schema_version": state.schema_version,
                "schema_migrations": state.schema_versions,
                "database": {
                    "filename": DATABASE_FILENAME,
                    "sha256": database_sha256,
                    "size_bytes": database_size,
                },
                "logical_fingerprint": {
                    "algorithm": FINGERPRINT_ALGORITHM,
                    "sha256": state.fingerprint,
                },
                "table_row_counts": state.table_row_counts,
                "counters": state.counters,
                "attachments": {
                    "directory": ATTACHMENTS_DIRNAME,
                    "count": len(files),
                    "total_bytes": sum(entry["size_bytes"] for entry in files),
                    "files": files,
                },
                "credentials_included": False,
                "excluded": list(_EXCLUDED),
                "source": {
                    "sqlite_library_version": sqlite3.sqlite_version,
                    "flipper_supported_schema_version": CURRENT_SCHEMA_VERSION,
                    "capture_attempts": attempt,
                    "unreferenced_attachment_files_excluded": len(unreferenced),
                },
            }
        )
        manifest_path = staging / MANIFEST_FILENAME
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        result = verify_backup(staging)
        if not result.ok:
            raise BackupError("captured backup failed verification: " + "; ".join(result.problems))
        if final.exists():
            raise BackupError("backup destination appeared during capture; refusing to overwrite")
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BackupResult(path=final, manifest=manifest, unreferenced_files=unreferenced)


def _capture(
    staging: Path, database: Path, root: Path, attempts: int, retry_delay: float
) -> tuple[DatabaseState, list[dict], int]:
    """Snapshot the database, then copy exactly the attachments that snapshot references.

    Attachment import writes the file before committing metadata, and removal moves the file
    away before deleting metadata. A referenced file that is absent therefore usually means a
    removal is in flight, so the whole capture is retried a bounded number of times. A file
    still missing afterwards is reported rather than silently omitted.
    """
    for attempt in range(1, attempts + 1):
        for entry in list(staging.iterdir()):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        snapshot = staging / DATABASE_FILENAME
        try:
            _snapshot_database(database, snapshot)
        except sqlite3.Error as exc:
            raise BackupError(f"source database could not be snapshotted: {exc}") from exc
        connection = open_read_only(snapshot, immutable=True)
        try:
            state = read_state(connection)
            if state.schema_version > CURRENT_SCHEMA_VERSION:
                raise BackupError(
                    "source database schema is newer than this Flipper version supports"
                )
            problems = _integrity_problems(connection)
            if problems:
                raise BackupError("source database is not consistent: " + "; ".join(problems))
            rows = _attachment_rows(connection)
        except (ValueError, sqlite3.Error) as exc:
            raise BackupError(f"source database could not be read: {exc}") from exc
        finally:
            connection.close()

        target_dir = staging / ATTACHMENTS_DIRNAME
        target_dir.mkdir()
        files: list[dict] = []
        missing: list[str] = []
        for attachment_id, stored_filename, byte_size in rows:
            if not _safe_name(stored_filename):
                raise BackupError(f"attachment {attachment_id} has an unsafe stored filename")
            source = root / stored_filename
            if not source.is_file():
                missing.append(attachment_id)
                continue
            try:
                size, sha256 = _copy_file(source, target_dir / stored_filename)
            except FileNotFoundError:
                missing.append(attachment_id)
                continue
            if size != byte_size:
                raise BackupError(
                    f"attachment {attachment_id} is {size} bytes but the database records "
                    f"{byte_size}; refusing to back up inconsistent data"
                )
            files.append(
                {
                    "attachment_id": attachment_id,
                    "path": f"{ATTACHMENTS_DIRNAME}/{stored_filename}",
                    "size_bytes": size,
                    "sha256": sha256,
                }
            )
        if not missing:
            return state, files, attempt
        if attempt < attempts:
            time.sleep(retry_delay)
    raise BackupError(
        f"database references {len(missing)} attachment file(s) missing from the attachment "
        f"directory (attachment IDs: {', '.join(missing)}); no backup was produced"
    )


class _ManifestError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _ManifestError(message)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_manifest(manifest: dict) -> None:
    """Check structure and internal consistency before trusting any recorded value."""
    _require(isinstance(manifest.get("created_at"), str), "created_at is missing")
    _require(_is_int(manifest.get("flipper_schema_version")), "schema version is missing")
    versions = manifest.get("schema_migrations")
    _require(
        isinstance(versions, list) and all(_is_int(v) for v in versions),
        "schema_migrations is missing",
    )
    database = manifest.get("database")
    _require(isinstance(database, dict), "database section is missing")
    _require(database.get("filename") == DATABASE_FILENAME, "database filename is unexpected")
    _require(
        isinstance(database.get("sha256"), str) and bool(_SHA256.match(database["sha256"])),
        "database sha256 is malformed",
    )
    _require(
        _is_int(database.get("size_bytes")) and database["size_bytes"] >= 0,
        "database size is malformed",
    )
    fingerprint = manifest.get("logical_fingerprint")
    _require(isinstance(fingerprint, dict), "logical fingerprint is missing")
    _require(
        fingerprint.get("algorithm") == FINGERPRINT_ALGORITHM,
        "logical fingerprint algorithm is not supported",
    )
    _require(
        isinstance(fingerprint.get("sha256"), str) and bool(_SHA256.match(fingerprint["sha256"])),
        "logical fingerprint is malformed",
    )
    counts = manifest.get("table_row_counts")
    _require(
        isinstance(counts, dict) and all(_is_int(v) and v >= 0 for v in counts.values()),
        "table_row_counts is malformed",
    )
    counters = manifest.get("counters")
    _require(
        isinstance(counters, dict)
        and set(counters) == set(COUNTER_TABLES)
        and all(v is None or _is_int(v) for v in counters.values()),
        "counters are malformed",
    )
    _require(manifest.get("credentials_included") is False, "credentials_included must be false")
    attachments = manifest.get("attachments")
    _require(isinstance(attachments, dict), "attachments section is missing")
    _require(attachments.get("directory") == ATTACHMENTS_DIRNAME, "attachment directory differs")
    files = attachments.get("files")
    _require(isinstance(files, list), "attachment file list is missing")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for entry in files:
        _require(isinstance(entry, dict), "attachment entry is malformed")
        path = entry.get("path")
        _require(
            isinstance(path, str)
            and path.startswith(f"{ATTACHMENTS_DIRNAME}/")
            and _safe_name(path.split("/", 1)[1]),
            "attachment path is unsafe",
        )
        _require(isinstance(entry.get("attachment_id"), str), "attachment ID is missing")
        _require(
            _is_int(entry.get("size_bytes")) and entry["size_bytes"] >= 0,
            "attachment size is malformed",
        )
        _require(
            isinstance(entry.get("sha256"), str) and bool(_SHA256.match(entry["sha256"])),
            "attachment sha256 is malformed",
        )
        _require(entry["attachment_id"] not in seen_ids, "attachment ID is duplicated")
        _require(path not in seen_paths, "attachment path is duplicated")
        seen_ids.add(entry["attachment_id"])
        seen_paths.add(path)
    _require(attachments.get("count") == len(files), "attachment count disagrees with files")
    _require(
        attachments.get("total_bytes") == sum(entry["size_bytes"] for entry in files),
        "attachment total bytes disagree with files",
    )


def _read_manifest(backup: Path) -> VerificationResult:
    """Load and structurally validate a manifest; status VALID means it can be trusted."""

    def invalid(message: str) -> VerificationResult:
        return VerificationResult(BackupStatus.INVALID, (message,))

    if not backup.is_dir():
        return invalid("backup directory does not exist")
    manifest_path = backup / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return invalid("manifest.json is missing; this is not a complete Flipper backup")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (ValueError, OSError):
        return invalid("manifest.json is not readable JSON")
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        return invalid("manifest does not describe a Flipper backup")
    version = manifest.get("format_version")
    if not _is_int(version):
        return invalid("manifest format version is missing")
    if version != BACKUP_FORMAT_VERSION:
        return VerificationResult(
            BackupStatus.UNSUPPORTED_FORMAT,
            (
                f"backup format version {version} is not supported; this Flipper reads "
                f"format version {BACKUP_FORMAT_VERSION}",
            ),
        )
    if _seal(manifest).get(CHECKSUM_KEY) != manifest.get(CHECKSUM_KEY):
        return invalid("manifest checksum mismatch; the manifest was modified or corrupted")
    try:
        _validate_manifest(manifest)
    except _ManifestError as exc:
        return invalid(f"manifest is malformed: {exc}")
    if manifest["flipper_schema_version"] > CURRENT_SCHEMA_VERSION:
        return VerificationResult(
            BackupStatus.NEWER_SCHEMA,
            (
                f"backup holds schema v{manifest['flipper_schema_version']}, newer than the "
                f"v{CURRENT_SCHEMA_VERSION} this Flipper understands; use a newer Flipper",
            ),
            manifest,
        )
    return VerificationResult(BackupStatus.VALID, (), manifest)


def _database_problems(
    database: Path, manifest: dict, *, immutable: bool
) -> tuple[list[str], list[tuple[str, str, int]]]:
    """Check SQLite integrity and logical state against the manifest."""
    problems: list[str] = []
    try:
        connection = open_read_only(database, immutable=immutable)
    except (FileNotFoundError, sqlite3.Error):
        return ["database is missing or cannot be opened"], []
    try:
        problems.extend(_integrity_problems(connection))
        state = read_state(connection)
        rows = _attachment_rows(connection)
    except ValueError as exc:
        return problems + [str(exc)], []
    except sqlite3.DatabaseError:
        return problems + ["database is not a readable SQLite database"], []
    finally:
        connection.close()
    if state.schema_version != manifest["flipper_schema_version"]:
        problems.append(
            f"database schema v{state.schema_version} does not match manifest "
            f"v{manifest['flipper_schema_version']}"
        )
    if state.schema_versions != manifest["schema_migrations"]:
        problems.append("schema_migrations history does not match the manifest")
    if state.schema_versions != list(range(1, state.schema_version + 1)):
        problems.append("schema_migrations history is not contiguous")
    expected_counts = manifest["table_row_counts"]
    for table in sorted(set(expected_counts) | set(state.table_row_counts)):
        expected = expected_counts.get(table)
        actual = state.table_row_counts.get(table)
        if expected != actual:
            problems.append(f"table {table} has {actual} rows; manifest records {expected}")
    if state.counters != manifest["counters"]:
        problems.append("Q/S/C counter values do not match the manifest")
    if state.fingerprint != manifest["logical_fingerprint"]["sha256"]:
        problems.append("logical fingerprint does not match the manifest")
    return problems, rows


def _attachment_problems(
    directory: Path, manifest: dict, database_rows: list[tuple[str, str, int]]
) -> list[str]:
    problems: list[str] = []
    files = manifest["attachments"]["files"]
    recorded = {
        (entry["attachment_id"], entry["path"].split("/", 1)[1], entry["size_bytes"])
        for entry in files
    }
    if recorded != set(database_rows):
        problems.append("attachment inventory does not match the database attachment records")
    expected_names = set()
    for entry in files:
        name = entry["path"].split("/", 1)[1]
        expected_names.add(name)
        path = directory / name
        if not path.is_file():
            problems.append(f"attachment {entry['attachment_id']} file is missing")
            continue
        size, sha256 = _hash_file(path)
        if size != entry["size_bytes"]:
            problems.append(f"attachment {entry['attachment_id']} size does not match")
        elif sha256 != entry["sha256"]:
            problems.append(f"attachment {entry['attachment_id']} sha256 does not match")
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if entry.name not in expected_names:
                problems.append(f"unexpected file in attachments: {entry.name}")
    elif files:
        problems.append("attachments directory is missing")
    return problems


def verify_backup(backup: str | Path) -> VerificationResult:
    """Independently verify a backup without touching the original source or the backup."""
    backup = Path(backup)
    result = _read_manifest(backup)
    if not result.ok:
        return result
    manifest = result.manifest
    problems: list[str] = []
    database = backup / DATABASE_FILENAME
    allowed = {MANIFEST_FILENAME, DATABASE_FILENAME, ATTACHMENTS_DIRNAME}
    for entry in sorted(backup.iterdir()):
        if entry.name not in allowed:
            problems.append(f"unexpected file in backup: {entry.name}")
    if not (backup / ATTACHMENTS_DIRNAME).is_dir():
        problems.append("attachments directory is missing")
    if not database.is_file():
        problems.append("database file is missing")
        return VerificationResult(BackupStatus.INVALID, tuple(problems), manifest)
    size, sha256 = _hash_file(database)
    if size != manifest["database"]["size_bytes"]:
        problems.append("database size does not match the manifest")
    if sha256 != manifest["database"]["sha256"]:
        problems.append("database sha256 does not match the manifest")
    database_problems, rows = _database_problems(database, manifest, immutable=True)
    problems.extend(database_problems)
    if not database_problems:
        problems.extend(_attachment_problems(backup / ATTACHMENTS_DIRNAME, manifest, rows))
    status = BackupStatus.INVALID if problems else BackupStatus.VALID
    return VerificationResult(status, tuple(dict.fromkeys(problems)), manifest)


def verify_restored(backup: str | Path, restored: str | Path) -> VerificationResult:
    """Prove a restored instance is logically equivalent to the backup it came from.

    The restored database is opened read-only (never immutable), so this is safe even after a
    restored Flipper has started. Any later change to its data makes it no longer equivalent.
    """
    result = verify_backup(backup)
    if not result.ok:
        return VerificationResult(
            result.status,
            tuple(f"backup: {problem}" for problem in result.problems),
            result.manifest,
        )
    restored = Path(restored)
    problems, rows = _database_problems(
        restored / DATABASE_FILENAME, result.manifest, immutable=False
    )
    if not problems:
        problems.extend(_attachment_problems(restored / ATTACHMENTS_DIRNAME, result.manifest, rows))
    status = BackupStatus.INVALID if problems else BackupStatus.VALID
    return VerificationResult(status, tuple(dict.fromkeys(problems)), result.manifest)


def restore_backup(
    backup: str | Path,
    destination: str | Path,
    *,
    active_database: str | Path | None = None,
    active_attachment_root: str | Path | None = None,
) -> RestoreResult:
    """Restore into a new directory laid out like ``FLIPPER_DATA_DIR``.

    The destination must not exist. It receives ``flipper_inventory.db`` and ``attachments/``
    and is never the active database. Flipper is not reconfigured, started, or migrated.
    """
    backup = Path(backup)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RestoreError("destination already exists; restore only creates a new destination")
    resolved_destination = destination.resolve()
    resolved_backup = backup.resolve()
    if _within(resolved_destination, resolved_backup) or _within(
        resolved_backup, resolved_destination
    ):
        raise RestoreError("destination must not be inside the backup or contain it")
    if active_database is not None:
        active = Path(active_database)
        active_root = (
            Path(active_attachment_root)
            if active_attachment_root is not None
            else active.parent / ATTACHMENTS_DIRNAME
        )
        if (resolved_destination / DATABASE_FILENAME) == active.resolve():
            raise RestoreError("destination would place the restore at the active database path")
        if _within(resolved_destination, active_root.resolve()) or (
            resolved_destination / ATTACHMENTS_DIRNAME == active_root.resolve()
        ):
            raise RestoreError("destination overlaps the active attachment directory")

    verification = verify_backup(backup)
    if not verification.ok:
        raise RestoreError(
            f"backup is {verification.status.value}; nothing was restored: "
            + "; ".join(verification.problems)
        )
    manifest = verification.manifest

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.", suffix=".restore-partial", dir=destination.parent
        )
    )
    try:
        size, sha256 = _copy_file(backup / DATABASE_FILENAME, staging / DATABASE_FILENAME)
        if (size, sha256) != (manifest["database"]["size_bytes"], manifest["database"]["sha256"]):
            raise RestoreError("backup database changed while restoring")
        (staging / ATTACHMENTS_DIRNAME).mkdir()
        for entry in manifest["attachments"]["files"]:
            name = entry["path"].split("/", 1)[1]
            copied = _copy_file(
                backup / ATTACHMENTS_DIRNAME / name, staging / ATTACHMENTS_DIRNAME / name
            )
            if copied != (entry["size_bytes"], entry["sha256"]):
                raise RestoreError(f"attachment {entry['attachment_id']} changed while restoring")
        check = verify_restored(backup, staging)
        if not check.ok:
            raise RestoreError(
                "restored data is not equivalent to the backup: " + "; ".join(check.problems)
            )
        if destination.exists():
            raise RestoreError("destination appeared during restore; refusing to overwrite")
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return RestoreResult(destination=destination, manifest=manifest)
