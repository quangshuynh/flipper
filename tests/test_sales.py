import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import ebay.sale_import as sale_import_module
from ebay.orders import EbayOrder, EbayOrderLineItem, Money, OrderPricingSummary
from ebay.reconciliation import ReconciliationResult, ReconciliationStatus
from ebay.sale_import import SaleImportStatus, import_ebay_sales
from inventory.store import InventoryStore, SaleConflictError, SaleImportError


SOLD_AT = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)
IMPORTED_AT = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def inventory(store, sku="Q0001", **overrides):
    values = {
        "title": f"Item {sku}",
        "source": "sale",
        "acquired_at": "2026-09-01",
        "acquisition_cost": "10.00",
        "marketplace": "eBay",
        "marketplace_sku": sku,
    }
    values.update(overrides)
    record = store.add(**values)
    if record.status == "acquired":
        record = store.transition_status(record.inventory_id, "listed")
    return record


def line(line_id="line-1", sku="Q0001", quantity=1, amount="42.99", currency="USD"):
    money = None if amount is None else Money(Decimal(amount), currency)
    return EbayOrderLineItem(line_id, f"item-{line_id}", f"Item {sku}", sku, quantity, money)


def order(*lines, order_id="order-1"):
    return EbayOrder(
        order_id,
        SOLD_AT,
        None,
        "FULFILLED",
        "PAID",
        None,
        tuple(lines),
        OrderPricingSummary(shipping=Money(Decimal("0"), "USD")),
    )


