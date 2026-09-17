import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import main
from ebay.finance_import import FinanceImportStatus, import_finance_transactions
from ebay.finance_transactions import normalize_transaction
from inventory.store import InventoryStore, SaleCostValidationError
from sales.economics import EconomicComponent, calculate_sale_economics


def raw_transaction(
    transaction_id="tx-1",
    transaction_type="SALE",
    *,
    amount="50.00",
    currency="USD",
    booking_entry="CREDIT",
    order_id="order-1",
    line_id="line-1",
    fees=None,
):
    value = {
        "transactionId": transaction_id,
        "transactionType": transaction_type,
        "transactionDate": "2026-09-15T12:00:00.000Z",
        "amount": {"value": amount, "currency": currency},
        "bookingEntry": booking_entry,
        "orderId": order_id,
        "orderLineItems": [
            {
                "orderLineItemId": line_id,
                "fees": fees
                if fees is not None
                else [
                    {
                        "feeType": "FINAL_VALUE_FEE",
                        "amount": {"value": "-5.125", "currency": currency},
                    }
                ],
                "buyer": {"email": "never-store@example.test"},
            }
        ],
        "buyer": {"username": "never-store"},
    }
    return value


def sold_store(tmp_path, *, second_line=False):
    store = InventoryStore(tmp_path / "inventory.db")
    for index, line_id in enumerate(["line-1", "line-2"] if second_line else ["line-1"], 1):
        item = store.add(
            title=f"Item {index}",
            source="test",
            acquired_at="2026-09-01",
            acquisition_cost="10.00",
            marketplace="eBay",
            marketplace_sku=f"Q{index}",
        )
        store.transition_status(item.inventory_id, "listed")
        store.import_sale(
            inventory_id=item.inventory_id,
            marketplace="eBay",
            external_order_id="order-1",
            external_line_item_id=line_id,
            marketplace_sku=f"Q{index}",
            quantity=1,
            gross_amount=Decimal("50.00"),
            currency="USD",
            sold_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        )
    return store


def test_supported_fee_exact_money_provenance_idempotency_and_profit(tmp_path):
    store = sold_store(tmp_path)
    transaction = normalize_transaction(raw_transaction())

    first = import_finance_transactions([transaction], store)
    second = import_finance_transactions([transaction], store)

    assert first[0].status is FinanceImportStatus.IMPORTED
    assert second[0].status is FinanceImportStatus.ALREADY_IMPORTED
    costs = store.list_sale_costs("S000001")
    assert len(costs) == 1
    assert (costs[0].category, costs[0].amount) == ("marketplace_fee", Decimal("5.125"))
    assert costs[0].source == "ebay_finances"
    assert costs[0].external_transaction_id == "tx-1"
    assert costs[0].external_component_key == "fee:line-1:FINAL_VALUE_FEE"
    assert "buyer" not in costs[0].note.casefold()
    economics = calculate_sale_economics(
        gross=Decimal("50"),
        acquisition_cost=Decimal("10"),
        currency="USD",
        acquisition_currency="USD",
        components=[EconomicComponent(costs[0].category, costs[0].amount, costs[0].currency)],
    )
    assert economics.recorded_profit == Decimal("34.875")


def test_supported_debit_refund_normalizes_sign(tmp_path):
    store = sold_store(tmp_path)
    transaction = normalize_transaction(
        raw_transaction("refund-1", "REFUND", amount="-7.50", booking_entry="DEBIT", fees=[])
    )
    result = import_finance_transactions([transaction], store)[0]
    assert result.status is FinanceImportStatus.IMPORTED
    assert (result.cost.category, result.cost.amount) == ("refund", Decimal("7.50"))


def test_shipping_label_debit_imports_but_sale_revenue_does_not_become_shipping(tmp_path):
    store = sold_store(tmp_path)
    sale_transaction = normalize_transaction(raw_transaction(fees=[]))
    shipping = normalize_transaction(
        raw_transaction("label-1", "SHIPPING_LABEL", amount="7.255", booking_entry="DEBIT", fees=[])
    )
    assert (
        import_finance_transactions([sale_transaction], store)[0].status
        is FinanceImportStatus.UNSUPPORTED
    )
    result = import_finance_transactions([shipping], store)[0]
    assert result.status is FinanceImportStatus.IMPORTED
    assert (result.cost.category, result.cost.amount, result.cost.effect) == (
        "shipping_cost",
        Decimal("7.255"),
        "reduce",
    )


