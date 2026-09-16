from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ebay.finance_reconciliation import FinanceMatchStatus, reconcile_finance_transactions
from ebay.finance_transactions import TransactionClassification, normalize_transaction
from ebay.finances import FinancesApiError, FinancesClient
from ebay.seller_oauth import SellerOAuthConfig
from inventory.store import InventoryStore


class OAuth:
    config = SellerOAuthConfig("production", "client", "secret", "runame")

    def __init__(self):
        self.calls = []

    def access_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return "token"


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def raw_transaction(
    transaction_id="tx-1",
    transaction_type="SALE",
    value="10.05",
    currency="USD",
    order_id="order-1",
    line_id="line-1",
):
    raw = {
        "transactionId": transaction_id,
        "transactionType": transaction_type,
        "transactionDate": "2026-09-15T12:00:00.000Z",
        "amount": {"value": value, "currency": currency},
        "bookingEntry": "CREDIT",
        "transactionStatus": "FUNDS_AVAILABLE_FOR_PAYOUT",
        "buyer": {"username": "must-not-survive"},
        "orderId": order_id,
        "orderLineItems": [
            {
                "orderLineItemId": line_id,
                "fees": [{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "1.23"}}],
                "title": "must-not-survive",
            }
        ],
    }
    return raw


def test_normalizes_exact_money_currency_native_semantics_and_excludes_pii():
    transaction = normalize_transaction(raw_transaction(value="10.050", currency="CAD"))
    assert transaction.amount.value == Decimal("10.050")
    assert transaction.amount.currency == "CAD"
    assert transaction.classification is TransactionClassification.SALE
    assert transaction.native_type == "SALE"
    assert transaction.order_lines[0].fee_types == ("FINAL_VALUE_FEE",)
    assert "buyer" not in transaction.__dataclass_fields__
    assert "title" not in transaction.order_lines[0].__dataclass_fields__


@pytest.mark.parametrize(
    ("native_type", "classification"),
    [
        ("REFUND", TransactionClassification.REFUND),
        ("CREDIT", TransactionClassification.CREDIT),
        ("DISPUTE", TransactionClassification.DISPUTE),
        ("SHIPPING_LABEL", TransactionClassification.SHIPPING_LABEL),
        ("NON_SALE_CHARGE", TransactionClassification.NON_SALE_CHARGE),
        ("ADJUSTMENT", TransactionClassification.ADJUSTMENT),
        ("WITHDRAWAL", TransactionClassification.WITHDRAWAL),
        ("NEW_EBAY_VALUE", TransactionClassification.UNKNOWN),
    ],
)
def test_preserves_supported_and_unknown_native_transaction_types(native_type, classification):
    transaction = normalize_transaction(raw_transaction(transaction_type=native_type))
    assert transaction.native_type == native_type
    assert transaction.classification is classification


def test_finances_client_paginates_with_bounded_date_filter_and_handles_currencies():
    session = Session(
        [
            Response(body={"total": 2, "transactions": [raw_transaction()]}),
            Response(
                body={
                    "total": 2,
                    "transactions": [
                        raw_transaction("tx-2", value="4", currency="EUR", line_id="line-2")
                    ],
                }
            ),
        ]
    )
    client = FinancesClient(OAuth(), session=session, page_size=1)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, 23, 59, 59, tzinfo=timezone.utc)
    transactions = client.get_transactions(start, end)
    assert session.calls[0][0] == "https://apiz.ebay.com/sell/finances/v1/transaction"
    assert [item.transaction_id for item in transactions] == ["tx-1", "tx-2"]
    assert [item.amount.currency for item in transactions] == ["USD", "EUR"]
    assert [call[1]["params"]["offset"] for call in session.calls] == [0, 1]
    assert session.calls[0][1]["params"]["filter"] == (
        "transactionDate:[2026-09-01T00:00:00.000Z..2026-09-16T23:59:59.000Z]"
    )


def test_finances_client_retries_auth_once_and_accepts_no_content():
    oauth = OAuth()
    session = Session([Response(401), Response(204)])
    assert (
        FinancesClient(oauth, session=session).get_transactions(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        == []
    )
    assert oauth.calls == [False, True]


def test_finances_client_rejects_unbounded_or_invalid_ranges():
    client = FinancesClient(OAuth(), session=Session([]))
    instant = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="before"):
        client.get_transactions(instant, instant)
    with pytest.raises(ValueError, match="timezone-aware"):
        client.get_transactions(datetime(2026, 9, 1), datetime(2026, 9, 2))


def _sale(store, inventory_id, order_id, line_id):
    item = store.add(
        title=inventory_id,
        source="test",
        acquired_at="2026-09-01",
        acquisition_cost="1.00",
        marketplace="eBay",
        marketplace_sku=inventory_id,
    )
    store.transition_status(item.inventory_id, "listed")
    return store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id=order_id,
        external_line_item_id=line_id,
        marketplace_sku=inventory_id,
        quantity=1,
        gross_amount=Decimal("10"),
        currency="USD",
        sold_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )[0]


def test_exact_unmatched_missing_and_ambiguous_sale_reconciliation(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    sale1 = _sale(store, "one", "order-1", "line-1")
    sale2 = _sale(store, "two", "order-1", "line-2")
    exact = normalize_transaction(raw_transaction())
    unmatched = normalize_transaction(raw_transaction("tx-2", order_id="missing", line_id="x"))
    missing_raw = raw_transaction("tx-3")
    missing_raw.pop("orderId")
    missing_raw.pop("orderLineItems")
    missing = normalize_transaction(missing_raw)
    ambiguous_raw = raw_transaction("tx-4")
    ambiguous_raw.pop("orderLineItems")
    ambiguous = normalize_transaction(ambiguous_raw)
    results = reconcile_finance_transactions(
        [exact, unmatched, missing, ambiguous], store.list_sales()
    )
    assert [result.status for result in results] == [
        FinanceMatchStatus.MATCHED,
        FinanceMatchStatus.UNMATCHED,
        FinanceMatchStatus.NOT_ENOUGH_IDENTIFIERS,
        FinanceMatchStatus.AMBIGUOUS,
    ]
    assert results[0].sales == (sale1,)
    assert results[3].sales == (sale1, sale2)


def test_api_errors_do_not_expose_response_payload():
    session = Session([Response(400, {"secret": "buyer@example.test"})])
    with pytest.raises(FinancesApiError) as error:
        FinancesClient(OAuth(), session=session).get_transactions(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
    assert "buyer@example.test" not in str(error.value)
