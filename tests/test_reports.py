from datetime import date, datetime, timezone
from decimal import Decimal

import main
from inventory.store import InventoryStore
from reports.service import build_summary_report, inventory_report, sales_report


def _add_item(
    store,
    number,
    *,
    acquired_at="2026-08-01",
    cost="10.00",
    status="acquired",
):
    item = store.add(
        title=f"Computer {number}",
        source="synthetic test",
        acquired_at=acquired_at,
        acquisition_cost=cost,
        marketplace="eBay",
        marketplace_sku=f"SKU-{number}",
    )
    if status in {"listed", "sold"}:
        item = store.transition_status(item.inventory_id, "listed")
    if status == "archived":
        item = store.transition_status(item.inventory_id, "archived")
    return item


def _sell(store, number, *, gross="50.00", currency="USD", sold_at="2026-09-01T12:00:00Z"):
    item = _add_item(store, number, status="listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id=f"order-{number}",
        external_line_item_id=f"line-{number}",
        marketplace_sku=f"SKU-{number}",
        quantity=1,
        gross_amount=Decimal(gross),
        currency=currency,
        sold_at=datetime.fromisoformat(sold_at.replace("Z", "+00:00")),
    )
    return sale


def test_empty_reports_are_exact_and_safe(tmp_path):
    report = build_summary_report(
        InventoryStore(tmp_path / "inventory.db"), today=date(2026, 9, 16)
    )
    assert report.inventory.active_count == 0
    assert report.inventory.active_capital_usd == Decimal(0)
    assert report.inventory.average_active_acquisition_cost_usd is None
    assert report.sales.rows == ()
    assert report.sales.gross_by_currency == {}
    assert report.sales.average_days_held is None