def test_fee_credit_links_to_original_and_increases_profit_once(tmp_path):
    store = sold_store(tmp_path)
    debit = normalize_transaction(raw_transaction())
    credit = normalize_transaction(
        raw_transaction(
            "credit-1",
            "CREDIT",
            amount="1.25",
            booking_entry="CREDIT",
            fees=[{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "1.25", "currency": "USD"}}],
        )
    )
    original = import_finance_transactions([debit], store)[0].cost
    first = import_finance_transactions([credit], store)[0]
    repeated = import_finance_transactions([credit], store)[0]
    assert first.status is FinanceImportStatus.IMPORTED
    assert repeated.status is FinanceImportStatus.ALREADY_IMPORTED
    assert first.cost.effect == "increase"
    assert first.cost.related_cost_id == original.cost_id
    costs = store.list_sale_costs("S000001")
    economics = calculate_sale_economics(
        gross=Decimal("50"),
        acquisition_cost=Decimal("10"),
        currency="USD",
        acquisition_currency="USD",
        components=[EconomicComponent(c.category, c.amount, c.currency, c.effect) for c in costs],
    )
    assert economics.recorded_profit == Decimal("36.125")


def test_credit_without_unique_prior_component_is_unsupported(tmp_path):
    store = sold_store(tmp_path)
    credit = normalize_transaction(raw_transaction("credit-1", "CREDIT", booking_entry="CREDIT"))
    assert import_finance_transactions([credit], store)[0].status is FinanceImportStatus.UNSUPPORTED
    assert store.list_sale_costs("S000001") == []


@pytest.mark.parametrize("transaction_type", ["TRANSFER", "CREDIT", "PAYOUT", "ADJUSTMENT"])
def test_unsupported_types_including_payouts_are_not_accounted(tmp_path, transaction_type):
    store = sold_store(tmp_path)
    transaction = normalize_transaction(raw_transaction(transaction_type=transaction_type))
    result = import_finance_transactions([transaction], store)[0]
    assert result.status is FinanceImportStatus.UNSUPPORTED
    assert store.list_sale_costs("S000001") == []


def test_sale_gross_is_not_imported_without_supported_fees(tmp_path):
    store = sold_store(tmp_path)
    transaction = normalize_transaction(raw_transaction(fees=[]))
    assert (
        import_finance_transactions([transaction], store)[0].status
        is FinanceImportStatus.UNSUPPORTED
    )
    assert store.list_sale_costs("S000001") == []


@pytest.mark.parametrize(
    ("mutation", "status"),
    [
        (lambda raw: raw.update(orderId="missing"), FinanceImportStatus.UNMATCHED),
        (
            lambda raw: (raw.pop("orderId"), raw.pop("orderLineItems")),
            FinanceImportStatus.INSUFFICIENT_IDENTIFIERS,
        ),
    ],
)
def test_non_exact_matches_do_not_import(tmp_path, mutation, status):
    store = sold_store(tmp_path)
    raw = (
        raw_transaction(transaction_type="REFUND", booking_entry="DEBIT", fees=[])
        if status is FinanceImportStatus.INSUFFICIENT_IDENTIFIERS
        else raw_transaction()
    )
    mutation(raw)
    assert import_finance_transactions([normalize_transaction(raw)], store)[0].status is status
    assert store.list_sale_costs("S000001") == []


def test_ambiguous_match_does_not_import(tmp_path):
    store = sold_store(tmp_path, second_line=True)
    raw = raw_transaction(transaction_type="REFUND", booking_entry="DEBIT", fees=[])
    raw.pop("orderLineItems")
    result = import_finance_transactions([normalize_transaction(raw)], store)[0]
    assert result.status is FinanceImportStatus.AMBIGUOUS
    assert all(store.list_sale_costs(sale.sale_id) == [] for sale in store.list_sales())


def test_only_fees_for_the_exact_matched_line_are_imported(tmp_path):
    store = sold_store(tmp_path)
    raw = raw_transaction()
    raw["orderLineItems"].append(
        {
            "orderLineItemId": "line-not-local",
            "fees": [
                {
                    "feeType": "FINAL_VALUE_FEE",
                    "amount": {"value": "99", "currency": "USD"},
                }
            ],
        }
    )
    result = import_finance_transactions([normalize_transaction(raw)], store)[0]
    assert result.status is FinanceImportStatus.IMPORTED
    assert store.list_sale_costs("S000001")[0].amount == Decimal("5.125")


def test_multiline_refund_is_not_attributed_to_one_sale(tmp_path):
    store = sold_store(tmp_path)
    raw = raw_transaction(transaction_type="REFUND", booking_entry="DEBIT", fees=[])
    raw["orderLineItems"].append({"orderLineItemId": "another-line", "fees": []})
    result = import_finance_transactions([normalize_transaction(raw)], store)[0]
    assert result.status is FinanceImportStatus.UNSUPPORTED
    assert store.list_sale_costs("S000001") == []


def test_currency_mismatch_is_rejected_without_partial_state(tmp_path):
    store = sold_store(tmp_path)
    result = import_finance_transactions(
        [normalize_transaction(raw_transaction(currency="CAD"))], store
    )[0]
    assert result.status is FinanceImportStatus.REJECTED
    assert "currency" in result.detail
    assert store.list_sale_costs("S000001") == []


