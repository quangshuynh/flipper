import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import main
import pytest
from inventory.store import (
    InventoryStore,
    SaleCostNotFoundError,
    SaleCostValidationError,
    SaleNotFoundError,
)
from sales.economics import EconomicComponent, calculate_sale_economics


SOLD_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)


def sold_inventory(store: InventoryStore, *, currency: str = "USD"):
    store.add(
        title="Test computer",
        source="estate sale",
        acquired_at="2026-09-01",
        acquisition_cost="15.00",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    store.transition_status("Q0001", "listed")
    return store.import_sale(
        inventory_id="Q0001",
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("72.00"),
        currency=currency,
        sold_at=SOLD_AT,
    )[0]


def test_domain_calculates_exact_recorded_profit_and_aggregates_components():
    economics = calculate_sale_economics(
        gross=Decimal("72.00"),
        acquisition_cost=Decimal("15.00"),
        currency="USD",
        acquisition_currency="USD",
        components=[
            EconomicComponent("marketplace_fee", Decimal("10.44"), "USD"),
            EconomicComponent("marketplace_fee", Decimal("0.01"), "USD"),
            EconomicComponent("shipping_cost", Decimal("7.25"), "USD"),
            EconomicComponent("refund", Decimal("5.00"), "USD"),
            EconomicComponent("other_adjustment", Decimal("0.30"), "USD"),
        ],
    )
    assert economics.components_by_category == {
        "marketplace_fee": Decimal("10.45"),
        "shipping_cost": Decimal("7.25"),
        "refund": Decimal("5.00"),
        "other_adjustment": Decimal("0.30"),
    }
    assert economics.recorded_profit == Decimal("34.00")


def test_domain_rejects_currency_mismatch_and_negative_amount():
    with pytest.raises(ValueError, match="acquisition currency"):
        calculate_sale_economics(
            gross=Decimal("10"),
            acquisition_cost=Decimal("1"),
            currency="EUR",
            acquisition_currency="USD",
            components=[],
        )
    with pytest.raises(ValueError, match="nonnegative"):
        calculate_sale_economics(
            gross=Decimal("10"),
            acquisition_cost=Decimal("1"),
            currency="USD",
            acquisition_currency="USD",
            components=[EconomicComponent("refund", Decimal("-1"), "USD")],
        )


def test_v4_migration_preserves_sale_and_starts_with_zero_components(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sale = sold_inventory(store)
    with sqlite3.connect(store.path) as connection:
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

    store.initialize()

    assert store.get_sale(sale.sale_id) == sale
    assert store.list_sale_costs(sale.sale_id) == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,)]


@pytest.mark.parametrize(
    "category", ["marketplace_fee", "shipping_cost", "refund", "other_adjustment"]
)
def test_cost_categories_persist_exact_money_and_provenance(tmp_path, category):
    store = InventoryStore(tmp_path / "inventory.db")
    sale = sold_inventory(store)
    cost = store.add_sale_cost(
        sale.sale_id, category=category, amount=Decimal("10.440"), note="manual entry"
    )
    assert cost.amount == Decimal("10.44")
    assert cost.currency == "USD"
    assert cost.source == "manual"
    assert cost.note == "manual entry"
    assert store.get_sale(sale.sale_id).gross_amount == Decimal("72")


def test_cost_rejects_invalid_values_unknown_sale_and_currency_mismatch(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sold_inventory(store)
    with pytest.raises(SaleCostValidationError, match="nonnegative"):
        store.add_sale_cost("S000001", category="refund", amount=Decimal("-1"))
    with pytest.raises(SaleCostValidationError, match="cost type"):
        store.add_sale_cost("S000001", category="credit", amount=Decimal("1"))
    with pytest.raises(SaleCostValidationError, match="does not match"):
        store.add_sale_cost(
            "S000001", category="shipping_cost", amount=Decimal("1"), currency="EUR"
        )
    with pytest.raises(SaleNotFoundError):
        store.add_sale_cost("S999999", category="refund", amount=Decimal("1"))


def test_sqlite_constraint_rejects_mismatched_component_currency(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sale = sold_inventory(store)
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="currency"),
    ):
        connection.execute(
            """INSERT INTO sale_costs (
                cost_id, sale_internal_id, category, amount_minor, amount_scale,
                currency, source, note, created_at
            ) VALUES ('C999999', ?, 'refund', 1, 0, 'EUR', 'manual', '',
                      '2026-09-16T00:00:00Z')""",
            (sale.internal_id,),
        )


