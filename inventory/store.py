"""Versioned SQLite storage for local acquisition and inventory records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

CURRENT_SCHEMA_VERSION = 6
VALID_STATUSES = ("acquired", "listed", "sold", "archived")
SALE_COST_TYPES = ("marketplace_fee", "shipping_cost", "refund", "other_adjustment")
SALE_COST_SOURCES = ("manual", "ebay_finances")
STATUS_TRANSITIONS = {
    "acquired": frozenset({"listed", "archived"}),
    "listed": frozenset({"acquired", "sold", "archived"}),
    "sold": frozenset({"listed"}),
    "archived": frozenset({"acquired"}),
}
_UNSET = object()


class InventoryValidationError(ValueError):
    """An inventory value is invalid."""


class InventoryNotFoundError(LookupError):
    """No inventory record has the requested human-facing ID."""


class SaleNotFoundError(LookupError):
    """No sale record has the requested human-facing ID."""


class SaleImportError(ValueError):
    """A sale cannot be imported without violating local invariants."""


class SaleConflictError(SaleImportError):
    """An external sale identity conflicts with its persisted economics."""


class SaleCostNotFoundError(LookupError):
    """No sale cost component has the requested stable identifier."""


class SaleCostValidationError(ValueError):
    """A sale cost component is invalid."""


class SaleCostConflictError(SaleCostValidationError):
    """An external cost identity conflicts with persisted accounting data."""


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
    listed_at: str | None
    sold_at: str | None

    @property
    def acquisition_cost(self) -> Decimal:
        return Decimal(self.acquisition_cost_cents) / 100


@dataclass(frozen=True)
class SaleRecord:
    """A minimized marketplace sale with an exact scaled-integer gross amount."""

    internal_id: int
    sale_id: str
    inventory_internal_id: int
    inventory_id: str
    marketplace: str
    external_order_id: str
    external_line_item_id: str
    marketplace_sku: str
    quantity: int
    gross_amount_minor: int
    gross_amount_scale: int
    currency: str
    sold_at: str
    imported_at: str

    @property
    def gross_amount(self) -> Decimal:
        return Decimal(self.gross_amount_minor).scaleb(-self.gross_amount_scale)


@dataclass(frozen=True)
class SaleCostRecord:
    """An explicit nonnegative economic component that reduces sale proceeds."""

    internal_id: int
    cost_id: str
    sale_internal_id: int
    sale_id: str
    category: str
    amount_minor: int
    amount_scale: int
    currency: str
    source: str
    external_transaction_id: str | None
    external_component_key: str | None
    note: str
    created_at: str

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.amount_scale)


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

    def __init__(self, path: str | Path, *, clock=None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _utc_now(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("inventory clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

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
            if 2 not in versions:
                timestamp_check = (
                    "CHECK ({column} IS NULL OR "
                    "(length({column}) = 20 AND substr({column}, 11, 1) = 'T' "
                    "AND substr({column}, -1) = 'Z'))"
                )
                connection.execute(
                    "ALTER TABLE inventory_items ADD COLUMN listed_at TEXT "
                    + timestamp_check.format(column="listed_at")
                )
                connection.execute(
                    "ALTER TABLE inventory_items ADD COLUMN sold_at TEXT "
                    + timestamp_check.format(column="sold_at")
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (2)")
            if 3 not in versions:
                duplicate = connection.execute(
                    """SELECT marketplace, marketplace_sku
                    FROM inventory_items
                    WHERE marketplace IS NOT NULL AND marketplace_sku IS NOT NULL
                    GROUP BY marketplace COLLATE NOCASE, marketplace_sku
                    HAVING COUNT(*) > 1
                    LIMIT 1"""
                ).fetchone()
                if duplicate is not None:
                    raise RuntimeError(
                        "inventory migration cannot enforce unique marketplace SKUs; "
                        "resolve duplicate marketplace + SKU records first"
                    )
                connection.execute("DROP INDEX IF EXISTS inventory_marketplace_sku")
                connection.execute(
                    """CREATE UNIQUE INDEX inventory_marketplace_sku
                    ON inventory_items (marketplace COLLATE NOCASE, marketplace_sku)
                    WHERE marketplace IS NOT NULL AND marketplace_sku IS NOT NULL"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (3)")
            if 4 not in versions:
                connection.execute(
                    """CREATE TABLE sale_id_sequence (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        last_value INTEGER NOT NULL CHECK (last_value >= 0)
                    )"""
                )
                connection.execute(
                    "INSERT INTO sale_id_sequence (singleton, last_value) VALUES (1, 0)"
                )
                connection.execute(
                    """CREATE TABLE sales (
                        internal_id INTEGER PRIMARY KEY,
                        sale_id TEXT NOT NULL UNIQUE,
                        inventory_internal_id INTEGER NOT NULL,
                        marketplace TEXT NOT NULL COLLATE NOCASE
                            CHECK (length(trim(marketplace)) > 0),
                        external_order_id TEXT NOT NULL
                            CHECK (length(trim(external_order_id)) > 0),
                        external_line_item_id TEXT NOT NULL
                            CHECK (length(trim(external_line_item_id)) > 0),
                        marketplace_sku TEXT NOT NULL
                            CHECK (length(marketplace_sku) > 0),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        gross_amount_minor INTEGER NOT NULL CHECK (gross_amount_minor >= 0),
                        gross_amount_scale INTEGER NOT NULL
                            CHECK (gross_amount_scale BETWEEN 0 AND 9),
                        currency TEXT NOT NULL
                            CHECK (length(currency) = 3 AND currency = upper(currency)),
                        sold_at TEXT NOT NULL
                            CHECK (length(sold_at) = 20 AND substr(sold_at, 11, 1) = 'T'
                                   AND substr(sold_at, -1) = 'Z'),
                        imported_at TEXT NOT NULL
                            CHECK (length(imported_at) = 20 AND substr(imported_at, 11, 1) = 'T'
                                   AND substr(imported_at, -1) = 'Z'),
                        FOREIGN KEY (inventory_internal_id)
                            REFERENCES inventory_items(internal_id),
                        UNIQUE (marketplace, external_order_id, external_line_item_id)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX sales_inventory_internal_id ON sales (inventory_internal_id)"
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (4)")
            if 5 not in versions:
                connection.execute(
                    """CREATE TABLE sale_cost_id_sequence (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        last_value INTEGER NOT NULL CHECK (last_value >= 0)
                    )"""
                )
                connection.execute(
                    "INSERT INTO sale_cost_id_sequence (singleton, last_value) VALUES (1, 0)"
                )
                connection.execute(
                    """CREATE TABLE sale_costs (
                        internal_id INTEGER PRIMARY KEY,
                        cost_id TEXT NOT NULL UNIQUE,
                        sale_internal_id INTEGER NOT NULL,
                        category TEXT NOT NULL CHECK (category IN (
                            'marketplace_fee', 'shipping_cost', 'refund', 'other_adjustment'
                        )),
                        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
                        amount_scale INTEGER NOT NULL CHECK (amount_scale BETWEEN 0 AND 9),
                        currency TEXT NOT NULL
                            CHECK (length(currency) = 3 AND currency = upper(currency)),
                        source TEXT NOT NULL CHECK (source IN ('manual', 'ebay_finances')),
                        note TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                            CHECK (length(created_at) = 20 AND substr(created_at, 11, 1) = 'T'
                                   AND substr(created_at, -1) = 'Z'),
                        FOREIGN KEY (sale_internal_id) REFERENCES sales(internal_id)
                            ON DELETE RESTRICT
                    )"""
                )
                connection.execute(
                    "CREATE INDEX sale_costs_sale_internal_id ON sale_costs (sale_internal_id)"
                )
                connection.execute(
                    """CREATE TRIGGER sale_cost_currency_matches_sale
                    BEFORE INSERT ON sale_costs
                    WHEN NEW.currency != (
                        SELECT currency FROM sales WHERE internal_id = NEW.sale_internal_id
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'sale cost currency must match sale currency');
                    END"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (5)")
            if 6 not in versions:
                legacy_external = connection.execute(
                    "SELECT cost_id FROM sale_costs WHERE source = 'ebay_finances' LIMIT 1"
                ).fetchone()
                if legacy_external is not None:
                    raise RuntimeError(
                        "inventory migration cannot assign external identities to existing "
                        "ebay_finances costs; remove or correct those records first"
                    )
                connection.execute("ALTER TABLE sale_costs ADD COLUMN external_transaction_id TEXT")
                connection.execute("ALTER TABLE sale_costs ADD COLUMN external_component_key TEXT")
                connection.execute(
                    """CREATE UNIQUE INDEX sale_costs_external_identity
                    ON sale_costs (source, external_transaction_id, external_component_key)
                    WHERE external_transaction_id IS NOT NULL"""
                )
                connection.execute(
                    """CREATE TRIGGER sale_cost_external_identity_insert
                    BEFORE INSERT ON sale_costs
                    WHEN (NEW.source = 'manual' AND (
                              NEW.external_transaction_id IS NOT NULL OR
                              NEW.external_component_key IS NOT NULL
                          )) OR
                         (NEW.source = 'ebay_finances' AND (
                              NEW.external_transaction_id IS NULL OR
                              length(trim(NEW.external_transaction_id)) = 0 OR
                              NEW.external_component_key IS NULL OR
                              length(trim(NEW.external_component_key)) = 0
                          ))
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid sale cost external identity');
                    END"""
                )
                connection.execute(
                    """CREATE TRIGGER sale_cost_external_identity_update
                    BEFORE UPDATE OF source, external_transaction_id, external_component_key
                    ON sale_costs
                    WHEN (NEW.source = 'manual' AND (
                              NEW.external_transaction_id IS NOT NULL OR
                              NEW.external_component_key IS NOT NULL
                          )) OR
                         (NEW.source = 'ebay_finances' AND (
                              NEW.external_transaction_id IS NULL OR
                              length(trim(NEW.external_transaction_id)) = 0 OR
                              NEW.external_component_key IS NULL OR
                              length(trim(NEW.external_component_key)) = 0
                          ))
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid sale cost external identity');
                    END"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (6)")
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

    def _validate_values(
        self,
        *,
        title: str,
        source: str,
        acquired_at: str,
        acquisition_cost: str | Decimal,
        quantity: int,
        notes: str,
        status: str,
        marketplace: str | None,
        marketplace_item_id: str | None,
        marketplace_sku: str | None,
    ) -> dict[str, object]:
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
        return {
            "title": title,
            "source": source,
            "acquired_at": acquired_at,
            "acquisition_cost_cents": cost_cents,
            "quantity": quantity,
            "notes": notes.strip(),
            "status": status,
            "marketplace": self._optional(marketplace),
            "marketplace_item_id": self._optional(marketplace_item_id),
            "marketplace_sku": self._optional(marketplace_sku),
        }

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
        if status != "acquired":
            raise InventoryValidationError(
                "new inventory items must start as acquired; use inventory status to transition"
            )
        values = self._validate_values(
            title=title,
            source=source,
            acquired_at=acquired_at,
            acquisition_cost=acquisition_cost,
            quantity=quantity,
            notes=notes,
            status=status,
            marketplace=marketplace,
            marketplace_item_id=marketplace_item_id,
            marketplace_sku=marketplace_sku,
        )
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
                (inventory_id, *values.values()),
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

    def update(
        self,
        inventory_id: str,
        *,
        title: str | object = _UNSET,
        source: str | object = _UNSET,
        acquired_at: str | object = _UNSET,
        acquisition_cost: str | Decimal | object = _UNSET,
        quantity: int | object = _UNSET,
        notes: str | object = _UNSET,
        status: str | object = _UNSET,
        marketplace: str | None | object = _UNSET,
        marketplace_item_id: str | None | object = _UNSET,
        marketplace_sku: str | None | object = _UNSET,
    ) -> InventoryRecord:
        """Apply a validated partial update without changing either record identifier."""
        if status is not _UNSET:
            raise InventoryValidationError(
                "status cannot be changed by a generic update; use the lifecycle transition"
            )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id.upper(),)
            ).fetchone()
            if row is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            changes = {
                "title": title,
                "source": source,
                "acquired_at": acquired_at,
                "acquisition_cost": acquisition_cost,
                "quantity": quantity,
                "notes": notes,
                "status": status,
                "marketplace": marketplace,
                "marketplace_item_id": marketplace_item_id,
                "marketplace_sku": marketplace_sku,
            }
            existing = {
                "title": row["title"],
                "source": row["source"],
                "acquired_at": row["acquired_at"],
                "acquisition_cost": Decimal(row["acquisition_cost_cents"]) / 100,
                "quantity": row["quantity"],
                "notes": row["notes"],
                "status": row["status"],
                "marketplace": row["marketplace"],
                "marketplace_item_id": row["marketplace_item_id"],
                "marketplace_sku": row["marketplace_sku"],
            }
            candidate = {
                name: existing[name] if value is _UNSET else value
                for name, value in changes.items()
            }
            values = self._validate_values(**candidate)
            connection.execute(
                """UPDATE inventory_items SET
                    title = ?, source = ?, acquired_at = ?, acquisition_cost_cents = ?,
                    quantity = ?, notes = ?, status = ?, marketplace = ?,
                    marketplace_item_id = ?, marketplace_sku = ?
                WHERE internal_id = ?""",
                (*values.values(), row["internal_id"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(inventory_id)

    def transition_status(self, inventory_id: str, status: str) -> InventoryRecord:
        """Apply one valid lifecycle transition and its UTC timestamps atomically."""
        if status not in VALID_STATUSES:
            raise InventoryValidationError(f"status must be one of: {', '.join(VALID_STATUSES)}")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id.upper(),)
            ).fetchone()
            if row is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            self._transition_row(connection, row, status)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(inventory_id)

    def _transition_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        status: str,
        *,
        event_timestamp: str | None = None,
    ) -> None:
        """Apply shared lifecycle semantics inside the caller's transaction."""
        current = row["status"]
        if status not in STATUS_TRANSITIONS[current]:
            allowed = ", ".join(sorted(STATUS_TRANSITIONS[current]))
            raise InventoryValidationError(
                f"cannot transition inventory item from {current} to {status}; allowed: {allowed}"
            )

        listed_at = row["listed_at"]
        sold_at = row["sold_at"]
        if current == "acquired" and status == "listed":
            listed_at = event_timestamp or self._utc_now()
        elif current == "listed" and status == "sold":
            sold_at = event_timestamp or self._utc_now()
        elif status == "acquired":
            listed_at = None
            sold_at = None
        elif current == "sold" and status == "listed":
            sold_at = None

        connection.execute(
            "UPDATE inventory_items SET status = ?, listed_at = ?, sold_at = ? "
            "WHERE internal_id = ?",
            (status, listed_at, sold_at, row["internal_id"]),
        )

    @staticmethod
    def _timestamp(value: datetime, name: str) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise SaleImportError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _scaled_amount(value: Decimal) -> tuple[int, int]:
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise SaleImportError("gross item amount must be a finite nonnegative decimal")
        normalized = value.normalize()
        scale = max(0, -normalized.as_tuple().exponent)
        if scale > 9:
            raise SaleImportError("gross item amount has unsupported precision")
        return int(normalized.scaleb(scale)), scale

    def import_sale(
        self,
        *,
        inventory_id: str,
        marketplace: str,
        external_order_id: str,
        external_line_item_id: str,
        marketplace_sku: str,
        quantity: int,
        gross_amount: Decimal,
        currency: str,
        sold_at: datetime,
    ) -> tuple[SaleRecord, bool]:
        """Atomically insert one sale and apply the listed-to-sold lifecycle transition."""
        marketplace = self._required(marketplace, "marketplace")
        external_order_id = self._required(external_order_id, "external order ID")
        external_line_item_id = self._required(external_line_item_id, "external line item ID")
        marketplace_sku = self._required(marketplace_sku, "marketplace SKU")
        currency = self._required(currency, "currency").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise SaleImportError("currency must be a three-letter code")
        if quantity != 1:
            raise SaleImportError("sale import currently supports only quantity 1")
        amount_minor, amount_scale = self._scaled_amount(gross_amount)
        sold_timestamp = self._timestamp(sold_at, "sale timestamp")
        imported_timestamp = self._utc_now()
        identity = (marketplace, external_order_id, external_line_item_id)

        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id.upper(),)
            ).fetchone()
            if item is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            existing = connection.execute(
                """SELECT sales.*, inventory_items.inventory_id
                FROM sales JOIN inventory_items
                  ON inventory_items.internal_id = sales.inventory_internal_id
                WHERE sales.marketplace = ? AND external_order_id = ?
                  AND external_line_item_id = ?""",
                identity,
            ).fetchone()
            immutable = (
                item["internal_id"],
                marketplace_sku,
                quantity,
                amount_minor,
                amount_scale,
                currency,
                sold_timestamp,
            )
            if existing is not None:
                persisted = (
                    existing["inventory_internal_id"],
                    existing["marketplace_sku"],
                    existing["quantity"],
                    existing["gross_amount_minor"],
                    existing["gross_amount_scale"],
                    existing["currency"],
                    existing["sold_at"],
                )
                if persisted != immutable:
                    raise SaleConflictError(
                        "external sale identity conflicts with the persisted sale record"
                    )
                connection.commit()
                return self._sale_record(existing), False

            if item["quantity"] != 1:
                raise SaleImportError("sale import requires local inventory quantity 1")
            next_value = connection.execute(
                "SELECT last_value + 1 FROM sale_id_sequence WHERE singleton = 1"
            ).fetchone()[0]
            sale_id = f"S{next_value:06d}"
            cursor = connection.execute(
                """INSERT INTO sales (
                    sale_id, inventory_internal_id, marketplace, external_order_id,
                    external_line_item_id, marketplace_sku, quantity, gross_amount_minor,
                    gross_amount_scale, currency, sold_at, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sale_id,
                    item["internal_id"],
                    marketplace,
                    external_order_id,
                    external_line_item_id,
                    marketplace_sku,
                    quantity,
                    amount_minor,
                    amount_scale,
                    currency,
                    sold_timestamp,
                    imported_timestamp,
                ),
            )
            try:
                self._transition_row(connection, item, "sold", event_timestamp=sold_timestamp)
            except InventoryValidationError as exc:
                raise SaleImportError(str(exc)) from exc
            connection.execute(
                "UPDATE sale_id_sequence SET last_value = ? WHERE singleton = 1", (next_value,)
            )
            connection.commit()
            internal_id = cursor.lastrowid
            assert internal_id is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_sale(sale_id), True

    def get_sale(self, sale_id: str) -> SaleRecord:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sales.*, inventory_items.inventory_id
                FROM sales JOIN inventory_items
                  ON inventory_items.internal_id = sales.inventory_internal_id
                WHERE sale_id = ?""",
                (sale_id.upper(),),
            ).fetchone()
        if row is None:
            raise SaleNotFoundError(f"sale {sale_id} was not found")
        return self._sale_record(row)

    def list_sales(self) -> list[SaleRecord]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sales.*, inventory_items.inventory_id
                FROM sales JOIN inventory_items
                  ON inventory_items.internal_id = sales.inventory_internal_id
                ORDER BY sales.internal_id"""
            ).fetchall()
        return [self._sale_record(row) for row in rows]

    def add_sale_cost(
        self,
        sale_id: str,
        *,
        category: str,
        amount: Decimal,
        currency: str | None = None,
        source: str = "manual",
        note: str = "",
    ) -> SaleCostRecord:
        """Persist one explicit, nonnegative component that reduces proceeds."""
        if category not in SALE_COST_TYPES:
            raise SaleCostValidationError(f"cost type must be one of: {', '.join(SALE_COST_TYPES)}")
        if source not in SALE_COST_SOURCES:
            raise SaleCostValidationError(
                f"cost source must be one of: {', '.join(SALE_COST_SOURCES)}"
            )
        if source != "manual":
            raise SaleCostValidationError(
                "external sale costs must be imported through their source integration"
            )
        try:
            amount_minor, amount_scale = self._scaled_amount(amount)
        except SaleImportError as exc:
            raise SaleCostValidationError(str(exc).replace("gross item", "cost")) from exc
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            sale = connection.execute(
                "SELECT * FROM sales WHERE sale_id = ?", (sale_id.upper(),)
            ).fetchone()
            if sale is None:
                raise SaleNotFoundError(f"sale {sale_id} was not found")
            component_currency = (currency or sale["currency"]).strip().upper()
            if component_currency != sale["currency"]:
                raise SaleCostValidationError(
                    f"cost currency {component_currency} does not match sale currency "
                    f"{sale['currency']}; currency conversion is not supported"
                )
            next_value = connection.execute(
                "SELECT last_value + 1 FROM sale_cost_id_sequence WHERE singleton = 1"
            ).fetchone()[0]
            cost_id = f"C{next_value:06d}"
            cursor = connection.execute(
                """INSERT INTO sale_costs (
                    cost_id, sale_internal_id, category, amount_minor, amount_scale,
                    currency, source, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cost_id,
                    sale["internal_id"],
                    category,
                    amount_minor,
                    amount_scale,
                    component_currency,
                    source,
                    note.strip(),
                    self._utc_now(),
                ),
            )
            connection.execute(
                "UPDATE sale_cost_id_sequence SET last_value = ? WHERE singleton = 1",
                (next_value,),
            )
            connection.commit()
            internal_id = cursor.lastrowid
            assert internal_id is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_sale_cost(cost_id)

    def import_ebay_finances_cost(
        self,
        sale_id: str,
        *,
        category: str,
        amount: Decimal,
        currency: str,
        external_transaction_id: str,
        external_component_key: str,
        note: str = "",
    ) -> tuple[SaleCostRecord, bool]:
        """Atomically validate and persist one externally identified Finances cost."""
        if category not in SALE_COST_TYPES:
            raise SaleCostValidationError(f"cost type must be one of: {', '.join(SALE_COST_TYPES)}")
        transaction_id = self._required(external_transaction_id, "external transaction ID")
        component_key = self._required(external_component_key, "external component key")
        try:
            amount_minor, amount_scale = self._scaled_amount(amount)
        except SaleImportError as exc:
            raise SaleCostValidationError(str(exc).replace("gross item", "cost")) from exc
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            sale = connection.execute(
                "SELECT * FROM sales WHERE sale_id = ?", (sale_id.upper(),)
            ).fetchone()
            if sale is None:
                raise SaleNotFoundError(f"sale {sale_id} was not found")
            component_currency = currency.strip().upper()
            if component_currency != sale["currency"]:
                raise SaleCostValidationError(
                    f"cost currency {component_currency} does not match sale currency "
                    f"{sale['currency']}; currency conversion is not supported"
                )
            existing = connection.execute(
                """SELECT sale_costs.*, sales.sale_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                WHERE source = 'ebay_finances' AND external_transaction_id = ?
                  AND external_component_key = ?""",
                (transaction_id, component_key),
            ).fetchone()
            immutable = (
                sale["internal_id"],
                category,
                amount_minor,
                amount_scale,
                component_currency,
            )
            if existing is not None:
                persisted = (
                    existing["sale_internal_id"],
                    existing["category"],
                    existing["amount_minor"],
                    existing["amount_scale"],
                    existing["currency"],
                )
                if persisted != immutable:
                    raise SaleCostConflictError(
                        "external Finances identity conflicts with persisted accounting data"
                    )
                connection.commit()
                return self._sale_cost_record(existing), False
            next_value = connection.execute(
                "SELECT last_value + 1 FROM sale_cost_id_sequence WHERE singleton = 1"
            ).fetchone()[0]
            cost_id = f"C{next_value:06d}"
            connection.execute(
                """INSERT INTO sale_costs (
                    cost_id, sale_internal_id, category, amount_minor, amount_scale,
                    currency, source, note, created_at, external_transaction_id,
                    external_component_key
                ) VALUES (?, ?, ?, ?, ?, ?, 'ebay_finances', ?, ?, ?, ?)""",
                (
                    cost_id,
                    sale["internal_id"],
                    category,
                    amount_minor,
                    amount_scale,
                    component_currency,
                    note.strip(),
                    self._utc_now(),
                    transaction_id,
                    component_key,
                ),
            )
            connection.execute(
                "UPDATE sale_cost_id_sequence SET last_value = ? WHERE singleton = 1", (next_value,)
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_sale_cost(cost_id), True

    def get_sale_cost(self, cost_id: str) -> SaleCostRecord:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sale_costs.*, sales.sale_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                WHERE cost_id = ?""",
                (cost_id.upper(),),
            ).fetchone()
        if row is None:
            raise SaleCostNotFoundError(f"sale cost {cost_id} was not found")
        return self._sale_cost_record(row)

    def list_sale_costs(self, sale_id: str) -> list[SaleCostRecord]:
        sale = self.get_sale(sale_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sale_costs.*, sales.sale_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                WHERE sale_internal_id = ? ORDER BY sale_costs.internal_id""",
                (sale.internal_id,),
            ).fetchall()
        return [self._sale_cost_record(row) for row in rows]

    def remove_sale_cost(self, cost_id: str) -> SaleCostRecord:
        """Remove an erroneous component without reusing its stable identifier."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT sale_costs.*, sales.sale_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                WHERE cost_id = ?""",
                (cost_id.upper(),),
            ).fetchone()
            if row is None:
                raise SaleCostNotFoundError(f"sale cost {cost_id} was not found")
            record = self._sale_cost_record(row)
            if record.source != "manual":
                raise SaleCostValidationError(
                    "externally sourced sale costs cannot be removed with the manual command"
                )
            connection.execute(
                "DELETE FROM sale_costs WHERE internal_id = ?", (row["internal_id"],)
            )
            connection.commit()
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _sale_cost_record(row: sqlite3.Row) -> SaleCostRecord:
        return SaleCostRecord(
            internal_id=row["internal_id"],
            cost_id=row["cost_id"],
            sale_internal_id=row["sale_internal_id"],
            sale_id=row["sale_id"],
            category=row["category"],
            amount_minor=row["amount_minor"],
            amount_scale=row["amount_scale"],
            currency=row["currency"],
            source=row["source"],
            external_transaction_id=row["external_transaction_id"],
            external_component_key=row["external_component_key"],
            note=row["note"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _sale_record(row: sqlite3.Row) -> SaleRecord:
        return SaleRecord(
            internal_id=row["internal_id"],
            sale_id=row["sale_id"],
            inventory_internal_id=row["inventory_internal_id"],
            inventory_id=row["inventory_id"],
            marketplace=row["marketplace"],
            external_order_id=row["external_order_id"],
            external_line_item_id=row["external_line_item_id"],
            marketplace_sku=row["marketplace_sku"],
            quantity=row["quantity"],
            gross_amount_minor=row["gross_amount_minor"],
            gross_amount_scale=row["gross_amount_scale"],
            currency=row["currency"],
            sold_at=row["sold_at"],
            imported_at=row["imported_at"],
        )

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
            listed_at=row["listed_at"],
            sold_at=row["sold_at"],
        )
