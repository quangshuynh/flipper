from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import main
import pytest
from ebay.finance_transactions import normalize_transaction
from ebay.listings import EbayActiveListing
from ebay.orders import EbayOrder, EbayOrderLineItem, Money, OrderPricingSummary
from ebay.seller_oauth import SellerNotConnectedError


class OAuth:
    class Config:
        api_host = "api.ebay.com"
        environment = "production"

    config = Config()


def order():
    return EbayOrder(
        order_id="12-34567-89012",
        creation_date=datetime(2026, 9, 15, tzinfo=timezone.utc),
        last_modified_date=None,
        fulfillment_status="FULFILLED",
        payment_status="PAID",
        cancellation_status=None,
        line_items=(EbayOrderLineItem("1", "2", "Sony Camera", None, 1, None),),
        pricing=OrderPricingSummary(total=Money(Decimal("42.99"), "USD")),
    )


def active_listing(sku="Q0001", item_id="137744631273"):
    return EbayActiveListing(item_id, sku, "Recorder", "Active", Money(Decimal("75.00"), "USD"), 1)


def test_listing_summary_and_explicit_import(monkeypatch, capsys, tmp_path):
    database = tmp_path / "inventory.db"
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(
        main.ActiveListingsClient,
        "get_active_listings",
        lambda self: [active_listing(), active_listing(None, "2")],
    )
    args = ["ebay", "listings", "--database", str(database)]
    assert main.main(args) == 0
    output = capsys.readouterr().out
    assert "$75.00" in output
    assert "total=2" in output and "missing_local=1" in output and "missing_sku=1" in output

    args = [
        "ebay",
        "import-listing",
        "Q0001",
        "--source",
        "gift",
        "--acquired-at",
        "2026-03-01",
        "--cost",
        "0.00",
        "--database",
        str(database),
    ]
    assert main.main(args) == 0
    record = main.InventoryStore(database).get("Q0001")
    assert record.acquisition_cost == Decimal("0") and record.status == "listed"
    assert main.main(args) == 0
    assert len(main.InventoryStore(database).list()) == 1


def test_orders_output(monkeypatch, capsys):
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(main.FulfillmentClient, "get_orders", lambda self, start, end: [order()])
    assert main.main(["ebay", "orders"]) == 0
    output = capsys.readouterr().out
    assert "Sony Camera" in output and "$42.99" in output and "12-34567-89012" in output
    assert "buyer" not in output.lower()


def test_empty_output(monkeypatch, capsys):
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(main.FulfillmentClient, "get_orders", lambda self, start, end: [])
    assert main.main(["ebay", "orders"]) == 0
    assert "No orders found" in capsys.readouterr().out


def test_disconnected_failure_does_not_expose_secret(monkeypatch, capsys):
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())

    def fail(self, start, end):
        raise SellerNotConnectedError("not connected")

    monkeypatch.setattr(main.FulfillmentClient, "get_orders", fail)
    assert main.main(["ebay", "orders"]) == 1
    error = capsys.readouterr().err
    assert "not connected" in error
    assert "access-secret" not in error


def test_default_order_range_is_exactly_30_days():
    now = datetime(2026, 9, 16, 12, 34, 56, 789000, tzinfo=timezone.utc)
    start, end = main._order_date_range(None, None, clock=lambda: now)
    assert end == now - main.EBAY_NOW_SAFETY_MARGIN
    assert end - start == timedelta(days=30)
    assert start < end


def test_to_today_uses_safe_utc_cutoff():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    today = datetime(2026, 9, 16, tzinfo=timezone.utc)
    _, end = main._order_date_range(None, today, clock=lambda: now)
    assert end == now - main.EBAY_NOW_SAFETY_MARGIN
    assert end < now


def test_historical_to_preserves_exact_inclusive_boundary():
    now = datetime(2026, 9, 16, 0, 3, tzinfo=timezone.utc)
    past = datetime(2026, 9, 14, tzinfo=timezone.utc)
    _, end = main._order_date_range(None, past, clock=lambda: now)
    assert end == datetime(2026, 9, 14, 23, 59, 59, 999000, tzinfo=timezone.utc)


def test_previous_day_near_midnight_is_capped_by_safe_cutoff():
    now = datetime(2026, 9, 16, 0, 3, tzinfo=timezone.utc)
    previous_day = datetime(2026, 9, 15, tzinfo=timezone.utc)
    _, end = main._order_date_range(None, previous_day, clock=lambda: now)
    assert end == now - main.EBAY_NOW_SAFETY_MARGIN


def test_non_utc_aware_clock_is_converted_not_relabelled():
    eastern = timezone(timedelta(hours=-4))
    local_now = datetime(2026, 9, 16, 8, 0, tzinfo=eastern)
    _, end = main._order_date_range(None, None, clock=lambda: local_now)
    assert end == datetime(2026, 9, 16, 11, 55, tzinfo=timezone.utc)


def test_simulated_fast_client_clock_keeps_now_boundary_behind_clock():
    client_now = datetime(2026, 9, 16, 12, 2, tzinfo=timezone.utc)
    simulated_ebay_now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    _, end = main._order_date_range(None, None, clock=lambda: client_now)
    assert end == datetime(2026, 9, 16, 11, 57, tzinfo=timezone.utc)
    assert end < simulated_ebay_now


def test_naive_clock_is_rejected_instead_of_assumed_utc():
    with pytest.raises(ValueError, match="timezone-aware"):
        main._order_date_range(None, None, clock=lambda: datetime(2026, 9, 16, 12, 0))


