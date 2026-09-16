"""Versioned SQLite storage for local acquisition and inventory records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

CURRENT_SCHEMA_VERSION = 1
VALID_STATUSES = ("acquired", "listed", "sold", "archived")


class InventoryValidationError(ValueError):
    """An inventory value is invalid."""


class InventoryNotFoundError(LookupError):
    """No inventory record has the requested human-facing ID."""


@dataclass(frozen=True)
class InventoryRecord:
    """A local inventory record. Acquisition cost is exact integer USD cents."""

    internal_id: int
    inventory_id: str
    title: str
    source: str
    acquired_at: str
    acquisition_cost_cents: int
    quantity: int
    notes: str
    status: str
    marketplace: str | None
    marketplace_item_id: str | None
    marketplace_sku: str | None

    @property
    def acquisition_cost(self) -> Decimal:
        return Decimal(self.acquisition_cost_cents) / 100


def parse_usd_cents(value: str | Decimal) -> int:
    """Parse a nonnegative USD amount with no more than two fractional digits."""
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InventoryValidationError("acquisition cost must be a valid USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise InventoryValidationError("acquisition cost must be nonnegative")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise InventoryValidationError("acquisition cost must have at most two decimal places")
    return int(cents)


class InventoryStore:
    """Own the local inventory database and its schema migrations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Apply all pending migrations in order."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if any(version > CURRENT_SCHEMA_VERSION for version in versions):
                raise RuntimeError("inventory database schema is newer than this Flipper version")
            if 1 not in versions:
                connection.execute(
                    """CREATE TABLE inventory_id_sequence (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        last_value INTEGER NOT NULL CHECK (last_value >= 0)
                    )"""
                )
                connection.execute(
                    "INSERT INTO inventory_id_sequence (singleton, last_value) VALUES (1, 0)"
                )
                connection.execute(
                    """CREATE TABLE inventory_items (
                        internal_id INTEGER PRIMARY KEY,
                        inventory_id TEXT NOT NULL UNIQUE,
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                        acquired_at TEXT NOT NULL CHECK (date(acquired_at) = acquired_at),
                        acquisition_cost_cents INTEGER NOT NULL
                            CHECK (acquisition_cost_cents >= 0),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        notes TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL
                            CHECK (status IN ('acquired', 'listed', 'sold', 'archived')),
                        marketplace TEXT,
                        marketplace_item_id TEXT,
                        marketplace_sku TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                connection.execute(
                    "CREATE INDEX inventory_marketplace_sku "
                    "ON inventory_items (marketplace, marketplace_sku)"
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _required(value: str, name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise InventoryValidationError(f"{name} is required")
        return cleaned

    @staticmethod
    def _optional(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def add(
        self,
        *,
        title: str,
        source: str,
        acquired_at: str,
        acquisition_cost: str | Decimal,
        quantity: int = 1,
        notes: str = "",
        status: str = "acquired",
        marketplace: str | None = None,
        marketplace_item_id: str | None = None,
        marketplace_sku: str | None = None,
    ) -> InventoryRecord:
        title = self._required(title, "title")
        source = self._required(source, "source")
        try:
            parsed_date = date.fromisoformat(acquired_at)
        except (TypeError, ValueError) as exc:
            raise InventoryValidationError("acquired-at must use YYYY-MM-DD") from exc
        if parsed_date.isoformat() != acquired_at:
            raise InventoryValidationError("acquired-at must use YYYY-MM-DD")
        cost_cents = parse_usd_cents(acquisition_cost)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise InventoryValidationError("quantity must be a positive integer")
        if status not in VALID_STATUSES:
            raise InventoryValidationError(f"status must be one of: {', '.join(VALID_STATUSES)}")
        notes = notes.strip()
        marketplace = self._optional(marketplace)
        marketplace_item_id = self._optional(marketplace_item_id)
        marketplace_sku = self._optional(marketplace_sku)
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            next_value = connection.execute(
                "SELECT last_value + 1 FROM inventory_id_sequence WHERE singleton = 1"
            ).fetchone()[0]
            inventory_id = f"Q{next_value:04d}"
            cursor = connection.execute(
                """INSERT INTO inventory_items (
                    inventory_id, title, source, acquired_at, acquisition_cost_cents,
                    quantity, notes, status, marketplace, marketplace_item_id, marketplace_sku
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inventory_id,
                    title,
                    source,
                    acquired_at,
                    cost_cents,
                    quantity,
                    notes,
                    status,
                    marketplace,
                    marketplace_item_id,
                    marketplace_sku,
                ),
            )
            connection.execute(
                "UPDATE inventory_id_sequence SET last_value = ? WHERE singleton = 1",
                (next_value,),
            )
            connection.commit()
            internal_id = cursor.lastrowid
            assert internal_id is not None
            return self.get(inventory_id)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, inventory_id: str) -> InventoryRecord:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id.upper(),)
            ).fetchone()
        if row is None:
            raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
        return self._record(row)

    def list(self) -> list[InventoryRecord]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inventory_items ORDER BY internal_id"
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> InventoryRecord:
        return InventoryRecord(
            internal_id=row["internal_id"],
            inventory_id=row["inventory_id"],
            title=row["title"],
            source=row["source"],
            acquired_at=row["acquired_at"],
            acquisition_cost_cents=row["acquisition_cost_cents"],
            quantity=row["quantity"],
            notes=row["notes"],
            status=row["status"],
            marketplace=row["marketplace"],
            marketplace_item_id=row["marketplace_item_id"],
            marketplace_sku=row["marketplace_sku"],
        )
