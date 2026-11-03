"""Deterministic, read-only CSV exports of inventory and completed sales."""

import csv
import io
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import main
import reports.csv_export as csv_export
from inventory.store import InventoryStore
from reports.csv_export import (
    INVENTORY_COLUMNS,
    SALES_COLUMNS,
    ExportError,
    escape_text,
    export_inventory_csv,
    export_sales_csv,
    format_money,
    format_timestamp,
)
from reports.service import sales_report
from tests.web_client import local_client
from web.app import app

BOM = "﻿".encode()


def _parse(content: bytes) -> list[dict[str, str]]:
    assert content.startswith(BOM)
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")))


def _dump(path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def _sell(store, inventory_id, number, *, gross="100.00", currency="USD", day=10, **amounts):
    if store.get(inventory_id).status == "acquired":
        store.transition_status(inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=inventory_id,
        marketplace="eBay",
        external_order_id=f"order-{number}",
        external_line_item_id=f"line-{number}",
        marketplace_sku=store.get(inventory_id).marketplace_sku,
        quantity=1,
        gross_amount=Decimal(gross),
        currency=currency,
        sold_at=datetime(2026, 9, day, 15, 30, 5, tzinfo=timezone.utc),
        **{key: Decimal(value) for key, value in amounts.items()},
    )
    return sale


def _item(store, number, *, cost="25.00", title=None, notes="", acquired_at="2026-08-01"):
    return store.add(
        title=title or f"Laptop {number}",
        source="estate sale",
        acquired_at=acquired_at,
        acquisition_cost=cost,
        marketplace="eBay",
        marketplace_sku=f"Q{number:04d}",
        notes=notes,
    )


@pytest.fixture
def ledger(tmp_path):
    """A small authoritative ledger with every accounting state the exports must keep apart."""
    database = tmp_path / "ledger.db"
    store = InventoryStore(database)

    # Q0001: fully reconciled sale with fees, seller shipping, a confirmed zero refund,
    # an adjustment credit, and actual sourcing travel.
    first = _item(store, 1, cost="40.00")
    store.add_valuation_snapshot(
        first.inventory_id,
        analyzed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        currency="USD",
        estimated_market_value=Decimal("150.00"),
        expected_resale_value=Decimal("140.00"),
        asking_price=Decimal("45.00"),
        ideal_buy_price=Decimal("60.00"),
        estimated_gross_profit=Decimal("95.00"),
        estimated_roi=Decimal("2.11"),
        deal_score=80,
    )
    store.set_sourcing_travel(first.inventory_id, fuel_cost="6.25", additional_expense="1.75")
    realized = _sell(
        store, first.inventory_id, 1, gross="83.07", item_revenue="75.00", buyer_shipping="8.07"
    )
    store.add_sale_cost(realized.sale_id, category="marketplace_fee", amount=Decimal("12.24"))
    store.add_sale_cost(realized.sale_id, category="shipping_cost", amount=Decimal("9.50"))
    store.add_sale_cost(
        realized.sale_id, category="other_adjustment", amount=Decimal("2.00"), effect="increase"
    )
    for category in ("fees", "shipping", "refunds", "adjustments"):
        store.set_reconciliation_confirmation(realized.sale_id, category, confirmed=True)

    # Q0002: provisional sale; only fees are recorded and nothing is confirmed.
    second = _item(store, 2, cost="0.00")
    provisional = _sell(store, second.inventory_id, 2, gross="50.00", day=5)
    store.add_sale_cost(provisional.sale_id, category="marketplace_fee", amount=Decimal("6.60"))

    # Q0003: non-USD sale; profit is withheld rather than converted.
    third = _item(store, 3, cost="10.00")
    _sell(store, third.inventory_id, 3, gross="70.00", currency="CAD", day=20)

    # Q0004: unknown acquisition cost (adopted eBay listing) sold; profit is unavailable.
    store.adopt_ebay_listing(
        "Q0004",
        title="Adopted listing",
        source=None,
        acquired_at=None,
        acquisition_cost=None,
        marketplace_item_id="item-4",
        marketplace_sku="Q0004",
    )
    _sell(store, "Q0004", 4, gross="30.00", day=7)

    # Q0005: still listed, with a baseline estimate and no sale.
    listed = _item(store, 5, cost="15.00")
    store.add_valuation_snapshot(
        listed.inventory_id,
        analyzed_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        currency="USD",
        estimated_market_value=Decimal("90.00"),
        expected_resale_value=Decimal("80.00"),
        asking_price=Decimal("20.00"),
        ideal_buy_price=Decimal("30.00"),
        estimated_gross_profit=Decimal("55.00"),
        estimated_roi=None,
        deal_score=70,
    )
    store.transition_status(listed.inventory_id, "listed")
    return database, store


def test_empty_database_exports_header_only(tmp_path):
    store = InventoryStore(tmp_path / "empty.db")

    inventory = export_inventory_csv(store)
    sales = export_sales_csv(store)

    expected_inventory = ",".join(column.name for column in INVENTORY_COLUMNS) + "\r\n"
    expected_sales = ",".join(column.name for column in SALES_COLUMNS) + "\r\n"
    assert inventory == BOM + expected_inventory.encode()
    assert sales == BOM + expected_sales.encode()
    assert _parse(inventory) == [] and _parse(sales) == []


def test_column_order_is_stable():
    assert [column.name for column in INVENTORY_COLUMNS][:7] == [
        "inventory_id",
        "title",
        "status",
        "quantity",
        "acquisition_source",
        "acquired_at",
        "acquisition_cost_usd",
    ]
    assert [column.name for column in SALES_COLUMNS][-6:] == [
        "other_adjustments_net",
        "reconciliation_state",
        "unconfirmed_categories",
        "recorded_profit",
        "realized_profit",
        "profit_status",
    ]
    assert len({column.name for column in SALES_COLUMNS}) == len(SALES_COLUMNS)


def test_exports_are_byte_identical_across_runs(ledger):
    _database, store = ledger

    assert export_inventory_csv(store) == export_inventory_csv(store)
    assert export_sales_csv(store) == export_sales_csv(store)
    assert export_sales_csv(InventoryStore(store.path)) == export_sales_csv(store)


def test_inventory_export_values_and_unknowns(ledger):
    _database, store = ledger

    rows = {row["inventory_id"]: row for row in _parse(export_inventory_csv(store))}

    assert list(rows) == ["Q0001", "Q0002", "Q0003", "Q0004", "Q0005"]
    first = rows["Q0001"]
    assert first["status"] == "sold"
    assert first["acquisition_cost_usd"] == "40.00"
    assert first["sourcing_fuel_cost_usd"] == "6.25"
    assert first["sourcing_additional_expense_usd"] == "1.75"
    assert first["sale_id"] == "S000001"
    assert first["acquired_at"] == "2026-08-01"
    assert first["sold_at"] == "2026-09-10T15:30:05Z"
    assert rows["Q0002"]["acquisition_cost_usd"] == "0.00", "known zero stays zero"
    assert rows["Q0004"]["acquisition_cost_usd"] == "", "unknown cost is never 0"
    assert rows["Q0004"]["acquired_at"] == ""
    assert rows["Q0004"]["acquisition_source"] == ""
    assert rows["Q0005"]["status"] == "listed"
    assert rows["Q0005"]["sale_id"] == ""
    assert rows["Q0005"]["sold_at"] == ""
    assert rows["Q0002"]["sourcing_fuel_cost_usd"] == ""


def test_research_estimates_stay_in_labeled_estimate_columns(ledger):
    _database, store = ledger

    inventory = {row["inventory_id"]: row for row in _parse(export_inventory_csv(store))}
    sales = {row["inventory_id"]: row for row in _parse(export_sales_csv(store))}

    assert inventory["Q0005"]["estimate_expected_resale_value"] == "80.00"
    assert inventory["Q0005"]["estimate_gross_profit"] == "55.00"
    assert inventory["Q0005"]["estimate_currency"] == "USD"
    assert inventory["Q0005"]["estimate_analyzed_at"] == "2026-08-02T00:00:00Z"
    assert inventory["Q0002"]["estimate_gross_profit"] == ""
    # The Q0001 estimate (95.00 profit, 140.00 resale) never appears as a sale fact.
    assert inventory["Q0001"]["estimate_gross_profit"] == "95.00"
    assert not any(column.name.startswith("estimate") for column in SALES_COLUMNS)
    assert "95.00" not in sales["Q0001"].values()
    assert "140.00" not in sales["Q0001"].values()


def test_sales_export_values_and_reconciliation_semantics(ledger):
    _database, store = ledger

    rows = _parse(export_sales_csv(store))

    # Sorted by sale time, not import order.
    assert [row["sale_id"] for row in rows] == ["S000002", "S000004", "S000001", "S000003"]
    by_item = {row["inventory_id"]: row for row in rows}

    realized = by_item["Q0001"]
    assert realized["seller_revenue"] == "83.07"
    assert realized["item_revenue"] == "75.00"
    assert realized["buyer_shipping_revenue"] == "8.07"
    assert realized["acquisition_cost_usd"] == "40.00"
    assert realized["sourcing_travel_expense_usd"] == "8.00"
    assert realized["marketplace_fees"] == "12.24"
    assert realized["seller_shipping_cost"] == "9.50"
    assert realized["refunds"] == "0.00", "confirmed zero is a known zero"
    assert realized["other_adjustments_net"] == "-2.00", "credits reduce net adjustments"
    assert realized["reconciliation_state"] == "fully_reconciled"
    assert realized["unconfirmed_categories"] == ""
    # 83.07 - 40.00 - 8.00 - 12.24 - 9.50 + 2.00
    assert realized["recorded_profit"] == "15.33"
    assert realized["realized_profit"] == "15.33"
    assert realized["profit_status"] == "realized"
    assert realized["days_held"] == "40"
    assert realized["sold_at"] == "2026-09-10T15:30:05Z"

    provisional = by_item["Q0002"]
    assert provisional["item_revenue"] == "", "legacy/absent components stay unknown"
    assert provisional["marketplace_fees"] == "6.60"
    assert provisional["seller_shipping_cost"] == "", "unconfirmed and unrecorded is unknown"
    assert provisional["refunds"] == ""
    assert provisional["other_adjustments_net"] == ""
    assert provisional["sourcing_travel_expense_usd"] == ""
    assert provisional["unconfirmed_categories"] == "fees;shipping;refunds;adjustments"
    assert provisional["reconciliation_state"] == "incomplete"
    assert provisional["recorded_profit"] == "43.40"
    assert provisional["realized_profit"] == "", "incomplete accounting is never realized"
    assert provisional["profit_status"] == "provisional"

    foreign = by_item["Q0003"]
    assert foreign["currency"] == "CAD"
    assert foreign["seller_revenue"] == "70.00"
    assert foreign["recorded_profit"] == foreign["realized_profit"] == ""
    assert foreign["profit_status"].startswith("unavailable: non-USD")

    unknown_cost = by_item["Q0004"]
    assert unknown_cost["acquisition_cost_usd"] == ""
    assert unknown_cost["days_held"] == ""
    assert unknown_cost["recorded_profit"] == unknown_cost["realized_profit"] == ""
    assert unknown_cost["profit_status"] == "unavailable: acquisition cost unknown"


def test_sales_export_matches_shared_report_calculation(ledger):
    """Audit: exported profit is exactly the existing report's recorded profit for every sale."""
    _database, store = ledger
    report = sales_report(
        store.list(),
        store.list_sales(),
        store.list_all_sale_costs(),
        store.list_reconciliation_confirmations(),
        store.list_sourcing_travel(),
    )
    expected = {
        row.sale.sale_id: format_money(row.economics.recorded_profit if row.economics else None)
        for row in report.rows
    }

    rows = _parse(export_sales_csv(store))

    assert {row["sale_id"]: row["recorded_profit"] for row in rows} == expected
    for row in rows:
        if row["reconciliation_state"] != "fully_reconciled" or not row["recorded_profit"]:
            continue
        components = [
            row[name]
            for name in (
                "marketplace_fees",
                "seller_shipping_cost",
                "refunds",
                "other_adjustments_net",
                "sourcing_travel_expense_usd",
            )
        ]
        total = Decimal(row["seller_revenue"]) - Decimal(row["acquisition_cost_usd"])
        total -= sum((Decimal(value) for value in components if value), Decimal(0))
        assert Decimal(row["realized_profit"]) == total


def test_money_formatting_is_exact():
    assert format_money(None) == ""
    assert format_money(Decimal("0")) == "0.00"
    assert format_money(Decimal("-0.00")) == "0.00"
    assert format_money(Decimal("12.5")) == "12.50"
    assert format_money(Decimal("0.125")) == "0.125", "stored precision is never rounded"
    assert format_money(Decimal("1E+2")) == "100.00"
    assert format_money(Decimal("-2")) == "-2.00"
    assert format_money(Decimal("1234567.89")) == "1234567.89", "no grouping separators"
    with pytest.raises(ExportError):
        format_money(Decimal("NaN"))


def test_sub_cent_sale_amounts_are_exported_exactly(tmp_path):
    store = InventoryStore(tmp_path / "precise.db")
    item = _item(store, 1, cost="1.00")
    _sell(store, item.inventory_id, 1, gross="10.125")

    row = _parse(export_sales_csv(store))[0]

    assert row["seller_revenue"] == "10.125"
    assert row["recorded_profit"] == "9.125"


def test_timestamps_are_normalized_to_utc():
    assert format_timestamp("2026-09-10T15:30:05Z") == "2026-09-10T15:30:05Z"
    assert format_timestamp("2026-09-10T11:30:05-04:00") == "2026-09-10T15:30:05Z"
    assert format_timestamp(None) == ""
    assert format_timestamp("not a time") == "not a time"
    assert format_timestamp("=1+1") == "'=1+1", "unparseable fallbacks are still escaped"


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\t=1", "\r=1", "\n=1", "＝HYPERLINK()"],
)
def test_formula_prefixes_are_neutralized(value):
    assert escape_text(value) == "'" + value


