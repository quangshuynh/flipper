import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

import main
from inventory.store import InventoryNotFoundError, InventoryStore
from reports.service import valuation_accuracy_report
from valuations import compare_valuation_to_sale
from web.app import app


NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def _item(store, number=1):
    return store.add(
        title=f"Synthetic computer {number}",
        source="synthetic",
        acquired_at="2026-08-01",
        acquisition_cost="40.00",
        marketplace="eBay",
        marketplace_sku=f"Q-{number}",
    )


def _valuation(store, inventory_id, **overrides):
    values = {
        "analyzed_at": NOW,
        "currency": "USD",
        "estimated_market_value": Decimal("120.25"),
        "expected_resale_value": Decimal("100.10"),
        "asking_price": Decimal("45.55"),
        "ideal_buy_price": Decimal("60.125"),
        "estimated_gross_profit": Decimal("54.55"),
        "estimated_roi": Decimal("1.1976"),
        "deal_score": 78,
        "pricing_method": "component-estimator",
    }
    values.update(overrides)
    return store.add_valuation_snapshot(inventory_id, **values)


def _sell(store, item, *, gross="110.10", currency="USD"):
    store.transition_status(item.inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id=f"order-{item.inventory_id}",
        external_line_item_id="line-1",
        marketplace_sku=item.marketplace_sku,
        quantity=1,
        gross_amount=Decimal(gross),
        currency=currency,
        sold_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    return sale


def test_inventory_without_valuation_and_no_fuzzy_linkage(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    assert store.baseline_valuation(item.inventory_id) is None
    try:
        _valuation(store, "Q9999")
    except InventoryNotFoundError:
        pass
    else:
        raise AssertionError("unknown Q-number must not be guessed")


def test_exact_snapshot_baseline_is_historical_and_additional_is_append_only(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    baseline = _valuation(store, item.inventory_id)
    later = _valuation(
        store,
        item.inventory_id,
        analyzed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        expected_resale_value=Decimal("80.01"),
    )
    snapshots = store.list_valuation_snapshots(item.inventory_id)
    assert baseline.expected_resale_value == Decimal("100.1")
    assert baseline.ideal_buy_price == Decimal("60.125")
    assert baseline.is_baseline is True
    assert later.is_baseline is False
    assert store.baseline_valuation(item.inventory_id) == baseline
    assert [value.expected_resale_value for value in snapshots] == [
        Decimal("100.1"),
        Decimal("80.01"),
    ]
    with sqlite3.connect(store.path) as connection:
        try:
            connection.execute(
                "UPDATE valuation_snapshots SET deal_score = 1 WHERE internal_id = ?",
                (baseline.internal_id,),
            )
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("snapshot update must be rejected")
        try:
            connection.execute(
                "DELETE FROM valuation_snapshots WHERE internal_id = ?", (baseline.internal_id,)
            )
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("snapshot delete must be rejected")


def test_comparison_math_zero_denominator_currency_and_profit_semantics():
    positive = compare_valuation_to_sale(
        estimated_resale=Decimal("100"),
        estimated_profit=Decimal("50"),
        estimate_currency="USD",
        actual_gross=Decimal("110"),
        sale_currency="USD",
        recorded_actual_profit=Decimal("60"),
        fully_reconciled=True,
    )
    assert positive.resale_error == Decimal("10")
    assert positive.absolute_resale_error == Decimal("10")
    assert positive.resale_percentage_error == Decimal("0.1")
    assert positive.profit_error is None
    negative = compare_valuation_to_sale(
        estimated_resale=Decimal("100"),
        estimated_profit=None,
        estimate_currency="USD",
        actual_gross=Decimal("90"),
        sale_currency="USD",
        recorded_actual_profit=Decimal("30"),
        fully_reconciled=False,
    )
    assert negative.resale_error == Decimal("-10")
    assert negative.absolute_resale_error == Decimal("10")
    assert negative.fully_reconciled_actual_profit is None
    assert negative.profit_error is None
    zero = compare_valuation_to_sale(
        estimated_resale=Decimal(0),
        estimated_profit=Decimal(0),
        estimate_currency="USD",
        actual_gross=Decimal(0),
        sale_currency="USD",
        recorded_actual_profit=Decimal(0),
        fully_reconciled=True,
    )
    assert zero.resale_percentage_error is None
    assert (
        compare_valuation_to_sale(
            estimated_resale=Decimal("1"),
            estimated_profit=None,
            estimate_currency="USD",
            actual_gross=Decimal("1"),
            sale_currency="EUR",
            recorded_actual_profit=None,
            fully_reconciled=False,
        )
        is None
    )


def test_reports_unsold_incomplete_and_fully_reconciled(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    unsold = _item(store, 1)
    _valuation(store, unsold.inventory_id)
    sold = _item(store, 2)
    _valuation(store, sold.inventory_id)
    sale = _sell(store, sold)
    report = valuation_accuracy_report(store)
    assert report.comparable_count == 1
    sold_row = next(row for row in report.rows if row.sale is not None)
    assert sold_row.comparison.recorded_actual_profit == Decimal("70.1")
    assert sold_row.comparison.fully_reconciled_actual_profit is None
    for category in ("fees", "shipping", "refunds", "adjustments"):
        store.set_reconciliation_confirmation(sale.sale_id, category, confirmed=True)
    complete = valuation_accuracy_report(store)
    sold_row = next(row for row in complete.rows if row.sale is not None)
    assert sold_row.comparison.fully_reconciled_actual_profit == Decimal("70.1")
    assert sold_row.comparison.profit_error is None


def test_multiple_comparisons_have_exact_aggregates_and_mixed_money_is_unavailable(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = _item(store, 1)
    second = _item(store, 2)
    _valuation(store, first.inventory_id, expected_resale_value=Decimal("100"))
    _valuation(store, second.inventory_id, expected_resale_value=Decimal("100"))
    _sell(store, first, gross="110")
    _sell(store, second, gross="80")
    report = valuation_accuracy_report(store)
    assert report.comparable_count == 2
    assert report.average_absolute_resale_error == Decimal("15")
    assert report.median_resale_error == Decimal("-5")
    assert report.average_percentage_error == Decimal("-0.05")
    assert report.aggregate_currency == "USD"

    third = _item(store, 3)
    _valuation(store, third.inventory_id, currency="EUR")
    _sell(store, third, gross="100.10", currency="EUR")
    mixed = valuation_accuracy_report(store)
    assert mixed.comparable_count == 3
    assert mixed.aggregate_currency is None
    assert mixed.average_absolute_resale_error is None
    assert mixed.median_resale_error is None


def test_v7_migration_adds_empty_snapshot_table_without_backfill(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER valuation_snapshots_immutable_update")
        connection.execute("DROP TRIGGER valuation_snapshots_immutable_delete")
        connection.execute("DROP TABLE valuation_snapshots")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
    store.initialize()
    assert store.get(item.inventory_id) == item
    assert store.list_valuation_snapshots() == []


def test_cli_and_web_show_snapshot_comparison_and_small_sample(monkeypatch, tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    item = _item(store)
    assert (
        main.main(
            [
                "inventory",
                "--database",
                str(database),
                "attach-valuation",
                item.inventory_id,
                "--market-value",
                "120.25",
                "--estimated-resale",
                "100.10",
                "--asking-price",
                "45.55",
                "--ideal-buy-price",
                "60.125",
                "--estimated-profit",
                "54.55",
                "--estimated-roi",
                "1.1976",
                "--deal-score",
                "78",
            ]
        )
        == 0
    )
    assert "baseline valuation" in capsys.readouterr().out
    sale = _sell(store, item)
    assert main.main(["reports", "--database", str(database), "valuation"]) == 0
    output = capsys.readouterr().out
    assert "actual gross: USD 110.10" in output
    assert "recorded actual profit (incomplete)" in output
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    client = TestClient(app)
    assert "Baseline valuation" in client.get(f"/inventory/{item.inventory_id}").text
    detail = client.get(f"/sales/{sale.sale_id}").text
    assert "Estimate vs actual" in detail
    assert "incomplete" in detail
    analytics = client.get("/analytics").text
    assert "One comparable item" in analytics
    assert "USD 10.00" in analytics
    assert "buyer" not in detail.lower()
    assert "address" not in detail.lower()