def test_inventory_snapshot_counts_statuses_and_excludes_sold_and_archived_capital(tmp_path):
    store = InventoryStore(
        tmp_path / "inventory.db",
        clock=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    _add_item(store, 1, cost="10.01")
    _add_item(store, 2, cost="20.02", status="listed")
    _add_item(store, 3, cost="30.03", status="archived")
    _sell(store, 4, gross="60.00")

    report = inventory_report(store.list(), store.list_sales(), today=date(2026, 9, 16))

    assert report.status_counts == {"acquired": 1, "listed": 1, "archived": 1, "sold": 1}
    assert report.active_count == 2
    assert report.active_capital_usd == Decimal("30.03")
    assert report.average_active_acquisition_cost_usd == Decimal("15.015")
    listed = next(row for row in report.rows if row.record.status == "listed")
    assert listed.listed_age_days == 6
    sold = next(row for row in report.rows if row.record.status == "sold")
    assert sold.linked_sale_id == "S000001"
    assert sold.days_held == 31
    archived = next(row for row in report.rows if row.record.status == "archived")
    assert archived.days_held is None


def test_sales_aggregate_exact_economics_reconciliation_margin_and_days(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = _sell(store, 1, gross="100.00", sold_at="2026-09-01T00:00:00Z")
    second = _sell(store, 2, gross="50.00", sold_at="2026-09-03T23:59:59Z")
    store.add_sale_cost(first.sale_id, category="marketplace_fee", amount=Decimal("10.11"))
    store.add_sale_cost(first.sale_id, category="shipping_cost", amount=Decimal("4.44"))
    store.add_sale_cost(
        first.sale_id,
        category="other_adjustment",
        amount=Decimal("1.25"),
        effect="increase",
    )
    for category in ("fees", "shipping", "refunds", "adjustments"):
        store.set_reconciliation_confirmation(first.sale_id, category, confirmed=True)
    store.set_reconciliation_confirmation(second.sale_id, "fees", confirmed=True)

    report = sales_report(
        store.list(),
        store.list_sales(),
        store.list_all_sale_costs(),
        store.list_reconciliation_confirmations(),
    )

    assert report.gross_by_currency == {"USD": Decimal("150")}
    assert report.acquisition_cost_usd == Decimal("20")
    assert report.reducing_by_currency == {"USD": Decimal("14.55")}
    assert report.increasing_by_currency == {"USD": Decimal("1.25")}
    assert report.recorded_profit_by_currency == {"USD": Decimal("116.70")}
    assert report.fully_reconciled_profit_by_currency == {"USD": Decimal("76.70")}
    assert report.incomplete_recorded_profit_by_currency == {"USD": Decimal("40")}
    assert report.aggregate_margin_by_currency == {"USD": Decimal("0.778")}
    assert report.reconciliation_counts == {
        "incomplete": 0,
        "partially_reconciled": 1,
        "fully_reconciled": 1,
    }
    assert report.average_days_held == Decimal("32")
    assert report.median_days_held == Decimal("32.0")


def test_unknown_acquisition_makes_accounting_and_holding_time_unavailable(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item, _ = store.adopt_ebay_listing(
        "Q0001",
        title="Unknown history",
        source=None,
        acquired_at=None,
        acquisition_cost=None,
        marketplace_item_id="item-1",
        marketplace_sku="Q0001",
    )
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("75"),
        currency="USD",
        sold_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )
    report = sales_report(store.list(), [sale], [], {})
    assert report.rows[0].acquisition_cost_usd is None
    assert report.rows[0].economics is None
    assert report.rows[0].days_held is None
    assert report.acquisition_cost_usd is None
    assert report.recorded_profit_by_currency == {}


def test_zero_gross_and_mixed_currency_do_not_invent_profit_or_margin(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    _sell(store, 1, gross="0", currency="USD")
    _sell(store, 2, gross="80.00", currency="EUR")

    report = sales_report(
        store.list(),
        store.list_sales(),
        store.list_all_sale_costs(),
        store.list_reconciliation_confirmations(),
    )

    assert report.gross_by_currency == {"USD": Decimal(0), "EUR": Decimal("80")}
    assert report.recorded_profit_by_currency == {"USD": Decimal("-10")}
    assert report.aggregate_margin_by_currency == {"USD": None}
    eur = next(row for row in report.rows if row.sale.currency == "EUR")
    assert eur.economics is None
    assert eur.margin is None


def test_date_ranges_are_inclusive_and_inventory_snapshot_is_not_sale_filtered(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    _add_item(store, 1, acquired_at="2026-08-31")
    _sell(store, 2, sold_at="2026-09-01T00:00:00Z")
    _sell(store, 3, sold_at="2026-09-30T23:59:59Z")
    _sell(store, 4, sold_at="2026-10-01T00:00:00Z")

    summary = build_summary_report(
        store,
        today=date(2026, 10, 2),
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
    )
    acquired = inventory_report(
        store.list(),
        store.list_sales(),
        today=date(2026, 10, 2),
        start=date(2026, 8, 31),
        end=date(2026, 8, 31),
    )

    assert [row.sale.sale_id for row in summary.sales.rows] == ["S000001", "S000002"]
    assert summary.inventory.active_count == 1
    assert [row.record.inventory_id for row in acquired.rows] == ["Q0001"]


def test_missing_or_invalid_lifecycle_timestamps_do_not_fabricate_ages(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _add_item(store, 1, acquired_at="2026-09-10", status="listed")
    report = inventory_report(store.list(), [], today=date(2026, 9, 1))
    row = report.rows[0]
    assert row.days_held is None
    assert row.listed_age_days is None
    assert item.listed_at is not None


def test_cli_reports_output_economics_attention_and_no_customer_pii(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    sale = _sell(store, 1, gross="72.00")
    store.add_sale_cost(sale.sale_id, category="marketplace_fee", amount=Decimal("10.44"))

    base = ["reports", "--database", str(database)]
    assert main.main([*base, "summary", "--from", "2026-09-01", "--to", "2026-09-01"]) == 0
    summary = capsys.readouterr().out
    assert "Recorded realized profit (USD): USD 51.56" in summary
    assert "Fully reconciled realized profit (USD): USD 0.00" in summary
    assert "1 incomplete" in summary
    assert main.main([*base, "sales"]) == 0
    sales_output = capsys.readouterr().out
    assert "needs attention: fees, shipping, refunds, adjustments" in sales_output
    assert "buyer" not in sales_output.lower()
    assert "address" not in sales_output.lower()
    assert main.main([*base, "inventory"]) == 0
    assert "Q0001 | sold" in capsys.readouterr().out


def test_cli_rejects_reversed_report_range(tmp_path, capsys):
    result = main.main(
        [
            "reports",
            "--database",
            str(tmp_path / "inventory.db"),
            "sales",
            "--from",
            "2026-09-02",
            "--to",
            "2026-09-01",
        ]
    )
    assert result == 1
    assert "start date must be on or before end date" in capsys.readouterr().err


def test_cli_money_output_preserves_subcent_precision(tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    _sell(store, 1, gross="12.345")
    assert main.main(["reports", "--database", str(database), "sales"]) == 0
    assert "gross: USD 12.345" in capsys.readouterr().out
