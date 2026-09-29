from dataclasses import replace
from datetime import datetime, timezone

from ebay.orders import EbayOrder, EbayOrderLineItem, OrderPricingSummary
from ebay.reconciliation import ReconciliationStatus, reconcile_ebay_orders, summarize
from inventory.store import InventoryStore


def inventory_values(**overrides):
    values = {
        "title": "Recorder",
        "source": "sale",
        "acquired_at": "2026-09-01",
        "acquisition_cost": "10.00",
    }
    values.update(overrides)
    return values


def line(line_id, sku, title="Order item"):
    return EbayOrderLineItem(line_id, f"item-{line_id}", title, sku, 1, None)


def order(order_id, *lines):
    return EbayOrder(
        order_id,
        datetime(2026, 9, 15, tzinfo=timezone.utc),
        None,
        None,
        None,
        None,
        tuple(lines),
        OrderPricingSummary(),
    )


def test_exact_sku_matching_multiple_records_lines_and_marketplace_scope(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = store.add(**inventory_values(marketplace="eBay", marketplace_sku="SKU-1"))
    second = store.add(
        **inventory_values(title="Camera", marketplace="EBAY", marketplace_sku="SKU-2")
    )
    store.add(**inventory_values(title="Other market", marketplace="Etsy", marketplace_sku="X"))
    store.add(**inventory_values(title="No SKU", marketplace="eBay"))

    results = reconcile_ebay_orders(
        [order("one", line("1", "SKU-2"), line("2", "SKU-1"), line("3", "X"))],
        store.list(),
    )

    assert [result.status for result in results] == [
        ReconciliationStatus.MATCHED,
        ReconciliationStatus.MATCHED,
        ReconciliationStatus.UNMATCHED,
    ]
    assert [result.inventory_matches[0] for result in results[:2]] == [second, first]


def test_missing_unmatched_and_case_distinct_skus_are_explicit(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**inventory_values(marketplace="eBay", marketplace_sku="Exact"))
    results = reconcile_ebay_orders(
        [order("one", line("1", None), line("2", "missing"), line("3", "exact"))],
        store.list(),
    )
    assert [result.status for result in results] == [
        ReconciliationStatus.MISSING_SKU,
        ReconciliationStatus.UNMATCHED,
        ReconciliationStatus.UNMATCHED,
    ]
    assert summarize(results).missing_sku == 1
    assert summarize(results).unmatched == 2


def test_ambiguous_legacy_input_is_sorted_deterministically(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = store.add(**inventory_values(marketplace="eBay", marketplace_sku="same"))
    second = store.add(**inventory_values(title="Second", marketplace_sku="different"))
    duplicate = replace(second, marketplace="ebay", marketplace_sku="same")

    results = reconcile_ebay_orders([order("one", line("1", "same"))], [duplicate, first])

    assert results[0].status is ReconciliationStatus.AMBIGUOUS
    assert [record.inventory_id for record in results[0].inventory_matches] == ["Q0001", "Q0002"]


def test_reconciliation_is_repeatable_and_never_mutates_inventory(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**inventory_values(marketplace="eBay", marketplace_sku="Q0001"))
    orders = [order("one", line("1", "Q0001"))]

    first = reconcile_ebay_orders(orders, store.list())
    second = reconcile_ebay_orders(orders, store.list())

    assert first == second
    assert store.get("Q0001") == original
    assert store.get("Q0001").status == "acquired"
    assert (store.get("Q0001").listed_at, store.get("Q0001").sold_at) == (None, None)