def test_reconcile_output_summary_and_date_range_reuse(monkeypatch, tmp_path, capsys):
    database = tmp_path / "inventory.db"
    main.InventoryStore(database).add(
        title="Sony Camera",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="20",
        marketplace="eBay",
        marketplace_sku="CAM-1",
    )
    matched = order()
    matched = EbayOrder(
        **{
            **matched.__dict__,
            "line_items": (EbayOrderLineItem("1", "2", "Sony Camera", "CAM-1", 1, None),),
        }
    )
    captured = {}
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())

    def get_orders(self, start, end):
        captured["range"] = (start, end)
        return [matched]

    monkeypatch.setattr(main.FulfillmentClient, "get_orders", get_orders)
    assert (
        main.main(
            [
                "ebay",
                "reconcile",
                "--from",
                "2026-09-01",
                "--to",
                "2026-09-15",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Q0001 | Sony Camera" in output
    assert "eBay order: 12-34567-89012" in output
    assert "SKU: CAM-1" in output
    assert "Match: matched" in output
    assert "Summary: 1 examined | 1 matched | 0 unmatched | 0 missing SKU | 0 ambiguous" in output
    assert captured["range"] == (
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 23, 59, 59, 999000, tzinfo=timezone.utc),
    )
    assert "buyer" not in output.lower()


def test_import_sales_summary_and_local_sales_inspection(monkeypatch, tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = main.InventoryStore(database)
    store.add(
        title="Sony Camera",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="20",
        marketplace="eBay",
        marketplace_sku="CAM-1",
    )
    store.transition_status("Q0001", "listed")
    matched = EbayOrder(
        **{
            **order().__dict__,
            "line_items": (
                EbayOrderLineItem(
                    "line-1", "2", "Sony Camera", "CAM-1", 1, Money(Decimal("42.99"), "USD")
                ),
            ),
        }
    )
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(main.FulfillmentClient, "get_orders", lambda self, start, end: [matched])

    assert main.main(["ebay", "import-sales", "--database", str(database)]) == 0
    output = capsys.readouterr().out
    assert "1 imported" in output and "Q0001" in output
    assert "buyer" not in output.lower()

    assert main.main(["sales", "--database", str(database), "list"]) == 0
    listed = capsys.readouterr().out
    assert "S000001 | Q0001 | eBay | USD 42.99" in listed

    assert main.main(["sales", "--database", str(database), "show", "S000001"]) == 0
    shown = capsys.readouterr().out
    assert "Inventory: Q0001" in shown
    assert "Order: 12-34567-89012" in shown
    assert "Gross: USD 42.99" in shown
    assert "buyer" not in shown.lower()


def test_import_sales_repeat_reports_already_imported(monkeypatch, tmp_path, capsys):
    database = tmp_path / "inventory.db"
    store = main.InventoryStore(database)
    store.add(
        title="Sony Camera",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="20",
        marketplace="eBay",
        marketplace_sku="CAM-1",
    )
    store.transition_status("Q0001", "listed")
    matched = EbayOrder(
        **{
            **order().__dict__,
            "line_items": (
                EbayOrderLineItem(
                    "line-1", "2", "Sony Camera", "CAM-1", 1, Money(Decimal("42.99"), "USD")
                ),
            ),
        }
    )
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(main.FulfillmentClient, "get_orders", lambda self, start, end: [matched])
    args = ["ebay", "import-sales", "--database", str(database)]
    assert main.main(args) == 0
    capsys.readouterr()
    assert main.main(args) == 0
    assert "1 already imported" in capsys.readouterr().out


def test_finances_output_summary_is_pii_free_and_does_not_mutate_database(
    monkeypatch, tmp_path, capsys
):
    database = tmp_path / "inventory.db"
    store = main.InventoryStore(database)
    store.add(
        title="Sony Camera",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="20",
        marketplace="eBay",
        marketplace_sku="CAM-1",
    )
    store.transition_status("Q0001", "listed")
    store.import_sale(
        inventory_id="Q0001",
        marketplace="eBay",
        external_order_id="12-34567-89012",
        external_line_item_id="line-1",
        marketplace_sku="CAM-1",
        quantity=1,
        gross_amount=Decimal("42.99"),
        currency="USD",
        sold_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    with sqlite3.connect(database) as connection:
        before = connection.iterdump()
        snapshot = "\n".join(before)

    transaction = normalize_transaction(
        {
            "transactionId": "tx-1",
            "transactionType": "SALE",
            "transactionDate": "2026-09-15T12:00:00.000Z",
            "amount": {"value": "42.99", "currency": "USD"},
            "orderId": "12-34567-89012",
            "buyer": {"username": "private-buyer"},
            "orderLineItems": [
                {
                    "orderLineItemId": "line-1",
                    "fees": [{"feeType": "FINAL_VALUE_FEE"}],
                }
            ],
        }
    )
    monkeypatch.setattr(main, "_seller_oauth", lambda: OAuth())
    monkeypatch.setattr(
        main.FinancesClient,
        "get_transactions",
        lambda self, start, end: [transaction],
    )
    assert main.main(["ebay", "finances", "--database", str(database)]) == 0
    output = capsys.readouterr().out
    assert "Transaction: tx-1" in output
    assert "Sale: S000001" in output
    assert "Match: matched" in output
    assert "1 transactions | 1 matched" in output
    assert "private-buyer" not in output

    with sqlite3.connect(database) as connection:
        after = "\n".join(connection.iterdump())
    assert after == snapshot