def test_remove_cost_corrects_mistake_without_reusing_identifier(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sold_inventory(store)
    first = store.add_sale_cost("S000001", category="refund", amount=Decimal("5"))
    assert store.remove_sale_cost(first.cost_id) == first
    with pytest.raises(SaleCostNotFoundError):
        store.remove_sale_cost(first.cost_id)
    second = store.add_sale_cost("S000001", category="refund", amount=Decimal("4"))
    assert second.cost_id == "C000002"


def test_cli_add_show_and_remove_cost(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    sold_inventory(InventoryStore(database))
    base = ["sales", "--database", str(database)]

    assert (
        main.main([*base, "add-cost", "S000001", "--type", "marketplace_fee", "--amount", "10.44"])
        == 0
    )
    assert "C000001" in capsys.readouterr().out
    assert (
        main.main([*base, "add-cost", "S000001", "--type", "shipping_cost", "--amount", "7.25"])
        == 0
    )
    capsys.readouterr()
    assert main.main([*base, "show", "S000001"]) == 0
    shown = capsys.readouterr().out
    assert "Gross sale: USD 72.00" in shown
    assert "Acquisition cost: -USD 15.00" in shown
    assert "Marketplace fees: -USD 10.44" in shown
    assert "Shipping: -USD 7.25" in shown
    assert "Refunds: -USD 0.00" in shown
    assert "Recorded realized profit: USD 39.31" in shown
    assert "Reconciliation: incomplete" in shown
    assert main.main([*base, "remove-cost", "C000001"]) == 0
    assert "Removed C000001" in capsys.readouterr().out


def test_show_non_usd_sale_does_not_claim_profit(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    sold_inventory(InventoryStore(database), currency="EUR")
    assert main.main(["sales", "--database", str(database), "show", "S000001"]) == 0
    shown = capsys.readouterr().out
    assert "Recorded profit: unavailable" in shown
    assert "Recorded realized profit" not in shown


def test_manual_increasing_adjustment_and_reconciliation_correction(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    sold_inventory(store)
    credit = store.add_sale_cost(
        "S000001", category="other_adjustment", amount=Decimal("2.50"), effect="increase"
    )
    assert credit.amount == Decimal("2.5")
    assert credit.effect == "increase"
    assert store.reconciliation_status("S000001") == (
        "incomplete",
        ("fees", "shipping", "refunds", "adjustments"),
    )
    base = ["sales", "--database", str(database)]
    for category in ("fees", "shipping", "refunds", "adjustments"):
        assert main.main([*base, "confirm", "S000001", "--category", category]) == 0
    assert store.reconciliation_status("S000001") == ("fully_reconciled", ())
    assert main.main([*base, "unconfirm", "S000001", "--category", "shipping"]) == 0
    assert store.reconciliation_status("S000001") == ("partially_reconciled", ("shipping",))
    capsys.readouterr()
    assert main.main([*base, "show", "S000001"]) == 0
    shown = capsys.readouterr().out
    assert "Other adjustments credits: +USD 2.50" in shown
    assert "Missing/unconfirmed: shipping" in shown


def test_v6_migration_preserves_reducing_economics_without_inventing_confirmation(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sold_inventory(store)
    old = store.add_sale_cost("S000001", category="shipping_cost", amount=Decimal("3.25"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER sale_cost_relationship_insert")
        connection.execute("DROP INDEX sale_costs_one_external_reversal")
        connection.execute("DROP TABLE sale_reconciliation_confirmations")
        connection.execute("ALTER TABLE sale_costs DROP COLUMN related_cost_internal_id")
        connection.execute("ALTER TABLE sale_costs DROP COLUMN effect")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
    migrated = store.get_sale_cost(old.cost_id)
    assert migrated.effect == "reduce"
    assert migrated.related_cost_id is None
    assert store.reconciliation_status("S000001")[0] == "incomplete"
