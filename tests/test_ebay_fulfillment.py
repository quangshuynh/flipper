from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ebay.fulfillment import FulfillmentApiError, FulfillmentClient, FulfillmentRateLimitError


class OAuth:
    class Config:
        api_host = "api.ebay.com"

    config = Config()

    def __init__(self):
        self.forces = []

    def access_token(self, force_refresh=False):
        self.forces.append(force_refresh)
        return "access-secret"


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def raw_order(**overrides):
    value = {
        "orderId": "12-34567-89012",
        "creationDate": "2026-09-15T12:00:00.000Z",
        "lastModifiedDate": "2026-09-15T13:00:00.000Z",
        "orderFulfillmentStatus": "FULFILLED",
        "orderPaymentStatus": "PAID",
        "cancelStatus": {"cancelState": "NONE_REQUESTED"},
        "buyer": {"username": "discard-me"},
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"email": "discard"}}}],
        "lineItems": [
            {
                "lineItemId": "line-1",
                "legacyItemId": "listing-1",
                "title": "Sony Camera",
                "sku": "Q0001",
                "quantity": 1,
                "lineItemCost": {"value": "40.00", "currency": "USD"},
            }
        ],
        "pricingSummary": {
            "priceSubtotal": {"value": "40.00", "currency": "USD"},
            "deliveryCost": {"value": "2.00", "currency": "USD"},
            "tax": {"value": "0.99", "currency": "USD"},
            "deliveryDiscount": {"value": "0.50", "currency": "USD"},
            "total": {"value": "42.49", "currency": "USD"},
        },
    }
    value.update(overrides)
    return value


def dates():
    return datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 16, tzinfo=timezone.utc)


def test_successful_normalization_discards_pii_and_retains_money():
    session = Session([Response(body={"orders": [raw_order()], "total": 1})])
    orders = FulfillmentClient(OAuth(), session=session).get_orders(*dates())
    order = orders[0]
    assert order.order_id == "12-34567-89012"
    assert order.pricing.total.value == Decimal("42.49")
    assert order.line_items[0].legacy_item_id == "listing-1"
    assert "buyer" not in repr(order).lower()
    assert "discard" not in repr(order)


def test_empty_and_missing_optional_fields():
    assert (
        FulfillmentClient(
            OAuth(), session=Session([Response(body={"orders": [], "total": 0})])
        ).get_orders(*dates())
        == []
    )
    raw = raw_order(
        lineItems=[{"lineItemId": "1", "title": "Item", "quantity": 2}], pricingSummary={}
    )
    order = FulfillmentClient(
        OAuth(), session=Session([Response(body={"orders": [raw], "total": 1})])
    ).get_orders(*dates())[0]
    assert order.line_items[0].sku is None
    assert order.pricing.total is None


def test_multiple_items_cancel_and_refund():
    items = raw_order()["lineItems"] + [
        {
            "lineItemId": "2",
            "title": "Lens",
            "quantity": 1,
            "appliedPromotions": [{"discountAmount": {"value": "2", "currency": "USD"}}],
            "refunds": [{"refundAmount": {"value": "5", "currency": "USD"}}],
        }
    ]
    raw = raw_order(lineItems=items, cancelStatus={"cancelState": "CANCELED"})
    order = FulfillmentClient(
        OAuth(), session=Session([Response(body={"orders": [raw], "total": 1})])
    ).get_orders(*dates())[0]
    assert len(order.line_items) == 2
    assert order.line_items[1].discounts[0].value == Decimal("2")
    assert order.line_items[1].refunds[0].value == Decimal("5")
    assert order.cancellation_status == "CANCELED"


def test_pagination_uses_offset_until_total():
    session = Session(
        [
            Response(body={"orders": [raw_order()], "total": 2}),
            Response(body={"orders": [raw_order(orderId="second")], "total": 2}),
        ]
    )
    orders = FulfillmentClient(OAuth(), session=session, page_size=1).get_orders(*dates())
    assert [order.order_id for order in orders] == ["12-34567-89012", "second"]
    assert session.calls[1][1]["params"]["offset"] == 1


@pytest.mark.parametrize(
    "response,error",
    [
        (Response(429, {}), FulfillmentRateLimitError),
        (Response(500, {}), FulfillmentApiError),
        (Response(200, ValueError()), FulfillmentApiError),
        (Response(200, {"orders": "bad", "total": 1}), FulfillmentApiError),
    ],
)
def test_api_errors(response, error):
    with pytest.raises(error):
        FulfillmentClient(OAuth(), session=Session([response])).get_orders(*dates())


def test_expired_access_token_retries_once_with_refresh():
    oauth = OAuth()
    session = Session([Response(401, {}), Response(body={"orders": [], "total": 0})])
    assert FulfillmentClient(oauth, session=session).get_orders(*dates()) == []
    assert oauth.forces == [False, True]


def test_pagination_failure_is_reported():
    session = Session([Response(body={"orders": [], "total": 2})])
    with pytest.raises(FulfillmentApiError, match="pagination"):
        FulfillmentClient(OAuth(), session=session).get_orders(*dates())
