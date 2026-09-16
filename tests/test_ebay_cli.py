from datetime import datetime, timedelta, timezone
from decimal import Decimal

import main
import pytest
from ebay.orders import EbayOrder, EbayOrderLineItem, Money, OrderPricingSummary
from ebay.seller_oauth import SellerNotConnectedError


class OAuth:
    class Config:
        api_host = "api.ebay.com"

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