def test_text_escaping_round_trips(tmp_path):
    store = InventoryStore(tmp_path / "escape.db")
    title = 'ThinkPad, "mint" — 日本語 ✓'
    notes = 'line one\nline "two", with comma\r\n=HYPERLINK("http://x")'
    _item(store, 1, title="=cmd|' /C calc'!A0", notes=notes)
    _item(store, 2, title=title, notes="@SUM(1)")

    content = export_inventory_csv(store)
    rows = _parse(content)

    assert rows[0]["title"] == "'=cmd|' /C calc'!A0"
    assert rows[0]["notes"] == notes, "text not starting with a formula prefix is unchanged"
    assert rows[1]["title"] == title
    assert rows[1]["notes"] == "'@SUM(1)"
    assert '"ThinkPad, ""mint"" — 日本語 ✓"'.encode() in content
    assert content.count(b"\r\n") >= 3


def test_generated_negative_amounts_are_not_formula_escaped(ledger):
    _database, store = ledger

    row = _parse(export_sales_csv(store))[2]

    assert row["inventory_id"] == "Q0001"
    assert row["other_adjustments_net"] == "-2.00"


def test_large_export_is_complete_and_ordered(tmp_path):
    store = InventoryStore(tmp_path / "large.db")
    count = 1200
    for number in range(1, count + 1):
        _item(store, number, cost=f"{number}.{number % 100:02d}")
    for number in range(1, 301):
        _sell(store, f"Q{number:04d}", number, gross="99.99", day=1 + number % 28)

    inventory = _parse(export_inventory_csv(store))
    sales = _parse(export_sales_csv(store))

    assert len(inventory) == count
    assert [row["inventory_id"] for row in inventory] == [
        f"Q{number:04d}" for number in range(1, count + 1)
    ]
    assert inventory[-1]["acquisition_cost_usd"] == "1200.00"
    assert len(sales) == 300
    keys = [(row["sold_at"], row["sale_id"]) for row in sales]
    assert keys == sorted(keys)


