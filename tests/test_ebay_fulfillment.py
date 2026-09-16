from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

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


def test_production_request_parameters_and_encoding_are_exact():
    session = Session([Response(body={"orders": [], "total": 0})])
    start = datetime(2026, 8, 17, 12, 34, 56, 789123, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, 12, 34, 56, 789123, tzinfo=timezone.utc)
    FulfillmentClient(OAuth(), session=session).get_orders(start, end)

    url, kwargs = session.calls[0]
    assert url == "https://api.ebay.com/sell/fulfillment/v1/order"
    assert kwargs["params"] == {
        "filter": "creationdate:[2026-08-17T12:34:56.789Z..2026-09-16T12:34:56.789Z]",
        "limit": 100,
        "offset": 0,
    }
    prepared = __import__("requests").Request("GET", url, params=kwargs["params"]).prepare()
    assert "%5B" in prepared.url and "%5D" in prepared.url
    assert parse_qs(urlsplit(prepared.url).query)["filter"] == [kwargs["params"]["filter"]]


def test_timestamp_converts_non_utc_zone_and_keeps_milliseconds():
    eastern = timezone(timedelta(hours=-4))
    value = datetime(2026, 9, 16, 8, 34, 56, 789999, tzinfo=eastern)
    assert FulfillmentClient._timestamp(value) == "2026-09-16T12:34:56.789Z"


def test_timestamp_rejects_naive_datetime_instead_of_appending_utc():
    with pytest.raises(ValueError, match="timezone-aware"):
        FulfillmentClient._timestamp(datetime(2026, 9, 16, 12, 0))


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


def test_api_error_reports_safe_structured_fields_only():
    body = {
        "errors": [
            {
                "errorId": 30850,
                "domain": "API_FULFILLMENT",
                "category": "REQUEST",
                "message": "Invalid date range",
                "longMessage": "Start and end dates cannot be in the future",
                "parameters": [{"name": "filter", "value": "buyer@example.com"}],
            }
        ],
        "access_token": "access-secret",
        "buyer": {"email": "buyer@example.com"},
    }
    with pytest.raises(FulfillmentApiError) as raised:
        FulfillmentClient(OAuth(), session=Session([Response(400, body)])).get_orders(*dates())
    message = str(raised.value)
    for expected in ("30850", "API_FULFILLMENT", "REQUEST", "Invalid date range", "filter"):
        assert expected in message
    assert "access-secret" not in message
    assert "buyer@example.com" not in message


def test_expired_access_token_retries_once_with_refresh():
    oauth = OAuth()
    session = Session([Response(401, {}), Response(body={"orders": [], "total": 0})])
    assert FulfillmentClient(oauth, session=session).get_orders(*dates()) == []
    assert oauth.forces == [False, True]


def test_pagination_failure_is_reported():
    session = Session([Response(body={"orders": [], "total": 2})])
    with pytest.raises(FulfillmentApiError, match="pagination"):
        FulfillmentClient(OAuth(), session=session).get_orders(*dates())
