"""Versioned SQLite storage for local acquisition and inventory records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from deals.snapshots import canonical_snapshot_json, parse_snapshot_json, validate_snapshot_payload

CURRENT_SCHEMA_VERSION = 14
VALID_STATUSES = ("acquired", "listed", "sold", "archived")
SALE_COST_TYPES = ("marketplace_fee", "shipping_cost", "refund", "other_adjustment")
SALE_COST_SOURCES = ("manual", "ebay_finances")
SALE_COMPONENT_EFFECTS = ("reduce", "increase")
RECONCILIATION_CATEGORIES = ("fees", "shipping", "refunds", "adjustments")
ATTACHMENT_CATEGORIES = ("product_photo", "receipt", "supporting_document", "other")
STATUS_TRANSITIONS = {
    "acquired": frozenset({"listed", "archived"}),
    "listed": frozenset({"acquired", "sold", "archived"}),
    "sold": frozenset({"listed"}),
    "archived": frozenset({"acquired"}),
}
_UNSET = object()


def q_number_value(value: str) -> int | None:
    """Return the positive canonical Q-number value, otherwise None."""
    if not isinstance(value, str) or not value.startswith("Q") or not value[1:].isdigit():
        return None
    number = int(value[1:])
    if number < 1 or value != f"Q{number:04d}":
        return None
    return number


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


class ValuationValidationError(ValueError):
    """A valuation snapshot is invalid."""


class AttachmentNotFoundError(LookupError):
    """No attachment has the requested durable identifier."""


class AttachmentValidationError(ValueError):
    """An attachment value is invalid."""


class ResearchSnapshotNotFoundError(LookupError):
    """No durable research snapshot has the requested identifier."""


class ResearchSnapshotValidationError(ValueError):
    """A durable research snapshot is invalid."""


class SourcingTravelValidationError(ValueError):
    """Actual sourcing travel facts are invalid."""


@dataclass(frozen=True)
class InventoryRecord:
    """A local inventory record. Acquisition cost is exact integer USD cents."""

    internal_id: int
    inventory_id: str
    title: str
    source: str | None
    acquired_at: str | None
    acquisition_cost_cents: int | None
    quantity: int
    notes: str
    status: str
    marketplace: str | None
    marketplace_item_id: str | None
    marketplace_sku: str | None
    listed_at: str | None
    sold_at: str | None

    @property
    def acquisition_cost(self) -> Decimal | None:
        if self.acquisition_cost_cents is None:
            return None
        return Decimal(self.acquisition_cost_cents) / 100


@dataclass(frozen=True)
class AttachmentRecord:
    """Metadata for one Flipper-owned file linked to an inventory item."""

    attachment_id: str
    inventory_internal_id: int
    inventory_id: str
    original_filename: str
    stored_filename: str
    media_type: str
    byte_size: int
    category: str
    created_at: str


@dataclass(frozen=True)
class SaleRecord:
    """A minimized sale with exact seller revenue and optional source components."""

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
    item_revenue_minor: int | None
    item_revenue_scale: int | None
    buyer_shipping_minor: int | None
    buyer_shipping_scale: int | None
    marketplace_tax_minor: int | None
    marketplace_tax_scale: int | None
    checkout_total_minor: int | None
    checkout_total_scale: int | None
    currency: str
    sold_at: str
    imported_at: str

    @property
    def gross_amount(self) -> Decimal:
        return Decimal(self.gross_amount_minor).scaleb(-self.gross_amount_scale)

    @staticmethod
    def _optional_amount(minor: int | None, scale: int | None) -> Decimal | None:
        if minor is None or scale is None:
            return None
        return Decimal(minor).scaleb(-scale)

    @property
    def item_revenue(self) -> Decimal | None:
        return self._optional_amount(self.item_revenue_minor, self.item_revenue_scale)

    @property
    def buyer_shipping(self) -> Decimal | None:
        return self._optional_amount(self.buyer_shipping_minor, self.buyer_shipping_scale)

    @property
    def marketplace_tax(self) -> Decimal | None:
        return self._optional_amount(self.marketplace_tax_minor, self.marketplace_tax_scale)

    @property
    def checkout_total(self) -> Decimal | None:
        return self._optional_amount(self.checkout_total_minor, self.checkout_total_scale)


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
    effect: str
    related_cost_id: str | None
    external_transaction_id: str | None
    external_component_key: str | None
    note: str
    created_at: str

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.amount_scale)


@dataclass(frozen=True)
class ValuationSnapshot:
    """An immutable historical copy of analyzer outputs for one Q-number."""

    internal_id: int
    inventory_internal_id: int
    inventory_id: str
    analyzed_at: str
    currency: str
    estimated_market_value_minor: int
    estimated_market_value_scale: int
    expected_resale_value_minor: int
    expected_resale_value_scale: int
    asking_price_minor: int
    asking_price_scale: int
    ideal_buy_price_minor: int
    ideal_buy_price_scale: int
    estimated_gross_profit_minor: int
    estimated_gross_profit_scale: int
    estimated_roi: Decimal | None
    deal_score: int
    pricing_method: str | None
    is_baseline: bool

    @staticmethod
    def _amount(minor: int, scale: int) -> Decimal:
        return Decimal(minor).scaleb(-scale)

    @property
    def estimated_market_value(self) -> Decimal:
        return self._amount(self.estimated_market_value_minor, self.estimated_market_value_scale)

    @property
    def expected_resale_value(self) -> Decimal:
        return self._amount(self.expected_resale_value_minor, self.expected_resale_value_scale)

    @property
    def asking_price(self) -> Decimal:
        return self._amount(self.asking_price_minor, self.asking_price_scale)

    @property
    def ideal_buy_price(self) -> Decimal:
        return self._amount(self.ideal_buy_price_minor, self.ideal_buy_price_scale)

    @property
    def estimated_gross_profit(self) -> Decimal:
        return self._amount(self.estimated_gross_profit_minor, self.estimated_gross_profit_scale)


@dataclass(frozen=True)
class ResearchSnapshotRecord:
    internal_id: int
    snapshot_id: str
    saved_at: str
    opportunity_identity: str
    source: str
    title: str
    category: str
    currency: str
    asking_price: Decimal
    expected_resale: Decimal | None
    expected_profit: Decimal | None
    inventory_id: str | None
    payload: dict | None


@dataclass(frozen=True)
class SourcingTravelRecord:
    """User-recorded actual sourcing-trip facts for one inventory item."""

    inventory_internal_id: int
    inventory_id: str
    round_trip_miles: Decimal | None
    fuel_cost: Decimal | None
    additional_expense: Decimal | None
    travel_minutes: int | None
    note: str
    updated_at: str

    @property
    def total_expense(self) -> Decimal | None:
        if self.fuel_cost is None or self.additional_expense is None:
            return None
        return self.fuel_cost + self.additional_expense

    @property
    def recorded_expense(self) -> Decimal:
        """Sum recorded expense facts for accounting; absent components add nothing."""
        return (self.fuel_cost or Decimal(0)) + (self.additional_expense or Decimal(0))


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
            has_migrations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            migrating_existing_inventory = bool(
                has_migrations
                and connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = 12"
                ).fetchone()
                and not connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = 13"
                ).fetchone()
            )
            if migrating_existing_inventory:
                # The v13 table rebuild temporarily removes the referenced parent table.
                # Disable enforcement before the transaction, then verify every foreign key
                # against the replacement table before committing.
                connection.execute("PRAGMA foreign_keys = OFF")
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
            if 7 not in versions:
                connection.execute(
                    "ALTER TABLE sale_costs ADD COLUMN effect TEXT NOT NULL DEFAULT 'reduce' "
                    "CHECK (effect IN ('reduce', 'increase'))"
                )
                connection.execute(
                    "ALTER TABLE sale_costs ADD COLUMN related_cost_internal_id INTEGER "
                    "REFERENCES sale_costs(internal_id) ON DELETE RESTRICT"
                )
                connection.execute(
                    """CREATE UNIQUE INDEX sale_costs_one_external_reversal
                    ON sale_costs (related_cost_internal_id)
                    WHERE source = 'ebay_finances' AND effect = 'increase'"""
                )
                connection.execute(
                    """CREATE TRIGGER sale_cost_relationship_insert
                    BEFORE INSERT ON sale_costs
                    WHEN (NEW.effect = 'reduce' AND NEW.related_cost_internal_id IS NOT NULL) OR
                         (NEW.source = 'ebay_finances' AND NEW.effect = 'increase' AND
                          NEW.related_cost_internal_id IS NULL)
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid sale cost relationship');
                    END"""
                )
                connection.execute(
                    """CREATE TABLE sale_reconciliation_confirmations (
                        sale_internal_id INTEGER NOT NULL,
                        category TEXT NOT NULL CHECK (category IN (
                            'fees', 'shipping', 'refunds', 'adjustments'
                        )),
                        confirmed_at TEXT NOT NULL,
                        PRIMARY KEY (sale_internal_id, category),
                        FOREIGN KEY (sale_internal_id) REFERENCES sales(internal_id)
                            ON DELETE RESTRICT
                    )"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (7)")
            if 8 not in versions:
                connection.execute(
                    """CREATE TABLE valuation_snapshots (
                        internal_id INTEGER PRIMARY KEY,
                        inventory_internal_id INTEGER NOT NULL,
                        analyzed_at TEXT NOT NULL
                            CHECK (length(analyzed_at) = 20 AND substr(analyzed_at, 11, 1) = 'T'
                                   AND substr(analyzed_at, -1) = 'Z'),
                        currency TEXT NOT NULL
                            CHECK (length(currency) = 3 AND currency = upper(currency)),
                        estimated_market_value_minor INTEGER NOT NULL
                            CHECK (estimated_market_value_minor >= 0),
                        estimated_market_value_scale INTEGER NOT NULL
                            CHECK (estimated_market_value_scale BETWEEN 0 AND 9),
                        expected_resale_value_minor INTEGER NOT NULL
                            CHECK (expected_resale_value_minor >= 0),
                        expected_resale_value_scale INTEGER NOT NULL
                            CHECK (expected_resale_value_scale BETWEEN 0 AND 9),
                        asking_price_minor INTEGER NOT NULL CHECK (asking_price_minor >= 0),
                        asking_price_scale INTEGER NOT NULL
                            CHECK (asking_price_scale BETWEEN 0 AND 9),
                        ideal_buy_price_minor INTEGER NOT NULL CHECK (ideal_buy_price_minor >= 0),
                        ideal_buy_price_scale INTEGER NOT NULL
                            CHECK (ideal_buy_price_scale BETWEEN 0 AND 9),
                        estimated_gross_profit_minor INTEGER NOT NULL,
                        estimated_gross_profit_scale INTEGER NOT NULL
                            CHECK (estimated_gross_profit_scale BETWEEN 0 AND 9),
                        estimated_roi TEXT,
                        deal_score INTEGER NOT NULL,
                        pricing_method TEXT,
                        is_baseline INTEGER NOT NULL CHECK (is_baseline IN (0, 1)),
                        FOREIGN KEY (inventory_internal_id) REFERENCES inventory_items(internal_id)
                            ON DELETE RESTRICT
                    )"""
                )
                connection.execute(
                    "CREATE INDEX valuation_snapshots_inventory "
                    "ON valuation_snapshots (inventory_internal_id, internal_id)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX valuation_snapshots_one_baseline "
                    "ON valuation_snapshots (inventory_internal_id) WHERE is_baseline = 1"
                )
                connection.execute(
                    """CREATE TRIGGER valuation_snapshots_immutable_update
                    BEFORE UPDATE ON valuation_snapshots
                    BEGIN
                        SELECT RAISE(ABORT, 'valuation snapshots are immutable');
                    END"""
                )
                connection.execute(
                    """CREATE TRIGGER valuation_snapshots_immutable_delete
                    BEFORE DELETE ON valuation_snapshots
                    BEGIN
                        SELECT RAISE(ABORT, 'valuation snapshots are immutable');
                    END"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (8)")
            if 9 not in versions:
                connection.execute(
                    """CREATE TABLE inventory_attachments (
                        attachment_id TEXT PRIMARY KEY,
                        inventory_internal_id INTEGER NOT NULL,
                        original_filename TEXT NOT NULL
                            CHECK (length(original_filename) > 0),
                        stored_filename TEXT NOT NULL UNIQUE
                            CHECK (length(stored_filename) > 0
                                   AND instr(stored_filename, '/') = 0
                                   AND instr(stored_filename, char(92)) = 0),
                        media_type TEXT NOT NULL CHECK (length(media_type) > 0),
                        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                        category TEXT NOT NULL CHECK (category IN (
                            'product_photo', 'receipt', 'supporting_document', 'other'
                        )),
                        created_at TEXT NOT NULL
                            CHECK (length(created_at) = 20 AND substr(created_at, 11, 1) = 'T'
                                   AND substr(created_at, -1) = 'Z'),
                        FOREIGN KEY (inventory_internal_id)
                            REFERENCES inventory_items(internal_id) ON DELETE RESTRICT
                    )"""
                )
                connection.execute(
                    "CREATE INDEX inventory_attachments_inventory "
                    "ON inventory_attachments (inventory_internal_id, created_at, attachment_id)"
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (9)")
            if 10 not in versions:
                duplicate = connection.execute(
                    """SELECT marketplace, marketplace_item_id FROM inventory_items
                    WHERE marketplace = 'ebay' COLLATE NOCASE AND marketplace_item_id IS NOT NULL
                    GROUP BY marketplace_item_id HAVING COUNT(*) > 1
                    LIMIT 1"""
                ).fetchone()
                if duplicate is not None:
                    raise RuntimeError(
                        "inventory migration cannot enforce unique marketplace item IDs; "
                        "resolve duplicate marketplace + item ID records first"
                    )
                connection.execute(
                    """CREATE UNIQUE INDEX inventory_ebay_item_id
                    ON inventory_items (marketplace_item_id)
                    WHERE marketplace = 'ebay' COLLATE NOCASE AND marketplace_item_id IS NOT NULL"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (10)")
            if 11 not in versions:
                connection.execute(
                    """CREATE TABLE research_snapshots (
                        internal_id INTEGER PRIMARY KEY,
                        snapshot_id TEXT NOT NULL UNIQUE
                            CHECK (length(snapshot_id) = 32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
                        saved_at TEXT NOT NULL
                            CHECK (length(saved_at) = 20 AND substr(saved_at, 11, 1) = 'T'
                                   AND substr(saved_at, -1) = 'Z'),
                        opportunity_identity TEXT NOT NULL
                            CHECK (length(trim(opportunity_identity)) BETWEEN 1 AND 500),
                        source TEXT NOT NULL CHECK (length(trim(source)) BETWEEN 1 AND 100),
                        title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
                        category TEXT NOT NULL CHECK (length(trim(category)) BETWEEN 1 AND 100),
                        currency TEXT NOT NULL
                            CHECK (length(currency) = 3 AND currency = upper(currency)),
                        asking_price TEXT NOT NULL CHECK (length(asking_price) BETWEEN 1 AND 100),
                        expected_resale TEXT CHECK (
                            expected_resale IS NULL OR length(expected_resale) BETWEEN 1 AND 100),
                        expected_profit TEXT CHECK (
                            expected_profit IS NULL OR length(expected_profit) BETWEEN 1 AND 100),
                        inventory_internal_id INTEGER,
                        save_token TEXT NOT NULL UNIQUE
                            CHECK (length(save_token) = 32 AND save_token NOT GLOB '*[^0-9a-f]*'),
                        payload_json TEXT NOT NULL
                            CHECK (length(payload_json) BETWEEN 2 AND 262144),
                        FOREIGN KEY (inventory_internal_id) REFERENCES inventory_items(internal_id)
                            ON DELETE SET NULL
                    )"""
                )
                connection.execute(
                    "CREATE INDEX research_snapshots_saved_at "
                    "ON research_snapshots (saved_at DESC, internal_id DESC)"
                )
                connection.execute(
                    "CREATE INDEX research_snapshots_inventory "
                    "ON research_snapshots (inventory_internal_id)"
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (11)")
            if 12 not in versions:
                connection.execute(
                    """CREATE TABLE sourcing_travel (
                        inventory_internal_id INTEGER PRIMARY KEY,
                        round_trip_miles_minor INTEGER,
                        round_trip_miles_scale INTEGER,
                        fuel_cost_minor INTEGER,
                        fuel_cost_scale INTEGER,
                        additional_expense_minor INTEGER,
                        additional_expense_scale INTEGER,
                        travel_minutes INTEGER CHECK (
                            travel_minutes IS NULL OR travel_minutes >= 0),
                        note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
                        updated_at TEXT NOT NULL CHECK (
                            length(updated_at) = 20 AND substr(updated_at, 11, 1) = 'T'
                            AND substr(updated_at, -1) = 'Z'),
                        CHECK ((round_trip_miles_minor IS NULL) =
                               (round_trip_miles_scale IS NULL)),
                        CHECK (round_trip_miles_minor IS NULL OR
                               (round_trip_miles_minor >= 0 AND
                                round_trip_miles_scale BETWEEN 0 AND 9)),
                        CHECK ((fuel_cost_minor IS NULL) = (fuel_cost_scale IS NULL)),
                        CHECK (fuel_cost_minor IS NULL OR
                               (fuel_cost_minor >= 0 AND fuel_cost_scale BETWEEN 0 AND 9)),
                        CHECK ((additional_expense_minor IS NULL) =
                               (additional_expense_scale IS NULL)),
                        CHECK (additional_expense_minor IS NULL OR
                               (additional_expense_minor >= 0 AND
                                additional_expense_scale BETWEEN 0 AND 9)),
                        FOREIGN KEY (inventory_internal_id) REFERENCES inventory_items(internal_id)
                            ON DELETE RESTRICT
                    )"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (12)")
            if 13 not in versions:
                # SQLite cannot remove NOT NULL constraints in place. Rebuild only the
                # inventory table while preserving identifiers, timestamps, and all rows.
                connection.execute(
                    """CREATE TABLE inventory_items_v13 (
                        internal_id INTEGER PRIMARY KEY,
                        inventory_id TEXT NOT NULL UNIQUE,
                        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                        source TEXT CHECK (source IS NULL OR length(trim(source)) > 0),
                        acquired_at TEXT CHECK (
                            acquired_at IS NULL OR date(acquired_at) = acquired_at),
                        acquisition_cost_cents INTEGER CHECK (
                            acquisition_cost_cents IS NULL OR acquisition_cost_cents >= 0),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        notes TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL
                            CHECK (status IN ('acquired', 'listed', 'sold', 'archived')),
                        marketplace TEXT,
                        marketplace_item_id TEXT,
                        marketplace_sku TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        listed_at TEXT CHECK (
                            listed_at IS NULL OR
                            (length(listed_at) = 20 AND substr(listed_at, 11, 1) = 'T'
                             AND substr(listed_at, -1) = 'Z')),
                        sold_at TEXT CHECK (
                            sold_at IS NULL OR
                            (length(sold_at) = 20 AND substr(sold_at, 11, 1) = 'T'
                             AND substr(sold_at, -1) = 'Z'))
                    )"""
                )
                connection.execute(
                    """INSERT INTO inventory_items_v13 (
                        internal_id, inventory_id, title, source, acquired_at,
                        acquisition_cost_cents, quantity, notes, status, marketplace,
                        marketplace_item_id, marketplace_sku, created_at, listed_at, sold_at
                    ) SELECT internal_id, inventory_id, title, source, acquired_at,
                             acquisition_cost_cents, quantity, notes, status, marketplace,
                             marketplace_item_id, marketplace_sku, created_at, listed_at, sold_at
                      FROM inventory_items"""
                )
                connection.execute("DROP TABLE inventory_items")
                connection.execute("ALTER TABLE inventory_items_v13 RENAME TO inventory_items")
                connection.execute(
                    """CREATE UNIQUE INDEX inventory_marketplace_sku
                    ON inventory_items (marketplace COLLATE NOCASE, marketplace_sku)
                    WHERE marketplace IS NOT NULL AND marketplace_sku IS NOT NULL"""
                )
                connection.execute(
                    """CREATE UNIQUE INDEX inventory_ebay_item_id
                    ON inventory_items (marketplace_item_id)
                    WHERE marketplace = 'ebay' COLLATE NOCASE AND marketplace_item_id IS NOT NULL"""
                )
                connection.execute("INSERT INTO schema_migrations (version) VALUES (13)")
            if 14 not in versions:
                # Existing gross amounts remain authoritative aggregates. Their newly
                # introduced composition is unknown and must not be invented.
                for name in (
                    "item_revenue_minor",
                    "item_revenue_scale",
                    "buyer_shipping_minor",
                    "buyer_shipping_scale",
                    "marketplace_tax_minor",
                    "marketplace_tax_scale",
                    "checkout_total_minor",
                    "checkout_total_scale",
                ):
                    connection.execute(f"ALTER TABLE sales ADD COLUMN {name} INTEGER")
                connection.execute("INSERT INTO schema_migrations (version) VALUES (14)")
            foreign_key_violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_violation is not None:
                raise RuntimeError("inventory migration failed foreign-key integrity validation")
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
        source: str | None,
        acquired_at: str | None,
        acquisition_cost: str | Decimal | None,
        quantity: int,
        notes: str,
        status: str,
        marketplace: str | None,
        marketplace_item_id: str | None,
        marketplace_sku: str | None,
        allow_unknown_acquisition: bool = False,
    ) -> dict[str, object]:
        title = self._required(title, "title")
        source = (
            self._optional(source)
            if allow_unknown_acquisition
            else self._required(source, "source")
        )
        if acquired_at in (None, "") and allow_unknown_acquisition:
            acquired_at = None
        else:
            try:
                parsed_date = date.fromisoformat(acquired_at)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise InventoryValidationError("acquired-at must use YYYY-MM-DD") from exc
            if parsed_date.isoformat() != acquired_at:
                raise InventoryValidationError("acquired-at must use YYYY-MM-DD")
        cost_cents = (
            None
            if acquisition_cost in (None, "") and allow_unknown_acquisition
            else parse_usd_cents(acquisition_cost)  # type: ignore[arg-type]
        )
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
            inventory_id, _ = self._insert_inventory(connection, values)
            connection.commit()
            return self.get(inventory_id)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def adopt_ebay_listing(
        self,
        inventory_id: str,
        *,
        title: str,
        source: str | None,
        acquired_at: str | None,
        acquisition_cost: str | Decimal | None,
        marketplace_item_id: str,
        marketplace_sku: str,
        quantity: int = 1,
    ) -> tuple[InventoryRecord, bool]:
        """Atomically adopt an externally assigned Q-number and advance the allocator."""
        number = q_number_value(inventory_id)
        if number is None:
            raise InventoryValidationError("inventory ID must be a valid Q-number")
        if inventory_id != marketplace_sku:
            raise InventoryValidationError("inventory ID and marketplace SKU must match exactly")
        values = self._validate_values(
            title=title,
            source=source,
            acquired_at=acquired_at,
            acquisition_cost=acquisition_cost,
            quantity=quantity,
            notes="",
            status="listed",
            marketplace="ebay",
            marketplace_item_id=marketplace_item_id,
            marketplace_sku=marketplace_sku,
            allow_unknown_acquisition=True,
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["marketplace"]
                    and existing["marketplace"].casefold() == "ebay"
                    and existing["marketplace_item_id"] == marketplace_item_id
                    and existing["marketplace_sku"] == marketplace_sku
                    and existing["source"] == values["source"]
                    and existing["acquired_at"] == values["acquired_at"]
                    and existing["acquisition_cost_cents"] == values["acquisition_cost_cents"]
                )
                if not same:
                    raise InventoryValidationError(
                        "listing import conflicts with existing inventory"
                    )
                connection.rollback()
                return self.get(inventory_id), False
            connection.execute(
                """INSERT INTO inventory_items (
                    inventory_id, title, source, acquired_at, acquisition_cost_cents,
                    quantity, notes, status, marketplace, marketplace_item_id, marketplace_sku,
                    listed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (inventory_id, *values.values(), self._utc_now()),
            )
            connection.execute(
                "UPDATE inventory_id_sequence SET last_value = MAX(last_value, ?) "
                "WHERE singleton = 1",
                (number,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(inventory_id), True

    def sync_ebay_listing(
        self, inventory_id: str, *, marketplace_item_id: str, marketplace_sku: str
    ) -> tuple[InventoryRecord, bool]:
        """Atomically fill identical-safe linkage and acquire-to-listed transition."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE inventory_id = ?", (inventory_id.upper(),)
            ).fetchone()
            if row is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            if row["status"] in {"sold", "archived"}:
                raise InventoryValidationError("active eBay listing conflicts with local lifecycle")
            if (
                (row["marketplace"] and row["marketplace"].casefold() != "ebay")
                or (
                    row["marketplace_item_id"] and row["marketplace_item_id"] != marketplace_item_id
                )
                or (row["marketplace_sku"] and row["marketplace_sku"] != marketplace_sku)
            ):
                raise InventoryValidationError("active eBay listing conflicts with durable linkage")
            changed = not (
                row["marketplace"] == "ebay"
                and row["marketplace_item_id"] == marketplace_item_id
                and row["marketplace_sku"] == marketplace_sku
                and row["status"] == "listed"
            )
            connection.execute(
                "UPDATE inventory_items SET marketplace = 'ebay', "
                "marketplace_item_id = ?, marketplace_sku = ? WHERE internal_id = ?",
                (marketplace_item_id, marketplace_sku, row["internal_id"]),
            )
            if row["status"] == "acquired":
                self._transition_row(connection, row, "listed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(inventory_id), changed

    @staticmethod
    def _insert_inventory(
        connection: sqlite3.Connection, values: dict[str, object]
    ) -> tuple[str, int]:
        """Allocate and insert one Q-number inside the caller's transaction."""
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
        internal_id = cursor.lastrowid
        assert internal_id is not None
        return inventory_id, internal_id

    def acquire_with_valuation(
        self,
        *,
        title: str,
        source: str,
        acquired_at: str,
        acquisition_cost: str | Decimal,
        analyzed_at: datetime,
        currency: str,
        estimated_market_value: Decimal,
        expected_resale_value: Decimal,
        asking_price: Decimal,
        ideal_buy_price: Decimal,
        estimated_gross_profit: Decimal,
        estimated_roi: Decimal | None,
        deal_score: int,
        pricing_method: str | None = None,
        quantity: int = 1,
        notes: str = "",
        marketplace: str | None = None,
        marketplace_item_id: str | None = None,
        marketplace_sku: str | None = None,
    ) -> InventoryRecord:
        """Create one acquired Q-number and its baseline valuation atomically."""
        item_values = self._validate_values(
            title=title,
            source=source,
            acquired_at=acquired_at,
            acquisition_cost=acquisition_cost,
            quantity=quantity,
            notes=notes,
            status="acquired",
            marketplace=marketplace,
            marketplace_item_id=marketplace_item_id,
            marketplace_sku=marketplace_sku,
        )
        valuation_values = self._validate_valuation_values(
            analyzed_at=analyzed_at,
            currency=currency,
            estimated_market_value=estimated_market_value,
            expected_resale_value=expected_resale_value,
            asking_price=asking_price,
            ideal_buy_price=ideal_buy_price,
            estimated_gross_profit=estimated_gross_profit,
            estimated_roi=estimated_roi,
            deal_score=deal_score,
            pricing_method=pricing_method,
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inventory_id, internal_id = self._insert_inventory(connection, item_values)
            self._insert_valuation_snapshot(
                connection, internal_id, valuation_values, is_baseline=True
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(inventory_id)

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
        if source is not _UNSET and (not isinstance(source, str) or not source.strip()):
            raise InventoryValidationError("source is required")
        if acquired_at is not _UNSET and acquired_at in (None, ""):
            raise InventoryValidationError("acquired-at must use YYYY-MM-DD")
        if acquisition_cost is not _UNSET and acquisition_cost in (None, ""):
            raise InventoryValidationError("acquisition cost must be a valid USD amount")
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
                "acquisition_cost": (
                    Decimal(row["acquisition_cost_cents"]) / 100
                    if row["acquisition_cost_cents"] is not None
                    else None
                ),
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
            values = self._validate_values(**candidate, allow_unknown_acquisition=True)
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
        item_revenue: Decimal | None = None,
        buyer_shipping: Decimal | None = None,
        marketplace_tax: Decimal | None = None,
        checkout_total: Decimal | None = None,
    ) -> tuple[SaleRecord, bool]:
        """Atomically insert one sale and apply its authoritative sold lifecycle state."""
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
        optional_amounts = []
        for value in (item_revenue, buyer_shipping, marketplace_tax, checkout_total):
            optional_amounts.extend((None, None) if value is None else self._scaled_amount(value))
        (
            item_minor,
            item_scale,
            shipping_minor,
            shipping_scale,
            tax_minor,
            tax_scale,
            total_minor,
            total_scale,
        ) = optional_amounts
        if item_revenue is not None and buyer_shipping is not None:
            if gross_amount != item_revenue + buyer_shipping:
                raise SaleImportError(
                    "seller revenue must equal item revenue plus buyer-paid shipping"
                )
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
                item_minor,
                item_scale,
                shipping_minor,
                shipping_scale,
                tax_minor,
                tax_scale,
                total_minor,
                total_scale,
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
                    existing["item_revenue_minor"],
                    existing["item_revenue_scale"],
                    existing["buyer_shipping_minor"],
                    existing["buyer_shipping_scale"],
                    existing["marketplace_tax_minor"],
                    existing["marketplace_tax_scale"],
                    existing["checkout_total_minor"],
                    existing["checkout_total_scale"],
                    existing["currency"],
                    existing["sold_at"],
                )
                if persisted != immutable:
                    legacy_identity_matches = (
                        existing["inventory_internal_id"],
                        existing["marketplace_sku"],
                        existing["quantity"],
                        existing["currency"],
                        existing["sold_at"],
                    ) == (
                        item["internal_id"],
                        marketplace_sku,
                        quantity,
                        currency,
                        sold_timestamp,
                    )
                    legacy_components_unknown = all(
                        existing[name] is None
                        for name in (
                            "item_revenue_minor",
                            "item_revenue_scale",
                            "buyer_shipping_minor",
                            "buyer_shipping_scale",
                            "marketplace_tax_minor",
                            "marketplace_tax_scale",
                            "checkout_total_minor",
                            "checkout_total_scale",
                        )
                    )
                    existing_gross = Decimal(existing["gross_amount_minor"]).scaleb(
                        -existing["gross_amount_scale"]
                    )
                    if (
                        legacy_identity_matches
                        and legacy_components_unknown
                        and item_revenue is not None
                        and buyer_shipping is not None
                        and existing_gross == item_revenue
                    ):
                        connection.execute(
                            """UPDATE sales SET
                                gross_amount_minor = ?, gross_amount_scale = ?,
                                item_revenue_minor = ?, item_revenue_scale = ?,
                                buyer_shipping_minor = ?, buyer_shipping_scale = ?,
                                marketplace_tax_minor = ?, marketplace_tax_scale = ?,
                                checkout_total_minor = ?, checkout_total_scale = ?
                            WHERE internal_id = ?""",
                            (
                                amount_minor,
                                amount_scale,
                                item_minor,
                                item_scale,
                                shipping_minor,
                                shipping_scale,
                                tax_minor,
                                tax_scale,
                                total_minor,
                                total_scale,
                                existing["internal_id"],
                            ),
                        )
                        enriched = connection.execute(
                            """SELECT sales.*, inventory_items.inventory_id
                            FROM sales JOIN inventory_items
                              ON inventory_items.internal_id = sales.inventory_internal_id
                            WHERE sales.internal_id = ?""",
                            (existing["internal_id"],),
                        ).fetchone()
                        connection.commit()
                        return self._sale_record(enriched), True
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
                    gross_amount_scale, item_revenue_minor, item_revenue_scale,
                    buyer_shipping_minor, buyer_shipping_scale, marketplace_tax_minor,
                    marketplace_tax_scale, checkout_total_minor, checkout_total_scale,
                    currency, sold_at, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    item_minor,
                    item_scale,
                    shipping_minor,
                    shipping_scale,
                    tax_minor,
                    tax_scale,
                    total_minor,
                    total_scale,
                    currency,
                    sold_timestamp,
                    imported_timestamp,
                ),
            )
            if item["status"] == "acquired":
                # A completed marketplace order can be the first durable evidence that an
                # acquired item was listed. Preserve listed_at as unknown rather than
                # inventing a listing timestamp.
                connection.execute(
                    "UPDATE inventory_items SET status = 'sold', sold_at = ? WHERE internal_id = ?",
                    (sold_timestamp, item["internal_id"]),
                )
            else:
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
        effect: str = "reduce",
    ) -> SaleCostRecord:
        """Persist one explicit, nonnegative component that reduces proceeds."""
        if category not in SALE_COST_TYPES:
            raise SaleCostValidationError(f"cost type must be one of: {', '.join(SALE_COST_TYPES)}")
        if source not in SALE_COST_SOURCES:
            raise SaleCostValidationError(
                f"cost source must be one of: {', '.join(SALE_COST_SOURCES)}"
            )
        if effect not in SALE_COMPONENT_EFFECTS:
            raise SaleCostValidationError("effect must be reduce or increase")
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
                    currency, source, note, created_at, effect
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    effect,
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
        effect: str = "reduce",
        related_cost_id: str | None = None,
    ) -> tuple[SaleCostRecord, bool]:
        """Atomically validate and persist one externally identified Finances cost."""
        if category not in SALE_COST_TYPES:
            raise SaleCostValidationError(f"cost type must be one of: {', '.join(SALE_COST_TYPES)}")
        transaction_id = self._required(external_transaction_id, "external transaction ID")
        component_key = self._required(external_component_key, "external component key")
        if effect not in SALE_COMPONENT_EFFECTS:
            raise SaleCostValidationError("effect must be reduce or increase")
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
            related = None
            if related_cost_id is not None:
                related = connection.execute(
                    "SELECT * FROM sale_costs WHERE cost_id = ?", (related_cost_id.upper(),)
                ).fetchone()
                if related is None or related["sale_internal_id"] != sale["internal_id"]:
                    raise SaleCostValidationError("related component must belong to this sale")
                if related["source"] != "ebay_finances" or related["effect"] != "reduce":
                    raise SaleCostValidationError(
                        "reversal must link to an imported reducing component"
                    )
            if effect == "increase" and related is None:
                raise SaleCostValidationError(
                    "imported increasing adjustment requires a relationship"
                )
            existing = connection.execute(
                """SELECT sale_costs.*, sales.sale_id,
                           related.cost_id AS related_cost_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                LEFT JOIN sale_costs AS related
                  ON related.internal_id = sale_costs.related_cost_internal_id
                WHERE sale_costs.source = 'ebay_finances'
                  AND sale_costs.external_transaction_id = ?
                  AND sale_costs.external_component_key = ?""",
                (transaction_id, component_key),
            ).fetchone()
            immutable = (
                sale["internal_id"],
                category,
                amount_minor,
                amount_scale,
                component_currency,
                effect,
                related["internal_id"] if related is not None else None,
            )
            if existing is not None:
                persisted = (
                    existing["sale_internal_id"],
                    existing["category"],
                    existing["amount_minor"],
                    existing["amount_scale"],
                    existing["currency"],
                    existing["effect"],
                    existing["related_cost_internal_id"],
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
                    external_component_key, effect, related_cost_internal_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'ebay_finances', ?, ?, ?, ?, ?, ?)""",
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
                    effect,
                    related["internal_id"] if related is not None else None,
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

    def find_imported_reversal_target(
        self, sale_id: str, *, category: str, external_component_key: str
    ) -> SaleCostRecord | None:
        """Return a unique unreversed imported reducing component, never amount-match."""
        candidates = [
            cost
            for cost in self.list_sale_costs(sale_id)
            if cost.source == "ebay_finances"
            and cost.effect == "reduce"
            and cost.category == category
            and cost.external_component_key == external_component_key
        ]
        reversed_ids = {
            cost.related_cost_id
            for cost in self.list_sale_costs(sale_id)
            if cost.effect == "increase" and cost.related_cost_id is not None
        }
        candidates = [cost for cost in candidates if cost.cost_id not in reversed_ids]
        return candidates[0] if len(candidates) == 1 else None

    def set_reconciliation_confirmation(
        self, sale_id: str, category: str, *, confirmed: bool
    ) -> None:
        if category not in RECONCILIATION_CATEGORIES:
            raise SaleCostValidationError(
                f"reconciliation category must be one of: {', '.join(RECONCILIATION_CATEGORIES)}"
            )
        sale = self.get_sale(sale_id)
        with self._connect() as connection:
            if confirmed:
                connection.execute(
                    "INSERT OR REPLACE INTO sale_reconciliation_confirmations "
                    "(sale_internal_id, category, confirmed_at) VALUES (?, ?, ?)",
                    (sale.internal_id, category, self._utc_now()),
                )
            else:
                connection.execute(
                    "DELETE FROM sale_reconciliation_confirmations "
                    "WHERE sale_internal_id = ? AND category = ?",
                    (sale.internal_id, category),
                )

    def reconciliation_status(self, sale_id: str) -> tuple[str, tuple[str, ...]]:
        sale = self.get_sale(sale_id)
        with self._connect() as connection:
            confirmed = {
                row[0]
                for row in connection.execute(
                    "SELECT category FROM sale_reconciliation_confirmations "
                    "WHERE sale_internal_id = ?",
                    (sale.internal_id,),
                )
            }
        missing = tuple(
            category for category in RECONCILIATION_CATEGORIES if category not in confirmed
        )
        status = (
            "fully_reconciled"
            if not missing
            else ("partially_reconciled" if confirmed else "incomplete")
        )
        return status, missing

    def get_sale_cost(self, cost_id: str) -> SaleCostRecord:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sale_costs.*, sales.sale_id,
                           related.cost_id AS related_cost_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                LEFT JOIN sale_costs AS related
                  ON related.internal_id = sale_costs.related_cost_internal_id
                WHERE sale_costs.cost_id = ?""",
                (cost_id.upper(),),
            ).fetchone()
        if row is None:
            raise SaleCostNotFoundError(f"sale cost {cost_id} was not found")
        return self._sale_cost_record(row)

    def list_sale_costs(self, sale_id: str) -> list[SaleCostRecord]:
        sale = self.get_sale(sale_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sale_costs.*, sales.sale_id,
                           related.cost_id AS related_cost_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                LEFT JOIN sale_costs AS related
                  ON related.internal_id = sale_costs.related_cost_internal_id
                WHERE sale_costs.sale_internal_id = ? ORDER BY sale_costs.internal_id""",
                (sale.internal_id,),
            ).fetchall()
        return [self._sale_cost_record(row) for row in rows]

    def list_all_sale_costs(self) -> list[SaleCostRecord]:
        """Return all recorded sale components in stable order for bulk reporting."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sale_costs.*, sales.sale_id,
                           related.cost_id AS related_cost_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                LEFT JOIN sale_costs AS related
                  ON related.internal_id = sale_costs.related_cost_internal_id
                ORDER BY sale_costs.internal_id"""
            ).fetchall()
        return [self._sale_cost_record(row) for row in rows]

    def list_reconciliation_confirmations(self) -> dict[int, frozenset[str]]:
        """Return confirmed categories keyed by internal sale ID for bulk reporting."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sale_internal_id, category
                FROM sale_reconciliation_confirmations
                ORDER BY sale_internal_id, category"""
            ).fetchall()
        confirmations: dict[int, set[str]] = {}
        for row in rows:
            confirmations.setdefault(row["sale_internal_id"], set()).add(row["category"])
        return {sale_id: frozenset(categories) for sale_id, categories in confirmations.items()}

    def remove_sale_cost(self, cost_id: str) -> SaleCostRecord:
        """Remove an erroneous component without reusing its stable identifier."""
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT sale_costs.*, sales.sale_id,
                           related.cost_id AS related_cost_id
                FROM sale_costs JOIN sales ON sales.internal_id = sale_costs.sale_internal_id
                LEFT JOIN sale_costs AS related
                  ON related.internal_id = sale_costs.related_cost_internal_id
                WHERE sale_costs.cost_id = ?""",
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
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return record

    @staticmethod
    def _valuation_amount(value: Decimal, name: str, *, nonnegative: bool) -> tuple[int, int]:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValuationValidationError(f"{name} must be a finite decimal")
        if nonnegative and value < 0:
            raise ValuationValidationError(f"{name} must be nonnegative")
        normalized = value.normalize()
        scale = max(0, -normalized.as_tuple().exponent)
        if scale > 9:
            raise ValuationValidationError(f"{name} has unsupported precision")
        return int(normalized.scaleb(scale)), scale

    def _validate_valuation_values(
        self,
        *,
        analyzed_at: datetime,
        currency: str,
        estimated_market_value: Decimal,
        expected_resale_value: Decimal,
        asking_price: Decimal,
        ideal_buy_price: Decimal,
        estimated_gross_profit: Decimal,
        estimated_roi: Decimal | None,
        deal_score: int,
        pricing_method: str | None,
    ) -> dict[str, object]:
        try:
            timestamp = self._timestamp(analyzed_at, "analysis timestamp")
        except SaleImportError as exc:
            raise ValuationValidationError(str(exc)) from exc
        currency = self._required(currency, "currency").upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValuationValidationError("currency must be a three-letter code")
        if isinstance(deal_score, bool) or not isinstance(deal_score, int):
            raise ValuationValidationError("deal score must be an integer")
        if estimated_roi is not None and (
            not isinstance(estimated_roi, Decimal) or not estimated_roi.is_finite()
        ):
            raise ValuationValidationError("estimated ROI must be a finite decimal")
        amounts: list[int] = []
        for value, name, nonnegative in (
            (estimated_market_value, "estimated market value", True),
            (expected_resale_value, "expected resale value", True),
            (asking_price, "asking price", True),
            (ideal_buy_price, "ideal buy price", True),
            (estimated_gross_profit, "estimated gross profit", False),
        ):
            amounts.extend(self._valuation_amount(value, name, nonnegative=nonnegative))
        return {
            "timestamp": timestamp,
            "currency": currency,
            "amounts": amounts,
            "estimated_roi": str(estimated_roi) if estimated_roi is not None else None,
            "deal_score": deal_score,
            "pricing_method": self._optional(pricing_method),
        }

    def _insert_valuation_snapshot(
        self,
        connection: sqlite3.Connection,
        inventory_internal_id: int,
        values: dict[str, object],
        *,
        is_baseline: bool,
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO valuation_snapshots (
                inventory_internal_id, analyzed_at, currency,
                estimated_market_value_minor, estimated_market_value_scale,
                expected_resale_value_minor, expected_resale_value_scale,
                asking_price_minor, asking_price_scale,
                ideal_buy_price_minor, ideal_buy_price_scale,
                estimated_gross_profit_minor, estimated_gross_profit_scale,
                estimated_roi, deal_score, pricing_method, is_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                inventory_internal_id,
                values["timestamp"],
                values["currency"],
                *values["amounts"],
                values["estimated_roi"],
                values["deal_score"],
                values["pricing_method"],
                int(is_baseline),
            ),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        return snapshot_id

    def add_valuation_snapshot(
        self,
        inventory_id: str,
        *,
        analyzed_at: datetime,
        currency: str,
        estimated_market_value: Decimal,
        expected_resale_value: Decimal,
        asking_price: Decimal,
        ideal_buy_price: Decimal,
        estimated_gross_profit: Decimal,
        estimated_roi: Decimal | None,
        deal_score: int,
        pricing_method: str | None = None,
    ) -> ValuationSnapshot:
        """Append an immutable snapshot; the first snapshot is the baseline."""
        values = self._validate_valuation_values(
            analyzed_at=analyzed_at,
            currency=currency,
            estimated_market_value=estimated_market_value,
            expected_resale_value=expected_resale_value,
            asking_price=asking_price,
            ideal_buy_price=ideal_buy_price,
            estimated_gross_profit=estimated_gross_profit,
            estimated_roi=estimated_roi,
            deal_score=deal_score,
            pricing_method=pricing_method,
        )
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT internal_id FROM inventory_items WHERE inventory_id = ?",
                (inventory_id.upper(),),
            ).fetchone()
            if item is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            has_snapshot = connection.execute(
                "SELECT 1 FROM valuation_snapshots WHERE inventory_internal_id = ? LIMIT 1",
                (item["internal_id"],),
            ).fetchone()
            snapshot_id = self._insert_valuation_snapshot(
                connection,
                item["internal_id"],
                values,
                is_baseline=has_snapshot is None,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        assert snapshot_id is not None
        return self.get_valuation_snapshot(snapshot_id)

    def get_valuation_snapshot(self, snapshot_id: int) -> ValuationSnapshot:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT valuation_snapshots.*, inventory_items.inventory_id
                FROM valuation_snapshots JOIN inventory_items
                  ON inventory_items.internal_id = valuation_snapshots.inventory_internal_id
                WHERE valuation_snapshots.internal_id = ?""",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ValuationValidationError("valuation snapshot was not found")
        return self._valuation_record(row)

    def list_valuation_snapshots(self, inventory_id: str | None = None) -> list[ValuationSnapshot]:
        self.initialize()
        parameters: tuple[object, ...] = ()
        where = ""
        if inventory_id is not None:
            item = self.get(inventory_id)
            where = "WHERE valuation_snapshots.inventory_internal_id = ?"
            parameters = (item.internal_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT valuation_snapshots.*, inventory_items.inventory_id
                FROM valuation_snapshots JOIN inventory_items
                  ON inventory_items.internal_id = valuation_snapshots.inventory_internal_id
                {where} ORDER BY valuation_snapshots.internal_id""",
                parameters,
            ).fetchall()
        return [self._valuation_record(row) for row in rows]

    def baseline_valuation(self, inventory_id: str) -> ValuationSnapshot | None:
        snapshots = self.list_valuation_snapshots(inventory_id)
        return next((snapshot for snapshot in snapshots if snapshot.is_baseline), None)

    def add_attachment_metadata(
        self,
        inventory_id: str,
        *,
        attachment_id: str,
        original_filename: str,
        stored_filename: str,
        media_type: str,
        byte_size: int,
        category: str,
    ) -> AttachmentRecord:
        """Persist attachment metadata after its Flipper-owned file is finalized."""
        if category not in ATTACHMENT_CATEGORIES:
            raise AttachmentValidationError(
                f"category must be one of: {', '.join(ATTACHMENT_CATEGORIES)}"
            )
        if not attachment_id or not original_filename or not stored_filename or not media_type:
            raise AttachmentValidationError("attachment metadata fields must not be empty")
        if "/" in stored_filename or "\\" in stored_filename:
            raise AttachmentValidationError("stored filename must be a single safe path component")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise AttachmentValidationError("attachment byte size must be a nonnegative integer")
        self.initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT internal_id FROM inventory_items WHERE inventory_id = ?",
                (inventory_id.upper(),),
            ).fetchone()
            if item is None:
                raise InventoryNotFoundError(f"inventory item {inventory_id} was not found")
            connection.execute(
                """INSERT INTO inventory_attachments (
                    attachment_id, inventory_internal_id, original_filename, stored_filename,
                    media_type, byte_size, category, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attachment_id,
                    item["internal_id"],
                    original_filename,
                    stored_filename,
                    media_type,
                    byte_size,
                    category,
                    self._utc_now(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_attachment(attachment_id, inventory_id=inventory_id)

    def get_attachment(
        self, attachment_id: str, *, inventory_id: str | None = None
    ) -> AttachmentRecord:
        self.initialize()
        where = "WHERE inventory_attachments.attachment_id = ?"
        parameters: tuple[object, ...] = (attachment_id,)
        if inventory_id is not None:
            where += " AND inventory_items.inventory_id = ?"
            parameters += (inventory_id.upper(),)
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT inventory_attachments.*, inventory_items.inventory_id
                FROM inventory_attachments JOIN inventory_items
                  ON inventory_items.internal_id = inventory_attachments.inventory_internal_id
                {where}""",
                parameters,
            ).fetchone()
        if row is None:
            raise AttachmentNotFoundError("attachment was not found for that inventory item")
        return self._attachment_record(row)

    def list_attachments(self, inventory_id: str) -> list[AttachmentRecord]:
        item = self.get(inventory_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT inventory_attachments.*, inventory_items.inventory_id
                FROM inventory_attachments JOIN inventory_items
                  ON inventory_items.internal_id = inventory_attachments.inventory_internal_id
                WHERE inventory_attachments.inventory_internal_id = ?
                ORDER BY inventory_attachments.created_at, inventory_attachments.attachment_id""",
                (item.internal_id,),
            ).fetchall()
        return [self._attachment_record(row) for row in rows]

    def attachment_counts(self) -> dict[int, int]:
        """Return attachment counts for all inventory records in one bounded query."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT inventory_internal_id, COUNT(*) AS count "
                "FROM inventory_attachments GROUP BY inventory_internal_id"
            ).fetchall()
        return {row["inventory_internal_id"]: row["count"] for row in rows}

    def remove_attachment_metadata(self, inventory_id: str, attachment_id: str) -> AttachmentRecord:
        """Delete metadata only after the attachment service has secured the file."""
        record = self.get_attachment(attachment_id, inventory_id=inventory_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM inventory_attachments WHERE attachment_id = ? "
                "AND inventory_internal_id = ?",
                (attachment_id, record.inventory_internal_id),
            )
            if cursor.rowcount != 1:
                raise AttachmentNotFoundError("attachment was not found for that inventory item")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return record

    @staticmethod
    def _attachment_record(row: sqlite3.Row) -> AttachmentRecord:
        return AttachmentRecord(
            attachment_id=row["attachment_id"],
            inventory_internal_id=row["inventory_internal_id"],
            inventory_id=row["inventory_id"],
            original_filename=row["original_filename"],
            stored_filename=row["stored_filename"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            category=row["category"],
            created_at=row["created_at"],
        )

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
            effect=row["effect"],
            related_cost_id=row["related_cost_id"],
            external_transaction_id=row["external_transaction_id"],
            external_component_key=row["external_component_key"],
            note=row["note"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _valuation_record(row: sqlite3.Row) -> ValuationSnapshot:
        return ValuationSnapshot(
            internal_id=row["internal_id"],
            inventory_internal_id=row["inventory_internal_id"],
            inventory_id=row["inventory_id"],
            analyzed_at=row["analyzed_at"],
            currency=row["currency"],
            estimated_market_value_minor=row["estimated_market_value_minor"],
            estimated_market_value_scale=row["estimated_market_value_scale"],
            expected_resale_value_minor=row["expected_resale_value_minor"],
            expected_resale_value_scale=row["expected_resale_value_scale"],
            asking_price_minor=row["asking_price_minor"],
            asking_price_scale=row["asking_price_scale"],
            ideal_buy_price_minor=row["ideal_buy_price_minor"],
            ideal_buy_price_scale=row["ideal_buy_price_scale"],
            estimated_gross_profit_minor=row["estimated_gross_profit_minor"],
            estimated_gross_profit_scale=row["estimated_gross_profit_scale"],
            estimated_roi=(Decimal(row["estimated_roi"]) if row["estimated_roi"] else None),
            deal_score=row["deal_score"],
            pricing_method=row["pricing_method"],
            is_baseline=bool(row["is_baseline"]),
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
            item_revenue_minor=row["item_revenue_minor"],
            item_revenue_scale=row["item_revenue_scale"],
            buyer_shipping_minor=row["buyer_shipping_minor"],
            buyer_shipping_scale=row["buyer_shipping_scale"],
            marketplace_tax_minor=row["marketplace_tax_minor"],
            marketplace_tax_scale=row["marketplace_tax_scale"],
            checkout_total_minor=row["checkout_total_minor"],
            checkout_total_scale=row["checkout_total_scale"],
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

    def save_research_snapshot(
        self,
        *,
        opportunity_identity: str,
        source: str,
        title: str,
        category: str,
        currency: str,
        asking_price: Decimal,
        expected_resale: Decimal | None,
        expected_profit: Decimal | None,
        payload: dict,
        save_token: str,
    ) -> ResearchSnapshotRecord:
        """Atomically persist one validated historical copy; retry tokens are idempotent."""
        self.initialize()
        if len(save_token) != 32 or any(char not in "0123456789abcdef" for char in save_token):
            raise ResearchSnapshotValidationError("save token is invalid")
        for value, name, limit in (
            (opportunity_identity, "opportunity identity", 500),
            (source, "source", 100),
            (title, "title", 300),
            (category, "category", 100),
        ):
            if not value.strip() or len(value) > limit:
                raise ResearchSnapshotValidationError(f"research snapshot {name} is invalid")
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ResearchSnapshotValidationError("research snapshot currency is invalid")
        for amount, name in (
            (asking_price, "asking price"),
            (expected_resale, "expected resale"),
            (expected_profit, "expected profit"),
        ):
            if amount is not None and (
                not amount.is_finite() or (name != "expected profit" and amount < 0)
            ):
                raise ResearchSnapshotValidationError(f"research snapshot {name} is invalid")
        try:
            validate_snapshot_payload(payload)
            payload_json = canonical_snapshot_json(payload)
        except ValueError as exc:
            raise ResearchSnapshotValidationError(str(exc)) from exc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT research_snapshots.*, inventory_items.inventory_id "
                "FROM research_snapshots LEFT JOIN inventory_items "
                "ON inventory_items.internal_id = "
                "research_snapshots.inventory_internal_id WHERE save_token = ?",
                (save_token,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["opportunity_identity"] != opportunity_identity
                    or existing["payload_json"] != payload_json
                ):
                    raise ResearchSnapshotValidationError(
                        "save token was already used for different research"
                    )
                connection.commit()
                return self._research_snapshot_record(existing)
            snapshot_id = uuid4().hex
            saved_at = self._utc_now()
            connection.execute(
                """INSERT INTO research_snapshots (
                    snapshot_id, saved_at, opportunity_identity, source, title, category,
                    currency, asking_price, expected_resale, expected_profit, save_token,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    saved_at,
                    opportunity_identity,
                    source,
                    title,
                    category,
                    currency,
                    str(asking_price),
                    str(expected_resale) if expected_resale is not None else None,
                    str(expected_profit) if expected_profit is not None else None,
                    save_token,
                    payload_json,
                ),
            )
            row = connection.execute(
                "SELECT research_snapshots.*, inventory_items.inventory_id "
                "FROM research_snapshots LEFT JOIN inventory_items "
                "ON inventory_items.internal_id = "
                "research_snapshots.inventory_internal_id WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            connection.commit()
            return self._research_snapshot_record(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_research_snapshot(self, snapshot_id: str) -> ResearchSnapshotRecord:
        self.initialize()
        if len(snapshot_id) != 32 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise ResearchSnapshotNotFoundError("research snapshot was not found")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT research_snapshots.*, inventory_items.inventory_id "
                "FROM research_snapshots LEFT JOIN inventory_items "
                "ON inventory_items.internal_id = "
                "research_snapshots.inventory_internal_id WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ResearchSnapshotNotFoundError("research snapshot was not found")
        return self._research_snapshot_record(row)

    def list_research_snapshots(self) -> list[ResearchSnapshotRecord]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT research_snapshots.*, inventory_items.inventory_id "
                "FROM research_snapshots LEFT JOIN inventory_items "
                "ON inventory_items.internal_id = "
                "research_snapshots.inventory_internal_id ORDER BY saved_at DESC, "
                "research_snapshots.internal_id DESC"
            ).fetchall()
        return [self._research_snapshot_record(row, include_payload=False) for row in rows]

    def latest_research_snapshots_by_opportunity(
        self, opportunity_identities: tuple[str, ...]
    ) -> dict[str, ResearchSnapshotRecord]:
        """Return the newest immutable snapshot for each requested bounded identity."""
        self.initialize()
        identities = tuple(dict.fromkeys(opportunity_identities))
        if not identities:
            return {}
        if len(identities) > 50:
            raise ValueError("at most 50 opportunity identities may be requested")
        placeholders = ",".join("?" for _ in identities)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT research_snapshots.*, inventory_items.inventory_id "
                "FROM research_snapshots LEFT JOIN inventory_items "
                "ON inventory_items.internal_id = research_snapshots.inventory_internal_id "
                f"WHERE opportunity_identity IN ({placeholders}) "
                "ORDER BY saved_at DESC, research_snapshots.internal_id DESC",
                identities,
            ).fetchall()
        latest: dict[str, ResearchSnapshotRecord] = {}
        for row in rows:
            identity = row["opportunity_identity"]
            if identity not in latest:
                latest[identity] = self._research_snapshot_record(row)
        return latest

    def link_research_snapshot(self, snapshot_id: str, inventory_id: str) -> ResearchSnapshotRecord:
        snapshot = self.get_research_snapshot(snapshot_id)
        item = self.get(inventory_id)
        if snapshot.inventory_id and snapshot.inventory_id != item.inventory_id:
            raise ResearchSnapshotValidationError("research snapshot is already linked")
        with self._connect() as connection:
            connection.execute(
                "UPDATE research_snapshots SET inventory_internal_id = ? WHERE snapshot_id = ?",
                (item.internal_id, snapshot.snapshot_id),
            )
        return self.get_research_snapshot(snapshot.snapshot_id)

    @staticmethod
    def _travel_decimal(value: str | Decimal | None, name: str) -> tuple[int, int] | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            amount = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SourcingTravelValidationError(f"{name} must be a valid number") from exc
        if not amount.is_finite() or amount < 0:
            raise SourcingTravelValidationError(f"{name} must be nonnegative")
        normalized = amount.normalize()
        scale = max(0, -normalized.as_tuple().exponent)
        if scale > 9:
            raise SourcingTravelValidationError(f"{name} has unsupported precision")
        return int(normalized.scaleb(scale)), scale

    def set_sourcing_travel(
        self,
        inventory_id: str,
        *,
        round_trip_miles: str | Decimal | None = None,
        fuel_cost: str | Decimal | None = None,
        additional_expense: str | Decimal | None = None,
        travel_minutes: str | int | None = None,
        note: str = "",
    ) -> SourcingTravelRecord:
        """Create or replace the authoritative actual travel facts for a Q item."""
        item = self.get(inventory_id)
        miles = self._travel_decimal(round_trip_miles, "round-trip miles")
        fuel = self._travel_decimal(fuel_cost, "fuel expense")
        additional = self._travel_decimal(additional_expense, "additional travel expense")
        minutes = None
        if travel_minutes is not None and str(travel_minutes).strip():
            try:
                minutes = int(str(travel_minutes))
            except ValueError as exc:
                raise SourcingTravelValidationError(
                    "travel minutes must be a whole number"
                ) from exc
            if minutes < 0 or str(minutes) != str(travel_minutes).strip():
                raise SourcingTravelValidationError(
                    "travel minutes must be a nonnegative whole number"
                )
        cleaned_note = note.strip()
        if len(cleaned_note) > 2000:
            raise SourcingTravelValidationError("travel note is too long")
        if all(value is None for value in (miles, fuel, additional, minutes)) and not cleaned_note:
            raise SourcingTravelValidationError("record at least one actual travel fact")
        values = [part for pair in (miles, fuel, additional) for part in (pair or (None, None))]
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sourcing_travel (
                    inventory_internal_id, round_trip_miles_minor, round_trip_miles_scale,
                    fuel_cost_minor, fuel_cost_scale, additional_expense_minor,
                    additional_expense_scale, travel_minutes, note, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inventory_internal_id) DO UPDATE SET
                    round_trip_miles_minor=excluded.round_trip_miles_minor,
                    round_trip_miles_scale=excluded.round_trip_miles_scale,
                    fuel_cost_minor=excluded.fuel_cost_minor,
                    fuel_cost_scale=excluded.fuel_cost_scale,
                    additional_expense_minor=excluded.additional_expense_minor,
                    additional_expense_scale=excluded.additional_expense_scale,
                    travel_minutes=excluded.travel_minutes, note=excluded.note,
                    updated_at=excluded.updated_at""",
                (item.internal_id, *values, minutes, cleaned_note, self._utc_now()),
            )
        return self.get_sourcing_travel(item.inventory_id)  # type: ignore[return-value]

    def get_sourcing_travel(self, inventory_id: str) -> SourcingTravelRecord | None:
        item = self.get(inventory_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sourcing_travel.*, inventory_items.inventory_id FROM sourcing_travel "
                "JOIN inventory_items ON inventory_items.internal_id = "
                "sourcing_travel.inventory_internal_id WHERE inventory_internal_id = ?",
                (item.internal_id,),
            ).fetchone()
        return self._sourcing_travel_record(row) if row else None

    def list_sourcing_travel(self) -> list[SourcingTravelRecord]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sourcing_travel.*, inventory_items.inventory_id FROM sourcing_travel "
                "JOIN inventory_items ON inventory_items.internal_id = "
                "sourcing_travel.inventory_internal_id ORDER BY inventory_internal_id"
            ).fetchall()
        return [self._sourcing_travel_record(row) for row in rows]

    def clear_sourcing_travel(self, inventory_id: str) -> SourcingTravelRecord:
        record = self.get_sourcing_travel(inventory_id)
        if record is None:
            raise SourcingTravelValidationError("actual sourcing travel was not found")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sourcing_travel WHERE inventory_internal_id = ?",
                (record.inventory_internal_id,),
            )
        return record

    @staticmethod
    def _sourcing_travel_record(row: sqlite3.Row) -> SourcingTravelRecord:
        def amount(prefix: str) -> Decimal | None:
            minor = row[f"{prefix}_minor"]
            return None if minor is None else Decimal(minor).scaleb(-row[f"{prefix}_scale"])

        return SourcingTravelRecord(
            row["inventory_internal_id"],
            row["inventory_id"],
            amount("round_trip_miles"),
            amount("fuel_cost"),
            amount("additional_expense"),
            row["travel_minutes"],
            row["note"],
            row["updated_at"],
        )

    @staticmethod
    def _research_snapshot_record(
        row: sqlite3.Row, *, include_payload: bool = True
    ) -> ResearchSnapshotRecord:
        return ResearchSnapshotRecord(
            internal_id=row["internal_id"],
            snapshot_id=row["snapshot_id"],
            saved_at=row["saved_at"],
            opportunity_identity=row["opportunity_identity"],
            source=row["source"],
            title=row["title"],
            category=row["category"],
            currency=row["currency"],
            asking_price=Decimal(row["asking_price"]),
            expected_resale=Decimal(row["expected_resale"])
            if row["expected_resale"] is not None
            else None,
            expected_profit=Decimal(row["expected_profit"])
            if row["expected_profit"] is not None
            else None,
            inventory_id=row["inventory_id"],
            payload=parse_snapshot_json(row["payload_json"]) if include_payload else None,
        )

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