def test_exports_do_not_modify_the_database(ledger):
    database, store = ledger
    before = _dump(database)

    export_inventory_csv(store)
    export_sales_csv(store)
    client = local_client(app)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FLIPPER_INVENTORY_DB", str(database))
        assert client.get("/exports/inventory.csv").status_code == 200
        assert client.get("/exports/sales.csv").status_code == 200
    assert main.main(["reports", "--database", str(database), "export", "sales"]) == 0

    assert _dump(database) == before


def test_export_failure_raises_without_partial_output(ledger, monkeypatch):
    database, store = ledger
    before = _dump(database)

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(InventoryStore, "list_all_sale_costs", broken)

    with pytest.raises(ExportError, match="disk I/O error"):
        export_sales_csv(store)
    assert _dump(database) == before


def test_mismatched_component_currency_fails_instead_of_mixing(ledger, monkeypatch):
    _database, store = ledger
    original = InventoryStore.list_all_sale_costs

    def foreign_costs(self):
        from dataclasses import replace

        return [replace(cost, currency="EUR") for cost in original(self)]

    monkeypatch.setattr(InventoryStore, "list_all_sale_costs", foreign_costs)

    with pytest.raises(ExportError):
        export_sales_csv(store)


def test_web_exports_download_complete_csv(ledger, monkeypatch):
    database, store = ledger
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    client = local_client(app)

    for dataset, exporter in (("inventory", export_inventory_csv), ("sales", export_sales_csv)):
        response = client.get(f"/exports/{dataset}.csv")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert (
            response.headers["content-disposition"]
            == f'attachment; filename="flipper-{dataset}.csv"'
        )
        assert response.headers["cache-control"] == "no-store"
        assert response.content == exporter(store)

    assert client.get("/exports/orders.csv").status_code == 404
    assert 'href="/exports/inventory.csv"' in client.get("/inventory").text
    assert 'href="/exports/sales.csv"' in client.get("/sales").text