def test_matched_line_imports_exact_money_currency_and_transitions_inventory(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db", clock=lambda: IMPORTED_AT)
    item = inventory(store)

    result = import_ebay_sales([order(line(amount="42.990"))], store)[0]

    assert result.status is SaleImportStatus.IMPORTED
    assert result.sale is not None
    assert result.sale.inventory_internal_id == item.internal_id
    assert result.sale.inventory_id == "Q0001"
    assert result.sale.gross_amount == Decimal("42.99")
    assert result.sale.currency == "USD"
    assert result.sale.sold_at == "2026-09-15T12:34:56Z"
    assert result.sale.imported_at == "2026-09-16T10:00:00Z"
    sold = store.get("Q0001")
    assert (sold.status, sold.sold_at) == ("sold", result.sale.sold_at)


def test_matched_sale_imports_from_acquired_without_inventing_listing_timestamp(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db", clock=lambda: IMPORTED_AT)
    item = store.add(
        title="Item Q0001",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )

    result = import_ebay_sales([order(line(amount="75.00"))], store)[0]

    assert item.status == "acquired"
    assert result.status is SaleImportStatus.IMPORTED
    sold = store.get("Q0001")
    assert sold.status == "sold"
    assert sold.listed_at is None
    assert sold.sold_at == "2026-09-15T12:34:56Z"
    assert result.sale is not None
    assert result.sale.gross_amount == Decimal("75.00")


def test_repeat_import_is_idempotent_and_conflicting_economics_are_explicit(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    first = import_ebay_sales([order(line(amount="10.50"))], store)[0]
    repeated = import_ebay_sales([order(line(amount="10.500"))], store)[0]
    conflict = import_ebay_sales([order(line(amount="10.51"))], store)[0]

    assert first.status is SaleImportStatus.IMPORTED
    assert repeated.status is SaleImportStatus.ALREADY_IMPORTED
    assert repeated.sale == first.sale
    assert conflict.status is SaleImportStatus.CONFLICT
    assert len(store.list_sales()) == 1


def test_persistence_unique_identity_rejects_duplicate_rows(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    sale = import_ebay_sales([order(line())], store)[0].sale
    assert sale is not None
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO sales (
                sale_id, inventory_internal_id, marketplace, external_order_id,
                external_line_item_id, marketplace_sku, quantity, gross_amount_minor,
                gross_amount_scale, currency, sold_at, imported_at
            ) SELECT 'S999999', inventory_internal_id, marketplace, external_order_id,
                     external_line_item_id, marketplace_sku, quantity, gross_amount_minor,
                     gross_amount_scale, currency, sold_at, imported_at FROM sales"""
        )


def test_sale_insert_and_inventory_transition_are_atomic(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_sale_transition BEFORE UPDATE ON inventory_items "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.import_sale(
            inventory_id="Q0001",
            marketplace="eBay",
            external_order_id="order-1",
            external_line_item_id="line-1",
            marketplace_sku="Q0001",
            quantity=1,
            gross_amount=Decimal("42.99"),
            currency="USD",
            sold_at=SOLD_AT,
        )

    assert store.list_sales() == []
    assert store.get("Q0001").status == "listed"


@pytest.mark.parametrize(
    ("order_line", "expected"),
    [
        (line(sku="missing"), SaleImportStatus.UNMATCHED),
        (line(sku=None), SaleImportStatus.MISSING_SKU),
        (line(amount=None), SaleImportStatus.MISSING_GROSS),
        (line(quantity=2), SaleImportStatus.UNSUPPORTED_QUANTITY),
    ],
)
def test_ineligible_lines_are_not_imported(tmp_path, order_line, expected):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    result = import_ebay_sales([order(order_line)], store)[0]
    assert result.status is expected
    assert store.list_sales() == []
    assert store.get("Q0001").status == "listed"


def test_local_multi_quantity_is_conservatively_unsupported(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store, quantity=2)
    result = import_ebay_sales([order(line())], store)[0]
    assert result.status is SaleImportStatus.UNSUPPORTED_QUANTITY
    assert store.list_sales() == []


def test_ambiguous_reconciliation_is_not_imported(monkeypatch, tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = inventory(store)
    duplicate = replace(first, internal_id=99, inventory_id="Q0099")
    order_line = line()
    ambiguous = ReconciliationResult(
        "order-1",
        SOLD_AT,
        order_line,
        ReconciliationStatus.AMBIGUOUS,
        (first, duplicate),
    )
    monkeypatch.setattr(
        sale_import_module, "reconcile_ebay_orders", lambda orders, records: [ambiguous]
    )

    result = import_ebay_sales([order(order_line)], store)[0]

    assert result.status is SaleImportStatus.AMBIGUOUS
    assert store.list_sales() == []
    assert store.get("Q0001").status == "listed"


def test_incompatible_lifecycle_is_not_forced(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = store.add(
        title="Item",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="1",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    item = store.transition_status(item.inventory_id, "archived")
    result = import_ebay_sales([order(line())], store)[0]
    assert item.status == "archived"
    assert result.status is SaleImportStatus.INCOMPATIBLE_INVENTORY
    assert store.get("Q0001").status == "archived"
    assert store.list_sales() == []


def test_multiple_lines_import_deterministically(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store, "A")
    inventory(store, "B")
    results = import_ebay_sales(
        [order(line("line-b", "B", amount="20"), line("line-a", "A", amount="10"))], store
    )
    assert [result.status for result in results] == [
        SaleImportStatus.IMPORTED,
        SaleImportStatus.IMPORTED,
    ]
    assert [sale.marketplace_sku for sale in store.list_sales()] == ["B", "A"]


def test_sales_schema_and_values_exclude_customer_pii(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    import_ebay_sales([order(line())], store)
    with sqlite3.connect(store.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sales)")}
        persisted = repr(connection.execute("SELECT * FROM sales").fetchall()).lower()
    assert not columns & {"buyer", "username", "email", "address", "phone", "recipient"}
    assert "buyer" not in persisted


def test_direct_conflict_raises_without_overwriting(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    kwargs = {
        "inventory_id": "Q0001",
        "marketplace": "eBay",
        "external_order_id": "order-1",
        "external_line_item_id": "line-1",
        "marketplace_sku": "Q0001",
        "quantity": 1,
        "gross_amount": Decimal("42.99"),
        "currency": "EUR",
        "sold_at": SOLD_AT,
    }
    store.import_sale(**kwargs)
    with pytest.raises(SaleConflictError):
        store.import_sale(**{**kwargs, "currency": "USD"})
    assert store.list_sales()[0].currency == "EUR"


def test_v3_database_migrates_to_sales_schema(tmp_path):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER sale_cost_currency_matches_sale")
        connection.execute("DROP TRIGGER sale_cost_external_identity_insert")
        connection.execute("DROP TRIGGER sale_cost_external_identity_update")
        connection.execute("DROP TRIGGER sale_cost_relationship_insert")
        connection.execute("DROP INDEX sale_costs_external_identity")
        connection.execute("DROP INDEX sale_costs_one_external_reversal")
        connection.execute("DROP TABLE sale_reconciliation_confirmations")
        connection.execute("DROP TABLE sale_costs")
        connection.execute("DROP TABLE sale_cost_id_sequence")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("DROP TABLE sales")
        connection.execute("DROP TABLE sale_id_sequence")
    store.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sales'"
        ).fetchone() == ("sales",)


def test_s000001_shipping_revenue_tax_and_fee_semantics(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    ebay_order = EbayOrder(
        **{
            **order(line(amount="75.00")).__dict__,
            "pricing": OrderPricingSummary(
                subtotal=Money(Decimal("75.00"), "USD"),
                shipping=Money(Decimal("8.07"), "USD"),
                tax=Money(Decimal("3.98"), "USD"),
                total=Money(Decimal("87.05"), "USD"),
            ),
        }
    )
    first = import_ebay_sales([ebay_order], store)[0]
    repeated = import_ebay_sales([ebay_order], store)[0]
    assert first.status is SaleImportStatus.IMPORTED
    assert repeated.status is SaleImportStatus.ALREADY_IMPORTED
    assert first.sale is not None
    assert first.sale.item_revenue == Decimal("75")
    assert first.sale.buyer_shipping == Decimal("8.07")
    assert first.sale.gross_amount == Decimal("83.07")
    assert first.sale.marketplace_tax == Decimal("3.98")
    assert first.sale.checkout_total == Decimal("87.05")
    assert store.reconciliation_status(first.sale.sale_id)[0] == "incomplete"
    fee = store.add_sale_cost(
        first.sale.sale_id, category="marketplace_fee", amount=Decimal("12.24")
    )
    assert first.sale.gross_amount - fee.amount == Decimal("70.83")
    from reports.service import sales_report

    row = sales_report(store.list(), store.list_sales(), store.list_all_sale_costs(), {}).rows[0]
    assert row.economics is not None
    assert row.economics.recorded_profit == Decimal("60.83")
    assert row.reconciliation_state == "incomplete"
    assert "shipping" in row.missing_categories


def test_explicit_reimport_enriches_matching_legacy_item_only_sale(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    legacy, _ = store.import_sale(
        inventory_id="Q0001",
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("75"),
        currency="USD",
        sold_at=SOLD_AT,
    )
    ebay_order = EbayOrder(
        **{
            **order(line(amount="75")).__dict__,
            "pricing": OrderPricingSummary(
                shipping=Money(Decimal("8.07"), "USD"),
                tax=Money(Decimal("3.98"), "USD"),
                total=Money(Decimal("87.05"), "USD"),
            ),
        }
    )

    enriched = import_ebay_sales([ebay_order], store)[0]
    repeated = import_ebay_sales([ebay_order], store)[0]

    assert enriched.status is SaleImportStatus.IMPORTED
    assert enriched.sale is not None
    assert enriched.sale.sale_id == legacy.sale_id
    assert enriched.sale.gross_amount == Decimal("83.07")
    assert enriched.sale.buyer_shipping == Decimal("8.07")
    assert repeated.status is SaleImportStatus.ALREADY_IMPORTED


def test_unknown_buyer_shipping_is_not_treated_as_known_zero(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    unknown = EbayOrder(**{**order(line()).__dict__, "pricing": OrderPricingSummary(shipping=None)})
    result = import_ebay_sales([unknown], store)[0]
    assert result.status is SaleImportStatus.MISSING_SELLER_REVENUE
    assert store.list_sales() == []


def test_v13_sale_migrates_with_revenue_components_unknown(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    sale, _ = store.import_sale(
        inventory_id="Q0001",
        marketplace="eBay",
        external_order_id="legacy-order",
        external_line_item_id="legacy-line",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("75"),
        currency="USD",
        sold_at=SOLD_AT,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
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
            connection.execute(f"ALTER TABLE sales DROP COLUMN {name}")
    store.initialize()
    migrated = store.get_sale(sale.sale_id)
    assert migrated.gross_amount == Decimal("75")
    assert migrated.item_revenue is None
    assert migrated.buyer_shipping is None
    assert migrated.marketplace_tax is None


def test_amount_precision_and_quantity_validation_are_safe(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    inventory(store)
    with pytest.raises(SaleImportError, match="quantity 1"):
        store.import_sale(
            inventory_id="Q0001",
            marketplace="eBay",
            external_order_id="o",
            external_line_item_id="l",
            marketplace_sku="Q0001",
            quantity=2,
            gross_amount=Decimal("1"),
            currency="USD",
            sold_at=SOLD_AT,
        )