def test_conflicting_external_identity_is_reported(tmp_path):
    store = sold_store(tmp_path)
    first = normalize_transaction(raw_transaction())
    changed = normalize_transaction(
        raw_transaction(
            fees=[{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "6", "currency": "USD"}}]
        )
    )
    assert import_finance_transactions([first], store)[0].status is FinanceImportStatus.IMPORTED
    assert import_finance_transactions([changed], store)[0].status is FinanceImportStatus.CONFLICT
    assert store.list_sale_costs("S000001")[0].amount == Decimal("5.125")


def test_database_unique_identity_and_manual_independence(tmp_path):
    store = sold_store(tmp_path)
    imported = import_finance_transactions([normalize_transaction(raw_transaction())], store)[
        0
    ].cost
    manual = store.add_sale_cost("S000001", category="marketplace_fee", amount=Decimal("5.125"))
    assert imported.cost_id != manual.cost_id
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO sale_costs (
                cost_id, sale_internal_id, category, amount_minor, amount_scale, currency,
                source, note, created_at, external_transaction_id, external_component_key
            ) VALUES ('C999999', 1, 'marketplace_fee', 5125, 3, 'USD',
                      'ebay_finances', '', '2026-09-16T00:00:00Z', 'tx-1',
                      'fee:line-1:FINAL_VALUE_FEE')"""
        )


def test_external_cost_cannot_be_removed_by_manual_command(tmp_path):
    store = sold_store(tmp_path)
    cost = import_finance_transactions([normalize_transaction(raw_transaction())], store)[0].cost
    with pytest.raises(SaleCostValidationError, match="externally sourced"):
        store.remove_sale_cost(cost.cost_id)
    assert store.get_sale_cost(cost.cost_id) == cost


def test_v5_migration_preserves_manual_cost_and_adds_empty_external_identity(tmp_path):
    store = sold_store(tmp_path)
    manual = store.add_sale_cost("S000001", category="shipping_cost", amount=Decimal("2.50"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER sale_cost_external_identity_insert")
        connection.execute("DROP TRIGGER sale_cost_external_identity_update")
        connection.execute("DROP INDEX sale_costs_external_identity")
        connection.execute("ALTER TABLE sale_costs DROP COLUMN external_component_key")
        connection.execute("ALTER TABLE sale_costs DROP COLUMN external_transaction_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")

    migrated = store.get_sale_cost(manual.cost_id)

    assert migrated.source == "manual"
    assert migrated.external_transaction_id is None
    assert migrated.external_component_key is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]


def test_v5_migration_rejects_unidentified_external_history(tmp_path):
    store = sold_store(tmp_path)
    manual = store.add_sale_cost("S000001", category="marketplace_fee", amount=Decimal("1"))
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER sale_cost_external_identity_insert")
        connection.execute("DROP TRIGGER sale_cost_external_identity_update")
        connection.execute("DROP INDEX sale_costs_external_identity")
        connection.execute(
            "UPDATE sale_costs SET source = 'ebay_finances' WHERE cost_id = ?", (manual.cost_id,)
        )
        connection.execute("ALTER TABLE sale_costs DROP COLUMN external_component_key")
        connection.execute("ALTER TABLE sale_costs DROP COLUMN external_transaction_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")

    with pytest.raises(RuntimeError, match="cannot assign external identities"):
        store.initialize()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT source FROM sale_costs").fetchone() == ("ebay_finances",)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (7,), (8,), (9,), (10,)]


def test_import_cli_summary_and_finances_command_remains_read_only(monkeypatch, tmp_path, capsys):
    store = sold_store(tmp_path)
    transaction = normalize_transaction(raw_transaction())

    class Client:
        def __init__(self, oauth):
            pass

        def get_transactions(self, start, end):
            return [transaction]

    monkeypatch.setattr(main, "_seller_oauth", lambda: object())
    monkeypatch.setattr(main, "FinancesClient", Client)
    database = str(store.path)
    assert main.main(["ebay", "finances", "--database", database]) == 0
    assert store.list_sale_costs("S000001") == []
    capsys.readouterr()
    assert main.main(["ebay", "import-finances", "--database", database]) == 0
    output = capsys.readouterr().out
    assert "eBay Finances Accounting Import" in output
    assert "imported: 1" in output
    assert "S000001" in output
    assert "never-store" not in output
    shown = _show_sale(database, capsys)
    assert "Reconciliation: incomplete" in shown
    assert "source: ebay_finances" in shown
    assert "eBay transaction: tx-1" in shown


def _show_sale(database, capsys):
    assert main.main(["sales", "--database", database, "show", "S000001"]) == 0
    return capsys.readouterr().out