def test_web_export_failure_returns_error_page_without_a_file(ledger, monkeypatch):
    database, _store = ledger
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    before = _dump(database)

    def broken(_store):
        raise ExportError("export failed: simulated")

    monkeypatch.setitem(csv_export.EXPORTS, "sales", broken)
    response = local_client(app).get("/exports/sales.csv")

    assert response.status_code == 500
    assert "content-disposition" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    assert "No file was produced" in response.text
    assert _dump(database) == before


def test_cli_export_writes_stdout_and_files(ledger, tmp_path, capsysbinary):
    database, store = ledger
    output = tmp_path / "out" / "sales.csv"

    assert main.main(["reports", "--database", str(database), "export", "inventory"]) == 0
    assert capsysbinary.readouterr().out == export_inventory_csv(store)
    assert (
        main.main(
            ["reports", "--database", str(database), "export", "sales", "--output", str(output)]
        )
        == 0
    )
    assert output.read_bytes() == export_sales_csv(store)
    assert list(output.parent.iterdir()) == [output]


def test_cli_export_failure_leaves_existing_file_untouched(ledger, tmp_path, monkeypatch, capsys):
    database, _store = ledger
    output = tmp_path / "sales.csv"
    output.write_bytes(b"previous export")

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(InventoryStore, "list_all_sale_costs", broken)

    result = main.main(
        ["reports", "--database", str(database), "export", "sales", "--output", str(output)]
    )

    assert result == 1
    assert "database is locked" in capsys.readouterr().err
    assert output.read_bytes() == b"previous export"
    assert list(tmp_path.glob(".sales.csv.*")) == []
