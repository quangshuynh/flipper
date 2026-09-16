from datetime import datetime, timezone
from decimal import Decimal

import main
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
