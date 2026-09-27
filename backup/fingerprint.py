"""Deterministic logical fingerprint of an authoritative Flipper database.

Two SQLite files can hold identical Flipper state with different physical layouts (page order,
free pages, file size), so file hashes cannot prove equivalence. The logical fingerprint hashes a
canonical serialization of every schema object and every stored value instead. See
``docs/operations/backup-boundary.md`` for the exact meaning.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

FINGERPRINT_ALGORITHM = "flipper-logical-v1"
COUNTER_TABLES = {
    "q_number": "inventory_id_sequence",
    "s_number": "sale_id_sequence",
    "c_number": "sale_cost_id_sequence",
}


@dataclass(frozen=True)
class DatabaseState:
    """Everything read from one database inside a single read transaction."""

    fingerprint: str
    table_row_counts: dict[str, int]
    schema_versions: list[int]
    counters: dict[str, int | None]

    @property
    def schema_version(self) -> int:
        return max(self.schema_versions, default=0)


def open_read_only(path: str | Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open an existing database without the ability to create or modify it.

    ``immutable`` additionally tells SQLite the file cannot change, so no lock or journal files
    are touched. Use it only for closed artifacts such as a finished backup, never for a live
    database another process may write.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("database file does not exist")
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    # Compare stored text byte-for-byte rather than through Python string decoding.
    connection.text_factory = bytes
    return connection


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _cell(storage_class: bytes | str, value) -> list[str]:
    """Encode one stored value with its SQLite storage class, never coercing types."""
    storage_class = _text(storage_class)
    if storage_class == "null":
        return ["null"]
    if storage_class == "integer":
        return ["integer", str(value)]
    if storage_class == "real":
        return ["real", float(value).hex()]
    # TEXT and BLOB are both compared as exact stored bytes.
    return [storage_class, bytes(value).hex()]


def _line(value) -> bytes:
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_stat%'"
    ).fetchall()
    return sorted(_text(row[0]) for row in rows)


def _fingerprint(connection: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    digest.update(_line([FINGERPRINT_ALGORITHM]))
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type, name"
    ).fetchall()
    for object_type, name, table_name, sql in schema:
        digest.update(
            _line(
                [
                    "schema",
                    _text(object_type),
                    _text(name),
                    _text(table_name),
                    None if sql is None else _text(sql),
                ]
            )
        )
    for table in _table_names(connection):
        columns = sorted(
            _text(row[1])
            for row in connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        )
        digest.update(_line(["table", table, columns]))
        selected = ", ".join(f"typeof({_quote(c)}), {_quote(c)}" for c in columns)
        # Order by storage class then exact binary value of every column, so the
        # serialization never depends on insertion order, rowid, or column collations.
        ordering = ", ".join(f"typeof({_quote(c)}), {_quote(c)} COLLATE BINARY" for c in columns)
        count = 0
        for row in connection.execute(
            f"SELECT {selected} FROM {_quote(table)} ORDER BY {ordering}"
        ):
            cells = [_cell(row[index], row[index + 1]) for index in range(0, len(row), 2)]
            digest.update(_line(["row", cells]))
            count += 1
        digest.update(_line(["end", table, count]))
        counts[table] = count
    return digest.hexdigest(), counts


def _schema_versions(connection: sqlite3.Connection) -> list[int]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        raise ValueError("database has no schema_migrations table; it is not a Flipper database")
    return [
        row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY 1")
    ]


def _counters(connection: sqlite3.Connection) -> dict[str, int | None]:
    """Last allocated Q/S/C values; None when a table predates its migration."""
    tables = set(_table_names(connection))
    values: dict[str, int | None] = {}
    for label, table in COUNTER_TABLES.items():
        row = None
        if table in tables:
            row = connection.execute(
                f"SELECT last_value FROM {_quote(table)} WHERE singleton = 1"
            ).fetchone()
        values[label] = None if row is None else row[0]
    return values


def read_state(connection: sqlite3.Connection) -> DatabaseState:
    """Read fingerprint, row counts, migrations, and counters in one read transaction."""
    connection.execute("BEGIN")
    try:
        versions = _schema_versions(connection)
        fingerprint, counts = _fingerprint(connection)
        return DatabaseState(
            fingerprint=fingerprint,
            table_row_counts=counts,
            schema_versions=versions,
            counters=_counters(connection),
        )
    finally:
        connection.execute("ROLLBACK")


def database_state(path: str | Path, *, immutable: bool = False) -> DatabaseState:
    """Read-only state of a database; safe while a running Flipper uses it (not immutable)."""
    connection = open_read_only(path, immutable=immutable)
    try:
        return read_state(connection)
    finally:
        connection.close()
